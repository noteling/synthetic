"""M26 MCP tools for the roster (design §4): roster_get, view_put, self_note, profile_put.

view_put and self_note write only the caller's private log (Store.roster_log, fenced by the session run).
profile_put writes the company log, fenced on the session run, and needs a company write grant (seeded: the
manager). Its ``auth`` is never an argument: it is copied from the cited source event in the caller's home
(C3), so an employee cannot assert authentication. Grants, aliases removal, retire and redaction are CLI only.
"""
from __future__ import annotations

from . import roster
from .projects import ProjectError


def _text(description=""):
    return {"type": "string", "description": description}


def _tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False}}


KEY = _text("Stable descriptive key for this intended operation. Reuse after interruption; a different payload "
            "with the same key is rejected.")
PERSON = _text("Company person ID, name or alias (e.g. codex-manager, Dana Example).")
CLAIM = {"type": "object", "additionalProperties": False,
         "properties": {"cid": _text("Claim ID within this view; reuse it to revise the claim."),
                        "text": _text(), "kind": {"type": "string", "enum": list(roster.CLAIM_KINDS)},
                        "source": _text("Event, memory, question or work ID, or file supporting the claim."),
                        "certainty": {"type": "string", "enum": list(roster.VIEW_CERTAINTIES)},
                        "ref": _text("For kind open: the question or work ID this thread is tracked by.")},
         "required": ["cid", "text", "kind", "source", "certainty"]}

TOOLS = [
    _tool("roster_get", "Read one person's official company profile and, for you only, your own private view of "
          "them. Resolves names and aliases to the canonical ID.", {"person": PERSON}, ["person"]),
    _tool("view_put", "Write your own private view of a company person: add or revise claims, retract claims, "
          "or record that you have caught up with an official role revision. Never changes official profiles. "
          "Views are private: in messages say 'in my view…' and never paste view text.", {
              "person": PERSON,
              "claims": {"type": "array", "items": CLAIM},
              "retract": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                     "properties": {"cid": _text(), "reason": _text()},
                                                     "required": ["cid"]}},
              "reconciles": {"type": "integer", "minimum": 1,
                             "description": "The official role_rev your view now accounts for."},
              "key": KEY}, ["person", "key"]),
    _tool("self_note", "Record a sourced note about how you work, your strengths and limits, or a standing "
          "commitment. Name, role and purpose stay operator-set.", {
              "section": {"type": "string", "enum": list(roster.SELF_SECTIONS)},
              "text": _text(), "source": _text(),
              "certainty": {"type": "string", "enum": list(roster.VIEW_CERTAINTIES)},
              "key": KEY}, ["section", "text", "source", "key"]),
    _tool("profile_put", "Add or update an official company profile. Needs a company write grant. Role changes "
          "need a source event with non-null auth; nobody changes their own role (ask the manager).", {
              "id": _text("Canonical person ID."), "name": _text(),
              "kind": {"type": "string", "enum": list(roster.PERSON_KINDS)},
              "role": _text(), "description": _text(),
              "aliases": {"type": "array", "items": {"type": "string"}},
              "source": _text("Event ID in your home whose words support this; its auth is recorded."),
              "certainty": {"type": "string", "enum": list(roster.CERTAINTIES)},
              "key": KEY}, ["id", "source", "key"]),
]
NAMES = {t["name"] for t in TOOLS}
MUTATIONS = {"view_put", "self_note", "profile_put"}


def _me(store):
    return store.identity()["id"]


def roster_get(store, person):
    company = roster.company_state_for_home(store.home)
    hit = roster.resolve(company, person)
    if not hit["id"]:
        raise ProjectError("unknown_person", "%r has no company profile; ask the manager (question_ask) to add one"
                           % person)
    profile = company["people"][hit["id"]]
    view = store.roster_rendered(company=company)["views"].get(hit["id"])
    return {"id": hit["id"], "via": hit["via"], "matched_as": roster.matched_as(company, hit["id"], person),
            "profile": profile,
            "profile_text": roster.render_person(profile), "view": view}


def _source_auth(store, source):
    """C3: auth of the cited event exactly as received; None for non-events or unauthenticated events."""
    if not source.startswith("event-"):
        return None
    event = store.event_get(source)
    auth = event.get("auth")
    return auth if isinstance(auth, dict) and auth else None


def profile_put(store, arguments):
    me = _me(store)
    source = arguments["source"]
    auth = _source_auth(store, source)
    certainty = arguments.get("certainty", "stated")
    fields = {k: arguments[k] for k in ("name", "kind", "role", "description") if k in arguments}
    path = roster.company_path(roster.root_of_home(store.home))
    path.parent.mkdir(parents=True, exist_ok=True)
    with roster.CompanyStore(path) as cs:
        state = cs.state()
        batch = []
        if fields or arguments["id"] not in state["people"]:
            batch.append({"kind": "person", "body": dict(fields, id=arguments["id"], source=source,
                                                         certainty=certainty, auth=auth)})
        have = set(state["people"].get(arguments["id"], {}).get("aliases") or [])
        for alias in arguments.get("aliases") or []:
            if alias not in have:
                batch.append({"kind": "alias", "body": {"id": arguments["id"], "alias": alias, "source": source,
                                                        "certainty": certainty, "auth": auth}})
        if not batch:
            raise ProjectError("no_change", "nothing to change in %s" % arguments["id"])
        out = cs.append(batch, author=me, run=store.run_id, fence=store.path,
                        key="mcp:%s:%s" % (me, arguments["key"]))
        cs.render()
        return out


def call(store, name, arguments):
    if name == "roster_get":
        return roster_get(store, arguments["person"])
    if name == "view_put":
        return store.view_put(arguments["person"], claims=arguments.get("claims"), retract=arguments.get("retract"),
                              reconciles=arguments.get("reconciles"), key=arguments["key"])
    if name == "self_note":
        return store.self_note(arguments["section"], arguments["text"], arguments["source"],
                               certainty=arguments.get("certainty", "observed"), key=arguments["key"])
    if name == "profile_put":
        return profile_put(store, arguments)
    raise ValueError("Unknown tool: %s" % name)

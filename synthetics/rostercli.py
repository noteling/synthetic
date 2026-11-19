"""M26 Roster CLI: `synthetics roster company …`, `roster render`, `roster show`, `roster seed`.

Company writes (put/alias/unalias/retire) are operator commands. Inside an employee session
(SYNTHETICS_SESSION_RUN set) put/alias/unalias are allowed only for an employee holding the company write grant
(retire stays owner-only: the company store refuses it with owner_only): the
write is authored by that employee, fenced on the session's run and audited in the employee's home, as
`project bind` is. grant/revoke/import/redact, `roster show` and `roster seed --apply` are operator-only and
refuse inside a session. No command writes another home's private log except `seed --apply`, which the
operator runs per home; seeded entries carry author `roster-seed` so the employee can see and retract them.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import roster
from .projects import ProjectError

SESSION_ENV = "SYNTHETICS_SESSION_RUN"
SEED_AUTHOR = "roster-seed"


def _refuse_in_session(what):
    if os.environ.get(SESSION_ENV):
        raise ValueError("%s is an operator operation and is refused inside an employee session (%s is set)."
                         % (what, SESSION_ENV))


def _source_auth(store, source):
    """C3: the cited event's auth exactly as received in this home; never supplied by an argument."""
    if not isinstance(source, str) or not source.startswith("event-"):
        return None
    try:
        auth = store.event_get(source).get("auth")
    except Exception:
        return None
    return auth if isinstance(auth, dict) and auth else None


def _company(store):
    path = roster.company_path(roster.root_of_home(store.home))
    path.parent.mkdir(parents=True, exist_ok=True)
    return roster.CompanyStore(path)


def company_command(store, args):
    from . import authority
    cmd = args.company_command
    if cmd in ("import", "redact"):
        authority.owner_only("company " + cmd)
    a = authority.actor() if cmd not in ("grants", "state", "export") else None
    # D8a: grants|state|export are read-only and have no --by.
    by = getattr(args, "by", None) if a is None else authority.check_by(a, getattr(args, "by", None),
                                                       roster.company_state_for_home(store.home))
    if cmd in ("grant", "revoke"):
        # M28 item 4: owner, or company.grant covering the mode; never to the actor itself (self_promotion).
        if not a["owner"]:
            grantee = roster.resolve(roster.company_state_for_home(store.home), args.employee)["id"] or args.employee
            if grantee == a["id"]:
                authority.audit(a["home"], "authority.refused", {"what": "company " + cmd, "actor": a["id"],
                                                                 "run": a["run"], "code": "self_promotion"})
                raise authority.AuthorityError("self_promotion", "a session cannot change its own company grant")
        mode = getattr(args, "mode", "read")
        warrant = authority.require("company.grant", home=store.home, what="company %s %s %s" % (cmd, args.employee, mode),
                          check=lambda sc: authority.COMPANY_MODES.index(mode)
                          <= authority.COMPANY_MODES.index(sc["max_mode"]))["warrant"]
    path = roster.company_path(roster.root_of_home(store.home))
    if cmd == "import":
        path.parent.mkdir(parents=True, exist_ok=True)
        roster.CompanyStore.import_jsonl(args.jsonl, path, roster.COMPANY).close()
        with roster.CompanyStore(path) as cs:
            cs.render()
            return dict(project=roster.COMPANY, head_seq=cs.head_seq())
    with _company(store) as cs:
        if cmd == "grant":
            return authority.fenced(warrant, lambda: cs.grant(args.employee, args.mode, by=by, warrant=warrant))
        if cmd == "revoke":
            return dict(employee=args.employee, revoked=authority.fenced(
                warrant, lambda: cs.revoke(args.employee, by=by, warrant=warrant)))
        if cmd == "grants":
            return cs.grants()
        if cmd == "redact":
            return cs.redact(args.entry, reason=args.reason, by=by)
        if cmd == "state":
            return cs.state()
        if cmd == "export":
            return dict(project=roster.COMPANY, export_through=cs.export(rebuild=args.rebuild))
        if a["owner"] or Path(a["home"]) == Path(store.home):
            auth = _source_auth(store, args.source)
        else:  # C3 + R4: a session cites events in its own home, whatever --home names
            from .store import Store
            with Store(Path(a["home"])) as own:
                auth = _source_auth(own, args.source)
        common = dict(source=args.source, certainty=args.certainty, auth=auth)
        if cmd == "put":
            body = dict(common, id=args.id)
            for field in ("name", "kind", "role", "description"):
                if getattr(args, field, None) is not None:
                    body[field] = getattr(args, field)
            batch = [{"kind": "person", "body": body}]
        elif cmd == "alias":
            batch = [{"kind": "alias", "body": dict(common, id=args.id, alias=args.alias)}]
        elif cmd == "unalias":
            batch = [{"kind": "unalias", "body": dict(common, alias=args.alias)}]
        elif cmd == "retire":
            batch = [{"kind": "retire", "body": dict(common, id=args.id, reason=args.reason)}]
        else:
            raise ValueError("unknown roster company command %s" % cmd)
        if not a["owner"]:
            # In a session the ACTOR writes as itself (R4: never the --home target's identity, never operator:*),
            # needs the company write grant, fenced on its own run and audited in its own home.
            out = cs.append(batch, author=a["id"], run=a["run"], fence=a["store_path"])
            authority.audit(a["home"], "roster.company." + cmd, {"what": out["ids"][0], "by": a["id"],
                                                                 "source": args.source, "batch": out.get("batch")})
        else:
            out = cs.append(batch, author="operator:" + by, operator=True)
        cs.render()
        return out


def render_command(store):
    with _company(store) as cs:
        company_files = cs.render()
        company = cs.state()
    return dict(home=store.roster_render(company=company), company=company_files)


def show_command(store, person=None):
    _refuse_in_session("roster show")
    company = roster.company_state_for_home(store.home)
    r = store.roster_rendered(company=company)
    if person:
        hit = roster.resolve(company, person)
        if not hit["id"]:
            raise ProjectError("unknown_person", "%r has no company profile" % person)
        return dict(id=hit["id"], view=r["views"].get(hit["id"]))
    return dict(self=r["self"], views=r["views"], stale=r["stale"])


# ---- seed (§9 step 4; C7) -----------------------------------------------------------------------------

def seed_plan(store, company=None):
    """Deterministic proposal list from this home's active memories. Nothing is written.

    C7: claim text = predicate + gist, source cites the memory ID and is marked roster-seed, never strengthened:
    belief → inferred; every other kind → observed, and only when the memory's own source is non-empty
    (memories with an empty source are skipped and counted).

    r2 (review-P1 R1, C1): self-subject memories are never proposed. They already appear once in the
    snapshot (the `self` index or memory_index); self notes come only from self_note. Counted as skipped_self.
    """
    if company is None:
        company = roster.company_state_for_home(store.home)
    me = store.identity()["id"]
    plan, counts = [], {"views": {}, "skipped_self": 0, "skipped_unsourced": 0, "skipped_unresolved": 0}
    memories = sorted((m for m in store._all("memories") if not m.get("superseded_by")),
                      key=lambda m: (m.get("created_at") or "", m["id"]))
    for m in memories:
        if m["subject"] == me:
            counts["skipped_self"] += 1
            continue
        if not (m.get("source") or "").strip():
            counts["skipped_unsourced"] += 1
            continue
        text = "%s: %s" % (m["predicate"], roster._gist(m.get("value") or ""))
        text = text[:roster.CLAIM_MAX]
        source = "%s (%s)" % (m["id"], SEED_AUTHOR)
        certainty = "inferred" if m.get("kind") == "belief" else "observed"
        key = "seed:" + m["id"]
        hit = roster.resolve(company, m["subject"])
        if hit["id"] == me:  # an alias of myself: still a self memory, never proposed (R1)
            counts["skipped_self"] += 1
            continue
        if not hit["id"]:
            counts["skipped_unresolved"] += 1
            continue
        claim = {"cid": "seed-" + m["id"].split("-", 1)[-1][:32], "text": text, "source": source,
                 "certainty": certainty,
                 "kind": "working" if m.get("kind") in ("preference", "procedure") else "history"}
        plan.append({"kind": "view", "key": key, "memory": m["id"], "body": {"person": hit["id"],
                                                                            "claims": [claim]}})
        counts["views"][hit["id"]] = counts["views"].get(hit["id"], 0) + 1
    return plan, counts


def seed_command(store, apply=False):
    if apply:
        _refuse_in_session("roster seed --apply")
    company = roster.company_state_for_home(store.home)
    plan, counts = seed_plan(store, company)
    report = dict(home=str(store.home), apply=bool(apply), counts=counts,
                  proposals=[{"kind": p["kind"], "memory": p["memory"],
                              "person": p["body"].get("person"), "section": p["body"].get("section")}
                             for p in plan])
    if apply:
        written = replayed = 0
        before = {e["id"] for e in store.roster_entries()}
        for p in plan:
            entry = store.roster_append(p["kind"], p["body"], key=p["key"], company=company, author=SEED_AUTHOR)
            if entry["id"] in before:
                replayed += 1
            else:
                written += 1
        report.update(written=written, replayed=replayed, rendered=store.roster_render(company=company))
    return report


def add_parser(sub):
    r = sub.add_parser("roster", help="M26 roster: company profiles, private views, render, seed")
    rs = r.add_subparsers(dest="roster_command", required=True)
    co = rs.add_parser("company", help="the company profile log (operator; in a session only with a grant)")
    cs = co.add_subparsers(dest="company_command", required=True)

    def write_args(p):
        p.add_argument("--by", required=True)
        p.add_argument("--source", required=True, help="event ID or document; an event's auth is recorded as "
                                                         "received in this home")
        p.add_argument("--certainty", choices=list(roster.CERTAINTIES), default="stated")

    put = cs.add_parser("put", help="create or revise a person")
    put.add_argument("id")
    put.add_argument("--name")
    put.add_argument("--kind", choices=list(roster.PERSON_KINDS))
    put.add_argument("--role")
    put.add_argument("--description")
    write_args(put)
    al = cs.add_parser("alias", help="add an alias for a person")
    al.add_argument("id")
    al.add_argument("alias")
    write_args(al)
    ua = cs.add_parser("unalias", help="remove an alias")
    ua.add_argument("alias")
    write_args(ua)
    rt = cs.add_parser("retire", help="retire a person")
    rt.add_argument("id")
    rt.add_argument("--reason", required=True)
    write_args(rt)
    gr = cs.add_parser("grant", help="owner or company.grant delegate (M28): grant company read or write")
    gr.add_argument("employee")
    gr.add_argument("--mode", choices=["read", "write"], required=True)
    gr.add_argument("--by", required=True)
    rv = cs.add_parser("revoke", help="owner or company.grant delegate: revoke a company grant")
    rv.add_argument("employee")
    rv.add_argument("--by", required=True)
    cs.add_parser("grants", help="list company grants")
    cs.add_parser("state", help="print the folded company state")
    rd = cs.add_parser("redact", help="operator only (the company log has no source bytes; always refused)")
    rd.add_argument("entry")
    rd.add_argument("--reason", required=True)
    rd.add_argument("--by", required=True)
    ex = cs.add_parser("export", help="bring company/log.jsonl up to date")
    ex.add_argument("--rebuild", action="store_true")
    im = cs.add_parser("import", help="operator only: load a log.jsonl into an empty company store")
    im.add_argument("jsonl")
    rs.add_parser("render", help="regenerate this home's roster Markdown and the company people files")
    sh = rs.add_parser("show", help="operator only: print this home's self file and private views")
    sh.add_argument("person", nargs="?")
    sd = rs.add_parser("seed", help="propose roster entries from sourced memories (§9, C7)")
    mode = sd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")


def command(store, args):
    cmd = args.roster_command
    if cmd == "company":
        return company_command(store, args)
    if cmd == "render":
        return render_command(store)
    if cmd == "show":
        return show_command(store, args.person)
    if cmd == "seed":
        return seed_command(store, apply=args.apply)
    raise ValueError("unknown roster command %s" % cmd)

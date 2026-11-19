"""Roster (M26): layer 2, the company profile log.

The company log is a ``ProjectStore`` at ``<root>/company/memory.sqlite3`` (``<root>`` = the parent of the
employee homes) with its own pure fold, ``company_fold``. ``company`` is a reserved name: project commands
and project tools refuse it, and it is never listed as a project.

Entry kinds (every body carries ``source`` and ``certainty``; the author is the ``--by``):
- ``person`` {id, kind: human|employee|system, name?, role?, description?, home?} creates or revises a
  profile. Each change of ``role`` bumps ``role_rev``.
- ``alias`` {alias, id} / ``unalias`` {alias}: aliases are unique host-wide after case folding and
  whitespace collapsing. A person's ID and name are implicit aliases.
- ``retire`` {id, reason}: the profile stays readable, marked retired.
- ``retract`` {target}: as in project memory. A non-owner may retract only a delegation child it wrote, never a
  ``retract`` or ``retire`` entry, and a ``person`` entry only under the role rules (A3/A4) and never removing a
  profile (issue #2). ``authority.recheck`` reads the same fold, so fenced writes and ``covering`` agree.
- ``delegation`` (M28 Warrant) {grantee, actions, scope, sources|parent, note?}: a ROOT cites authenticated
  human events ({event, excerpt, auth} each, verified by authority.verify_sources) and is written by the owner
  only; a CHILD names ``parent`` (an active root), narrows it and cannot hold delegation.grant.
- ``policy`` (M28 P1c) {name, reason, ...}: team policy the manager sets without a re-bootstrap (D13). Names
  (authority.POLICIES): ``limit.ceiling`` {max_usd_per_day: number > 0 | "none", allow_disable: bool} and
  ``worker.defaults`` {allowed_models, allowed_claude}. The newest active entry of a name wins; retracting it
  restores the previous one. ``adapter.reviews`` {sums_path, sums_sha256, review_ref} (P1c-b) is a list policy:
  every active entry is one accepted review (state ``policy_lists``). A session writer must hold the matching action (authority.POLICIES) under an
  active root and the company write grant; it cites no human event (team policy, not human words).

Auth (C3): an entry records ``auth`` exactly as supplied by the caller from the cited event (null when
the caller has none). Neither the fold nor validation ever sets or upgrades it. ``certainty: stated``
means "the cited source says so", not "authenticated".

Company write grants reuse the ProjectStore grants table (operator CLI, audited).
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from pathlib import Path

from .projectlog import active_entries
from .projects import ProjectError, ProjectStore, _canon

COMPANY = "company"
COMPANY_KINDS = ("person", "alias", "unalias", "retire", "retract", "delegation", "policy")
PERSON_KINDS = ("human", "employee", "system")
CERTAINTIES = ("stated", "inferred")
NAME_MAX = 120
ROLE_MAX = 300
DESCRIPTION_MAX = 600
ALIAS_MAX = 120
_PERSON_FIELDS = ("name", "role", "description", "home")


def fold_alias(text: str) -> str:
    """Case-fold and collapse whitespace: 'Dana  Example' and 'dana example' are the same alias."""
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def company_path(root: str | os.PathLike) -> Path:
    return Path(root) / COMPANY / "memory.sqlite3"


def root_of_home(home: str | os.PathLike) -> Path:
    """The roster root for an employee home (homes live side by side under it, like projects/)."""
    return Path(home).parent


def _provenance(e: dict) -> dict:
    b = e["body"]
    return {"entry": e["id"], "seq": e["seq"], "by": e["author"], "at": e["at"], "source": b.get("source"),
            "certainty": b.get("certainty"), "auth": copy.deepcopy(b.get("auth"))}


def company_fold(entries: list[dict]) -> dict:
    """Deterministic fold of the company log: one profile per person ID plus the alias table."""
    state: dict = {
        "through": max((e["seq"] for e in entries), default=0),
        "people": {},
        "aliases": {},     # folded alias -> {id, alias, entry}
        "retracted": sorted(e["body"]["target"] for e in entries if e["kind"] == "retract"),
        "delegations": {},  # M28: active delegation entries by id
        "policies": {},     # M28 P1c: newest active policy entry by name
        "policy_lists": {}, # M28 P1c-b: every active entry, by name then entry id (authority.LIST_POLICIES)
    }
    people = state["people"]
    for e in active_entries(entries):
        kind, body = e["kind"], e["body"]
        if kind == "person":
            pid = body["id"]
            p = people.get(pid)
            if p is None:
                p = people[pid] = {"id": pid, "kind": body.get("kind"), "name": None, "role": None,
                                   "description": None, "home": None, "role_rev": 0, "roles": [],
                                   "retired": None, "history": []}
            if body.get("kind"):
                p["kind"] = body["kind"]
            for f in _PERSON_FIELDS:
                if f in body and body[f] is not None:
                    if f == "role" and body[f] != p["role"]:
                        p["role_rev"] += 1
                        p["roles"].append(dict(_provenance(e), rev=p["role_rev"], role=body[f]))
                    p[f] = body[f]
            p["history"].append(_provenance(e))
        elif kind == "alias":
            state["aliases"][fold_alias(body["alias"])] = {"id": body["id"], "alias": body["alias"],
                                                           "entry": e["id"]}
            if body["id"] in people:  # backlog 31: alias edits are visible in the profile's provenance
                people[body["id"]]["history"].append(dict(_provenance(e), change="alias", alias=body["alias"]))
        elif kind == "unalias":
            gone = state["aliases"].pop(fold_alias(body["alias"]), None)
            if gone is not None and gone["id"] in people:
                people[gone["id"]]["history"].append(dict(_provenance(e), change="unalias", alias=gone["alias"]))
        elif kind == "delegation":
            state["delegations"][e["id"]] = {
                "id": e["id"], "seq": e["seq"], "by": e["author"], "at": e["at"], "run": e.get("run"),
                "grantee": body["grantee"], "actions": list(body["actions"]), "scope": copy.deepcopy(body["scope"]),
                "parent": body.get("parent"), "sources": copy.deepcopy(body.get("sources") or []),
                "context_refs": list(body.get("context_refs") or []),
                "note": body.get("note")}
        elif kind == "policy":
            state["policies"][body["name"]] = dict(copy.deepcopy(body), id=e["id"], seq=e["seq"], by=e["author"],
                                                   at=e["at"], run=e.get("run"))
            state["policy_lists"].setdefault(body["name"], {})[e["id"]] = state["policies"][body["name"]]
        elif kind == "retire":
            p = people.get(body["id"])
            if p is not None:
                p["retired"] = dict(_provenance(e), reason=body.get("reason", ""))
                p["history"].append(dict(_provenance(e), change="retire", reason=body.get("reason", "")))
    dels = state["delegations"]
    for d in list(dels.values()):  # a child whose root is gone (retracted) or itself a child is not active
        if d["parent"] and (d["parent"] not in dels or dels[d["parent"]]["parent"]):
            del dels[d["id"]]
    for p in people.values():
        p["aliases"] = sorted(a["alias"] for a in state["aliases"].values() if a["id"] == p["id"])
        p["delegations"] = [{"id": d["id"], "actions": d["actions"], "parent": d["parent"]}
                            for d in sorted(dels.values(), key=lambda d: d["seq"]) if d["grantee"] == p["id"]]
    return state


def resolve(state: dict, name: str) -> dict:
    """Map an ID, name or alias to {id, via}; {id: None, via: 'unknown'} otherwise."""
    if not isinstance(name, str) or not name.strip():
        return {"id": None, "via": "unknown"}
    people = state.get("people", {})
    if name in people:
        return {"id": name, "via": "id"}
    key = fold_alias(name)
    for pid in sorted(people):
        if fold_alias(pid) == key:
            return {"id": pid, "via": "id"}
    hit = state.get("aliases", {}).get(key)
    for pid in sorted(people):
        if people[pid].get("name") and fold_alias(people[pid]["name"]) == key:
            # The text is also this person's alias: report the alias (Rowan's M26 feedback).
            return {"id": pid, "via": "alias" if hit and hit["id"] == pid else "name"}
    if hit and hit["id"] in people:
        return {"id": hit["id"], "via": "alias"}
    return {"id": None, "via": "unknown"}


def matched_as(state: dict, pid: str, name: str) -> list[str]:
    """Every way ``name`` names ``pid``: a subset of ["id", "name", "alias"], in that order."""
    p = state.get("people", {}).get(pid)
    if p is None or not isinstance(name, str):
        return []
    key = fold_alias(name)
    out = []
    if name == pid or fold_alias(pid) == key:
        out.append("id")
    if p.get("name") and fold_alias(p["name"]) == key:
        out.append("name")
    hit = state.get("aliases", {}).get(key)
    if hit and hit["id"] == pid:
        out.append("alias")
    return out


def provenance_label(prov: dict) -> str:
    """How a profile entry's provenance reads. auth stays as recorded; nothing is upgraded."""
    if prov.get("auth"):
        return "authenticated"
    if str(prov.get("by") or "").startswith("operator:") and "identity row" in str(prov.get("source") or ""):
        return "operator seed (identity row)"
    return "unauthenticated source"


def _holder(state: dict, text: str, *, except_id: str | None = None) -> str | None:
    """Who already answers to ``text`` (as ID, name or alias), ignoring ``except_id``."""
    key = fold_alias(text)
    for pid, p in state["people"].items():
        if pid == except_id:
            continue
        if fold_alias(pid) == key or (p.get("name") and fold_alias(p["name"]) == key):
            return pid
    hit = state["aliases"].get(key)
    if hit and hit["id"] != except_id:
        return hit["id"]
    return None


def _text(body: dict, field: str, limit: int, where: str, required: bool = False) -> None:
    v = body.get(field)
    if v is None:
        if required:
            raise ProjectError("bad_entry", f"{where}: {field} is required")
        return
    if not isinstance(v, str) or not v.strip():
        raise ProjectError("bad_entry", f"{where}: {field} must be non-empty text")
    if len(v.encode("utf-8")) > limit:
        raise ProjectError("too_large", f"{where}: {field} over {limit} bytes")


class CompanyStore(ProjectStore):
    """The company profile log. Same storage, fence, grants, export and import as a project."""

    _fold = staticmethod(company_fold)

    def __init__(self, path: str | os.PathLike, project: str = COMPANY):
        super().__init__(path, COMPANY)

    @classmethod
    def at_root(cls, root: str | os.PathLike) -> "CompanyStore":
        return cls(company_path(root))

    def resolve(self, name: str) -> dict:
        return resolve(self.state(), name)

    def grants_enforced(self, db=None) -> bool:
        """Unlike a project, the company log never accepts a non-operator write without a write grant."""
        return True

    def render(self) -> dict:
        """Write <root>/company/people/<id>.md from the company fold; returns the hash index."""
        return write_rendered(Path(self.path).parent, render_company_files(self.state()))

    def person(self, name: str) -> dict | None:
        st = self.state()
        hit = resolve(st, name)
        return copy.deepcopy(st["people"][hit["id"]]) if hit["id"] else None

    def redact(self, entry_id: str, *, reason: str, by: str) -> dict:  # no source bytes in the company log
        raise ProjectError("bad_redact", "the company log has no source entries; retract instead")

    def _validate(self, existing: list[dict], batch: list[dict], *, author: str, operator: bool):
        sim = list(existing)
        staged = []
        seq = existing[-1]["seq"] if existing else 0
        for n, raw in enumerate(batch):
            kind = raw.get("kind")
            where = f"entry {n}"
            if kind not in COMPANY_KINDS:
                raise ProjectError("bad_kind", f"{where}: unknown company kind {kind!r}")
            body = json.loads(_canon(raw.get("body", {})))
            eid = raw.get("id") or "co-" + uuid.uuid4().hex
            state = company_fold(sim)
            if kind == "delegation":
                self._validate_delegation(state, body, where, author=author, operator=operator)
            elif kind == "policy":
                self._validate_policy(state, body, where, author=author, operator=operator)
                if not operator and body.get("name") == "worker.defaults":
                    from . import authority  # D15: bounded by owner-installed workers' published lists
                    try:
                        authority.check_worker_defaults(Path(self.path).parent.parent, state,
                                                        body["allowed_models"], body["allowed_claude"])
                    except authority.AuthorityError as error:
                        raise ProjectError(error.code, f"{where}: {error}")
            elif kind != "retract":
                src = body.get("source")
                if not isinstance(src, str) or not src.strip():
                    raise ProjectError("no_source", f"{where}: every company entry cites a source")
                if body.get("certainty") not in CERTAINTIES:
                    raise ProjectError("bad_certainty", f"{where}: certainty must be one of {CERTAINTIES}")
                body.setdefault("auth", None)  # C3: recorded as supplied, never derived or upgraded
            if kind == "person":
                pid = body.get("id")
                if not isinstance(pid, str) or not pid.strip() or pid != pid.strip():
                    raise ProjectError("bad_entry", f"{where}: person needs an id")
                existing_p = state["people"].get(pid)
                if existing_p is None:
                    if body.get("kind") not in PERSON_KINDS:
                        raise ProjectError("bad_entry", f"{where}: kind must be one of {PERSON_KINDS}")
                    _text(body, "name", NAME_MAX, where, required=True)
                elif body.get("kind") is not None and body["kind"] not in PERSON_KINDS:
                    raise ProjectError("bad_entry", f"{where}: kind must be one of {PERSON_KINDS}")
                _text(body, "name", NAME_MAX, where)
                _text(body, "role", ROLE_MAX, where)
                _text(body, "description", DESCRIPTION_MAX, where)
                role = body.get("role")
                if role is not None and role != (existing_p or {}).get("role") and not operator:
                    # A3: nobody changes their own company role; corrections go to the manager by question.
                    me = resolve(state, author)["id"] or author
                    if pid in (author, me):
                        raise ProjectError("own_role", f"{where}: an employee cannot set its own company role; "
                                           "ask the manager (question_ask) or the operator")
                    # A4: a role revision (and hence any STALE flag) needs an authenticated source.
                    if not body.get("auth"):
                        raise ProjectError("unauthenticated_role", f"{where}: a role change needs a source "
                                           "with non-null auth, or the operator CLI")
                for label in (pid, body.get("name")):
                    if label:
                        holder = _holder(state, label, except_id=pid)
                        if holder:
                            raise ProjectError("alias_taken", f"{where}: {label!r} already names {holder}",
                                               alias=label, holder=holder)
            elif kind == "alias":
                _text(body, "alias", ALIAS_MAX, where, required=True)
                pid = body.get("id")
                if pid not in state["people"]:
                    raise ProjectError("unknown_person", f"{where}: {pid!r} has no company profile")
                # An explicit alias equal to the same person's own name is allowed (it records a configured
                # address such as `vesper` and survives a rename); a second explicit alias with the same
                # folded text is a duplicate, and any other holder is a collision.
                explicit = state["aliases"].get(fold_alias(body["alias"]))
                holder = explicit["id"] if explicit else _holder(state, body["alias"], except_id=pid)
                if holder:
                    raise ProjectError("alias_taken", f"{where}: {body['alias']!r} already names {holder}",
                                       alias=body["alias"], holder=holder)
            elif kind == "unalias":
                _text(body, "alias", ALIAS_MAX, where, required=True)
                if fold_alias(body["alias"]) not in state["aliases"]:
                    raise ProjectError("unknown_alias", f"{where}: {body['alias']!r} is not an alias")
            elif kind == "retire":
                if body.get("id") not in state["people"]:
                    raise ProjectError("unknown_person", f"{where}: {body.get('id')!r}")
                _text(body, "reason", DESCRIPTION_MAX, where, required=True)
            elif kind == "retract":
                target = next((e for e in sim if e["id"] == body.get("target")), None)
                if target is None:
                    raise ProjectError("unknown_target", f"{where}: {body.get('target')}")
                if target["kind"] == "delegation" and not operator and (
                        not target["body"].get("parent") or target["author"] != author):
                    # D1.5: the owner may retract anything; a delegate only the children it wrote.
                    raise ProjectError("not_author", f"{where}: only the owner or the writer of a delegation "
                                       "child may retract it")
                if target["kind"] == "policy" and not operator:
                    # P1c: a session retracts a policy entry only while it holds the action that writes it.
                    self._validate_policy(state, dict(target["body"]), where, author=author, operator=False,
                                          shape=False)
                if target["kind"] in ("retract", "retire") and not operator:
                    # Issue #2: un-retracting restores the inner target (a revoked delegation, a policy, a role, a
                    # retirement), and un-retiring restores a home; only the owner may do either.
                    raise ProjectError("owner_only", f"{where}: only the owner may retract a {target['kind']} "
                                       "entry")
                if target["kind"] == "person" and not operator:
                    # Issue #2: a retract that changes a role is a role change (A3/A4); removing a profile is owner-only.
                    after = company_fold(sim + [{"seq": seq + 1, "id": eid, "batch": "", "kind": kind,
                                                 "author": author, "run": None, "at": "", "body": body}])
                    me = resolve(state, author)["id"] or author
                    for pid, before in state["people"].items():
                        now = after["people"].get(pid)
                        if now is None:
                            raise ProjectError("owner_only", f"{where}: only the owner may remove the profile "
                                               f"{pid}")
                        if now["role"] != before["role"]:
                            if pid in (author, me):
                                raise ProjectError("own_role", f"{where}: an employee cannot change its own "
                                                   "company role, including by retracting an entry")
                            if not body.get("auth"):
                                raise ProjectError("unauthenticated_role", f"{where}: a retract that changes a "
                                                   "role needs a source with non-null auth, or the operator CLI")
            seq += 1
            sim.append({"seq": seq, "id": eid, "batch": "", "kind": kind, "author": author, "run": None,
                        "at": "", "body": body})
            staged.append(({"kind": kind, "id": eid}, body, None))
        return staged

    @staticmethod
    def _validate_policy(state, body, where, *, author, operator, shape=True):
        from . import authority
        name = body.get("name")
        if name not in authority.POLICIES:
            raise ProjectError("bad_entry", f"{where}: policy name must be one of {sorted(authority.POLICIES)}")
        if shape:
            try:
                authority.parse_policy(body)
            except authority.AuthorityError as error:
                raise ProjectError(error.code, f"{where}: {error}")
        if not operator:
            me = resolve(state, author)["id"] or author
            action = authority.POLICIES[name]
            if authority.covering(state, me, action, lambda sc: True) is None:
                raise ProjectError("not_delegated", f"{where}: {me} holds no active {action} delegation, needed to "
                                   f"write policy {name}")

    @staticmethod
    def _validate_delegation(state, body, where, *, author, operator):
        from . import authority
        try:
            scope = authority.parse_scope(body.get("actions"), body.get("scope"),
                                          max_usd=authority.ceiling(state)["usd"])
        except authority.AuthorityError as error:
            raise ProjectError(error.code, f"{where}: {error}")
        body["scope"] = scope
        grantee = body.get("grantee")
        hit = resolve(state, grantee) if isinstance(grantee, str) else {"id": None}
        if not hit["id"]:
            raise ProjectError("unknown_person", f"{where}: grantee {grantee!r} has no company profile")
        body["grantee"] = grantee = hit["id"]
        me = resolve(state, author)["id"] or author
        if body.get("parent") is None:
            if not operator:
                raise ProjectError("root_requires_owner", f"{where}: a root delegation (citing human words) is "
                                   "written by the owner only; sessions record children under a root they hold")
            srcs = body.get("sources")
            if not isinstance(srcs, list) or not srcs or not all(
                    isinstance(s, dict) and s.get("event") and s.get("excerpt") and s.get("auth") for s in srcs):
                raise ProjectError("not_human_source", f"{where}: a root needs verified sources "
                                   "[{event, excerpt, auth}] (authority.verify_sources)")
            refs = body.get("context_refs")
            if refs is not None and (not isinstance(refs, list) or not all(isinstance(r, str) and r for r in refs)):
                raise ProjectError("bad_entry", f"{where}: context_refs is a list of IDs (context only, D5)")
            return
        if body.get("sources"):
            raise ProjectError("bad_entry", f"{where}: a child cites its parent, not human events")
        parent = state["delegations"].get(body["parent"])
        if parent is None:
            raise ProjectError("unknown_parent", f"{where}: {body['parent']!r} is not an active delegation")
        if parent["parent"]:
            raise ProjectError("exceeds_parent", f"{where}: children cannot be re-delegated (depth 1)")
        if not operator and (parent["grantee"] != me or authority.GRANT not in parent["actions"]):
            raise ProjectError("not_delegated", f"{where}: {me} does not hold {authority.GRANT} under {parent['id']}")
        if grantee == me:
            raise ProjectError("self_promotion", f"{where}: a delegation child cannot name its writer")
        why = authority.child_within(body["actions"], scope, parent)
        if why:
            raise ProjectError("exceeds_parent", f"{where}: {why}")


# ---------------------------------------------------------------------------------------------------------
# Layers 1 and 3: the private log (table ``roster_log`` in the employee's own state.sqlite3, schema 5).
#
# Entry kinds (author = the employee; every write is fenced by the session's run):
# - ``self`` {section, text, source, certainty}: notes about how I work. Name/role/purpose stay in identity.
# - ``view`` {person, claims: [{cid, text, kind, source, certainty}], basis_role_rev}: adds or revises claims
#   about one company person. A claim with an existing cid replaces that claim's current text.
# - ``view_retract`` {person, cid, reason}: the claim leaves the current view; history keeps it.
# - ``view_reconcile`` {person, role_rev, note}: the view now accounts for that official role revision.
# Nothing here is ever read by the company fold, so a private write cannot change an official profile.

PRIVATE_KINDS = ("self", "view", "view_retract", "view_reconcile")
SELF_SECTIONS = ("how-i-work", "strengths-limits", "commitments")
CLAIM_KINDS = ("history", "working", "expectation", "open")
VIEW_CERTAINTIES = ("observed", "inferred")
CLAIM_MAX = 600
CLAIMS_PER_ENTRY = 20


def private_fold(entries: list[dict]) -> dict:
    """Deterministic fold of one home's private roster log."""
    state: dict = {"through": max((e["seq"] for e in entries), default=0), "self": {}, "views": {}}
    for e in sorted(entries, key=lambda x: x["seq"]):
        kind, body = e["kind"], e["body"]
        prov = {"entry": e["id"], "seq": e["seq"], "at": e["at"], "run": e.get("run")}
        if kind == "self":
            state["self"].setdefault(body["section"], []).append(
                dict(prov, text=body["text"], source=body["source"], certainty=body["certainty"]))
            continue
        view = state["views"].setdefault(body["person"], {"person": body["person"], "claims": {},
                                                           "retracted": {}, "basis_role_rev": 0,
                                                           "reconciled": [], "history": []})
        if kind == "view":
            for c in body["claims"]:
                view["retracted"].pop(c["cid"], None)
                view["claims"][c["cid"]] = dict(prov, **{k: c[k] for k in ("cid", "text", "kind", "source",
                                                                            "certainty", "ref") if k in c})
            view["basis_role_rev"] = max(view["basis_role_rev"], body.get("basis_role_rev") or 0)
        elif kind == "view_retract":
            old = view["claims"].pop(body["cid"], None)
            if old is not None:
                view["retracted"][body["cid"]] = dict(old, retracted=dict(prov, reason=body.get("reason", "")))
        elif kind == "view_reconcile":
            view["basis_role_rev"] = max(view["basis_role_rev"], body["role_rev"])
            view["reconciled"].append(dict(prov, role_rev=body["role_rev"], note=body.get("note", "")))
        view["history"].append(dict(prov, kind=kind))
    return state


def _need_text(value, where: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError("bad_entry", f"{where} must be non-empty text")
    if len(value.encode("utf-8")) > limit:
        raise ProjectError("too_large", f"{where} over {limit} bytes")
    return value


def _need_source(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError("unsourced_claim", f"{where}: every claim cites a source")
    return value


def validate_private(kind: str, body: dict, *, current: dict, company: dict) -> dict:
    """Check one private entry against the current private state and company state; return the body to store.

    ``company`` is a company_fold state. ``person`` may be an ID, name or alias; the stored body uses the
    canonical ID. Raises ProjectError with codes bad_kind, bad_entry, too_large, unsourced_claim,
    bad_certainty, unknown_person, unknown_claim, bad_reconcile.
    """
    if kind not in PRIVATE_KINDS:
        raise ProjectError("bad_kind", f"unknown roster kind {kind!r}")
    if not isinstance(body, dict):
        raise ProjectError("bad_entry", "body must be an object")
    body = json.loads(_canon(body))
    if kind == "self":
        if body.get("section") not in SELF_SECTIONS:
            raise ProjectError("bad_entry", f"section must be one of {SELF_SECTIONS}")
        _need_text(body.get("text"), "text", CLAIM_MAX)
        _need_source(body.get("source"), "self note")
        if body.get("certainty") not in VIEW_CERTAINTIES:
            raise ProjectError("bad_certainty", f"certainty must be one of {VIEW_CERTAINTIES}")
        return {k: body[k] for k in ("section", "text", "source", "certainty")}
    hit = resolve(company, body.get("person") if isinstance(body.get("person"), str) else "")
    if hit["id"] is None:
        raise ProjectError("unknown_person", f"{body.get('person')!r} has no company profile; ask the manager",
                           person=body.get("person"))
    pid = hit["id"]
    rev = company["people"][pid]["role_rev"]
    view = current["views"].get(pid, {"claims": {}})
    if kind == "view":
        claims = body.get("claims")
        if not isinstance(claims, list) or not claims or len(claims) > CLAIMS_PER_ENTRY:
            raise ProjectError("bad_entry", f"claims must be a list of 1-{CLAIMS_PER_ENTRY}")
        out, seen = [], set()
        for n, c in enumerate(claims):
            where = f"claim {n}"
            if not isinstance(c, dict):
                raise ProjectError("bad_entry", f"{where} must be an object")
            _need_text(c.get("text"), f"{where} text", CLAIM_MAX)
            _need_source(c.get("source"), where)
            if c.get("kind") not in CLAIM_KINDS:
                raise ProjectError("bad_entry", f"{where}: kind must be one of {CLAIM_KINDS}")
            if c.get("certainty") not in VIEW_CERTAINTIES:
                raise ProjectError("bad_certainty", f"{where}: certainty must be one of {VIEW_CERTAINTIES}")
            cid = c.get("cid") or "c-" + uuid.uuid4().hex[:12]
            if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", cid) or cid in seen:
                raise ProjectError("bad_entry", f"{where}: bad or duplicate cid {cid!r}")
            seen.add(cid)
            prev = view["claims"].get(cid)
            if (prev is not None and prev["certainty"] == "inferred" and c["certainty"] == "observed"
                    and c["source"].strip() == (prev.get("source") or "").strip()):
                # A5: repeating a claim never strengthens it; an upgrade needs a different source.
                raise ProjectError("same_source_upgrade", f"{where}: raising {cid!r} to observed needs a source "
                                   "different from the previous revision", cid=cid)
            item = {"cid": cid, "text": c["text"], "kind": c["kind"], "source": c["source"],
                    "certainty": c["certainty"]}
            ref = c.get("ref")
            if c["kind"] == "open" or ref is not None:
                # A1: open threads link to the live question/work record instead of restating it.
                if not isinstance(ref, str) or not re.fullmatch(r"(question|work)-[0-9a-f]{8,32}", ref):
                    raise ProjectError("bad_entry", f"{where}: an open-thread claim needs ref = a question-… "
                                       "or work-… ID")
                item["ref"] = ref
            out.append(item)
        return {"person": pid, "claims": out, "basis_role_rev": rev}
    if kind == "view_retract":
        if body.get("cid") not in view["claims"]:
            raise ProjectError("unknown_claim", f"no current claim {body.get('cid')!r} about {pid}")
        return {"person": pid, "cid": body["cid"], "reason": _need_text(body.get("reason"), "reason", CLAIM_MAX)}
    # view_reconcile
    r = body.get("role_rev")
    if not isinstance(r, int) or isinstance(r, bool) or r < 1 or r > rev:
        raise ProjectError("bad_reconcile", f"role_rev must be between 1 and the current {rev}")
    note = body.get("note", "")
    if note:
        _need_text(note, "note", CLAIM_MAX)
    return {"person": pid, "role_rev": r, "note": note}


def company_state_for_home(home: str | os.PathLike) -> dict:
    """The company fold visible from a home, without creating the company log when it doesn't exist."""
    path = company_path(root_of_home(home))
    if not path.exists():
        return company_fold([])
    with CompanyStore(path) as cs:
        return cs.state()


# --- Rendering (step 3) -----------------------------------------------------------------------------------
# The Markdown files are generated views of the logs, never authority (design D1). The snapshot embeds the
# same text. Each render records the sha256 of every file it wrote in ``.rendered.json`` next to them, so
# ``drift`` can report hand edits (roster_view_drift); the next render overwrites them.

SELF_BUDGET = 4096
PERSON_BUDGET = 1024
VIEW_BUDGET = 2048
GIST_CHARS = 80
RENDER_INDEX = ".rendered.json"
_MEMORY_REF = re.compile(r"memory-[0-9a-f]{8,32}")


def private_marker(owner_name: str) -> str:
    """A7: the fixed marker on every private view (tests grep for it)."""
    return f"PRIVATE VIEW — not official (private to {owner_name})"


def _gist(text: str, n: int = GIST_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n] + "…"


def _fit(lines: list[str], budget: int, overflow: list[str] | None = None) -> str:
    """Join lines; if over budget, drop from the end and append the overflow index lines that fit."""
    out = "\n".join(lines) + "\n"
    if len(out.encode("utf-8")) <= budget:
        return out
    kept: list[str] = []
    tail = ["", "_(truncated: over %d bytes)_" % budget] + list(overflow or [])
    for line in lines:
        trial = "\n".join(kept + [line] + tail) + "\n"
        if len(trial.encode("utf-8")) > budget:
            break
        kept.append(line)
    out = "\n".join(kept + tail) + "\n"
    while len(out.encode("utf-8")) > budget and tail:
        tail.pop()
        out = "\n".join(kept + tail) + "\n"
    return out


def render_self(identity: dict, self_state: dict, self_memories: list[dict]) -> str:
    """C1: identity + explicit self entries + a short index of self-subject memories (IDs and gists only)."""
    lines = [f"# Self: {identity.get('name')}", "",
             f"- id: {identity.get('id')}", f"- role: {identity.get('role')}",
             f"- purpose: {_gist(identity.get('purpose'), 400)}", ""]
    overflow = []
    for section in SELF_SECTIONS:
        items = self_state.get("self", {}).get(section, [])
        lines.append(f"## {section}")
        if not items:
            lines.append("_(none yet)_")
        for it in items:
            mark = "" if it["certainty"] == "observed" else " (inferred)"
            lines.append(f"- {it['text']}{mark} — source: {it['source']} [{it['entry']}]")
            overflow.append(f"- {section}: {it['entry']}")
        lines.append("")
    mems = [m for m in self_memories if not m.get("superseded_by")]
    if mems:
        lines.append("## memory index (self-subject; fetch by ID)")
        for m in mems:
            lines.append(f"- {m['id']} [{m.get('kind')}] {_gist(m.get('predicate'), 60)}: {_gist(m.get('value'))}")
    return _fit(lines, SELF_BUDGET, overflow)


def render_person(p: dict) -> str:
    """One company profile (layer 2, official)."""
    lines = [f"# {p.get('name') or p['id']} (official company profile)", "",
             f"- id: {p['id']}", f"- kind: {p.get('kind')}"]
    if p.get("role"):
        last = p["roles"][-1] if p.get("roles") else {}
        auth = provenance_label(last)
        lines.append(f"- role (rev {p['role_rev']}): {p['role']} — by {last.get('by')}, source "
                     f"{last.get('source')}, {last.get('certainty')}, {auth}")
    if p.get("aliases"):
        lines.append("- aliases: " + ", ".join(p["aliases"]))
    if p.get("home"):
        lines.append(f"- home: {p['home']}")
    if p.get("retired"):
        lines.append(f"- RETIRED: {p['retired'].get('reason')} (by {p['retired'].get('by')})")
    for d in p.get("delegations") or []:  # M28: delegated authority is public
        lines.append(f"- delegation {d['id']}{' (child of ' + d['parent'] + ')' if d.get('parent') else ''}: "
                     + ", ".join(d["actions"]))
    if p.get("description"):
        lines += ["", _gist(p["description"], DESCRIPTION_MAX)]
    return _fit(lines, PERSON_BUDGET)


def render_view(view: dict, profile: dict | None, owner_name: str, *, ref_status=None, memory_superseded=None) -> str:
    """One private view (layer 3). ``ref_status(id)`` gives a live question/work status or None (A1);
    ``memory_superseded(id)`` gives the superseding memory ID or None (A2). Nothing changes automatically."""
    ref_status = ref_status or (lambda _id: None)
    memory_superseded = memory_superseded or (lambda _id: None)
    pid = view["person"]
    name = (profile or {}).get("name") or pid
    lines = [f"# {private_marker(owner_name)}", f"## View of {name} ({pid})", ""]
    rev = (profile or {}).get("role_rev", 0)
    basis = view.get("basis_role_rev", 0)
    if profile and rev > basis:
        roles = profile.get("roles", [])
        old = next((r["role"] for r in roles if r["rev"] == basis), None)
        new = roles[-1] if roles else {}
        lines += [f"**STALE**: official role changed rev {basis} → {rev} ({old!r} → {new.get('role')!r}, by "
                  f"{new.get('by')}, source {new.get('source')}); your view was written against rev {basis}. "
                  "Reconcile with view_put(reconciles=%d)." % rev, ""]
    for kind in CLAIM_KINDS:
        claims = [c for c in view.get("claims", {}).values() if c["kind"] == kind]
        if not claims:
            continue
        lines.append(f"### {kind}")
        for c in sorted(claims, key=lambda x: x["seq"]):
            mark = "" if c["certainty"] == "observed" else " (inferred)"
            line = f"- [{c['cid']}] {c['text']}{mark} — source: {c['source']}"
            if c.get("ref"):
                st = ref_status(c["ref"])
                line += f" — → {c['ref']} ({st or 'not found'})"
            for mid in _MEMORY_REF.findall(c["source"]):
                newer = memory_superseded(mid)
                if newer:
                    line += f" — basis superseded → {newer}"
            lines.append(line)
        lines.append("")
    if not view.get("claims"):
        lines.append("_(no current claims)_")
    return _fit(lines, VIEW_BUDGET, [f"- {cid}" for cid in sorted(view.get("claims", {}))])


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_rendered(directory: str | os.PathLike, files: dict[str, str]) -> dict:
    """Write ``files`` (relative path -> text) under ``directory`` (0700 dirs, 0600 files), remove stale
    rendered files from the previous index, and record hashes. Returns the new index."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    idx_path = d / RENDER_INDEX
    old = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    for rel in old:
        if rel not in files and (d / rel).exists():
            (d / rel).unlink()
    index = {}
    for rel, text in sorted(files.items()):
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, p)
        index[rel] = _sha(text)
    tmp = idx_path.with_name(RENDER_INDEX + ".tmp")
    tmp.write_text(json.dumps(index, sort_keys=True, indent=1), encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, idx_path)
    return index


def drift(directory: str | os.PathLike) -> list[dict]:
    """Rendered files whose bytes no longer match the recorded hash (hand edits) or that are missing."""
    d = Path(directory)
    idx_path = d / RENDER_INDEX
    if not idx_path.exists():
        return []
    out = []
    for rel, sha in sorted(json.loads(idx_path.read_text()).items()):
        p = d / rel
        if not p.exists():
            out.append({"file": rel, "problem": "missing"})
        elif _sha(p.read_text(encoding="utf-8")) != sha:
            out.append({"file": rel, "problem": "roster_view_drift"})
    return out


def _safe_name(pid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", pid) + ".md"


def render_company_files(state: dict) -> dict[str, str]:
    return {"people/" + _safe_name(pid): render_person(p) for pid, p in sorted(state["people"].items())}

"""M28 "Warrant": delegated authority instead of the blanket "operator" gate.

Actor: ``actor()`` reads SYNTHETICS_SESSION_RUN and SYNTHETICS_HOME (brain.py exports both to every session).
No run -> the owner (host account / desktop). Both set -> that home's identity, provided the run is
``running`` there. Anything else is refused (never a fallback to owner). The target home of a command is
resolved separately and compared by identity and path, never by a --by label.

Authority: ``delegation`` entries in the company log (roster.py). A ROOT cites authenticated human events
(validated here, at write) and can be written only by the owner actor (D1). A CHILD names ``parent`` = a
root, narrows it, and is written from a session holding ``delegation.grant`` under that root plus the
company write grant (fenced on its run). ``require()`` walks child -> parent at use. Access grants,
humans.json operator classification and --by labels are never read here (R1, R3).

Honest limit (design §0): the shell is not sandboxed. A session that strips its environment looks like the
owner. Warrant makes the supported path scoped and audited; it is not an OS boundary.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

HOME_ENV = "SYNTHETICS_HOME"
SESSION_ENV = "SYNTHETICS_SESSION_RUN"

MODES = ("read", "post", "write")
COMPANY_MODES = ("read", "write")
INITIAL_MAX_USD = 150.0  # initial team policy (manager-set), not an owner ceiling (D13); see ceiling()
POLICY_USD = "policy"  # D18: limit.raise max_usd_per_day bounded only by the effective limit.ceiling
MAX_USD = INITIAL_MAX_USD  # kept for callers that predate P1c; use ceiling(state) for the effective value
DURATIONS = ("today", "persistent")
WORKER_OPS_SCOPE = ("install", "start", "stop", "restart", "remove", "kickstart", "*")  # D9a; kickstart = restart

# action -> {scope key: kind}. kinds: "list" (subset; "*" in a parent list covers anything), "homes"
# (canonical home identity IDs, or "employees" = every employee home under the same root), "mode" /
# "company_mode" (rank <=), "usd" (a number <= the limit.ceiling policy, or "policy", D18), "durations" (subset of DURATIONS), "labels" (launchd labels:
# exact, "*" (root only), or a prefix pattern ending in ".*" on a dot boundary, D19).
# D1 ceiling: no owner-only or credential item and nothing about private views is ever in this table.
CATALOGUE = {
    "project.grant": {"projects": "list", "max_mode": "mode"},
    "project.redact": {"projects": "list"},
    "company.grant": {"max_mode": "company_mode"},
    "runtime.request": {"homes": "homes"},
    "worker.manage": {"homes": "homes", "ops": "list"},                       # P1b
    "limit.resume": {"homes": "homes"},                                       # P1b
    "limit.raise": {"homes": "homes", "max_usd_per_day": "usd", "durations": "durations"},  # P1b
    "adapter.manage": {"labels": "labels"},                                   # P1b; D19 prefix patterns
    "kit.run": {"kits": "paths"},                                             # P1b
    "adapter.install": {"labels": "labels", "roots": "paths"},               # P1c-b; D19 prefix patterns
    "delegation.grant": {},
}
GRANT = "delegation.grant"

# P1c: company-log ``policy`` names -> the action a session writer must hold (under an active root).
POLICIES = {"limit.ceiling": "limit.raise", "worker.defaults": "worker.manage", "adapter.reviews": "adapter.install",
            "host.rules": "runtime.request"}  # PUB-1 G3: host rules shown to every home's sessions
HOST_RULES_MAX = 8192
# P1c-b: policies where every active entry counts (a list of accepted items), not only the newest.
LIST_POLICIES = ("adapter.reviews",)
NOT_YET = "not yet implemented under delegation"

# Not in the catalogue, with the authority they actually lack (docs/authority.md, D2/D3).
OWNER_ONLY = {
    "project import": "recovery-only: rebuilds an empty store from an export",
    "company import": "recovery-only: rebuilds an empty company store from an export",
    "company redact": "the company log has no source bytes; retract instead",
    "access grant": "not yet implemented under delegation (owned: Vesper backlog, scoped read-only access.grant)",
    "access revoke": "not yet implemented under delegation (owned: Vesper backlog, with access.grant)",
    "work --override": "evidence integrity: it closes work without citable evidence",
    "history import": "fabricated provenance: it writes events with chosen senders into a home's record",
    "init": "defines who is human in a new home (humans.json)",
    "routine seed": "provisioning (RTN, docs/routines.md); a session re-arms its own routine with routine_rearm",
    "routine cadence": "provisioning (RTN, docs/routines.md); a session re-arms its own routine with routine_rearm",
    "routine restore": "provisioning (RTN, docs/routines.md); a session clears its own quarantine with routine_rearm",
    "routine teardown": "provisioning (RTN, docs/routines.md); a session cannot close a routine",
    "limit raise": "a --date other than today: not yet implemented under delegation (D7). Above the effective "
                   "limit.ceiling or 0 without allow_disable is the team policy refusal exceeds_ceiling (D13, D18-r2)",
    "adapter install": "without adapter.install and a --review listed in `policy adapter.reviews`, or with an "
                       "executable other than /usr/bin/python3 or CLT python3, code outside the delegation's roots "
                       "or files that differ from the reviewed sums: third-party or unreviewed executables stay "
                       "with David (D13); replacing a non-com.synthetics.adapter.* label: not yet implemented "
                       "under delegation",
    "adapter stop": "cuts a channel; delegated adapter ops are start/restart/reinstall only",
    "adapter remove": "cuts a channel; delegated adapter ops are start/restart/reinstall only",
    "byline set": "attribution.json is the owner's statement of how this employee is attributed (#15 Byline)",
    "byline clear": "attribution.json is the owner's statement of how this employee is attributed (#15 Byline)",
    "roster delegate (root)": "the owner's judgement that a human's words authorize this scope (D1)",
}
CREDENTIAL = ("channel provision", "channel register", "feed --credential-file", "ingest --credential-file",
              "postern run", "channel contract", "auth backfill", "question-routed")


class AuthorityError(ValueError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.detail = detail


# ---- actor -----------------------------------------------------------------------------------------------

def actor(env=None) -> dict:
    """{owner: True} or {owner: False, id, home, run, store_path}. Raises actor_unverified / run_inactive."""
    env = os.environ if env is None else env
    run, home = env.get(SESSION_ENV) or None, env.get(HOME_ENV) or None
    if not run:
        # Deviation from design §1 (handoff P1a-D1): SYNTHETICS_HOME alone is the CLI's ordinary --home default,
        # set by operators and test helpers, so it does not make a session. A session that unsets its run looks
        # like the owner either way (honest limit, §0).
        return {"owner": True, "id": "owner"}
    if not home:
        raise AuthorityError("actor_unverified", "%s is set but %s is not; a session needs both"
                             % (SESSION_ENV, HOME_ENV))
    path = Path(home).expanduser().resolve()
    db = path / "state.sqlite3"
    if not db.is_file():
        raise AuthorityError("actor_unverified", "%s=%s is not an employee home" % (HOME_ENV, path))
    from .sqlro import connect_ro
    conn = connect_ro(db)
    try:
        row = conn.execute("SELECT data FROM runs WHERE id=?", (run,)).fetchone()
        ident = conn.execute("SELECT data FROM identity LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None or json.loads(row[0]).get("status") != "running":
        raise AuthorityError("run_inactive", "run %s is not running in %s" % (run, path), run=run)
    if ident is None:
        raise AuthorityError("actor_unverified", "%s has no identity" % path)
    return {"owner": False, "id": json.loads(ident[0])["id"], "home": str(path), "run": run,
            "store_path": str(db)}


def home_identity(home) -> str | None:
    db = Path(home).expanduser().resolve() / "state.sqlite3"
    if not db.is_file():
        return None
    from .sqlro import connect_ro
    conn = connect_ro(db)
    try:
        row = conn.execute("SELECT data FROM identity LIMIT 1").fetchone()
    finally:
        conn.close()
    return json.loads(row[0])["id"] if row else None


# ---- audit -----------------------------------------------------------------------------------------------

def audit(home, action: str, data: dict) -> None:
    """Best effort audit row in a home (the actor's, or the target's for owner/target-side rows)."""
    if not home:
        return
    from .store import Store
    try:
        with Store(Path(home)) as s:
            s.audit_note(action, data.get("delegation") or data.get("what") or data.get("action"), data)
    except Exception:
        pass


# ---- refusal helpers -------------------------------------------------------------------------------------

def owner_only(what: str, env=None) -> dict:
    """Allow only the owner actor; a session (or an unverifiable one) is refused and audited."""
    try:
        a = actor(env)
    except AuthorityError as error:
        raise AuthorityError("owner_only", "%s is owner-only (%s); the caller could not be verified as the "
                             "owner: %s" % (what, OWNER_ONLY.get(what, "credential boundary"), error), what=what)
    if a["owner"]:
        return a
    audit(a["home"], "authority.refused", {"what": what, "code": "owner_only", "run": a["run"], "actor": a["id"]})
    raise AuthorityError("owner_only", "%s is owner-only: %s" % (what, OWNER_ONLY.get(what, "credential boundary")),
                         what=what)


def credential(what: str, env=None) -> None:
    """C1-C7: refused for any session actor, whatever delegations it holds."""
    try:
        a = actor(env)
    except AuthorityError as error:
        raise AuthorityError("credential_boundary", "%s is refused: %s" % (what, error), what=what)
    if not a["owner"]:
        audit(a["home"], "authority.refused", {"what": what, "code": "credential_boundary", "run": a["run"],
                                               "actor": a["id"]})
        raise AuthorityError("credential_boundary", "%s holds or mints channel credentials and is refused inside "
                             "an employee session (%s is set)" % (what, SESSION_ENV), what=what)


def check_by(a: dict, by: str | None, company: dict | None = None) -> str:
    """R4: for a session actor --by is a display label that must name the actor (or be omitted)."""
    if a["owner"]:
        return by or "operator"
    if by in (None, "", a["id"]):
        return a["id"]
    if company is not None:
        from . import roster
        if roster.resolve(company, by)["id"] == a["id"]:
            return a["id"]
    raise AuthorityError("by_mismatch", "--by %r does not name the session actor %s" % (by, a["id"]),
                         by=by, actor=a["id"])


def exceeds_ceiling(a: dict, usd: float, cap: dict) -> None:
    """D18-r2 (4): a session ``limit raise`` above the effective ``limit.ceiling``, or 0 while it does not set
    allow_disable, is refused under team policy (D13), not as owner-only. Audited in the actor home."""
    if usd == 0:
        why = "0 switches the daily guard off, and the limit.ceiling policy does not set allow_disable"
    else:
        why = "%s exceeds the effective limit.ceiling of %s per day" % (usd, cap["usd"])
    entry = cap.get("entry") or "initial team policy $%s (no policy entry)" % INITIAL_MAX_USD
    audit(a.get("home"), "authority.refused", {"what": "limit raise", "code": "exceeds_ceiling", "run": a.get("run"),
                                               "actor": a.get("id"), "usd": usd, "ceiling": cap["usd"],
                                               "allow_disable": cap["allow_disable"], "entry": cap.get("entry")})
    raise AuthorityError("exceeds_ceiling", "limit raise refused by team policy: %s (limit.ceiling: %s). The manager "
                         "can change it with `authority policy limit.ceiling`." % (why, entry),
                         ceiling=cap["usd"], allow_disable=cap["allow_disable"], entry=cap.get("entry"))


def finite_usd(text) -> float:
    """D18-r2: argparse ``type`` for USD amounts. Refuses NaN and +/-infinity (float() accepts "nan", "inf")."""
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("not a finite number: %r" % (text,))
    return v


def _no_constant(token):
    raise AuthorityError("bad_json", "non-finite JSON number %s is not allowed (D18-r2)" % token)


def strict_json(text: str):
    """D18-r2: json.loads without the NaN / Infinity / -Infinity extensions Python accepts by default."""
    return json.loads(text, parse_constant=_no_constant)


def _finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ---- scope -----------------------------------------------------------------------------------------------

def parse_policy(body: dict) -> dict:
    """Validate (and normalize in place) a ``policy`` body. Raises AuthorityError(bad_policy)."""
    name = body.get("name")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise AuthorityError("bad_policy", "a policy entry needs a reason")
    fields = {"limit.ceiling": {"max_usd_per_day", "allow_disable"},
              "worker.defaults": {"allowed_models", "allowed_claude"},
              "adapter.reviews": {"sums_path", "sums_sha256", "review_ref"},
              "host.rules": {"text"}}.get(name)
    if fields is None:
        raise AuthorityError("bad_policy", "unknown policy %r (known: %s)" % (name, ", ".join(sorted(POLICIES))))
    extra = set(body) - fields - {"name", "reason", "context_ref"}
    if extra or fields - set(body):
        raise AuthorityError("bad_policy", "policy %s takes %s (+ name, reason, context_ref?)" % (name, sorted(fields)))
    if name == "limit.ceiling":
        v = body["max_usd_per_day"]
        if v != "none" and (not _finite_number(v) or not v > 0):  # D18-r2: NaN/inf refused
            raise AuthorityError("bad_policy", "limit.ceiling max_usd_per_day must be a finite number > 0 or \"none\"")
        if not isinstance(body["allow_disable"], bool):
            raise AuthorityError("bad_policy", "limit.ceiling allow_disable must be true or false")
    elif name == "host.rules":
        t = body["text"]
        if not isinstance(t, str) or not t.strip():
            raise AuthorityError("bad_policy", "host.rules text must be non-empty")
        if len(t.encode("utf-8")) > HOST_RULES_MAX:
            raise AuthorityError("bad_policy", "host.rules text is limited to %d bytes" % HOST_RULES_MAX)
    elif name == "adapter.reviews":
        sp, sha, ref = body["sums_path"], body["sums_sha256"], body["review_ref"]
        if not isinstance(sp, str) or not os.path.isabs(sp):
            raise AuthorityError("bad_policy", "adapter.reviews sums_path must be an absolute path")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise AuthorityError("bad_policy", "adapter.reviews sums_sha256 must be 64 lowercase hex digits")
        if not isinstance(ref, str) or not ref.strip():
            raise AuthorityError("bad_policy", "adapter.reviews review_ref must name the review record")
        body["sums_path"] = os.path.normpath(sp)
    else:
        for k in ("allowed_models", "allowed_claude"):
            v = body[k]
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
                raise AuthorityError("bad_policy", "worker.defaults %s must be a non-empty list" % k)
            if k == "allowed_claude" and not all(os.path.isabs(x) for x in v):
                raise AuthorityError("bad_policy", "worker.defaults allowed_claude entries must be absolute paths")
            body[k] = sorted(set(v))
    return body


def ceiling(state: dict | None) -> dict:
    """P1c: the effective limit.raise ceiling = the newest active ``policy limit.ceiling``, else the initial
    $150 team policy. {usd: float | None (no ceiling), allow_disable, entry}."""
    pol = (state or {}).get("policies", {}).get("limit.ceiling")
    if not pol:
        return {"usd": INITIAL_MAX_USD, "allow_disable": False, "entry": None}
    v = pol["max_usd_per_day"]
    return {"usd": None if v == "none" else float(v), "allow_disable": bool(pol["allow_disable"]), "entry": pol["id"]}


def worker_defaults(state: dict | None) -> dict | None:
    """P1c (c): the company ``policy worker.defaults`` {allowed_models, allowed_claude, entry}, or None."""
    pol = (state or {}).get("policies", {}).get("worker.defaults")
    if not pol:
        return None
    return {"models": list(pol["allowed_models"]), "claude": list(pol["allowed_claude"]), "entry": pol["id"]}


def adapter_reviews(state: dict | None) -> list[dict]:
    """P1c-b: every active ``policy adapter.reviews`` entry {sums_path, sums_sha256, review_ref, id}; each entry
    accepts one frozen review's SHA256SUMS file (list semantics: retracting an entry removes only that one)."""
    return [dict(v) for v in sorted(((state or {}).get("policy_lists", {}).get("adapter.reviews") or {}).values(),
                                    key=lambda v: v["seq"])]


def owner_allow_lists(root, state: dict | None) -> list[dict]:
    """D15: the allow-lists that owner-installed member workers publish. A member = a registered, non-retired
    employee home directly under the company ``root`` (D8b); its runtime.state.json ``policy`` gives
    {allowed_models, allowed_claude}. A home whose own launch_ops ledger records a delegated worker ``install``
    is not owner-installed and never counts (otherwise a delegated install from worker.defaults would publish
    the defaults and vouch for itself). Read-only; nothing in another home is written."""
    from . import runtime
    from .sqlro import connect_ro
    import sqlite3
    out = []
    people = (state or {}).get("people", {})
    try:
        homes = sorted(p for p in Path(root).expanduser().resolve().iterdir() if p.is_dir())
    except OSError:
        return out
    for home in homes:
        if home.name == "company" or not (home / "state.sqlite3").is_file():
            continue
        try:
            tid = home_identity(home)
        except Exception:
            continue
        person = people.get(tid) if tid else None
        if not person or person.get("kind") != "employee" or person.get("retired"):
            continue
        pol = (runtime.read_state(home) or {}).get("policy") or {}
        models, claudes = pol.get("allowed_models"), pol.get("allowed_claude")
        if not models or not claudes:
            continue
        try:
            conn = connect_ro(home / "state.sqlite3")
            try:
                rows = conn.execute("SELECT data FROM launch_ops").fetchall()
            except sqlite3.OperationalError:
                rows = []  # a home on a pre-D14 schema has no ledger, hence no delegated install
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if any((json.loads(r[0]) or {}).get("kind") == "worker" and json.loads(r[0]).get("op") == "install"
               for r in rows):
            continue
        out.append({"home": str(home), "id": tid, "models": list(models),
                    "claude": [os.path.realpath(c) for c in claudes]})
    return out


def check_worker_defaults(root, state: dict | None, models, claudes) -> None:
    """D15: every model and every allowed_claude realpath must appear in the published lists of one
    owner-installed member worker. Raises AuthorityError(bad_policy) otherwise (checked at write and at use)."""
    lists = owner_allow_lists(root, state)
    want_c = {os.path.realpath(c) for c in claudes}
    want_m = set(models)
    for item in lists:
        if want_c <= set(item["claude"]) and want_m <= set(item["models"]):
            return
    raise AuthorityError("bad_policy", "worker.defaults %s / %s is not within the allow-lists any owner-installed "
                         "member worker publishes (runtime.state.json); an unreviewed executable or model stays "
                         "with the owner (D13, D15)" % (sorted(want_m), sorted(want_c)),
                         published=[{"id": i["id"], "models": i["models"], "claude": i["claude"]} for i in lists])


def clamped(state: dict | None) -> list[dict]:
    """Active limit.raise delegations whose max_usd_per_day exceeds the effective ceiling (clamped at use)."""
    cap = ceiling(state)["usd"]
    if cap is None:
        return []
    return [{"delegation": d["id"], "grantee": d["grantee"], "max_usd_per_day": d["scope"]["limit.raise"]["max_usd_per_day"],
             "ceiling": cap}
            for d in sorted((state or {}).get("delegations", {}).values(), key=lambda d: d["seq"])
            if "limit.raise" in d["actions"] and d["scope"]["limit.raise"]["max_usd_per_day"] != POLICY_USD
            and d["scope"]["limit.raise"]["max_usd_per_day"] > cap]


def scope_usd(v, state: dict | None) -> float | None:
    """D18: the daily amount a limit.raise ``max_usd_per_day`` allows now. ``"policy"`` → the effective
    ``limit.ceiling`` (None = no bound when the ceiling is "none"); a number → min(number, ceiling)."""
    cap = ceiling(state)["usd"]
    if v == POLICY_USD:
        return cap
    return float(v) if cap is None else min(float(v), cap)


def limit_bound(state: dict | None, d: dict) -> float | None:
    """D18: the resolved bound of delegation ``d`` for limit.raise; a child is also held to its parent's
    resolved bound (defence in depth beside the write-time exceeds_parent check). None = no daily bound."""
    bounds = [scope_usd(d["scope"]["limit.raise"]["max_usd_per_day"], state)]
    parent = (state or {}).get("delegations", {}).get(d.get("parent")) if d.get("parent") else None
    if parent and "limit.raise" in parent.get("actions", []):
        bounds.append(scope_usd(parent["scope"]["limit.raise"]["max_usd_per_day"], state))
    bounds = [b for b in bounds if b is not None]
    return min(bounds) if bounds else None


def parse_scope(actions, scope, max_usd=MAX_USD) -> dict:
    if not isinstance(actions, list) or not actions or len(set(actions)) != len(actions):
        raise AuthorityError("bad_scope", "actions must be a non-empty list without duplicates")
    for a in actions:
        if a not in CATALOGUE:
            raise AuthorityError("bad_scope", "unknown action %r (catalogue: %s)" % (a, ", ".join(CATALOGUE)))
    scope = scope or {}
    if not isinstance(scope, dict) or set(scope) - set(actions):
        raise AuthorityError("bad_scope", "scope keys must be a subset of actions")
    out = {}
    for a in actions:
        spec, given = CATALOGUE[a], scope.get(a) or {}
        if not isinstance(given, dict) or set(given) - set(spec):
            raise AuthorityError("bad_scope", "%s scope takes %s" % (a, sorted(spec) or "nothing"))
        missing = set(spec) - set(given)
        if missing:
            raise AuthorityError("bad_scope", "%s scope needs %s" % (a, sorted(missing)))
        for k, kind in spec.items():
            v = given[k]
            if kind == "paths":
                if isinstance(v, str):
                    v = [v]
                if not isinstance(v, list) or not v or not all(
                        isinstance(x, str) and (x == "*" or os.path.isabs(x)) for x in v):
                    raise AuthorityError("bad_scope", "%s.%s must be a non-empty list of absolute paths" % (a, k))
                v = sorted({x if x == "*" else os.path.normpath(x) for x in v})
            elif kind in ("list", "homes", "durations", "labels"):
                if isinstance(v, str):
                    v = [v]
                if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                    raise AuthorityError("bad_scope", "%s.%s must be a non-empty list of names" % (a, k))
                if kind == "labels":
                    bad = [x for x in v if x != "*" and "*" in (x[:-2] if x.endswith(".*") else x)
                           or x.endswith(".*") and len(x) < 3]
                    if bad:
                        raise AuthorityError("bad_scope", "%s.%s entries are exact labels, \"*\", or a prefix "
                                             "pattern ending in \".*\" (D19); not %s" % (a, k, bad))
                if kind == "durations" and set(v) - set(DURATIONS):
                    raise AuthorityError("bad_scope", "%s.%s ⊆ %s" % (a, k, DURATIONS))
                if (a, k) == ("worker.manage", "ops") and set(v) - set(WORKER_OPS_SCOPE):
                    raise AuthorityError("bad_scope", "worker.manage.ops ⊆ %s (D9a: `worker` subcommand names)"
                                         % (WORKER_OPS_SCOPE,))
                v = sorted(set(v))
            elif kind == "mode" and v not in MODES or kind == "company_mode" and v not in COMPANY_MODES:
                raise AuthorityError("bad_scope", "%s.%s must be one of %s" % (a, k, MODES if kind == "mode" else COMPANY_MODES))
            elif kind == "usd":
                if v == POLICY_USD:  # D18: bounded only by the effective limit.ceiling, resolved at use
                    pass
                elif not _finite_number(v) or not 0 < v or (  # D18-r2: NaN/inf refused
                        max_usd is not None and v > max_usd):
                    raise AuthorityError("bad_scope", "%s.%s must be \"policy\" or a finite number in (0, %s] "
                                         "(limit.ceiling)"
                                         % (a, k, "no ceiling" if max_usd is None else max_usd))
            out.setdefault(a, {})[k] = v
        out.setdefault(a, {})
    return out


def _within(kind, child, parent) -> bool:
    # D6c: semantic attenuation. "*" is never allowed in a child; "employees" covers a named member but a named
    # member never covers "employees" (plain set inclusion gives both once "*" is excluded).
    if kind == "paths":  # a child path must equal or sit under some parent path
        if "*" in child:
            return False
        return "*" in parent or all(path_under(c, parent) for c in child)
    if kind == "labels":
        # D19: each child entry must be covered by the parent: an exact label by label_matches, a prefix
        # pattern only by "*", the same pattern or a parent pattern whose prefix it extends. Never "*".
        if "*" in child:
            return False
        return all(pattern_within(c, parent) for c in child)
    if kind in ("list", "homes", "durations"):
        if "*" in child:
            return False
        if kind == "homes" and "employees" in parent:
            return True
        return "*" in parent or set(child) <= set(parent)
    if kind == "mode":
        return MODES.index(child) <= MODES.index(parent)
    if kind == "company_mode":
        return COMPANY_MODES.index(child) <= COMPANY_MODES.index(parent)
    if kind == "usd":
        # D18 (Q1): a "policy" child under a numeric parent is refused (a raised ceiling would let it pass
        # its parent); any number or "policy" fits under a "policy" parent (the ceiling binds both at use).
        if parent == POLICY_USD:
            return True
        if child == POLICY_USD:
            return False
        return child <= parent
    return False


def label_matches(label, entries) -> bool:
    """D19: True when the launchd ``label`` is covered by a labels scope: "*", the exact label, or a prefix
    pattern "a.b.*" on a dot boundary ("com.synthetics.adapter.*" covers com.synthetics.adapter.X, never
    com.synthetics.adapterX nor com.synthetics.adapter. itself)."""
    if not isinstance(label, str) or not label:
        return False
    for e in entries or []:
        if e == "*" or e == label:
            return True
        if isinstance(e, str) and e.endswith(".*") and len(e) > 2:
            prefix = e[:-1]  # keeps the trailing dot
            if label.startswith(prefix) and len(label) > len(prefix):
                return True
    return False


def pattern_within(entry, parent) -> bool:
    """D19 attenuation for one child labels entry: an exact label is within when the parent covers it; a
    pattern "p.*" only when the parent holds "*", the same pattern, or a pattern "q.*" with p. starting q."""
    if entry == "*":
        return False
    if not entry.endswith(".*"):
        return label_matches(entry, parent)
    prefix = entry[:-1]
    return any(e == "*" or e == entry or (e.endswith(".*") and len(e) > 2 and prefix.startswith(e[:-1]))
               for e in parent or [])


def path_under(path, roots) -> bool:
    """True when ``path`` (resolved) equals or sits under one of ``roots`` (resolved); "*" covers anything."""
    if "*" in roots:
        return True
    try:
        p = Path(path).resolve()
        return any(p == Path(r).resolve() or Path(r).resolve() in p.parents for r in roots)
    except (OSError, RuntimeError):
        return False


def child_within(child_actions, child_scope, parent) -> str | None:
    """None when the child narrows the parent, else the reason (D1 exceeds_parent)."""
    if GRANT in child_actions:
        return "a child cannot hold %s (depth 1)" % GRANT
    extra = set(child_actions) - set(parent["actions"])
    if extra:
        return "actions not in parent: %s" % sorted(extra)
    for a in child_actions:
        for k, kind in CATALOGUE[a].items():
            if not _within(kind, child_scope[a][k], parent["scope"][a][k]):
                return "%s.%s %r exceeds parent %r" % (a, k, child_scope[a][k], parent["scope"][a][k])
    return None


# ---- root source validation (owner path) -----------------------------------------------------------------

def verify_sources(store, sources: list[dict]) -> list[dict]:
    """Each {event, excerpt} must be a message in ``store`` (the home holding David's words) with non-null
    auth, role human under that home's humans.json (secretary/operator/board refused), and contain the
    excerpt byte-for-byte. Returns [{event, excerpt, auth, home}] with auth copied from the event."""
    from . import capture
    if not sources:
        raise AuthorityError("no_source", "a root delegation cites at least one human event")
    cfg = capture.humans(store)
    out = []
    for s in sources:
        eid, excerpt = s.get("event"), s.get("excerpt")
        try:
            event = store.event_get(eid)
        except Exception:
            raise AuthorityError("unknown_source", "%s is not an event in %s" % (eid, store.home))
        eid = event["id"]
        if event.get("kind") != "message" or not event.get("auth"):
            raise AuthorityError("not_human_source", "%s is not an authenticated message (kind %s, auth %s)"
                                 % (eid, event.get("kind"), event.get("auth")))
        role = capture.role_of(event, "in", cfg)
        if role != "human":
            raise AuthorityError("not_human_source", "%s has role %s, not human" % (eid, role))
        if not isinstance(excerpt, str) or not excerpt.strip() or excerpt.encode() not in (event.get("body") or "").encode():
            raise AuthorityError("excerpt_mismatch", "the excerpt is not an exact substring of %s" % eid)
        out.append({"event": eid, "excerpt": excerpt, "auth": event["auth"], "sender": event.get("sender"),
                    "home": str(store.home)})
    return out


# ---- require ---------------------------------------------------------------------------------------------

def covering(state: dict, actor_id: str, action: str, check) -> dict | None:
    """First active delegation (root or child of an active root) for actor_id whose scope passes check."""
    dels = state.get("delegations", {})
    for d in sorted(dels.values(), key=lambda d: d["seq"]):
        if d["grantee"] != actor_id or action not in d["actions"]:
            continue
        root = d if not d.get("parent") else dels.get(d["parent"])
        if root is None or root.get("parent"):
            continue  # retracted (or invalid) ancestor invalidates the child
        if not root.get("sources") or not all(s.get("auth") for s in root["sources"]):
            continue
        sc = d["scope"].get(action, {})
        if action == "limit.raise" and "max_usd_per_day" in sc:
            # D18: check sees the resolved bound (None = no daily bound), never the raw "policy" string.
            sc = dict(sc, max_usd_per_day=limit_bound(state, d))
        if check(sc):
            return d
    return None


def require(action: str, *, home=None, target_home=None, check=lambda s: True, what: str = "",
            env=None, extra: dict | None = None) -> dict:
    """Authorize ``action``. Returns {actor, delegation}. Owner -> allowed (audited in target home if any).
    A session needs a covering delegation; every allow/refusal is audited in the actor home (and in the target
    home when the action writes it). ``check(scope_for_action)`` tests the target and params."""
    from . import roster
    a = actor(env)
    row = {"action": action, "what": what or action, **(extra or {})}
    if a["owner"]:
        audit(target_home, "authority.owner", row)
        return {"actor": a, "delegation": None, "warrant": None}
    row.update(actor=a["id"], run=a["run"])
    state = roster.company_state_for_home(home or a["home"])
    d = covering(state, a["id"], action, check)
    if d is None:
        audit(a["home"], "authority.refused", dict(row, code="not_delegated"))
        raise AuthorityError("not_delegated", "%s holds no delegation covering %s %s" % (a["id"], action, what),
                             action=action, actor=a["id"])
    row["delegation"] = d["id"]
    audit(a["home"], "authority.used", row)
    from . import roster as _r
    chain = [d["id"]] + ([d["parent"]] if d.get("parent") else [])
    warrant = {"actor": a["id"], "run": a["run"], "home": a["home"], "store_path": a["store_path"],
               "company_path": str(_r.company_path(_r.root_of_home(home or a["home"]))),
               "delegation": d["id"], "chain": chain, "action": action, "what": what or action}
    return {"actor": a, "delegation": d["id"], "warrant": warrant}


def member_home(target_home, actor_home) -> bool:
    """D6b: a member home sits directly under the actor's own company root, checked after resolve() (so a symlink
    pointing outside the root, another company's home or a stray temp home is not a member)."""
    try:
        root = Path(actor_home).expanduser().resolve().parent
        target = Path(target_home).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if not (target.parent == root and (target / "state.sqlite3").is_file() and target.name != "company"):
        return False
    # D8b: the home's identity must be a registered, non-retired person of kind employee in the actor's
    # company state. A fresh same-root home, an unknown ID or a retired person is not a member.
    from . import roster
    tid = home_identity(target)
    person = roster.company_state_for_home(actor_home).get("people", {}).get(tid) if tid else None
    return bool(person) and person.get("kind") == "employee" and not person.get("retired")


def homes_cover(scope_homes, target_home, actor_home=None) -> bool:
    """Scope ``homes`` covers ``target_home``: always contained in the actor's company root (D6b); then "*",
    "employees" (any member) or the member's identity ID."""
    if actor_home is not None and not member_home(target_home, actor_home):
        return False
    if actor_home is None and "*" not in scope_homes:
        return False  # a session check always names its actor home; without it nothing is a member
    if "*" in scope_homes:
        return True
    tid = home_identity(target_home)
    return bool(tid) and (tid in scope_homes or "employees" in scope_homes)


# ---- D4: transactional re-check and target receipts --------------------------------------------------------

def _actor_schema(db, warrant) -> str:
    """"main" when the connection's main DB is the actor's own Store (self-target), else "w_actor"."""
    for _, name, path in db.execute("PRAGMA database_list").fetchall():
        if name == "main" and path and same_home(Path(path).parent, Path(warrant["store_path"]).parent) \
                and Path(path).name == Path(warrant["store_path"]).name:
            return "main"
    return "w_actor"


def attach(db, warrant, *, company_is_main=False) -> None:
    """ATTACH the actor's Store (and the company log unless it is the target DB) before BEGIN IMMEDIATE, so the
    re-check and the target write share one transaction (r6 5.1 fence pattern)."""
    if warrant is None:
        return
    if _actor_schema(db, warrant) != "main":  # P1b self-target (D7): the actor's Store is the target DB itself
        db.execute("ATTACH DATABASE ? AS w_actor", (warrant["store_path"],))
    if not company_is_main:
        db.execute("ATTACH DATABASE ? AS w_co", (warrant["company_path"],))


def detach(db, warrant, *, company_is_main=False) -> None:
    if warrant is None:
        return
    if "w_actor" in {r[1] for r in db.execute("PRAGMA database_list")}:
        db.execute("DETACH DATABASE w_actor")
    if not company_is_main:
        db.execute("DETACH DATABASE w_co")


def recheck(db, warrant, *, company_is_main=False) -> None:
    """Inside the target write transaction: the actor's run is still running and no delegation in the chain has
    been retracted. Raises AuthorityError (the caller rolls back; ``fenced()`` audits the refusal)."""
    if warrant is None:
        return
    schema = "w_actor" if "w_actor" in {r[1] for r in db.execute("PRAGMA database_list")} else _actor_schema(db, warrant)
    row = db.execute("SELECT data FROM %s.runs WHERE id=?" % schema, (warrant["run"],)).fetchone()
    if row is None or json.loads(row[0]).get("status") != "running":
        raise AuthorityError("run_inactive", "run %s finished before the write" % warrant["run"], run=warrant["run"])
    co = "main" if company_is_main else "w_co"
    # Issue #2: one rule with covering(): a chain delegation is in effect iff the company fold keeps it active
    # (a retract that is itself retracted no longer counts; a child whose root is gone is gone).
    from .roster import company_fold
    rows = db.execute("SELECT seq,id,batch,kind,author,run,at,body FROM %s.entries ORDER BY seq" % co).fetchall()
    active = company_fold([{"seq": r[0], "id": r[1], "batch": r[2], "kind": r[3], "author": r[4], "run": r[5],
                            "at": r[6], "body": json.loads(r[7])} for r in rows])["delegations"]
    for target in warrant["chain"]:
        if target not in active:
            raise AuthorityError("delegation_retracted", "delegation %s was retracted before the write" % target,
                                 delegation=target)


def receipt(warrant, key=None) -> dict:
    """The target-side receipt, keyed for reconciliation: actor, run, delegation, action, idempotency key."""
    return {"actor": warrant["actor"], "run": warrant["run"], "delegation": warrant["delegation"],
            "chain": list(warrant["chain"]), "action": warrant["action"], "key": key}


def fenced(warrant, fn):
    """Run the target write ``fn()``; a D4 refusal from inside its transaction is audited in the actor home after
    the rollback (never while the target transaction still holds the attached actor DB)."""
    try:
        return fn()
    except AuthorityError as error:
        if warrant is not None and error.code in ("run_inactive", "delegation_retracted"):
            audit(warrant["home"], "authority.refused", {"action": warrant["action"], "what": warrant["what"],
                                                         "actor": warrant["actor"], "run": warrant["run"],
                                                         "delegation": warrant["delegation"], "code": error.code,
                                                         "stage": "write"})
        raise


def fenced_file(warrant, fn):
    """D4 for file effects (usage_limit.json): hold a write lock on the actor's Store and the company log
    (BEGIN IMMEDIATE over both ATTACHed), re-check run + chain, run ``fn()`` and commit. The actor's run cannot
    finish, and no delegation can be retracted, between the re-check and the file write."""
    if warrant is None:
        return fn()
    import sqlite3

    def body():
        db = sqlite3.connect(":memory:", isolation_level=None, timeout=30)
        try:
            attach(db, warrant)
            db.execute("BEGIN IMMEDIATE")
            try:
                recheck(db, warrant)
                result = fn()
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            return result
        finally:
            db.close()
    return fenced(warrant, body)


def same_home(a, b) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def self_raises(home, limit=200) -> list[dict]:
    """D7: every limit.raise a session made on its own home (audited in that home), newest first."""
    from .store import Store
    try:
        with Store(home) as s:
            rows = s.audit_list(limit)
    except Exception:
        return []
    out = []
    for r in rows:
        data = r.get("data") or {}
        if r.get("action") == "authority.used" and data.get("action") == "limit.raise" and data.get("self_raise"):
            out.append({"at": r.get("at") or r.get("created_at"), "run": data.get("run"),
                        "usd": data.get("usd"), "duration": data.get("duration"),
                        "delegation": data.get("delegation")})
    return out


# ---- CLI: `synthetics authority …` -----------------------------------------------------------------------

def add_parser(sub):
    p = sub.add_parser("authority", help="M28 delegated authority: delegate, retract, policy, show, check, ops, settle")
    s = p.add_subparsers(dest="authority_command", required=True)
    d = s.add_parser("delegate", help="record a delegation: a root (owner only, cites human events in --home) or "
                                      "a child (--parent; from a session holding delegation.grant)")
    d.add_argument("--grantee", required=True)
    d.add_argument("--action", action="append", required=True, dest="actions")
    d.add_argument("--scope", default="{}", help="JSON {action: {key: value}}; see docs/authority.md")
    d.add_argument("--source", action="append", default=[], dest="sources", help="root: event ID in --home")
    d.add_argument("--excerpt", action="append", default=[], dest="excerpts",
                   help="root: exact text of the matching --source event (same order)")
    d.add_argument("--context-ref", action="append", default=[], dest="context_refs",
                   help="root: related event IDs kept as context only; never checked as a source (D5)")
    d.add_argument("--parent", help="child: the root delegation it narrows")
    d.add_argument("--note")
    d.add_argument("--by", help="owner: display label (author operator:<by>); session: must name the actor")
    d.add_argument("--key")
    d.add_argument("--dry-run", action="store_true", help="validate and print the entry without writing")
    r = s.add_parser("retract", help="retract a delegation (owner: any; session: a child it wrote)")
    r.add_argument("id")
    r.add_argument("--reason", required=True)
    r.add_argument("--by")
    po = s.add_parser("policy", help="P1c: write a company-log team policy entry (limit.ceiling, worker.defaults, "
                                     "adapter.reviews); "
                                     "a session needs the matching action and the company write grant")
    po.add_argument("name", choices=sorted(POLICIES))
    po.add_argument("--body", required=True, help='JSON, e.g. {"max_usd_per_day": 200, "allow_disable": false}')
    po.add_argument("--reason", required=True)
    po.add_argument("--context-ref")
    po.add_argument("--by")
    po.add_argument("--key")
    sh = s.add_parser("show", help="active delegations")
    sh.add_argument("--grantee")
    o = s.add_parser("ops", help="D14: delegated launchd ops in this home's ledger (default: pending only)")
    o.add_argument("--all", action="store_true")
    st = s.add_parser("settle", help="D14: settle a pending launchd op from evidence (plist, launchd state, new PID); "
                                     "executes nothing; a session needs the op's action over its target")
    st.add_argument("op_id")
    c = s.add_parser("check", help="exit 0 if the caller may perform ACTION here (audited), else refuse")
    c.add_argument("action", choices=sorted(CATALOGUE))
    c.add_argument("--project")
    c.add_argument("--mode")
    c.add_argument("--target-home")
    c.add_argument("--item", help="list-scoped item to check (kit path, label, op)")


def _company_for(store):
    from . import roster
    path = roster.company_path(roster.root_of_home(store.home))
    path.parent.mkdir(parents=True, exist_ok=True)
    return roster.CompanyStore(path)


def command(store, args):
    from . import roster
    cmd = args.authority_command
    if cmd == "show":
        st = roster.company_state_for_home(store.home)
        out = []
        for d in sorted(st.get("delegations", {}).values(), key=lambda d: d["seq"]):
            if args.grantee and roster.resolve(st, args.grantee)["id"] != d["grantee"]:
                continue
            out.append(d)
        return out
    if cmd == "ops":
        return store.launch_ops(pending_only=not args.all)
    if cmd == "settle":
        from . import install
        try:
            record = store.launch_op(args.op_id)
        except ValueError:
            raise AuthorityError("unknown_op", "no launchd op %s in this home" % args.op_id)
        if record.get("status") != "pending":
            raise AuthorityError("already_settled", "%s is already settled (%s)" % (args.op_id, record.get("outcome")))
        a = actor()
        warrant = None
        if not a["owner"]:  # the same authority that could have run the op may settle it
            kind = record.get("kind")
            # D17a: settle under the action the row records (validated), not a hard-coded one
            action = (record.get("action") or "adapter.manage") if kind == "adapter" else "worker.manage"
            if action not in CATALOGUE or (kind == "adapter") != action.startswith("adapter."):
                raise AuthorityError("bad_record", "%s records unknown action %r" % (args.op_id, action))
            item = record.get("label") if kind == "adapter" else record.get("op")
            if kind == "adapter" and not same_home(store.home, a["home"]):
                # adapter ops are ledgered in the acting session's own home; a session settles only its own
                audit(a["home"], "authority.refused", {"action": action, "what": "settle %s" % args.op_id,
                                                       "actor": a["id"], "run": a["run"], "code": "not_own_ledger"})
                raise AuthorityError("not_own_ledger", "a session settles adapter ops only in its own home",
                                     action=action)

            def covers(scope):
                if action == "adapter.install":
                    # D17a: same coverage as the install: label listed, cwd and every ledgered file under roots
                    roots = scope.get("roots") or []
                    return install.label_covered(item, scope.get("labels")) and bool(record.get("cwd")) \
                        and path_under(record["cwd"], roots) \
                        and all(path_under(f, roots) for f in record.get("files") or [])
                # D16a: a worker op is settled only by a delegation whose homes cover the op's target home
                if kind != "adapter" and not homes_cover(scope.get("homes", []), str(store.home), a["home"]):
                    return False
                if "labels" in scope and not label_matches(item, scope["labels"]):  # D19 patterns
                    return False
                if "ops" in scope and not ("*" in scope["ops"] or item in scope["ops"]
                                           or (item == "restart" and "kickstart" in scope["ops"])):
                    return False
                return True
            res = require(action, home=a["home"], target_home=None if kind == "adapter" else str(store.home),
                          check=covers, what="settle %s" % args.op_id)
            warrant = res["warrant"]
        judged = install.op_evidence(record)
        # D16a: the settle write re-checks run + delegation chain in its own transaction (refusal → row pending)
        settled = fenced(warrant, lambda: store.launch_op_settle(
            args.op_id, judged["outcome"], warrant=warrant, evidence=judged["evidence"], reconciled=True,
            reconciled_by=a.get("id") or "owner", reconciled_run=a.get("run")))
        audit(str(store.home) if a["owner"] else a["home"], "authority.settle",
              {"what": "settle", "op": args.op_id, "outcome": judged["outcome"], "actor": a.get("id") or "owner",
               "run": a.get("run")})
        return settled
    if cmd == "check":
        def ok(scope):
            if args.project and args.project not in scope.get("projects", [args.project]) and "*" not in scope.get("projects", []):
                return False
            if args.mode and "max_mode" in scope:
                modes = MODES if args.action != "company.grant" else COMPANY_MODES
                if args.mode not in modes or modes.index(args.mode) > modes.index(scope["max_mode"]):
                    return False
            if args.target_home and "homes" in scope and not homes_cover(scope["homes"], args.target_home,
                                                                         actor().get("home")):
                return False
            if args.item:
                if "kits" in scope and not path_under(args.item, scope["kits"]):
                    return False
                if "labels" in scope and not label_matches(args.item, scope["labels"]):  # D19 patterns
                    return False
                if "ops" in scope and not ("*" in scope["ops"] or args.item in scope["ops"]):
                    return False
            return True
        if args.action == "kit.run":
            # T10: a kit guard names the kit file it is; the file must exist (no check of a path not yet written).
            if not args.item or not Path(args.item).is_file():
                a = actor()
                if not a["owner"]:
                    audit(a["home"], "authority.refused", {"action": "kit.run", "what": "check %s" % args.item,
                                                           "actor": a["id"], "run": a["run"], "code": "bad_item"})
                raise AuthorityError("bad_item", "kit.run check needs --item <existing kit file>", action="kit.run")
        res = require(args.action, home=store.home, target_home=args.target_home, check=ok,
                      what="check " + " ".join(str(v) for v in (args.project, args.mode, args.target_home, args.item) if v))
        return {"allowed": True, "actor": res["actor"]["id"], "delegation": res["delegation"]}
    a = actor()
    with _company_for(store) as cs:
        state = cs.state()
        by = check_by(a, getattr(args, "by", None), state)
        if cmd == "policy":
            body = dict(strict_json(args.body), name=args.name, reason=args.reason)
            if args.context_ref:
                body["context_ref"] = args.context_ref
            parse_policy(body)
            if not a["owner"]:  # P1c: audited use of the action that owns this policy (fence: cs.append below)
                # PUB-1 G3: host rules reach every home's sessions, so the writer's runtime.request must cover all
                # member homes ("employees" or "*"), not only named ones.
                every = (lambda sc: bool({"employees", "*"} & set(sc.get("homes") or []))) \
                    if args.name == "host.rules" else (lambda sc: True)
                require(POLICIES[args.name], home=store.home, what="policy %s" % args.name, check=every)
            batch = [{"kind": "policy", "body": body}]
        elif cmd == "retract":
            batch = [{"kind": "retract", "body": {"target": args.id, "reason": args.reason}}]
        else:
            scope = strict_json(args.scope)
            body = {"grantee": args.grantee, "actions": args.actions, "scope": scope}
            if args.note:
                body["note"] = args.note
            if args.parent:
                if args.sources or args.excerpts or args.context_refs:
                    raise AuthorityError("bad_entry", "a child cites its parent, not --source events")
                body["parent"] = args.parent
            else:
                if not a["owner"]:
                    audit(a["home"], "authority.refused", {"what": "roster delegate (root)", "actor": a["id"],
                                                           "run": a["run"], "code": "root_requires_owner"})
                    raise AuthorityError("root_requires_owner", "a root delegation is written by the owner only "
                                         "(D1); record a child with --parent under a root you hold")
                if len(args.sources) != len(args.excerpts):
                    raise AuthorityError("bad_entry", "give one --excerpt per --source, in the same order")
                body["sources"] = verify_sources(store, [{"event": e, "excerpt": x}
                                                         for e, x in zip(args.sources, args.excerpts)])
                if args.context_refs:  # D5: context only; never satisfies the human + non-null-auth check
                    body["context_refs"] = list(args.context_refs)
            batch = [{"kind": "delegation", "body": body}]
        if getattr(args, "dry_run", False):
            # Validate against a throwaway copy of the fold: same checks as append, nothing written.
            cs._validate(cs.entries(), batch, author=("operator:" + by) if a["owner"] else a["id"],
                         operator=a["owner"])
            return {"dry_run": True, "entry": batch[0]}
        if a["owner"]:
            out = cs.append(batch, author="operator:" + by, operator=True, key=getattr(args, "key", None))
        else:
            out = cs.append(batch, author=a["id"], run=a["run"], fence=a["store_path"], key=getattr(args, "key", None))
            audit(a["home"], "authority.policy" if cmd == "policy" else "authority.delegation." + cmd,
                  {"what": cmd, "actor": a["id"], "run": a["run"], "entry": out["ids"][0],
                   **({"name": args.name} if cmd == "policy" else {})})
        cs.render()
        return out

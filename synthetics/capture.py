"""M21 step 4: project bindings and ingest capture in the receiving employee Store (schema 4).

Bindings (r7b B1): {project, thread|null, source_prefixes[]}; match = thread equal (if set) AND source starts with
any prefix (if any). Prospective by receipt seq: an event is bound iff active_from_seq < seq and (not ended or
seq <= ended_seq). Feed writes a capture_outbox row in the same transaction as the event; capture_drain appends the
source entry (parts if > 65536 B) to the project log with key capture:<home>:<event_id>. Never upgrades auth.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .projects import QUOTE_MAX, ProjectStore, utf8_cut
from .sqlro import connect_ro

SCHEMA = """
CREATE TABLE IF NOT EXISTS project_bindings (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, thread TEXT, source_prefixes TEXT NOT NULL,
  active_from_seq INTEGER NOT NULL, active_from_at TEXT NOT NULL, ended TEXT,
  created_by TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture_outbox (
  event_id TEXT NOT NULL, project TEXT NOT NULL, mode TEXT NOT NULL, binding_id TEXT, direction TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL, error TEXT, entry_ids TEXT, created_at TEXT NOT NULL,
  last_attempt TEXT, PRIMARY KEY(event_id, project));
CREATE TABLE IF NOT EXISTS coverage_marks (
  event_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, reason TEXT NOT NULL, project TEXT, ids TEXT,
  at TEXT NOT NULL, run TEXT);
CREATE TABLE IF NOT EXISTS capture_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
INSERT OR IGNORE INTO capture_meta(k, v) SELECT 'uncovered_from_seq', CAST(COALESCE(MAX(seq), 0) AS TEXT) FROM events;
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def home_id(store) -> str:
    try:
        return store.identity()["id"]
    except Exception:  # noqa: BLE001 - homes without an identity (tests, fresh homes)
        return Path(store.home).name


def projects_root(store) -> Path:
    return Path(store.home).parent / "projects"


def project_path(store, project: str) -> Path:
    if project in RESERVED_PROJECTS:
        raise ValueError("%s is a reserved name, not a project." % project)
    return projects_root(store) / project / "memory.sqlite3"


def _binding(row) -> dict:
    return {"id": row[0], "project": row[1], "thread": row[2], "source_prefixes": json.loads(row[3]),
            "active_from": {"seq": row[4], "at": row[5]}, "ended": json.loads(row[6]) if row[6] else None,
            "created_by": row[7], "reason": row[8]}


_COLS = "id,project,thread,source_prefixes,active_from_seq,active_from_at,ended,created_by,reason"


def bindings(store, include_ended: bool = False) -> list[dict]:
    rows = store.db.execute("SELECT %s FROM project_bindings ORDER BY active_from_seq, id" % _COLS).fetchall()
    out = [_binding(tuple(r)) for r in rows]
    return out if include_ended else [b for b in out if b["ended"] is None]


def matches(b: dict, event: dict, seq: int) -> bool:
    if seq <= b["active_from"]["seq"]:
        return False
    if b["ended"] is not None and seq > b["ended"]["seq"]:
        return False
    if b["thread"] is not None and event.get("thread") != b["thread"]:
        return False
    prefixes = b["source_prefixes"]
    return not prefixes or any((event.get("source") or "").startswith(p) for p in prefixes)


# Who counts as a human is installation configuration, not code (M21 phase 2; M25 §4 + C1).
# <home>/humans.json v1:
#   {"version": 1, "default_human": "<id>" | null,
#    "senders": [...], "channel_prefixes": [...], "secretary_prefixes": [...],
#    "operators": {"senders": [...], "source_prefixes": [...]},
#    "limit_bypass": {"senders": [...], "channel_prefixes": [...], "require_auth": true}}
# The phase-2 shape (no "version") still loads; missing keys default to empty. A home WITHOUT the file has no
# humans at all (M25 removed the phase-2 LEGACY fallback): nobody is "human", nothing bypasses a limit, and CLI
# commands that used to default to a literal human ID require an explicit one. `doctor` warns humans_missing.
HUMAN_KEYS = ("senders", "channel_prefixes", "secretary_prefixes")
HUMANS_VERSION = 1


def empty_humans() -> dict:
    return {"version": HUMANS_VERSION, "default_human": None, "senders": [], "channel_prefixes": [],
            "secretary_prefixes": [], "operators": {"senders": [], "source_prefixes": []},
            "limit_bypass": {"senders": [], "channel_prefixes": [], "require_auth": True}, "present": False}


def _home_of(store_or_home):
    """A Store's home, or the path itself. (Path has a .home classmethod, so never getattr a path.)"""
    if isinstance(store_or_home, (str, os.PathLike)):
        return store_or_home
    return getattr(store_or_home, "home", store_or_home)


def humans_path(store_or_home) -> Path:
    return Path(_home_of(store_or_home)) / "humans.json"


def _str_list(path, data, key, label=None):
    v = data.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ValueError("%s: %s must be a list of non-empty strings" % (path, label or key))
    return list(v)


def parse_humans(data, path="humans.json") -> dict:
    """Validate a humans.json object (v1 or the phase-2 shape). Raises ValueError; never guesses."""
    if not isinstance(data, dict):
        raise ValueError("%s must be a JSON object" % path)
    version = data.get("version", HUMANS_VERSION)
    if version != HUMANS_VERSION:
        raise ValueError("%s: unsupported version %r (this code reads version 1)" % (path, version))
    out = empty_humans()
    out["present"] = True
    for k in HUMAN_KEYS:
        out[k] = _str_list(path, data, k)
    dh = data.get("default_human")
    if dh is not None and (not isinstance(dh, str) or not dh):
        raise ValueError("%s: default_human must be a non-empty string or null" % path)
    out["default_human"] = dh
    ops = data.get("operators", {})
    if not isinstance(ops, dict):
        raise ValueError("%s: operators must be an object" % path)
    out["operators"] = {"senders": _str_list(path, ops, "senders", "operators.senders"),
                        "source_prefixes": _str_list(path, ops, "source_prefixes", "operators.source_prefixes")}
    lb = data.get("limit_bypass", {})
    if not isinstance(lb, dict):
        raise ValueError("%s: limit_bypass must be an object" % path)
    if lb.get("require_auth", True) is not True:
        raise ValueError("%s: limit_bypass.require_auth must be true in version 1" % path)
    out["limit_bypass"] = {"senders": _str_list(path, lb, "senders", "limit_bypass.senders"),
                           "channel_prefixes": _str_list(path, lb, "channel_prefixes",
                                                         "limit_bypass.channel_prefixes"),
                           "require_auth": True}
    overlap = set(out["senders"]) & set(out["operators"]["senders"])
    if overlap:
        raise ValueError("%s: %s cannot be both a human and an operator" % (path, ", ".join(sorted(overlap))))
    return out


def humans(store) -> dict:
    """Configured humans for this home. Missing file: no humans. A malformed file fails loudly."""
    home = _home_of(store) if store is not None else None
    if not home:
        return empty_humans()
    path = humans_path(home)
    if not path.exists():
        return empty_humans()
    return parse_humans(json.loads(path.read_text(encoding="utf-8")), path)


def humans_document(cfg: dict) -> dict:
    """The on-disk v1 form of a parsed config (drops the in-memory 'present' flag)."""
    return {k: cfg[k] for k in ("version", "default_human", "senders", "channel_prefixes", "secretary_prefixes",
                                "operators", "limit_bypass")}


def write_humans(home, cfg: dict) -> Path:
    """Write humans.json atomically (0644). Validates first."""
    doc = humans_document(parse_humans(humans_document(dict(empty_humans(), **cfg))))
    path = humans_path(home)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(path)
    return path


def default_human(store_or_home) -> str | None:
    return humans(store_or_home)["default_human"]


def is_operator(event: dict, cfg: dict) -> bool:
    ops = cfg.get("operators") or {}
    return (event.get("sender") in ops.get("senders", ())
            or (event.get("source") or "").startswith(tuple(ops.get("source_prefixes", ()))))


BOARD_SOURCE = "board:"


def is_board(event: dict) -> bool:
    """M27 (iv): local wake events raised by a project board post (source board:<project>)."""
    return (event.get("source") or "").startswith(BOARD_SOURCE)


def role_of(event: dict, direction: str, cfg: dict | None = None) -> str:
    """N1: human iff the sender is a configured human sender (and not an operator); operators and secretary
    sources are 'secretary' (C1: never upgraded to human)."""
    cfg = cfg or empty_humans()
    if direction == "out":
        return "out"
    if is_board(event):
        return "other"  # M27 (iv) C2: a board event is never a human's or a secretary's words
    if (event.get("source") or "").startswith(tuple(cfg["secretary_prefixes"])) or is_operator(event, cfg):
        return "secretary"
    if event.get("sender") in cfg["senders"]:
        return "human"
    return "other"


def may_bypass_limit(event: dict, cfg: dict, limit_allow=()) -> bool:
    """M25 §4: replaces engine.LIMIT_BYPASS_SOURCES. Requires auth (M22 Q-C), a configured bypass channel and
    sender, and (intersection) the runtime's limit_allow."""
    if is_board(event):
        return False  # M27 (iv) N2: a board wake never bypasses the limit, whatever humans.json says
    lb = cfg.get("limit_bypass") or {}
    return bool(event.get("auth")
                and (event.get("source") or "").startswith(tuple(lb.get("channel_prefixes", ())))
                and event.get("sender") in lb.get("senders", ())
                and event.get("sender") in set(limit_allow or ())
                and not is_operator(event, cfg))


def _max_seq(store) -> int:
    return store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]


def bind(store, project: str, *, thread: str | None, source_prefixes: list[str], by: str, reason: str) -> dict:
    """Create a prospective binding (CLI; allowed inside a session per E2/B2, audited with --by)."""
    if project in RESERVED_PROJECTS:
        raise ValueError("%s is a reserved name, not a project." % project)
    prefixes = [p for p in (source_prefixes or []) if p]
    if not thread and not prefixes:
        raise ValueError("A binding needs --thread or at least one --source-prefix.")
    if not by or not reason:
        raise ValueError("A binding needs --by and --reason.")
    at = _now()
    with store._tx():
        b = {"id": "pb-" + uuid.uuid4().hex, "project": project, "thread": thread or None,
             "source_prefixes": prefixes, "active_from": {"seq": _max_seq(store), "at": at}, "ended": None,
             "created_by": by, "reason": reason}
        store.db.execute("INSERT INTO project_bindings(%s) VALUES (?,?,?,?,?,?,?,?,?)" % _COLS,
                         (b["id"], project, b["thread"], json.dumps(prefixes), b["active_from"]["seq"], at, None,
                          by, reason))
        store._audit("binding.created", b["id"], b)
    _log_binding(store, b, by)
    return b


def unbind(store, binding_id: str, *, by: str, reason: str) -> dict:
    if not by or not reason:
        raise ValueError("unbind needs --by and --reason.")
    with store._tx():
        row = store.db.execute("SELECT %s FROM project_bindings WHERE id=?" % _COLS, (binding_id,)).fetchone()
        if row is None:
            raise ValueError("No binding %s." % binding_id)
        b = _binding(tuple(row))
        if b["ended"] is not None:
            raise ValueError("Binding %s already ended." % binding_id)
        b["ended"] = {"seq": _max_seq(store), "at": _now(), "by": by, "reason": reason}
        store.db.execute("UPDATE project_bindings SET ended=? WHERE id=?", (json.dumps(b["ended"]), binding_id))
        store._audit("binding.ended", binding_id, b)
    _log_binding(store, b, by)
    return b


def _log_binding(store, b: dict, by: str) -> None:
    """Every P reader can see which channel feeds P and since when (r7a A1.2)."""
    with ProjectStore(project_path(store, b["project"]), b["project"]) as ps:
        ps.append([{"kind": "binding", "body": dict(b, binding_id=b["id"], home=home_id(store))}],
                  author="operator:" + by, operator=True,
                  key="binding:%s:%s" % (b["id"], "ended" if b["ended"] else "created"))


PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
# M26: `company` is the roster's company log, never a project.
RESERVED_PROJECTS = frozenset({"company"})


def is_project_name(raw) -> bool:
    return isinstance(raw, str) and bool(PROJECT_NAME.fullmatch(raw)) and raw not in RESERVED_PROJECTS
TAG_BY = ("feeder", "sender")


def sender_may_write(store, project: str, sender: str) -> bool:
    """Read-only grant check against P's own grants (D7.2); never creates the project file."""
    path = project_path(store, project)
    conn = connect_ro(path)
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='grants'").fetchone()
        if row is None or row[0] != "on":
            return True  # same rule as ProjectStore._check: grants not provisioned = not enforced
        g = conn.execute("SELECT mode FROM grants WHERE employee=?", (sender,)).fetchone()
        return g is not None and g[0] == "write"
    finally:
        conn.close()


def tag_ref(store, raw, by: str, sender: str, bound: list[str]) -> tuple[dict, bool]:
    """A1.3/D3.1 item 2: interpret a per-message project tag. Returns (project_ref, capture?)."""
    if not is_project_name(raw):
        return {"raw": str(raw)[:80], "valid": False, "effect": "none", "reason": "invalid"}, False
    ref = {"id": raw, "valid": True, "by": by}
    if bound:
        if raw in bound:
            return dict(ref, effect="binding", reason="bound-here"), False
        return dict(ref, effect="none", reason="bound-elsewhere"), False
    if not project_path(store, raw).exists():
        return dict(ref, effect="none", reason="unknown-project"), False
    if by == "sender" and not sender_may_write(store, raw, sender):
        return dict(ref, effect="none", reason="sender-not-granted"), False
    return dict(ref, effect="capture"), True


def _queue(store, event_id, project, mode, binding_id, direction):
    store.db.execute("INSERT OR IGNORE INTO capture_outbox(event_id,project,mode,binding_id,direction,status,"
                     "attempts,created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (event_id, project, mode, binding_id, direction, "pending", 0, _now()))


def enqueue(store, event: dict, direction: str = "in", tag: dict | None = None) -> list[str]:
    """Called inside the feed/_send transaction. Writes outbox rows for every matching active binding, then
    interprets a per-message tag {project, by} (bindings decide on a bound event). Sets event["project_ref"]
    when a tag was given; the caller persists it."""
    if is_board(event):
        return []  # the post already lives in the project log; never capture a copy back
    row = store.db.execute("SELECT seq FROM events WHERE id=?", (event["id"],)).fetchone()
    seq = row[0] if row else _max_seq(store) + 1
    projects = []
    for b in bindings(store, include_ended=True):
        if direction == "out":
            ok = b["ended"] is None and b["thread"] is not None and event.get("thread") == b["thread"]
        else:
            ok = matches(b, event, seq)
        if ok and b["project"] not in projects:
            _queue(store, event["id"], b["project"], "binding", b["id"], direction)
            projects.append(b["project"])
    if tag is not None:
        ref, take = tag_ref(store, tag.get("project"), tag.get("by", "feeder"), event.get("sender") or "", projects)
        event["project_ref"] = ref
        store._audit("event.project_ref", event["id"], ref)
        if take:
            _queue(store, event["id"], ref["id"], "tag", None, direction)
            projects.append(ref["id"])
    return projects


def split_parts(data: bytes, limit: int = QUOTE_MAX) -> list[bytes]:
    parts, i = [], 0
    while i < len(data) or not parts:
        end = i + utf8_cut(data[i:], limit) if len(data) - i > limit else len(data)
        parts.append(data[i:end])
        i = end
        if i >= len(data):
            break
    return parts


def _source_items(store, event: dict, row: dict) -> list[dict]:
    data = event["body"].encode("utf-8")
    parts = split_parts(data)
    origin = {"home": home_id(store), "event_id": event["id"], "source": event.get("source"),
              "external_id": event.get("external_id"), "thread": event.get("thread"),
              "sender": event.get("sender"), "occurred_at": event.get("occurred_at") or event.get("created_at"),
              "auth": event.get("auth"), "role": role_of(event, row["direction"], humans(store)),
              "direction": row["direction"]}
    items = []
    for i, part in enumerate(parts):
        body = {"origin": origin, "capture": {"mode": row["mode"], "binding_id": row["binding_id"]},
                "quote_source": "secondhand" if origin["role"] == "secretary" else "event"}
        if len(parts) > 1:
            body["part"] = {"i": i + 1, "n": len(parts)}
        items.append({"kind": "source", "body": body, "bytes": part.decode("utf-8")})
    return items


RETRY_BACKOFF = 60.0


def capture_drain(store, limit: int = 50, opener=None, backoff: float = 0.0) -> dict:
    """Append pending captures. Idempotent (key capture:<home>:<event_id>); failures stay pending and visible.

    backoff > 0 skips rows whose last failed attempt is more recent than that many seconds (engine idle step)."""
    opener = opener or (lambda p: ProjectStore(project_path(store, p), p))
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=backoff)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = store.db.execute("SELECT event_id,project,mode,binding_id,direction,attempts FROM capture_outbox "
                            "WHERE status='pending' AND (last_attempt IS NULL OR last_attempt <= ?) "
                            "ORDER BY created_at LIMIT ?", (cutoff, limit)).fetchall()
    done, failed = 0, 0
    for r in rows:
        row = dict(zip(("event_id", "project", "mode", "binding_id", "direction", "attempts"), tuple(r)))
        if row["direction"] == "out":
            event = dict(store._get("messages", row["event_id"]), source="outbox")
        else:
            event = store._get("events", row["event_id"])
        key = "capture:%s:%s" % (home_id(store), row["event_id"])
        author = "ingest:" + (event.get("source") or "")
        try:
            with opener(row["project"]) as ps:
                out = ps.append(_source_items(store, event, row), author=author, key=key, operator=True)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
            failed += 1
            err = "%s: %s" % (type(exc).__name__, exc)
            with store._tx():
                prev = store.db.execute("SELECT error FROM capture_outbox WHERE event_id=? AND project=?",
                                        (row["event_id"], row["project"])).fetchone()[0]
                store.db.execute("UPDATE capture_outbox SET attempts=attempts+1, error=?, last_attempt=? "
                                 "WHERE event_id=? AND project=?", (err, _now(), row["event_id"], row["project"]))
                if prev != err:
                    store._audit("capture.failed", row["event_id"], {"project": row["project"], "error": err[:200]})
            continue
        with store._tx():
            store.db.execute("UPDATE capture_outbox SET status='done', attempts=attempts+1, error=NULL, "
                             "entry_ids=?, last_attempt=? WHERE event_id=? AND project=?",
                             (json.dumps(out["ids"]), _now(), row["event_id"], row["project"]))
            store._audit("capture.done", row["event_id"], {"project": row["project"], "ids": out["ids"]})
        done += 1
    return {"done": done, "failed": failed}


def has_pending(store) -> bool:
    return store.db.execute("SELECT 1 FROM capture_outbox WHERE status='pending' LIMIT 1").fetchone() is not None


def binding_lines(store) -> list[str]:
    """E5: one line per active binding for the receiving employee's snapshot."""
    out = []
    for b in bindings(store):
        out.append("%s %s: thread=%s prefixes=%s active_from=seq %d (%s) by %s" % (
            b["id"], b["project"], b["thread"] or "*", ",".join(b["source_prefixes"]) or "*",
            b["active_from"]["seq"], b["active_from"]["at"], b["created_by"]))
    return out


def capture_status(store) -> dict:
    rows = store.db.execute("SELECT event_id,project,attempts,error,status FROM capture_outbox "
                            "WHERE status='pending' ORDER BY created_at").fetchall()
    return {"pending": len(rows),
            "failing": [{"event_id": r[0], "project": r[1], "attempts": r[2], "error": (r[3] or "")[:200]}
                        for r in rows if r[3]]}


# ---- step 5: promotion, private coverage marks, uncovered report (r7 D3.6, D4) ---------------------------



def _under(path: Path, roots) -> bool:
    for root in roots or ():
        try:
            path.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _private_roster(path: Path, store=None) -> bool:
    """M26 step 7: is ``path`` inside any home's roster/ directory (a ``roster`` dir next to state.sqlite3)?"""
    homes = []
    if store is not None and getattr(store, "home", None) is not None:
        homes.append(Path(store.home).resolve())
    for home in homes:
        try:
            path.relative_to(home / "roster")
            return True
        except ValueError:
            pass
    for parent in path.parents:
        if parent.name == "roster" and (parent.parent / "state.sqlite3").exists():
            return True
    return False


def promote(store, project: str, *, text: str, author: str, event_id: str | None = None, file: str | None = None,
            granted_roots=(), extra: list | None = None, considered: dict | None = None, run: str | None = None,
            fence=None, key: str | None = None, opener=None) -> dict:
    """D3.6: append a byte-exact excerpt of a private event (in this home) or a file under a granted root.

    The excerpt must be an exact byte substring of the origin. One project per call. Records a private
    coverage mark ``promoted`` for an event origin. ``extra`` entries (claims, considered) join the same batch."""
    from .projects import ProjectError
    if (event_id is None) == (file is None):
        raise ProjectError("bad_origin", "promotion names exactly one of event_id or file")
    data = text.encode("utf-8")
    if len(data) > QUOTE_MAX:
        raise ProjectError("quote_too_large", "%d bytes > %d" % (len(data), QUOTE_MAX), size=len(data),
                           limit=QUOTE_MAX)
    if not data:
        raise ProjectError("bad_source", "empty excerpt")
    if event_id is not None:
        event = store._find("events", event_id)
        if event is None:
            raise ProjectError("unknown_origin", "event %s is not in this employee's home" % event_id)
        whole = (event.get("body") or "").encode("utf-8")
        origin = {"home": home_id(store), "event_id": event_id, "source": event.get("source"),
                  "external_id": event.get("external_id"), "thread": event.get("thread"),
                  "sender": event.get("sender"), "occurred_at": event.get("occurred_at") or event.get("created_at"),
                  "auth": event.get("auth"), "role": role_of(event, "in", humans(store)), "direction": "in"}
        quote_source = "secondhand" if origin["role"] == "secretary" else "event"
    else:
        path = Path(file).resolve()
        if _private_roster(path, store):
            raise ProjectError("private_roster", "file %s is in a home's private roster (M26: views never "
                               "enter a project)" % file)
        if not _under(path, granted_roots):
            raise ProjectError("unknown_origin", "file %s is not under a granted root" % file)
        try:
            whole = path.read_bytes()
        except OSError as exc:
            raise ProjectError("unknown_origin", "cannot read %s: %s" % (file, exc))
        origin = {"file": str(path), "sha256_file": __import__("hashlib").sha256(whole).hexdigest(),
                  "auth": None, "role": "other", "direction": "in"}
        quote_source = "file"
    if data not in whole:
        raise ProjectError("not_substring", "excerpt is not an exact byte substring of its origin")
    item = {"kind": "source", "id": "pm-" + uuid.uuid4().hex, "bytes": text,
            "body": {"origin": origin, "capture": {"mode": "promotion", "by": author},
                     "quote_source": quote_source, "quote_checked_by": None}}
    batch = [item]
    if considered is not None:
        batch.append({"kind": "considered", "body": dict(considered, source=item["id"])})
    batch.extend(_resolve_source(extra or [], item["id"]))
    opener = opener or (lambda p: ProjectStore(project_path(store, p), p))
    with opener(project) as ps:
        out = ps.append(batch, author=author, run=run, fence=fence, key=key)
    if event_id is not None:
        _mark(store, event_id, "promoted", "promoted to %s" % project, project, out["ids"], run)
    return out


def _resolve_source(value, source_id):
    """Extra entries may name the new source as '$source' (its ID is chosen here)."""
    if value == "$source":
        return source_id
    if isinstance(value, dict):
        return {k: _resolve_source(v, source_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_source(v, source_id) for v in value]
    return value


def _mark(store, event_id, outcome, reason, project, ids, run):
    with store._tx():
        store.db.execute("INSERT INTO coverage_marks(event_id,outcome,reason,project,ids,at,run) VALUES "
                         "(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET outcome=excluded.outcome, "
                         "reason=excluded.reason, project=excluded.project, ids=excluded.ids, at=excluded.at, "
                         "run=excluded.run", (event_id, outcome, reason, project, json.dumps(ids or []), _now(), run))
        store._audit("coverage." + outcome, event_id, {"reason": reason, "project": project, "ids": ids or []})


def skip(store, event_id: str, reason: str, run: str | None = None) -> dict:
    """D4.2: private skip mark in the caller's own home only (fenced by store._tx when run_id is set)."""
    if not reason or not reason.strip():
        raise ValueError("A skip needs a reason.")
    if store._find("events", event_id) is None:
        raise ValueError("No event %s in this home." % event_id)
    _mark(store, event_id, "skipped", reason.strip()[:500], None, [], run)
    return {"event_id": event_id, "outcome": "skipped"}


def _is_human_channel(event: dict, cfg: dict | None = None) -> bool:
    cfg = cfg or empty_humans()
    if is_board(event):
        return False  # C2: board events never enter project_uncovered's private part
    return (event.get("sender") in cfg["senders"]
            or (event.get("source") or "").startswith(tuple(cfg["channel_prefixes"])))


def uncovered(store, employee: str, projects: list[str] | None = None, limit: int = 20, cursor: int = 0,
              opener=None) -> dict:
    """D4.3: per-reader report. Project part = sources with no considered entry in projects the caller may
    read; private part = human-channel messages in this home after uncovered_from_seq, not captured, unmarked."""
    limit = max(1, min(int(limit), 50))
    opener = opener or (lambda p: ProjectStore(project_path(store, p), p))
    root = projects_root(store)
    names = projects if projects is not None else sorted(
        d.name for d in root.iterdir()
        if (d / "memory.sqlite3").exists() and d.name not in RESERVED_PROJECTS) if root.exists() else []
    project_part = []
    for name in names:
        if name in RESERVED_PROJECTS or not project_path(store, name).exists():
            continue
        with opener(name) as ps:
            if not ps.may_read(employee):
                continue
            st = ps.state()
            for e in ps.entries():
                if e["kind"] == "source" and e["id"] in st["sources"] and not st["coverage"].get(e["id"]):
                    o = e["body"].get("origin", {})
                    project_part.append({"project": name, "id": e["id"], "seq": e["seq"], "role": o.get("role"),
                                         "event_id": o.get("event_id"), "at": o.get("occurred_at")})
    start = int(store.db.execute("SELECT v FROM capture_meta WHERE k='uncovered_from_seq'").fetchone()[0])
    captured = {r[0] for r in store.db.execute("SELECT event_id FROM capture_outbox")}
    marked = {r[0] for r in store.db.execute("SELECT event_id FROM coverage_marks")}
    cfg = humans(store)
    private = []
    after = max(start, int(cursor or 0))
    next_cursor = None
    for seq, data in store.db.execute("SELECT seq, data FROM events WHERE seq > ? ORDER BY seq", (after,)):
        ev = json.loads(data)
        if ev.get("kind") != "message" or not _is_human_channel(ev, cfg):
            continue
        if ev["id"] in captured or ev["id"] in marked:
            continue
        if len(private) >= limit:
            next_cursor = private[-1]["seq"]
            break
        private.append({"event_id": ev["id"], "seq": seq, "source": ev.get("source"), "at": ev.get("occurred_at")})
    return {"project": project_part[:limit], "project_total": len(project_part), "private": private,
            "next_cursor": next_cursor, "uncovered_from_seq": start}


def uncovered_counts(store, employee: str) -> dict:
    rep = uncovered(store, employee, limit=50)
    return {"project": rep["project_total"], "private": len(rep["private"]), "private_more": bool(rep["next_cursor"])}

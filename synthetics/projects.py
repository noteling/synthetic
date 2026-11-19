"""Project memory store (M21 phase 1, step 2): append-only log + materialised fold + JSONL export.

One SQLite file per project at ``<root>/<project>/memory.sqlite3``. Entries are never updated or deleted
(two exceptions, both operator redaction, which leaves a hash tombstone: (1) a source's bytes are emptied;
(2) backlog 27: a post's ``entries.body`` is rewritten in place to an empty placeholder that keeps
``reply_to``, and any ``keys.payload`` holding the post is replaced by its sha256). Redaction covers this
project DB and its log.jsonl only; copies already fed into homes as ``board:<p>`` events are not touched.
After every committed batch the full fold (``projectlog.fold``) is materialised into the ``state`` table in
the same transaction; test 9 asserts that a fresh re-fold equals it. Reads accept ``as_of`` (a log seq).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .projectlog import (BACKLOG_ORIGINS, BACKLOG_STATUSES, CLAIM_RELS, ENTRY_KINDS, FOLD_CODE, SOURCE_ROLES,
                         fold, split_ref)

# Backlog 60: name of the state row that records which fold code wrote the rest (never a fold key).
FOLD_STAMP = "__fold_code__"
QUOTE_MAX = 65536
EXCERPT_GET = 2048
EXCERPT_FLOOR = 512
CHUNK_MAX = 4096
GET_BUDGET = 8192
LESSON_TEXT_GET = 600
LESSON_NOTE_GET = 200
MAX_CLAIMS = 8
CLAIM_TEXT_MAX = 300
TITLE_MAX = 120
SCOPE_MAX = 300
MAX_RELATIONS = 16
AS_OF_CAP = 20000
OPERATOR_KINDS = ("redact", "binding")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, batch TEXT NOT NULL, kind TEXT NOT NULL,
  author TEXT NOT NULL, run TEXT, at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_bytes (id TEXT PRIMARY KEY, bytes BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS keys (key TEXT PRIMARY KEY, payload TEXT NOT NULL, ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants (
  employee TEXT PRIMARY KEY, mode TEXT NOT NULL, granted_by TEXT NOT NULL, at TEXT NOT NULL);
"""

GRANT_MODES = ("read", "post", "write")
GRANT_RANK = {"read": 0, "post": 1, "write": 2}
# M27 (iv): kinds a `post` grant may append; everything else needs write.
POST_KINDS = ("post", "backlog", "considered")
POST_TOPIC_MAX = 120
POST_BODY_MAX = 2048
POST_LIST_MAX = 8
BACKLOG_TITLE_MAX = 200
BACKLOG_WAITING_MAX = 200


class ProjectError(ValueError):
    """A rejected append or read. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utf8_cut(data: bytes, limit: int) -> int:
    """Largest n <= limit such that data[:n] ends on a UTF-8 character boundary."""
    n = min(limit, len(data))
    while 0 < n < len(data) and (data[n] & 0xC0) == 0x80:
        n -= 1
    return n


def is_boundary(data: bytes, offset: int) -> bool:
    return 0 <= offset <= len(data) and (offset == len(data) or (data[offset] & 0xC0) != 0x80)


class ProjectStore:
    # M26: subclasses (roster.CompanyStore) swap the fold and override _validate.
    _fold = staticmethod(fold)
    # Backlog 60: the code ID of ``_fold`` as loaded in this process. The materialised state carries it in a
    # FOLD_STAMP row of the ``state`` table; state() re-folds when the stamp differs or is missing.
    _fold_code = FOLD_CODE

    def __init__(self, path: str | os.PathLike, project: str):
        self.path = Path(path)
        self.project = project
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.export_path = self.path.parent / "log.jsonl"
        self.view_path = self.path.parent / "view.md"

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- log access -------------------------------------------------------------------------
    def entries(self, as_of: int | None = None) -> list[dict]:
        sql = "SELECT seq,id,batch,kind,author,run,at,body FROM entries"
        args: tuple = ()
        if as_of is not None:
            sql += " WHERE seq <= ?"
            args = (as_of,)
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [{"seq": r[0], "id": r[1], "batch": r[2], "kind": r[3], "author": r[4], "run": r[5],
                 "at": r[6], "body": json.loads(r[7])} for r in rows]

    def head_seq(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(seq),0) FROM entries").fetchone()[0]

    def source_bytes(self, entry_id: str) -> bytes:
        row = self.db.execute("SELECT bytes FROM source_bytes WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise ProjectError("not_found", f"no source entry {entry_id}")
        return bytes(row[0])

    def _materialised_rows(self) -> tuple[dict, str | None]:
        rows = self.db.execute("SELECT name,value FROM state").fetchall()
        st = {name: json.loads(value) for name, value in rows}
        return st, st.pop(FOLD_STAMP, None)

    def materialised(self) -> dict:
        return self._materialised_rows()[0]

    def fold_stamp(self) -> str | None:
        """Code ID of the fold that last materialised ``state``; None if written by code without stamps."""
        return self._materialised_rows()[1]

    def state(self, as_of: int | None = None) -> dict:
        head = self.head_seq()
        if as_of is None or as_of >= head:
            # Backlog 60: a long-lived process running older fold code (or code from before stamps, whose
            # DELETE FROM state also drops the stamp) may have appended last. Serve its cache only when it was
            # written by the fold this process runs; otherwise fold in memory. Reads never write the cache;
            # the next append from current code re-materialises it with a matching stamp.
            st, stamp = self._materialised_rows()
            if stamp == self._fold_code:
                return st
            return self._fold(self.entries())
        if as_of < 0:
            raise ProjectError("bad_as_of", "as_of must be >= 0")
        if as_of > AS_OF_CAP:
            raise ProjectError("too_large", f"as_of fold over {AS_OF_CAP} entries")
        return self._fold(self.entries(as_of))

    # ---- append -----------------------------------------------------------------------------
    def append(self, batch: list[dict], *, author: str, run: str | None = None, key: str | None = None,
               operator: bool = False, expected_seq: int | None = None, guard=None,
               fence: str | os.PathLike | None = None, warrant: dict | None = None) -> dict:
        """Append ``batch`` atomically. Each item: {kind, body, bytes?}. Returns {batch, ids, seq_from, seq_to}.

        ``fence`` = path of the caller's employee Store: it is ATTACHed and the run must be ``running`` inside the
        same BEGIN IMMEDIATE transaction (r6 5.1). Non-operator appends need a write grant once grants are on.
        ``guard(conn)`` runs inside the write transaction before validation (test hook).
        """
        if not batch:
            raise ProjectError("empty_batch", "batch has no entries")
        payload = _canon({"author": author, "run": run, "batch": batch_payload(batch)})
        db = self.db
        if warrant is not None:  # M28 D4: re-check the delegated actor inside this write transaction
            guard = self._warrant_guard(warrant, guard)
            self._warrant_attach(warrant)
        if fence is not None:
            # r6 5.1: ATTACH the caller's employee Store before BEGIN IMMEDIATE so the write lock is taken on
            # both files; the run-active check and the project write then share one transaction.
            db.execute("ATTACH DATABASE ? AS emp", (str(fence),))
        try:
            self._append_tx(db, batch, payload, author=author, run=run, key=key, operator=operator,
                            expected_seq=expected_seq, guard=guard, fenced=fence is not None)
            result = self._last_result
        finally:
            if fence is not None:
                db.execute("DETACH DATABASE emp")
            if warrant is not None:
                self._warrant_detach(warrant)
        if not result.get("replayed"):
            self.export()
        return result

    def _append_tx(self, db, batch, payload, *, author, run, key, operator, expected_seq, guard, fenced):
        db.execute("BEGIN IMMEDIATE")
        try:
            if fenced:
                self._check_fence(db, author, run)
            elif run and any(item.get("kind") == "post" for item in batch):
                # M27 Q4: a post's run marks it fenced (the board wake event is then auth {channel: board}).
                raise ProjectError("not_fenced", "a post carrying a run must be a fenced append")
            if not operator:
                self._check_grant(db, author, self._need(batch))
            if guard is not None:
                guard(db)
            if key is not None:
                row = db.execute("SELECT payload, ids FROM keys WHERE key=?", (key,)).fetchone()
                if row is not None:
                    if row[0] != payload and row[0] != _hashed(payload):
                        raise ProjectError("key_conflict", f"key {key} was used with a different payload")
                    db.execute("ROLLBACK")
                    out = json.loads(row[1])
                    out["replayed"] = True
                    self._last_result = out
                    return
            head = db.execute("SELECT COALESCE(MAX(seq),0) FROM entries").fetchone()[0]
            if expected_seq is not None and expected_seq != head:
                raise ProjectError("stale_seq", "log moved", expected=expected_seq, actual=head)
            existing = self.entries()
            staged = self._validate(existing, batch, author=author, operator=operator)
            batch_id = "batch-" + uuid.uuid4().hex
            at = _now()
            ids = []
            for i, (item, body, data) in enumerate(staged):
                seq = head + 1 + i
                eid = item["id"]
                db.execute("INSERT INTO entries(seq,id,batch,kind,author,run,at,body) VALUES (?,?,?,?,?,?,?,?)",
                           (seq, eid, batch_id, item["kind"], author, run, at, _canon(body)))
                if data is not None:
                    db.execute("INSERT INTO source_bytes(id,bytes) VALUES (?,?)", (eid, data))
                if item["kind"] == "redact":
                    _apply_redaction(db, body["target"])
                ids.append(eid)
            self._materialise(db)
            counts: dict[str, int] = {}
            for item, _, _ in staged:
                counts[item["kind"]] = counts.get(item["kind"], 0) + 1
            result = {"batch": batch_id, "ids": ids, "seq_from": head + 1, "seq_to": head + len(ids)}
            db.execute("INSERT INTO audit(at,action,detail) VALUES (?,?,?)",
                       (at, "append", _canon({**result, "author": author, "run": run, "kinds": counts})))
            if key is not None:
                db.execute("INSERT INTO keys(key,payload,ids) VALUES (?,?,?)", (key, payload, _canon(result)))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        self._last_result = result

    @staticmethod
    def _check_fence(db, author: str, run: str | None) -> None:
        if not run:
            raise ProjectError("not_fenced", "a fenced append needs the session run ID")
        row = db.execute("SELECT data FROM emp.runs WHERE id=?", (run,)).fetchone()
        if row is None or json.loads(row[0]).get("status") != "running":
            raise ProjectError("run_inactive",
                               "This run is no longer active; stale work cannot modify project memory.", run=run)

    def _need(self, batch: list[dict]) -> str:
        """Strongest grant the batch needs: post kinds (and retracting a post) need post, the rest write."""
        need = "read"
        posts = None
        for raw in batch:
            kind = raw.get("kind")
            if kind == "retract":
                if posts is None:
                    posts = {e["id"] for e in self.entries() if e["kind"] == "post"}
                    posts |= {r.get("id") for r in batch if r.get("kind") == "post" and r.get("id")}
                k = "post" if (raw.get("body") or {}).get("target") in posts else "write"
            else:
                k = "post" if kind in POST_KINDS else "write"
            if GRANT_RANK[k] > GRANT_RANK[need]:
                need = k
        return need

    def _check_grant(self, db, employee: str, need: str) -> None:
        if not self.grants_enforced(db):
            return
        row = db.execute("SELECT mode FROM grants WHERE employee=?", (employee,)).fetchone()
        if row is None or GRANT_RANK.get(row[0], -1) < GRANT_RANK[need]:
            raise ProjectError("no_grant", f"{employee} has no {need} grant on project {self.project}",
                               employee=employee, need=need)

    def grants_enforced(self, db=None) -> bool:
        """Grants are enforced once the operator has provisioned the project (meta grants=on)."""
        row = (db or self.db).execute("SELECT v FROM meta WHERE k='grants'").fetchone()
        return row is not None and row[0] == "on"

    # ---- grants (operator only; never via MCP) -----------------------------------------------
    # ---- M28 D4: delegated writes re-check run + delegation chain inside the target transaction --------
    def _company_is_main(self, warrant) -> bool:
        return Path(warrant["company_path"]).resolve() == self.path.resolve()

    def _warrant_attach(self, warrant) -> None:
        from . import authority
        authority.attach(self.db, warrant, company_is_main=self._company_is_main(warrant))

    def _warrant_detach(self, warrant) -> None:
        from . import authority
        authority.detach(self.db, warrant, company_is_main=self._company_is_main(warrant))

    def _warrant_guard(self, warrant, inner=None, key=None):
        from . import authority

        def guard(db):
            authority.recheck(db, warrant, company_is_main=self._company_is_main(warrant))
            db.execute("INSERT INTO audit(at,action,detail) VALUES (?,?,?)",
                       (_now(), "warrant", _canon(authority.receipt(warrant, key))))
            if inner is not None:
                inner(db)
        return guard

    def _warranted(self, warrant, body, key=None):
        """Run body(db) in BEGIN IMMEDIATE; with a warrant, attach + re-check + receipt in the same transaction."""
        db = self.db
        if warrant is not None:
            self._warrant_attach(warrant)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                if warrant is not None:
                    self._warrant_guard(warrant, key=key)(db)
                out = body(db)
                db.execute("COMMIT")
                return out
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            if warrant is not None:
                self._warrant_detach(warrant)

    def grant(self, employee: str, mode: str, *, by: str, warrant: dict | None = None) -> dict:
        if mode not in GRANT_MODES:
            raise ProjectError("bad_mode", f"mode must be one of {GRANT_MODES}")
        at = _now()

        def body(db):
            db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('grants','on')")
            db.execute("INSERT OR REPLACE INTO grants(employee,mode,granted_by,at) VALUES (?,?,?,?)",
                       (employee, mode, by, at))
            db.execute("INSERT INTO audit(at,action,detail) VALUES (?,?,?)",
                       (at, "grant", _canon({"employee": employee, "mode": mode, "by": by})))
        self._warranted(warrant, body)
        return {"project": self.project, "employee": employee, "mode": mode, "by": by, "at": at}

    def revoke(self, employee: str, *, by: str, warrant: dict | None = None) -> bool:
        at = _now()

        def body(db):
            cur = db.execute("DELETE FROM grants WHERE employee=?", (employee,))
            db.execute("INSERT INTO audit(at,action,detail) VALUES (?,?,?)",
                       (at, "revoke", _canon({"employee": employee, "by": by, "existed": cur.rowcount > 0})))
            return cur.rowcount > 0
        return self._warranted(warrant, body)

    def grants(self) -> list[dict]:
        rows = self.db.execute("SELECT employee,mode,granted_by,at FROM grants ORDER BY employee").fetchall()
        return [{"employee": r[0], "mode": r[1], "by": r[2], "at": r[3]} for r in rows]

    def may_read(self, employee: str) -> bool:
        if not self.grants_enforced():
            return True
        return self.db.execute("SELECT 1 FROM grants WHERE employee=?", (employee,)).fetchone() is not None

    def _materialise(self, db) -> None:
        st = self._fold(self.entries())
        db.execute("DELETE FROM state")
        for name, value in st.items():
            db.execute("INSERT INTO state(name,value) VALUES (?,?)", (name, _canon(value)))
        db.execute("INSERT INTO state(name,value) VALUES (?,?)", (FOLD_STAMP, _canon(self._fold_code)))

    def _validate(self, existing: list[dict], batch: list[dict], *, author: str, operator: bool):
        sim = list(existing)
        staged = []
        seq = existing[-1]["seq"] if existing else 0
        for n, raw in enumerate(batch):
            kind = raw.get("kind")
            where = f"entry {n}"
            if kind not in ENTRY_KINDS:
                raise ProjectError("bad_kind", f"{where}: unknown kind {kind!r}")
            if kind in OPERATOR_KINDS and not operator:
                raise ProjectError("operator_only", f"{where}: {kind} is an operator operation")
            body = json.loads(_canon(raw.get("body", {})))
            eid = raw.get("id") or "pm-" + uuid.uuid4().hex
            state = fold(sim)
            data = None
            if kind == "source":
                text = raw.get("bytes")
                if not isinstance(text, (str, bytes)):
                    raise ProjectError("bad_source", f"{where}: source needs bytes")
                data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
                if len(data) > QUOTE_MAX:
                    raise ProjectError("quote_too_large", f"{where}: {len(data)} bytes > {QUOTE_MAX}",
                                       size=len(data), limit=QUOTE_MAX)
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    raise ProjectError("bad_source", f"{where}: bytes are not UTF-8")
                origin = body.setdefault("origin", {})
                role = origin.get("role", "other")
                if role not in SOURCE_ROLES:
                    raise ProjectError("bad_role", f"{where}: role {role!r}")
                body["size"] = len(data)
                body["sha256"] = hashlib.sha256(data).hexdigest()
            elif kind == "intent":
                # Issue #3 low (a): wrong types are refused as bad_intent/bad_claims, never an AttributeError.
                for f in ("title", "scope_note"):
                    if body.get(f) is not None and not isinstance(body[f], str):
                        raise ProjectError("bad_intent", f"{where}: {f} must be a string")
                _check_text(body.get("title") or "", TITLE_MAX, where, "title")
                _check_text(body.get("scope_note") or "", SCOPE_MAX, where, "scope_note")
                claims = body.get("claims", [])
                if not isinstance(claims, list) or not claims or len(claims) > MAX_CLAIMS:
                    raise ProjectError("bad_claims", f"{where}: 1..{MAX_CLAIMS} claims required")
                cids = set()
                for c in claims:
                    if (not isinstance(c, dict) or not isinstance(c.get("text", ""), str)
                            or not isinstance(c.get("cid"), str)):
                        raise ProjectError("bad_claims", f"{where}: each claim is {{cid, text}} with string values")
                    _check_text(c.get("text", ""), CLAIM_TEXT_MAX, where, "claim text")
                    if not c.get("cid") or c["cid"] in cids:
                        raise ProjectError("bad_claims", f"{where}: claim ids must be unique and non-empty")
                    cids.add(c["cid"])
                srcs = body.get("sources", [])
                if not srcs:
                    raise ProjectError("no_source", f"{where}: an intent cites at least one source entry")
                for s in srcs:
                    src = state["sources"].get(s)
                    if src is None:
                        raise ProjectError("unknown_source", f"{where}: {s} is not a source entry")
                    if src["role"] != "human":
                        raise ProjectError("not_human_source",
                                           f"{where}: {s} has role {src['role']}; cite it as context instead",
                                           source=s, role=src["role"])
            elif kind == "lesson":
                missing = [f for f in ("statement", "applies_when", "uncertainty") if not body.get(f)]
                if missing:
                    raise ProjectError("bad_lesson", f"{where}: {', '.join(missing)} required (lesson body: "
                                       "statement, applies_when, uncertainty; optional lesson_kind, evidence)",
                                       missing=missing)
                # Issue #3: fold and view read these after commit, so a wrong type must be refused here.
                for f in ("statement", "applies_when", "uncertainty", "title", "lesson_kind"):
                    if body.get(f) is not None and not isinstance(body[f], str):
                        raise ProjectError("bad_lesson", f"{where}: {f} must be text")
                ev = body.get("evidence")
                if not (ev is None or isinstance(ev, str) or (isinstance(ev, list) and all(
                        isinstance(x, dict) and isinstance(x.get("ref"), str) and x["ref"] for x in ev))):
                    raise ProjectError("bad_lesson", f"{where}: evidence is a list of {{ref, level?, note?}} "
                                                     "objects (or one line of text)")
            elif kind == "relate":
                rel = body.get("rel")
                if rel not in CLAIM_RELS:
                    raise ProjectError("bad_rel", f"{where}: rel {rel!r}")
                # Issue #3: `from` names an existing record (pm-...) or claim (pm-...#cN); fold splits it.
                src = body.get("from")
                if not isinstance(src, str) or not src:
                    raise ProjectError("bad_rel", f"{where}: from must be a record or claim ID (pm-...[#cN])")
                sid, scid = split_ref(src)
                if sid not in state["records"] or (scid is not None and src not in state["claims"]):
                    raise ProjectError("unknown_target", f"{where}: from {src} is not a record or claim")
                target = body.get("target", "")
                if not isinstance(target, str) or not target:
                    raise ProjectError("unknown_target", f"{where}: target {target!r}")
                tid, tcid = split_ref(target)
                if tid not in state["records"] and tid not in state["sources"]:
                    raise ProjectError("unknown_target", f"{where}: {target}")
                if tcid is not None and target not in state["claims"]:
                    raise ProjectError("unknown_target", f"{where}: {target}")
                if rel == "revises" and tcid is not None:
                    actual = state["claims"][target]["head"]
                    expected = body.get("expected_head")
                    if expected != actual:
                        raise ProjectError("stale_head", f"{where}: {target} head is {actual}",
                                           target=target, expected=expected, actual=actual)
            elif kind in ("retract", "redact"):
                target = body.get("target")
                hit = next((e for e in sim if e["id"] == target), None)
                if hit is None:
                    raise ProjectError("unknown_target", f"{where}: {target}")
                if kind == "retract" and hit["kind"] == "post" and hit["author"] != author:
                    raise ProjectError("not_author", f"{where}: only the author retracts a post",
                                       target=target, author=hit["author"])
                if kind == "retract" and hit["kind"] == "backlog" and not operator:
                    raise ProjectError("projection_only",
                                       f"{where}: backlog rows change only through the owner's work record")
                if kind == "redact":
                    if hit["kind"] not in ("source", "post"):
                        raise ProjectError("unknown_target", f"{where}: redact targets a source or post entry",
                                           target=target, target_kind=hit["kind"])
                    if any(e["kind"] == "redact" and e["body"].get("target") == target for e in sim):
                        raise ProjectError("already_redacted", f"{where}: {target} is already redacted",
                                           target=target)
            elif kind == "post":
                _check_text(body.get("topic", ""), POST_TOPIC_MAX, where, "topic")
                if not body.get("topic") or not body.get("body"):
                    raise ProjectError("bad_post", f"{where}: topic and body are required")
                _check_text(body.get("body", ""), POST_BODY_MAX, where, "body")
                for field in ("mentions", "refs"):
                    vals = body.setdefault(field, [])
                    if not isinstance(vals, list) or len(vals) > POST_LIST_MAX or not all(
                            isinstance(v, str) and v for v in vals):
                        raise ProjectError("bad_post", f"{where}: {field} is a list of ≤{POST_LIST_MAX} IDs")
                body["mentions"] = [m for m in dict.fromkeys(body["mentions"]) if m != author]
                rt = body.get("reply_to")
                if rt is not None and rt not in state["posts"]:
                    raise ProjectError("unknown_target", f"{where}: reply_to {rt} is not a live post")
                body["reply_to"] = rt
            elif kind == "backlog":
                if not isinstance(body.get("work_id"), str) or not body["work_id"].startswith("work-"):
                    raise ProjectError("bad_backlog", f"{where}: work_id must be a work ID")
                if body.get("status") not in BACKLOG_STATUSES:
                    raise ProjectError("bad_backlog", f"{where}: status must be one of {BACKLOG_STATUSES}")
                if body.get("origin") not in (None,) + BACKLOG_ORIGINS:
                    raise ProjectError("bad_backlog", f"{where}: origin must be one of {BACKLOG_ORIGINS}")
                _check_text(body.get("title") or "", BACKLOG_TITLE_MAX, where, "title")
                _check_text(body.get("waiting_for") or "", BACKLOG_WAITING_MAX, where, "waiting_for")
                ev = body.setdefault("evidence", [])
                if not isinstance(ev, list) or len(ev) > POST_LIST_MAX or not all(isinstance(v, str) for v in ev):
                    raise ProjectError("bad_backlog", f"{where}: evidence is a list of ≤{POST_LIST_MAX} refs")
                if not isinstance(body.get("priority", 0), int):
                    raise ProjectError("bad_backlog", f"{where}: priority must be an integer")
                bb = body.get("blocked_by")
                if bb is not None:
                    # Docket P1b: question ID + hop only, never question text.
                    from .board import BLOCKED_BY
                    if (not isinstance(bb, list) or len(bb) > POST_LIST_MAX
                            or not all(isinstance(v, str) and BLOCKED_BY.fullmatch(v) for v in bb)):
                        raise ProjectError("bad_backlog", f"{where}: blocked_by is a list of "
                                                          f"\"<question ID> <hop>\" lines")
            elif kind == "considered":
                if body.get("source") not in state["sources"]:
                    raise ProjectError("unknown_source", f"{where}: {body.get('source')}")
            elif kind == "status":
                rec = state["records"].get(body.get("target"))
                if rec is None or rec["kind"] != "lesson" or body.get("to") not in (
                        "accepted", "disputed", "retired"):
                    raise ProjectError("bad_status", f"{where}: status targets a lesson")
            elif kind == "confirm":
                if body.get("target") not in state["claims"]:
                    raise ProjectError("unknown_target", f"{where}: {body.get('target')}")
            seq += 1
            item = {"kind": kind, "id": eid}
            sim.append({"seq": seq, "id": eid, "batch": "", "kind": kind, "author": author, "run": None,
                        "at": "", "body": body})
            staged.append((item, body, data))
        return staged

    # ---- redaction (operator CLI only; refuses in sessions at the CLI layer) -----------------
    def redact(self, entry_id: str, *, reason: str, by: str, warrant: dict | None = None) -> dict:
        """Empty a source entry's bytes or a post's body, append a ``redact`` tombstone {target, sha256, size,
        reason, by} and regenerate log.jsonl so the removed text leaves the export too (D3.7, backlog 27).

        For a post, sha256/size are over the exact canonical bytes stored in ``entries.body`` (B1)."""
        if not reason or not by:
            raise ProjectError("bad_redact", "redact needs --reason and --by")
        row = self.db.execute("SELECT kind, body FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is not None and row[0] == "post":
            data = row[1].encode("utf-8")
        elif row is not None and row[0] == "source":
            data = self.source_bytes(entry_id)
        else:
            data = b""  # _validate raises unknown_target
        body = {"target": entry_id, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "reason": reason, "by": by}
        # M28: a delegated redaction is authored by the session actor itself, never a forgeable operator:* label.
        author = warrant["actor"] if warrant is not None else "operator:" + by
        try:
            result = self.append([{"kind": "redact", "body": body}], author=author, operator=True, warrant=warrant)
        except BaseException as exc:
            # Issue #3: rebuild even if something after the commit raised, so the removed text never stays in
            # log.jsonl. A refused append committed nothing, and the rebuild then rewrites the same lines.
            # Low (c): a failing rebuild must not mask the original error; report both.
            try:
                self.export(rebuild=True)
            except Exception as rebuild_exc:
                raise ProjectError("redact_rebuild_failed",
                                   f"redact failed ({type(exc).__name__}: {exc}) and the log.jsonl rebuild also "
                                   f"failed ({type(rebuild_exc).__name__}: {rebuild_exc}); log.jsonl may still "
                                   "hold the removed text") from exc
            raise
        self.export(rebuild=True)
        out = dict(result, redacted=entry_id, kind=row[0], sha256=body["sha256"], size=body["size"])
        if row[0] == "post":
            readers = [g["employee"] for g in self.grants()] if self.grants_enforced() else None
            out["copies"] = {
                "removed": False,
                "scope": "shared project log and its log.jsonl export only",
                "note": ("Copies already fed into homes as board:%s events, and transcripts of sessions that saw "
                         "the post, are not removed. This store does not record which homes received a copy; any "
                         "home with read access may hold one. Quoted text inside replies is not redacted."
                         % self.project),
                "read_access": readers,  # grant holders (None: grants not enforced, so every home may read)
            }
        return out

    # ---- export / import --------------------------------------------------------------------
    def export(self, rebuild: bool = False) -> int:
        """Append entries after export_through to log.jsonl. Returns the new export_through."""
        row = self.db.execute("SELECT v FROM meta WHERE k='export_through'").fetchone()
        through = 0 if (rebuild or row is None) else int(row[0])
        mode = "w" if through == 0 else "a"
        new = [e for e in self.entries() if e["seq"] > through]
        try:
            with open(self.export_path, mode, encoding="utf-8") as fh:
                for e in new:
                    line = dict(e)
                    if e["kind"] == "source":
                        line["bytes"] = self.source_bytes(e["id"]).decode("utf-8")
                    fh.write(_canon(line) + "\n")
        except OSError:
            self.write_view()
            return through
        last = new[-1]["seq"] if new else through
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES ('export_through',?)", (str(last),))
        self.write_view()
        return last

    # ---- generated read-only view (M21 phase 3) ---------------------------------------------
    def view(self, as_of: int | None = None) -> str:
        """Markdown view of the log as of ``as_of`` (default head); a pure function of the log."""
        from .projectview import render
        if as_of is not None and as_of < 0:
            raise ProjectError("bad_as_of", "as_of must be >= 0")
        gone = {t for (t,) in self.db.execute(
            "SELECT json_extract(body,'$.target') FROM entries WHERE kind='redact'").fetchall()}

        def excerpt(entry_id: str):
            if entry_id in gone:
                return None
            row = self.db.execute("SELECT bytes FROM source_bytes WHERE id=?", (entry_id,)).fetchone()
            return None if row is None else bytes(row[0])
        return render(self.entries(as_of), self.project, excerpt)

    def write_view(self) -> bool:
        """Rewrite view.md (0600, temp file + rename) outside any write transaction. A failure never fails the
        append that triggered it; the next write or `project view --write` regenerates it. Returns success."""
        tmp = self.view_path.with_name(".view.md.%d.tmp" % os.getpid())
        try:
            text = self.view()
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(str(tmp), str(self.view_path))
            return True
        except Exception:  # Issue #3: any render error, not only I/O, must leave the committed append alone.
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

    @classmethod
    def import_jsonl(cls, jsonl: str | os.PathLike, path: str | os.PathLike, project: str) -> "ProjectStore":
        """Load an exported log into an empty store, keeping seq/id/author/at, and re-fold."""
        store = cls(path, project)
        if store.head_seq():
            store.close()
            raise ProjectError("not_empty", "import target must be empty")
        db = store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            with open(jsonl, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    e = json.loads(line)
                    db.execute("INSERT INTO entries(seq,id,batch,kind,author,run,at,body) VALUES (?,?,?,?,?,?,?,?)",
                               (e["seq"], e["id"], e["batch"], e["kind"], e["author"], e["run"], e["at"],
                                _canon(e["body"])))
                    if e["kind"] == "source":
                        db.execute("INSERT INTO source_bytes(id,bytes) VALUES (?,?)",
                                   (e["id"], e.get("bytes", "").encode("utf-8")))
            # B5: an older export may predate a tombstone it carries; apply every redaction after loading.
            for (target,) in db.execute(
                    "SELECT json_extract(body,'$.target') FROM entries WHERE kind='redact' ORDER BY seq").fetchall():
                _apply_redaction(db, target)
            store._materialise(db)
            db.execute("INSERT INTO audit(at,action,detail) VALUES (?,?,?)",
                       (_now(), "import", _canon({"from": str(jsonl), "head": store.head_seq()})))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            store.close()
            raise
        store.export(rebuild=True)
        return store

    # ---- reads ------------------------------------------------------------------------------
    def quote_chunk(self, entry_id: str, cursor: int = 0) -> dict:
        data = self.source_bytes(entry_id)
        if not is_boundary(data, cursor):
            raise ProjectError("bad_cursor", f"cursor {cursor} is not a character boundary")
        end = cursor + utf8_cut(data[cursor:], CHUNK_MAX)
        return {"id": entry_id, "quote_bytes": len(data), "offset": cursor,
                "chunk": data[cursor:end].decode("utf-8"), "next_cursor": end if end < len(data) else None}

    def get(self, ref: str, *, history: int = 5, cursor: int = 0, part: str | None = None,
            as_of: int | None = None) -> dict:
        if part == "quote":
            return self.quote_chunk(ref, cursor)
        st = self.state(as_of)
        as_of_seq = st["through"]
        if ref in st["sources"]:
            src = st["sources"][ref]
            data = self.source_bytes(ref)
            n = utf8_cut(data, EXCERPT_GET)
            return {"as_of_seq": as_of_seq, "source": src, **_excerpt(data, n)}
        rec = st["records"].get(ref)
        if rec is None:
            raise ProjectError("not_found", f"no record {ref}")
        claims = [dict(st["claims"][f"{ref}#{c}"], ref=f"{ref}#{c}") for c in rec["claims"]]
        quote_src = rec["sources"][0] if rec["sources"] else None
        data = self.source_bytes(quote_src) if quote_src else b""
        heads = [{"claim": c["ref"], "head": c["head"],
                  "gist": _gist(st["claims"].get(c["head"], {}).get("text", ""))} for c in claims]
        hist_all = _history(st, ref)
        page = hist_all[cursor:cursor + history]
        excerpt_cap = EXCERPT_GET
        # A lesson's applies_when, uncertainty and evidence live only in its entry body (the fold keeps the
        # statement), so they were invisible here and a "no evidence refs" check read wrong (pm-3c8d97c7).
        lesson = self._lesson_body(ref) if rec["kind"] == "lesson" else None
        out = None
        while True:
            out = {"as_of_seq": as_of_seq, "record": rec, "claims": claims, "quote_source": quote_src,
                   **_excerpt(data, utf8_cut(data, excerpt_cap)), "heads": heads, "history": page,
                   "next_cursor": cursor + len(page) if cursor + len(page) < len(hist_all) else None,
                   "history_truncated": len(page) < min(history, len(hist_all) - cursor)}
            if lesson is not None:
                out["lesson"] = lesson
            if len(_canon(out).encode("utf-8")) <= GET_BUDGET:
                return out
            if page:
                page = page[:-1]
                continue
            if any("gist" in h for h in heads):
                heads = [{"claim": h["claim"], "head": h["head"]} for h in heads]
                continue
            if lesson is not None and _shrink_lesson(lesson):
                continue
            if excerpt_cap > EXCERPT_FLOOR:
                excerpt_cap = max(EXCERPT_FLOOR, excerpt_cap - 256)
                continue
            fixed = {"as_of_seq": as_of_seq, "record": rec, "claims": claims, "quote_source": quote_src,
                     "quote_bytes": len(data), "quote_cursor": 0 if data else None, "budget_exceeded": True}
            if lesson is not None:
                fixed["lesson"] = lesson
            return fixed

    def _lesson_body(self, entry_id: str) -> dict:
        """The lesson fields project_memory_get shows besides the folded statement: applies_when, uncertainty
        and evidence. Evidence keeps its stored shape: a list of {ref, level?, note?} (only those keys), or one
        line of free text (as projectview shows it). Each text is cut to LESSON_TEXT_GET characters."""
        row = self.db.execute("SELECT body FROM entries WHERE id=?", (entry_id,)).fetchone()
        body = json.loads(row[0]) if row else {}
        out = {f: _cut_text(body.get(f) or "", LESSON_TEXT_GET) for f in ("applies_when", "uncertainty")}
        ev = body.get("evidence")
        if isinstance(ev, str):
            out["evidence"] = _cut_text(ev, LESSON_TEXT_GET)
            out["evidence_count"] = 1 if ev.strip() else 0
        else:
            items = []
            for x in ev or []:
                if isinstance(x, dict) and x.get("ref"):
                    item = {"ref": str(x["ref"])}
                    for k in ("level", "note"):
                        if isinstance(x.get(k), str) and x[k]:
                            item[k] = _cut_text(x[k], LESSON_NOTE_GET if k == "note" else LESSON_TEXT_GET)
                    items.append(item)
            out["evidence"] = items
            out["evidence_count"] = len(items)
        return out

    def search(self, query: str, *, kind: str | None = None, status: str | None = None, limit: int = 10,
               as_of: int | None = None) -> dict:
        st = self.state(as_of)
        q = query.strip()
        words = [w for w in q.lower().split() if len(w) >= 3]
        hits = []
        for rid, rec in st["records"].items():
            if kind and rec["kind"] != kind:
                continue
            rstatus = rec.get("status")
            if status is None and rstatus in ("superseded", "retired", "withdrawn"):
                continue
            if status is not None and rstatus != status:
                continue
            text = " ".join([rec.get("title", ""), rec.get("statement", "")] + [st["claims"][f"{rid}#{c}"]["text"] for c in rec["claims"]])
            body_text = text.lower()
            if len(q) >= 15 and rid.startswith(q):
                score = 1000.0
            else:
                score = float(sum(body_text.count(w) for w in words))
            if score > 0:
                hits.append((score, rec["seq"], rid, rec))
        hits.sort(key=lambda h: (-h[0], -h[1]))
        return {"as_of_seq": st["through"], "results": [
            {"id": rid, "kind": rec["kind"], "status": rec.get("status"), "title": rec.get("title", ""),
             "flags": rec["flags"]} for _, _, rid, rec in hits[:limit]]}


REDACTED_POST_FIELDS = ("topic", "body", "mentions", "refs")


def _hashed(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply_redaction(db, target: str) -> None:
    """Remove the target's text inside the open transaction: a source's bytes, or a post's body (rewritten to an
    empty placeholder keeping reply_to, so replies keep their parent). Any keys.payload whose batch produced the
    target is replaced by its sha256, so key replay still matches but the text is gone (B2)."""
    row = db.execute("SELECT kind, body FROM entries WHERE id=?", (target,)).fetchone()
    if row is None:
        return
    if row[0] == "source":
        db.execute("UPDATE source_bytes SET bytes=? WHERE id=?", (b"", target))
    elif row[0] == "post":
        old = json.loads(row[1])
        new = {"topic": "", "body": "", "mentions": [], "refs": [], "reply_to": old.get("reply_to"),
               "redacted": True}
        db.execute("UPDATE entries SET body=? WHERE id=?", (_canon(new), target))
    like = "%" + json.dumps(target) + "%"
    for key, payload in db.execute("SELECT key, payload FROM keys WHERE ids LIKE ?", (like,)).fetchall():
        if not payload.startswith("sha256:"):
            db.execute("UPDATE keys SET payload=? WHERE key=?", (_hashed(payload), key))


def batch_payload(batch: list[dict]) -> list[dict]:
    out = []
    for item in batch:
        x = {"kind": item.get("kind"), "body": item.get("body", {})}
        if "bytes" in item:
            b = item["bytes"]
            x["bytes_sha"] = hashlib.sha256(b.encode("utf-8") if isinstance(b, str) else bytes(b)).hexdigest()
        out.append(x)
    return out


def _check_text(text: str, limit: int, where: str, field: str) -> None:
    if len(text.encode("utf-8")) > limit:
        raise ProjectError("too_long", f"{where}: {field} over {limit} bytes")


def _gist(text: str) -> str:
    data = " ".join(text.split()).encode("utf-8")
    return data[:utf8_cut(data, 80)].decode("utf-8")


def _cut_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _shrink_lesson(lesson: dict) -> bool:
    """One step of trimming the lesson block to fit GET_BUDGET: notes go first, then evidence items from the
    end (evidence_count keeps the full number, evidence_truncated says so). False when nothing is left to trim."""
    ev = lesson.get("evidence")
    if isinstance(ev, list):
        noted = [x for x in ev if "note" in x]
        if noted:
            for x in ev:
                x.pop("note", None)
            return True
        if ev:
            ev.pop()
            lesson["evidence_truncated"] = True
            return True
    return False


def _excerpt(data: bytes, n: int) -> dict:
    return {"quote_excerpt": data[:n].decode("utf-8"), "quote_bytes": len(data),
            "quote_truncated": n < len(data), "quote_cursor": n if n < len(data) else None}


def _history(st: dict, rid: str) -> list[dict]:
    """Revisions linked to rid's claims, in either direction, nearest (by seq distance) first."""
    rec = st["records"][rid]
    related: dict[str, str] = {}
    for c in rec["claims"]:
        claim = st["claims"][f"{rid}#{c}"]
        for link in claim["links"]:
            related.setdefault(split_ref(link["from"])[0], link["rel"])
    for ref, claim in st["claims"].items():
        for link in claim["links"]:
            if split_ref(link["from"])[0] == rid:
                related.setdefault(claim["record"], link["rel"] + "-of")
    items = []
    for other, rel in related.items():
        o = st["records"].get(other)
        if o is None or other == rid:
            continue
        first = o["claims"][0] if o["claims"] else None
        items.append({"id": other, "rel": rel, "act": o.get("act") or o.get("lesson_kind"),
                      "gist": _gist(st["claims"][f"{other}#{first}"]["text"]) if first else _gist(o.get("title", "")),
                      "distance": abs(o["seq"] - rec["seq"]), "seq": o["seq"]})
    items.sort(key=lambda i: (i["distance"], i["seq"]))
    return items

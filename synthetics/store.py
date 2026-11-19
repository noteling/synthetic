"""Durable employee state. Model sessions are consumers of this state, never its owners."""

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import board
from . import capture
from . import ledgerline
from . import roster


UNSET = object()
WORK_ID = re.compile(r"^work-[0-9a-f]{32}$")
# Backlog 24: a unique prefix (8..31 lowercase hex) of an own-home record ID is accepted at the tool
# boundary and expanded to the full ID before anything is checked, keyed or stored.
SHORT_ID = re.compile(r"^(event|run|question|message|memory|work|reminder)-([0-9a-f]{8,31})$")
ID_TABLES = {"event": "events", "run": "runs", "question": "questions", "message": "messages",
             "memory": "memories", "work": "works", "reminder": "reminders"}


def _known_cost(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value)


def timestamp(value=None):
    """Normalize a timezone-aware timestamp; reject ambiguous local clock values."""
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Use an ISO timestamp with a timezone.") from exc
    else:
        raise ValueError("Use an ISO timestamp with a timezone.")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be nonempty text." % name)
    return value.strip()


def routines_mod():
    """The routines helpers, imported lazily like the store's other routine code."""
    from . import routines
    return routines


def _search_text(value):
    """The values of a record as one text, without its key names (issue #7: a query word that is only a
    key name such as "subject" or "source" must not match every record)."""
    if isinstance(value, dict):
        return "\n".join(_search_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_search_text(item) for item in value)
    return "" if value is None else str(value)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("State must contain JSON values.") from exc


class WorkerHeld(ValueError):
    """M28 D10c: a delegated worker op holds this home's idle boundary; no run may start."""


class LaunchOpPending(ValueError):
    """M28 D14: a delegated launchd op on this label is still pending; settle it before acting again."""

    def __init__(self, op_id, label):
        super().__init__("%s on %s is still pending; settle it first (authority ops / authority settle)" % (op_id, label))
        self.op_id = op_id
        self.label = label


class ChannelError(ValueError):
    """M25 contract error with a closed-set code (docs/channel-contract.md)."""

    CODES = ("bad_version", "bad_line", "auth_failed", "source_not_allowed", "not_routed", "unknown_message",
             "conflict", "invalid_state", "in_session")

    def __init__(self, code, message):
        assert code in self.CODES, code
        super().__init__(message)
        self.code = code


CLAIM_LEASE_SECONDS = 600
RETRY_AFTER_SECONDS = 60
FINAL_STATES = ("delivered", "failed")

SESSION_ENV = "SYNTHETICS_SESSION_RUN"
DEFAULT_CHANNEL_DIR = "~/.config/synthetics-channels"


def read_credential_file(path):
    """Read a channel key file. Refuses files readable by group or others."""
    path = Path(path).expanduser()
    mode = path.stat().st_mode
    if mode & 0o077:
        raise ValueError("Credential file %s must not be readable by group or others (chmod 600)." % path)
    data = path.read_bytes().strip()
    if len(data) < 16:
        raise ValueError("Credential file %s is too short." % path)
    return data


ROSTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS roster_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    author TEXT NOT NULL, run TEXT, at TEXT NOT NULL, body TEXT NOT NULL
);
"""


class Store:
    TABLES = ("identity", "entities", "memories", "works", "questions", "messages", "reminders", "runs", "access_grants",
              "thread_records", "runtime_requests", "channels", "launch_ops", "glances")

    def __init__(self, employee_home, run_id=None):
        self.home = Path(employee_home).expanduser().resolve()
        self.run_id = run_id
        self.resolved = {}  # backlog 24: short ID prefixes expanded during the current tool call
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace = self.home / "workspace"
        self.workspace.mkdir(exist_ok=True, mode=0o700)
        self.path = self.home / "state.sqlite3"
        self.db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        for table in self.TABLES:
            self.db.execute("CREATE TABLE IF NOT EXISTS %s (id TEXT PRIMARY KEY, data TEXT NOT NULL)" % table)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL, processed_run TEXT
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                namespace TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                record_id TEXT NOT NULL, PRIMARY KEY(namespace,key)
            );
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                action TEXT NOT NULL, record_id TEXT NOT NULL, data TEXT NOT NULL
            );
        """)
        self.db.executescript(capture.SCHEMA)
        self.db.executescript(ROSTER_SCHEMA)
        self._migrate()
        self.path.chmod(0o600)

    SCHEMA_VERSION = 5

    def _migrate(self):
        """Bring an older home up to SCHEMA_VERSION; never downgrade a newer one."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= self.SCHEMA_VERSION:
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version < 2:
                # Version 2: completion needs evidence. Work finished before that rule
                # stays readable and editable, and is marked so it is never mistaken
                # for work closed with evidence (or for a later bypass).
                # A pre-M14 Store resets user_version to 1 on every open, so this
                # can rerun on a home that already holds M14 closures: those carry
                # a cancellation or qualifying evidence and are never marked.
                for work in self._all("works"):
                    if work.get("status") in ("done", "cancelled") and not work.get("legacy") \
                            and not self._closed_under_v2(work):
                        work["legacy"] = True
                        self._save("works", work, "work.legacy_marked")
            # Version 3 (M22): the channels table (created above with the other
            # tables). Existing events keep no auth stamp: they are legacy input
            # and support at most observed until an operator backfill corroborates them.
            # Version 4 (M21): project_bindings, capture_outbox, coverage_marks (created above,
            # empty). No existing event is captured: bindings are prospective.
            # Version 5 (M26): roster_log, the private self/view log (created above, empty). Views are
            # seeded later only by an explicit `roster seed --apply`, never by migration.
            if version < self.SCHEMA_VERSION:
                self.db.execute("PRAGMA user_version=%d" % self.SCHEMA_VERSION)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _closed_under_v2(work):
        """True when a terminal item was closed under the evidence rule."""
        if work.get("cancellation"):
            return True
        return any(isinstance(item, dict) and item.get("level") in ("observed", "verified", "operator-override")
                   for item in work.get("evidence") or [])

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.run_id is not None and self._get("runs", self.run_id)["status"] != "running":
                raise ValueError("This run is no longer active; stale work cannot modify employee state.")
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _get(self, table, record_id):
        row = self.db.execute("SELECT data FROM %s WHERE id=?" % table, (record_id,)).fetchone()
        if row is None:
            raise ValueError("No %s record %s." % (table, record_id))
        return json.loads(row["data"])

    def _find(self, table, record_id):
        row = self.db.execute("SELECT data FROM %s WHERE id=?" % table, (record_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def _resolve(self, value, table=None):
        """Backlog 24: expand a unique short ID prefix to the full ID of a row in this home.

        Only the table the prefix names (question- in questions, ...), and only when the caller
        expects that table (table=None accepts any of ID_TABLES). Full IDs, other strings and
        prefixes matching nothing come back unchanged, so the existing not-found errors stand.
        Two or more matches raise ambiguous_id before anything is written. Expansions are
        collected in self.resolved ({given: full}) for the tool result.
        """
        match = SHORT_ID.match(value) if isinstance(value, str) else None
        if not match or (table is not None and ID_TABLES[match.group(1)] != table):
            return value
        rows = [row["id"] for row in self.db.execute(
            "SELECT id FROM %s WHERE id >= ? AND id < ? ORDER BY id LIMIT 3" % ID_TABLES[match.group(1)],
            (value, value + "g"))]
        if len(rows) > 1:
            raise ValueError("ambiguous_id: %s matches %s%s; use the full ID." % (
                value, ", ".join(rows[:2]), ", ..." if len(rows) > 2 else ""))
        if not rows:
            return value
        self.resolved[value] = rows[0]
        return rows[0]

    def _resolve_text(self, text):
        """K1: rewrite question-<prefix> tokens in free text (waiting_for) to full IDs."""
        if not isinstance(text, str):
            return text
        return re.sub(r"\bquestion-[A-Za-z0-9_-]+", lambda m: self._resolve(m.group(0), "questions"), text)

    EVIDENCE_PREFIXES = ("event-", "run-", "question-", "message-", "memory-", "work-", "receipt:", "https://", "/")

    def _resolve_evidence_ref(self, ref):
        """Backlog 30: a workspace-relative file path becomes the absolute resolved path.

        Last fallback only (R1): a ref with any known prefix (an ID, receipt:, https://, an
        absolute path) is never treated as relative, so a file named like an ID (work-notes.md)
        still takes the ID branch. Resolved against Store.workspace, never the process cwd (Q1);
        ../ is allowed and symlinks are followed (Q2) - the runs/ refusal in _evidence_item then
        applies to the resolved path. A missing file stays as given and fails as before (R5).
        """
        if not isinstance(ref, str) or not ref or ref.startswith(self.EVIDENCE_PREFIXES):
            return ref
        path = (self.workspace / ref).resolve()
        if not path.is_file():
            return ref
        self.resolved[ref] = str(path)
        return str(path)

    def _all(self, table):
        return [json.loads(row["data"]) for row in self.db.execute("SELECT data FROM %s ORDER BY rowid" % table)]

    def _save(self, table, record, action):
        data = _json(record)
        self.db.execute("INSERT INTO %s(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data" % table,
                        (record["id"], data))
        self._audit(action, record["id"], record)
        if self._touches_board(table, record, action):
            # Docket P1b: blocked_by follows question hops and delivery, and asking/answering changes a work's
            # status without work_put; board.publish_drain reconciles on the engine's next step.
            self.db.execute("INSERT INTO capture_meta(k,v) VALUES ('publish_dirty','1') "
                            "ON CONFLICT(k) DO UPDATE SET v='1'")
        return record

    @staticmethod
    def _touches_board(table, record, action):
        if table == "works":
            return action != "work.published" and bool(record.get("publish") or record.get("published"))
        if table == "questions":
            return True
        return table == "messages" and bool(record.get("question_id"))

    def _audit(self, action, record_id, data):
        self.db.execute("INSERT INTO audit(at,action,record_id,data) VALUES (?,?,?,?)",
                        (timestamp(), action, record_id, _json(data)))

    def _once(self, namespace, key, payload):
        if key is None:
            return None
        key = _text(key, "key")
        row = self.db.execute("SELECT payload,record_id FROM idempotency WHERE namespace=? AND key=?", (namespace, key)).fetchone()
        if row:
            if row["payload"] != _json(payload):
                raise ValueError("Idempotency key was already used with different content.")
            return row["record_id"]
        return None

    def _key(self, namespace, key, payload, record_id):
        if key is not None:
            self.db.execute("INSERT INTO idempotency(namespace,key,payload,record_id) VALUES (?,?,?,?)",
                            (namespace, _text(key, "key"), _json(payload), record_id))

    # -- Roster layers 1 and 3: the private log (M26). Only this home's employee writes it. --

    def roster_entries(self):
        return [{"seq": r["seq"], "id": r["id"], "kind": r["kind"], "author": r["author"], "run": r["run"],
                 "at": r["at"], "body": json.loads(r["body"])}
                for r in self.db.execute("SELECT * FROM roster_log ORDER BY seq")]

    def roster_state(self):
        return roster.private_fold(self.roster_entries())

    def roster_append(self, kind, body, key=None, company=None, author=None):
        """Append one validated private entry; returns the stored entry. Fenced by the run like memories.

        ``company`` (a company_fold state) defaults to the company log next to this home. ``author`` is the
        employee by default; only the operator's `roster seed --apply` passes ``roster-seed`` (C7).
        """
        payload = {"kind": kind, "body": body}
        if company is None:
            company = roster.company_state_for_home(self.home)
        with self._tx():
            existing = self._once("roster", key, payload)
            if existing:
                return next(e for e in self.roster_entries() if e["id"] == existing)
            stored = roster.validate_private(kind, body, current=self.roster_state(), company=company)
            author = author or self.identity()["id"]
            entry = {"id": "ro-" + uuid.uuid4().hex, "kind": kind, "author": author, "run": self.run_id,
                     "at": timestamp(), "body": stored}
            cur = self.db.execute("INSERT INTO roster_log(id,kind,author,run,at,body) VALUES (?,?,?,?,?,?)",
                                  (entry["id"], kind, author, self.run_id, entry["at"], _json(stored)))
            entry["seq"] = cur.lastrowid
            self._audit("roster." + kind, entry["id"], {"seq": entry["seq"], "person": stored.get("person"),
                                                         "section": stored.get("section")})
            self._key("roster", key, payload, entry["id"])
            return entry

    def self_note(self, section, text, source, certainty="observed", key=None):
        return self.roster_append("self", {"section": section, "text": text, "source": source,
                                           "certainty": certainty}, key=key)

    def view_put(self, person, claims=None, retract=None, reconciles=None, key=None):
        """Add/revise claims about one person, retract claims, and/or reconcile a role revision.

        Each part is its own entry, appended in order: claims, retractions, reconcile. With a key, the
        whole call is idempotent.
        """
        payload = {"person": person, "claims": claims, "retract": retract, "reconciles": reconciles}
        if key is not None:
            with self._tx():
                done = self._once("roster.view_put", key, payload)
            if done:
                ids = json.loads(done)
                return [e for e in self.roster_entries() if e["id"] in ids]
        if not claims and not retract and reconciles is None:
            raise roster.ProjectError("bad_entry", "view_put needs claims, retract or reconciles")
        company = roster.company_state_for_home(self.home)
        parts = []
        if claims:
            parts.append(("view", {"person": person, "claims": claims}))
        for r in retract or []:
            r = r if isinstance(r, dict) else {"cid": r}
            parts.append(("view_retract", {"person": person, "cid": r.get("cid"), "reason": r.get("reason")}))
        if reconciles is not None:
            parts.append(("view_reconcile", {"person": person, "role_rev": reconciles}))
        with self._tx():
            done = self._once("roster.view_put", key, payload)  # a concurrent call with this key won
            if done:
                ids = json.loads(done)
                return [e for e in self.roster_entries() if e["id"] in ids]
            current, out = self.roster_state(), []
            author = self.identity()["id"]
            for kind, body in parts:
                stored = roster.validate_private(kind, body, current=current, company=company)
                entry = {"id": "ro-" + uuid.uuid4().hex, "kind": kind, "author": author, "run": self.run_id,
                         "at": timestamp(), "body": stored}
                cur = self.db.execute("INSERT INTO roster_log(id,kind,author,run,at,body) VALUES (?,?,?,?,?,?)",
                                      (entry["id"], kind, author, self.run_id, entry["at"], _json(stored)))
                entry["seq"] = cur.lastrowid
                self._audit("roster." + kind, entry["id"], {"seq": entry["seq"], "person": stored.get("person")})
                out.append(entry)
                current = roster.private_fold(self.roster_entries())
            self._key("roster.view_put", key, payload, _json([e["id"] for e in out]))
            return out

    def roster_rendered(self, company=None):
        """Render this home's private layer (self.md, views/<id>.md) as text, without writing files.

        Returns {"self": text, "views": {person_id: text}, "stale": [person_id, ...]}. Pure reads.
        """
        if company is None:
            company = roster.company_state_for_home(self.home)
        ident = self.identity()
        state = self.roster_state()
        mems = [m for m in self._all("memories") if m.get("subject") == ident["id"]]
        mems.sort(key=lambda m: m.get("created_at", ""), reverse=True)

        def ref_status(rid):
            rec = self._find("questions" if rid.startswith("question-") else "works", rid)
            return rec.get("status") if rec else None

        def superseded(mid):
            rec = self._find("memories", mid)
            return rec.get("superseded_by") if rec else None

        views, stale = {}, []
        for pid, view in sorted(state["views"].items()):
            profile = company["people"].get(pid)
            if profile and profile.get("role_rev", 0) > view.get("basis_role_rev", 0):
                stale.append(pid)
            views[pid] = roster.render_view(view, profile, ident["name"], ref_status=ref_status,
                                            memory_superseded=superseded)
        return {"self": roster.render_self(ident, state, mems), "views": views, "stale": stale}

    PEOPLE_BYTES = 24 * 1024
    VIEWS_BYTES = 16 * 1024

    def _snapshot_entities(self):
        """Design §5: person rows and the self row leave `entities`; the people/self sections replace them."""
        company = roster.company_state_for_home(self.home)
        out = []
        for e in self.entities():
            if e.get("kind") == "self":
                continue
            if e.get("kind") == "person":
                if self._company_person(e["id"], company) or self._company_person(e.get("name", ""), company):
                    continue  # shown in `people`
                e = dict(e, unofficial=True)  # no company profile yet (design §5)
            out.append(e)
        return out

    @staticmethod
    def _capped(texts, cap, priority):
        """Full texts for priority keys first, then the rest in key order, while under ``cap`` bytes.

        Returns (included {key: text}, index [{id, first line}], truncated)."""
        order = [k for k in sorted(texts) if k in priority] + [k for k in sorted(texts) if k not in priority]
        full, index, size = {}, [], 0
        for k in order:
            cost = len(texts[k].encode("utf-8"))
            if size + cost <= cap:
                full[k] = texts[k]
                size += cost
            else:
                index.append({"id": k, "line": texts[k].split("\n", 1)[0][:120]})
        return full, index, bool(index)

    def _roster_sections(self, identity, events, work, questions):
        """M26 §7 snapshot sections: self (≤4 KB), people (official, cap 24 KB), views (private, cap 16 KB).

        Read-only: nothing is written to <home>/roster/ here (files come from `roster render`)."""
        company = roster.company_state_for_home(self.home)
        rendered = self.roster_rendered(company=company)
        mine = set(rendered["views"])
        referenced = set(mine)
        names = [e.get("sender") for e in events] + [q.get("to") for q in questions]
        for item in work:
            names.append((item.get("woken_by_ref") or {}).get("sender"))
        for n in names:
            hit = roster.resolve(company, n) if n else {"id": None}
            if hit["id"]:
                referenced.add(hit["id"])
        people_texts = {pid: roster.render_person(p) for pid, p in company["people"].items()}
        people, people_index, people_truncated = self._capped(people_texts, self.PEOPLE_BYTES, referenced)
        views, views_index, views_truncated = self._capped(rendered["views"], self.VIEWS_BYTES, referenced)
        # Only self-subject memories whose ID made it into the rendered self text (≤4 KB) leave memory_index;
        # the rest stay there, so a full self section never hides a memory (found in the N3 measurement).
        self_index = [m for m in self._all("memories")
                      if m.get("subject") == identity["id"] and not m.get("superseded_by")
                      and m["id"] in rendered["self"]]
        return {"self": rendered["self"], "people": people, "people_index": people_index,
                "people_truncated": people_truncated, "views": views, "views_index": views_index,
                "views_truncated": views_truncated, "views_stale": rendered["stale"],
                "_self_index": self_index}

    def roster_render(self, company=None):
        """Write <home>/roster/self.md and views/<id>.md (0600 in 0700 dirs); returns the hash index."""
        r = self.roster_rendered(company=company)
        files = {"self.md": r["self"]}
        files.update({"views/" + roster._safe_name(pid): text for pid, text in r["views"].items()})
        return roster.write_rendered(self.home / "roster", files)

    def initialize(self, name, role, purpose=""):
        name, role = _text(name, "name"), _text(role, "role")
        with self._tx():
            if self._all("identity"):
                raise ValueError("This employee is already initialized.")
            identity = {"id": "employee-" + uuid.uuid4().hex, "name": name, "role": role,
                        "purpose": purpose, "created_at": timestamp()}
            self._save("identity", identity, "identity.created")
            self._save("entities", {"id": identity["id"], "name": name, "kind": "self",
                                   "description": role, "created_at": identity["created_at"],
                                   "updated_at": identity["created_at"]}, "entity.created")
            return identity

    def identity(self):
        identities = self._all("identity")
        if not identities:
            raise ValueError("Initialize an employee first.")
        return identities[0]

    def access_grant(self, path, mode, purpose, granted_by):
        """Declare assigned filesystem scope. This does not create an OS sandbox."""
        if self.run_id is not None:
            raise ValueError("Only an operator may grant employee access.")
        if not isinstance(path, (str, Path)):
            raise ValueError("path must be a directory path.")
        location = Path(_text(str(path), "path")).expanduser().resolve()
        if location.exists() and not location.is_dir():
            raise ValueError("Access grants must name directories, not files.")
        mode = _text(mode, "mode")
        if mode not in ("read", "write"):
            raise ValueError("Access mode must be read or write.")
        purpose = _text(purpose, "purpose")
        granted_by = _text(granted_by, "granted_by")
        with self._tx():
            self.identity()
            old = next((grant for grant in self._all("access_grants")
                        if grant["path"] == str(location) and grant["mode"] == mode), None)
            if (old and old["purpose"] == purpose and old["granted_by"] == granted_by
                    and old.get("revoked_at") is None):
                return old
            now = timestamp()
            record = dict(id=old["id"] if old else "access-" + uuid.uuid4().hex,
                          path=str(location), mode=mode, purpose=purpose, granted_by=granted_by,
                          created_at=old["created_at"] if old else now, updated_at=now,
                          revoked_at=None, revoked_by=None)
            return self._save("access_grants", record, "access.granted")

    def access_list(self):
        return sorted((grant for grant in self._all("access_grants") if grant.get("revoked_at") is None),
                      key=lambda grant: (grant["path"], grant["mode"]))

    def access_revoke(self, id, granted_by):
        if self.run_id is not None:
            raise ValueError("Only an operator may revoke employee access.")
        id, granted_by = _text(id, "id"), _text(granted_by, "granted_by")
        with self._tx():
            record = self._get("access_grants", id)
            if record.get("revoked_at") is not None:
                return record
            now = timestamp()
            record.update(revoked_at=now, revoked_by=granted_by, updated_at=now)
            return self._save("access_grants", record, "access.revoked")

    def _company_person(self, name, company=None):
        """Canonical company person ID for an ID, name or alias (design §5, C2); None when unresolved."""
        if company is None:
            company = roster.company_state_for_home(self.home)
        return roster.resolve(company, name)["id"]

    def _person_aliases(self, pid, company=None):
        """Every folded label that names company person ``pid``: its ID, name and aliases."""
        if company is None:
            company = roster.company_state_for_home(self.home)
        p = company.get("people", {}).get(pid) or {}
        labels = {pid, p.get("name") or ""} | set(p.get("aliases") or [])
        return {roster.fold_alias(x) for x in labels if x}

    def entity_put(self, id, name, kind, description=""):
        id, name, kind = _text(id, "id"), _text(name, "name"), _text(kind, "kind")
        if kind == "person":
            company = roster.company_state_for_home(self.home)
            canonical = self._company_person(id, company) or self._company_person(name, company)
            if canonical:
                raise roster.ProjectError("use_company_person", "%r is the company person %s; use that ID as "
                                          "a memory subject, and view_put for your own view of them"
                                          % (id, canonical), id=canonical)
        with self._tx():
            identity = self.identity()
            if id == identity["id"] or kind == "self":
                raise ValueError("The employee identity cannot be replaced through entity_put.")
            try:
                old = self._get("entities", id)
            except ValueError:
                old = {}
            now = timestamp()
            return self._save("entities", {"id": id, "name": name, "kind": kind, "description": description,
                                           "created_at": old.get("created_at", now), "updated_at": now}, "entity.put")

    def entities(self, query=""):
        return [item for item in self._all("entities") if query.casefold() in _search_text(item).casefold()]

    def memory_put(self, subject, predicate, value, kind="fact", source="", key=None, replaces=None):
        subject, predicate = _text(subject, "subject"), _text(predicate, "predicate")
        source = _text(source, "source")
        if kind not in ("fact", "belief", "preference", "procedure"):
            raise ValueError("Memory kind must be fact, belief, preference, or procedure.")
        if value is None or value == "":
            raise ValueError("Memory value must not be empty.")
        given = subject
        canonical = self._company_person(subject)
        if canonical:
            subject = canonical  # C2: store the canonical company ID; the alias as given stays in the audit
        replaces = self._resolve(replaces, "memories")
        payload = dict(subject=subject, predicate=predicate, value=value, kind=kind, source=source, replaces=replaces)
        with self._tx():
            if not canonical:
                self._get("entities", subject)
            previous = self._once("memory", key, payload)
            if previous:
                return self._get("memories", previous)
            old = self._get("memories", replaces) if replaces else None
            reserved = routines_mod().WORKING_CONTEXT
            if old and old.get("predicate") == reserved and predicate != reserved:
                # H1: the reserved record is replaced only by another working_context, so the dream guard below
                # always applies and the pin can't be dropped by an ordinary memory_put(replaces=...).
                raise ValueError("%s %s is the dream's working context: only a dream session replaces it, with "
                                 "predicate %s." % (reserved, old["id"], reserved))
            if predicate == reserved:
                self._check_working_context(subject, value, replaces, kind)
            same = (lambda x: x == subject or (canonical and roster.fold_alias(x) in self._person_aliases(canonical)))
            if old and (not same(old["subject"]) or old.get("superseded_by")):
                raise ValueError("Replace an active memory about the same subject.")
            memory = dict(payload, id="memory-" + uuid.uuid4().hex, created_at=timestamp(), superseded_by=None)
            self._save("memories", memory, "memory.created")
            if given != subject:
                self._audit("memory.subject_resolved", memory["id"], {"given": given, "subject": subject})
            if old:
                old["superseded_by"] = memory["id"]
                self._save("memories", old, "memory.superseded")
            self._key("memory", key, payload, memory["id"])
            return memory

    def _working_context(self):
        """D3: this employee's active working_context memory (newest), or None."""
        me = self.identity()["id"]
        found = [m for m in self._all("memories") if m.get("predicate") == routines_mod().WORKING_CONTEXT
                 and m.get("subject") == me and not m.get("superseded_by")]
        return found[-1] if found else None

    def _check_working_context(self, subject, value, replaces, kind):
        """Addendum (b): working_context is written only by the employee's own dream session, through memory_put;
        one active record, bounded, each new one replacing the last."""
        r = routines_mod()
        if self.run_id is None:
            raise ValueError("%s is written only by the employee's own dream session (memory_put with replaces), "
                             "never from the CLI or another home." % r.WORKING_CONTEXT)
        if subject != self.identity()["id"]:
            raise ValueError("%s is about yourself: use your own identity ID as subject." % r.WORKING_CONTEXT)
        moment = r.parse(timestamp())
        if not any(w["routine"]["kind"] == "dream" and r.due_state(w, moment) for w in self._routines()):
            raise ValueError("%s is written by a dream session: this session has no dream routine in "
                             "routines_due." % r.WORKING_CONTEXT)
        if kind not in r.WORKING_CONTEXT_KINDS:
            # H3: a reflection derived from reviewed material, never a new fact, evidence or authority.
            raise ValueError("%s is a derived reflection: kind must be %s, not %s."
                             % (r.WORKING_CONTEXT, " or ".join(r.WORKING_CONTEXT_KINDS), kind))
        if not isinstance(value, str) or len(value) > r.WORKING_CONTEXT_MAX:
            raise ValueError("%s is text of at most %d characters." % (r.WORKING_CONTEXT, r.WORKING_CONTEXT_MAX))
        current = self._working_context()
        if replaces and not current:
            raise ValueError("%s replaces only the active %s; there is none, so leave replaces out."
                             % (r.WORKING_CONTEXT, r.WORKING_CONTEXT))
        if current and replaces != current["id"]:
            raise ValueError("%s %s is active: pass replaces=%s so only one stays active."
                             % (r.WORKING_CONTEXT, current["id"], current["id"]))

    @staticmethod
    def _since_cursor(cursor, tag):
        """H2: parse a since= page cursor `<tag>|<created_at>|<tie-break>` into (created_at, tie-break)."""
        parts = cursor.split("|", 2) if isinstance(cursor, str) else []
        if len(parts) != 3 or parts[0] != tag or not parts[2]:
            raise ValueError("cursor must be the next_cursor of an earlier %s since= page."
                             % ("memory_search" if tag == "m" else "event_search"))
        moment = timestamp(parts[1])
        if tag == "e":
            if not parts[2].isdigit():
                raise ValueError("cursor must be the next_cursor of an earlier event_search since= page.")
            return moment, int(parts[2])
        return moment, parts[2]

    @staticmethod
    def _since_page(found, since, limit, name, tag, tie, budget=None):
        """E1 (P2): a since= retrieval in (created_at, tie-break) order with an explicit capped flag. unread_from
        is the created_at of the first item not returned: record it as an omission's `from`. H2: next_cursor
        (after the last item returned) continues the read without repeating or skipping items whose created_at
        ties. F2: with budget=(max_bytes, size, cut) the page also stops before the item that would exceed
        max_bytes (capped, same cursor rule); a first item too big alone is cut to fit and still counts as
        returned, so next_cursor moves past it. truncated_from = created_at of the first cut item, else null."""
        page = found[:limit]
        if budget is not None:
            page = Store._fit(page, *budget)
        n = len(page)
        capped = len(found) > n
        last = page[-1] if capped else None
        out = {name: page, "since": since, "capped": capped,
               "unread_from": found[n].get("created_at") if capped else None,
               "next_cursor": "%s|%s|%s" % (tag, last.get("created_at"), tie(last)) if capped else None}
        if budget is not None:
            out["truncated_from"] = next((item.get("created_at") for item in page if item.get("body_truncated")), None)
        return out

    # F2: compact retrieval. Bytes are counted on json.dumps(..., ensure_ascii=False) in UTF-8, as the MCP
    # server serializes results; PAGE_ENVELOPE is reserved for the page's own keys (since, cursor, flags).
    PAGE_ENVELOPE = 512
    MIN_MAX_BYTES = 1024

    @staticmethod
    def _json_size(value):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    @staticmethod
    def _check_compact(body_chars, max_bytes):
        for name, value, low in (("body_chars", body_chars, 0), ("max_bytes", max_bytes, Store.MIN_MAX_BYTES)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < low):
                raise ValueError("%s must be an integer of at least %d." % (name, low))

    @staticmethod
    def _preview(event, body_chars=None):
        """F2: an event with body_len and body_truncated; the body cut to body_chars characters when longer.
        A body_truncated event is a preview, not a read: event_get returns the original body."""
        body = event.get("body")
        if not isinstance(body, str):
            return dict(event, body_len=None, body_truncated=False)
        cut = body_chars is not None and len(body) > body_chars
        return dict(event, body=body[:body_chars] if cut else body, body_len=len(body), body_truncated=cut)

    @staticmethod
    def _cut_to_fit(event, max_bytes):
        """F2: the event with the longest body prefix that fits max_bytes (body_len keeps the original length).

        Sizes are serialized UTF-8 bytes, which a character count doesn't track (an accented letter is 2 bytes, a
        newline is escaped to 2), so the prefix length is found by bisection, not by subtracting bytes from
        characters. Size grows with the prefix, so the largest fitting prefix is exact; an event that doesn't fit
        even with an empty body comes back with an empty body."""
        body = event.get("body")
        if not isinstance(body, str):
            return event
        truncated = event.get("body_truncated")

        def trial(n):
            return dict(event, body=body[:n], body_truncated=truncated or n < len(body))

        def fits(n):
            return Store._json_size(trial(n)) + 2 <= max_bytes

        if fits(len(body)):
            return trial(len(body))
        low, high = 0, len(body)  # fits(low) or low == 0; not fits(high)
        while high - low > 1:
            mid = (low + high) // 2
            if fits(mid):
                low = mid
            else:
                high = mid
        return trial(low)

    @staticmethod
    def _fit(items, max_bytes, size, cut):
        """F2: the longest prefix of items whose serialized size fits max_bytes less the page envelope; at least
        one item, cut to fit (cut may be None: memories are never cut)."""
        room = max_bytes - Store.PAGE_ENVELOPE
        kept, used = [], 0
        for item in items:
            n = size(item) + 2
            if used + n > room:
                if not kept:
                    kept.append(cut(item, room) if cut else item)
                break
            kept.append(item)
            used += n
        return kept

    MEMORY_STOPWORDS = frozenset(
        "the and for are was were does did how what which who whom why when where with from that this these those "
        "into onto about after before over under than then them they their there here has have had not but can "
        "you your our its any all".split())
    MEMORY_ID = re.compile(r"memory-[0-9a-f]{0,32}")
    MEMORY_FIELDS = (("predicate", 3.0), ("subject", 2.0), ("value", 1.0), ("source", 0.5))

    @classmethod
    def _memory_terms(cls, text):
        # 5-character prefix stems; stopwords and tokens under 3 characters
        # never make an any-word match on their own.
        return {token[:5] for token in re.findall(r"[a-z0-9]+", str(text).casefold())
                if len(token) >= 3 and token not in cls.MEMORY_STOPWORDS}

    def memory_search(self, query="", subject=None, limit=30, include_superseded=False, match="any", since=None,
                      cursor=None, max_bytes=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Memory limit must be from 1 to 500.")
        self._check_compact(None, max_bytes)
        budget = (max_bytes, self._json_size, None) if max_bytes is not None else None
        if match not in ("all", "any"):
            raise ValueError('Memory match must be "all" or "any".')
        after = self._since_cursor(cursor, "m") if cursor is not None else None
        if since is not None:
            since = timestamp(since)
        elif after:
            since = after[0]
        words = query.casefold().split()
        subjects = None
        if subject is not None:
            # Design §5: subject=X also finds memories filed under any alias of X's company person.
            canonical = self._company_person(subject)
            subjects = self._person_aliases(canonical) if canonical else {roster.fold_alias(subject)}
        candidates = [memory for memory in reversed(self._all("memories"))
                      if (subjects is None or roster.fold_alias(memory["subject"]) in subjects)
                      and (include_superseded or not memory.get("superseded_by"))]
        out = [memory for memory in candidates if all(word in _search_text(memory).casefold() for word in words)]
        if since is not None:
            # P2 since=: memories created at or after since, oldest first, every query word required (no ranked
            # any-word fill), so a capped page has a well-defined earliest unread item.
            # H2: ties on created_at are ordered by ID, and a cursor resumes strictly after (created_at, ID).
            order = (lambda m: (str(m.get("created_at", "")), m["id"]))
            found = sorted((m for m in out if str(m.get("created_at", "")) >= since
                            and (after is None or order(m) > after)), key=order)
            return self._since_page(found, since, limit, "memories", "m", lambda m: m["id"], budget)
        # Fetch by ID: an exact memory ID (or a prefix of 15+ characters) puts
        # that memory ahead of memories that only cite it.
        probe = query.strip().casefold()
        if len(probe) >= 15 and self.MEMORY_ID.fullmatch(probe):
            hits = [memory for memory in out if memory["id"].startswith(probe)]
            out = hits + [memory for memory in out if not memory["id"].startswith(probe)]
        if match == "any" and len(out) < limit:
            terms = self._memory_terms(query)
            seen = {memory["id"] for memory in out}
            rest = [memory for memory in candidates if memory["id"] not in seen]
            if terms and rest:
                docs = [{field: self._memory_terms(memory.get(field, "")) for field, _ in self.MEMORY_FIELDS}
                        for memory in rest]
                df = {term: sum(any(term in doc[field] for field, _ in self.MEMORY_FIELDS) for doc in docs)
                      for term in terms}
                scored = []
                for rank, (memory, doc) in enumerate(zip(rest, docs)):
                    score = sum(weight * math.log(1 + len(docs) / df[term])
                                for term in terms if df[term] for field, weight in self.MEMORY_FIELDS
                                if term in doc[field])
                    if score > 0:
                        scored.append((-score, rank, memory))
                out += [memory for _, _, memory in sorted(scored, key=lambda item: item[:2])]
        if budget is not None:
            # F2 C1: query mode under max_bytes returns {memories, capped}; capped = the byte budget cut it.
            page = self._fit(out[:limit], *budget)
            return {"memories": page, "capped": len(page) < len(out[:limit])}
        return out[:limit]

    MEMORY_INDEX_LIMIT = 300
    MEMORY_INDEX_BYTES = 24 * 1024
    MEMORY_GIST = 80

    def _memory_index(self, shown):
        # Compact pointers to active memories not shown in full, newest first.
        shown = {memory["id"] for memory in shown}
        entries, size, truncated = [], 0, False
        for memory in reversed(self._all("memories")):
            if memory.get("superseded_by") or memory["id"] in shown:
                continue
            gist = " ".join(str(memory.get("value", "")).split())
            entry = dict(id=memory["id"], subject=memory["subject"], predicate=memory["predicate"],
                         kind=memory.get("kind"), date=str(memory.get("created_at", ""))[:10],
                         gist=gist[:self.MEMORY_GIST] + ("…" if len(gist) > self.MEMORY_GIST else ""))
            cost = len(_json(entry).encode())
            if len(entries) >= self.MEMORY_INDEX_LIMIT or size + cost > self.MEMORY_INDEX_BYTES:
                truncated = True
                break
            entries.append(entry)
            size += cost
        return entries, truncated

    EVIDENCE_LEVELS = ("attempted", "observed", "verified")
    # Inputs from the runtime itself, not from a colleague or operator.
    SYSTEM_SENDERS = ("clock", "runtime", "source-watcher")
    SYSTEM_EVENT_KINDS = ("reminder", "work_due", "wake", "source_update")

    def _evidence_item(self, item, work_id):
        """Check one evidence reference and return the stored form.

        The level a reference can support is decided here, not by the caller's label:
        the employee's own records are its own word (attempted), things the runtime or
        the filesystem recorded are observed, and only a colleague's or operator's
        input (or an answered question) can verify."""
        if not isinstance(item, dict):
            raise ValueError("Each evidence item must be an object with level and ref.")
        extra = set(item) - {"level", "ref", "note"}
        if extra:
            raise ValueError("Unexpected evidence field: %s." % ", ".join(sorted(extra)))
        level = item.get("level")
        if level not in self.EVIDENCE_LEVELS:
            raise ValueError("Evidence level must be attempted, observed, or verified.")
        ref = _text(item.get("ref"), "evidence ref")
        note = item.get("note", "")
        if not isinstance(note, str):
            raise ValueError("Evidence note must be text.")
        me = self.identity()["id"]
        check = {}
        if ref.startswith("event-"):
            event = self._get("events", ref)
            kind = "event"
            auth = event.get("auth")
            check = dict(sender=event.get("sender"), source=event.get("source"),
                         auth=auth.get("channel") if isinstance(auth, dict) else None)
            foreign = (event.get("sender") not in (me,) + self.SYSTEM_SENDERS
                       and event.get("kind") not in self.SYSTEM_EVENT_KINDS)
            # Only input stamped by an authenticated channel can verify; a sender
            # label alone is whatever the caller of feed() wrote (M22 N1).
            ceiling = "verified" if foreign and auth else "observed"
        elif ref.startswith("run-"):
            if ref == self.run_id:
                raise ValueError("The current run cannot be its own evidence; cite what it produced.")
            run = self._get("runs", ref)
            if run["status"] != "finished":
                raise ValueError("Run %s did not finish; it cannot support completion." % ref)
            kind, ceiling, check = "run", "observed", dict(status=run["status"])
        elif ref.startswith("question-"):
            question = self._get("questions", ref)
            kind = "question"
            answered = question.get("status") == "answered" and question.get("answer_event_id")
            answer_auth = None
            if answered:
                row = self.db.execute("SELECT data FROM events WHERE id=?", (question["answer_event_id"],)).fetchone()
                answer_auth = json.loads(row["data"]).get("auth") if row else None
            check = dict(status=question.get("status"), answer_event_id=question.get("answer_event_id"),
                         auth=answer_auth.get("channel") if isinstance(answer_auth, dict) else None)
            ceiling = ("verified" if answer_auth else "observed") if answered else "attempted"
            if question.get("status") == "answered_by_proxy":
                # M27 (iii) Q3: a proxy answer is secondhand; it never counts as verified.
                ceiling = "observed"
        elif ref.startswith(("message-", "memory-", "work-")):
            kind = ref.split("-", 1)[0]
            if kind == "work" and ref == work_id:
                raise ValueError("Work cannot be its own evidence.")
            record = self._get({"message": "messages", "memory": "memories", "work": "works"}[kind], ref)
            ceiling = "attempted"
            if kind == "message":
                # A delivery recorded by an authenticated adapter is observed;
                # delivery is not the colleague's acknowledgement, so never verified (M22 N2).
                delivered = [d for d in record.get("deliveries") or [] if d.get("status") in ("delivered", "reconciled:delivered")]
                if delivered:
                    ceiling = "observed"
                    check = dict(delivered_by=delivered[0].get("channel"), delivered_at=delivered[0].get("at"))
        elif ref.startswith("/"):
            path = Path(ref).resolve()
            if not path.is_file():
                raise ValueError("Evidence file %s does not exist." % ref)
            runs = (self.home / "runs").resolve()
            if path == runs or runs in path.parents:
                raise ValueError("Own session transcripts are the employee's own word, not evidence.")
            data = path.read_bytes()
            kind, ceiling = "file", "observed"
            check = dict(path=str(path), sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        elif ref.startswith(("receipt:", "https://")) and len(ref.split(":", 1)[1].strip("/")) > 0:
            # Free-form strings nothing checks: citable, but they cannot complete
            # work. Cite the delivered message- ID or a file instead (M22 N2).
            kind, ceiling = "external", "attempted"
        else:
            raise ValueError("Evidence ref must be an event-, run-, question-, message-, memory- or work- ID, "
                             "an absolute file path, receipt:… or https://…. A relative file path resolves "
                             "against your workspace %s and must name an existing file." % self.workspace)
        if self.EVIDENCE_LEVELS.index(level) > self.EVIDENCE_LEVELS.index(ceiling):
            raise ValueError("%s evidence %s supports at most %s. %s" % (
                kind, ref, ceiling, self._ceiling_hint(kind, ceiling)))
        return dict(level=level, ref=ref, ref_kind=kind, note=note, check=check,
                    recorded_at=timestamp(), recorded_by_run=self.run_id)

    def _ceiling_hint(self, kind, ceiling):
        """Backlog 30 (R4): say what to cite instead when a ref is capped below the asked level."""
        if ceiling == "observed":
            return "verified needs a colleague's reply or an authenticated answer (its event- ID or the answered question- ID)."
        if kind in ("memory", "work"):
            return "Your own %s is your own word; cite the source it records (event, file or delivered message) instead." % kind
        if kind == "message":
            return "An undelivered message is only attempted; cite it once delivered, or the colleague's reply."
        if kind == "question":
            return "An unanswered question is only attempted; cite it once answered, or the answer's event."
        if kind == "external":
            return "Free-form receipts are unchecked; cite the delivered message- ID or a file instead."
        return "Cite a colleague's reply, a recorded event or a file instead."

    def work_put(self, title=None, description=UNSET, id=None, status=UNSET, waiting_for=UNSET,
                 wake_at=UNSET, priority=UNSET, note=UNSET, key=None, evidence=UNSET, cancel_reason=UNSET,
                 override=None, publish=UNSET, origin=UNSET):
        # Backlog 24 (K2/K3): expand short ID prefixes before any check and before the idempotency payload.
        id = self._resolve(id, "works")
        if isinstance(waiting_for, str):
            waiting_for = self._resolve_text(waiting_for)
        if isinstance(evidence, list):
            # Backlog 30 (R2): workspace-relative file refs too, so both forms replay as one call.
            evidence = [dict(item, ref=self._resolve_evidence_ref(self._resolve(item["ref"])))
                        if isinstance(item, dict) and "ref" in item
                        else item for item in evidence]
        supplied = {name: value for name, value in dict(title=title, description=description, status=status,
                    waiting_for=waiting_for, wake_at=wake_at, priority=priority, note=note).items()
                    if value is not UNSET and not (name == "title" and value is None)}
        # M27 (iv) Board: publish=<project> projects title/status/waiting_for/evidence refs/priority/origin to
        # that project's backlog (board.publish_drain); publish="" unpublishes. Never description or note.
        if publish is not UNSET:
            if publish is None:
                publish = ""
            if not isinstance(publish, str) or (publish and not capture.PROJECT_NAME.fullmatch(publish)):
                raise ValueError("publish must be a project name (lowercase letters, digits, - or _) or \"\" to unpublish.")
            if publish in capture.RESERVED_PROJECTS:
                raise ValueError("%s is a reserved name, not a project." % publish)
            supplied["publish"] = publish
        if origin is not UNSET:
            if origin not in (None, "self", "assigned"):
                raise ValueError("origin must be self or assigned.")
            supplied["origin"] = origin
        if "wake_at" in supplied and supplied["wake_at"] is not None:
            supplied["wake_at"] = timestamp(supplied["wake_at"])
        payload = dict(supplied, id=id)
        # Omit absent additions so keys recorded before M14 still reconcile.
        if evidence is not UNSET:
            if not isinstance(evidence, list):
                raise ValueError("evidence must be a list of {level, ref, note} objects.")
            payload["evidence"] = evidence
        if cancel_reason is not UNSET:
            payload["cancel_reason"] = cancel_reason
        if override is not None:
            if self.run_id is not None:
                raise ValueError("Override is an operator action and is not available inside an employee run.")
            payload["override"] = _text(override, "override")
        with self._tx():
            previous = self._once("work", key, payload)
            if previous:
                return self._get("works", previous)
            now = timestamp()
            old = self._get("works", id) if id is not None else None
            if old and isinstance(old.get("routine"), dict):
                # RTN P1: a routine's status and wake_at change only through routine_rearm (the proof of review)
                # or the `routine` CLI (restore/teardown); its tag is never writable here.
                changed = [name for name in ("status", "wake_at") if name in supplied and supplied[name] != old.get(name)]
                if changed:
                    raise ValueError("%s is a %s routine: %s cannot change through work_put. Re-arm it with routine_rearm "
                                     "(after doing its review); close or restore it with `synthetics routine teardown|restore`."
                                     % (old["id"], old["routine"].get("kind"), " and ".join(changed)))
                # P2-fix (3): waiting_for naming an open question would link it outside the guarded status/wake args.
                if isinstance(supplied.get("waiting_for"), str) and re.search(r"\bquestion-[A-Za-z0-9_-]+",
                                                                             supplied["waiting_for"]):
                    raise ValueError(self.ROUTINE_LINK % (old["id"], old["routine"].get("kind"), "a question (waiting_for)",
                                                          "question"))
            record = old.copy() if old else dict(id="work-" + uuid.uuid4().hex, title="", description="", status="ready",
                        waiting_for="", waiting_question_id=None, waiting_question_ids=[], wake_at=None, priority=0, note="", created_at=now)
            record.update(supplied)
            before = old["status"] if old else None
            history = list(record.get("evidence") or [])
            if evidence is not UNSET:
                history += [self._evidence_item(item, record["id"]) for item in evidence]
            if override is not None:
                history.append(dict(level="operator-override", ref=None, ref_kind="override", note=payload["override"],
                                    check={}, recorded_at=now, recorded_by_run=None))
            if history:
                record["evidence"] = history
            if before in ("done", "cancelled") and record["status"] in ("ready", "waiting"):
                # Reopened: earlier evidence stays as history but cannot close it again.
                record["reopened_at"] = now
                record["evidence_floor"] = len(history)
            if record["status"] == "done" and before != "done":
                current = history[record.get("evidence_floor", 0):]
                if not any(item["level"] in ("observed", "verified", "operator-override") for item in current):
                    raise ValueError("Completing work needs evidence: pass evidence=[{level, ref, note}] citing an "
                                     "event, run, question, file or receipt. attempted alone does not complete work.")
            if record["status"] == "cancelled" and before != "cancelled":
                reason = cancel_reason if cancel_reason is not UNSET else payload.get("override")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("Cancelling work needs cancel_reason: say why the commitment is dropped.")
                record["cancellation"] = dict(reason=reason.strip(), recorded_at=now, recorded_by_run=self.run_id)
            record["title"] = _text(record["title"], "title")
            for field in ("description", "waiting_for", "note"):
                if not isinstance(record[field], str):
                    raise ValueError("%s must be text." % field)
            if record["status"] not in ("ready", "waiting", "done", "cancelled"):
                raise ValueError("Work status must be ready, waiting, done, or cancelled.")
            if not isinstance(record["priority"], int) or isinstance(record["priority"], bool):
                raise ValueError("Priority must be an integer.")
            if record["status"] == "waiting":
                if not record["waiting_for"] and not record["wake_at"]:
                    raise ValueError("Waiting work needs waiting_for or wake_at.")
                # A readable explanation is not the durable correlation ID. Preserve
                # that ID while notes or wording change, and recover it for older homes.
                linked = self._waiting_question(old) if old else None
                referenced = []
                for question_id in re.findall(r"\bquestion-[A-Za-z0-9_-]+", record["waiting_for"]):
                    question = self._get("questions", question_id)
                    if question["status"] != "open":
                        raise ValueError("Cannot wait for a question that has already been answered.")
                    # Docket P1a (§2a): one open question this home asked may block several works (a shared
                    # blocker). It keeps its origin work_id; an answer wakes every linked work.
                    referenced.append(question)
                # M27 (iii): a work may wait on several open questions (parallel asks); any answer wakes it.
                ids = [qid for qid in (self._linked_ids(old) if old else [])
                       if (self._find("questions", qid) or {}).get("status") == "open"]
                for question in sorted(referenced, key=lambda item: item["created_at"]):
                    if question["id"] not in ids:
                        ids.append(question["id"])
                linked = ids[-1] if ids else None
                if self.run_id and supplied.get("status") == "waiting" and linked is None:
                    # A reply can arrive after the worker's input snapshot, wake this
                    # work, and then be overwritten by the worker's old intent to wait.
                    # Keep ready until a session has actually received that answer.
                    captured = self._get("runs", self.run_id)["event_ids"]
                    for question in self._all("questions"):
                        answer_id = question.get("answer_event_id")
                        if question.get("work_id") != record["id"] or not answer_id or answer_id in captured:
                            continue
                        answer = self.db.execute("SELECT processed_run FROM events WHERE id=?", (answer_id,)).fetchone()
                        if answer is not None and answer["processed_run"] is None:
                            raise ValueError("New answer arrived during this session or awaits this session's input batch; "
                                             "leave work ready for the next session or ask a new correlated question.")
                    # M23: the same guard for a work reference that woke this work.
                    woke = (old or {}).get("woken_by_ref") or {}
                    if woke.get("event") and woke["event"] not in captured:
                        row = self.db.execute("SELECT processed_run FROM events WHERE id=?", (woke["event"],)).fetchone()
                        if row is not None and row["processed_run"] is None:
                            raise ValueError("A message referencing this work (%s) woke it during this session and awaits "
                                             "the next session's input batch; leave work ready." % woke["event"])
                record["waiting_question_id"] = linked
                record["waiting_question_ids"] = ids
                if record["wake_at"] and record["wake_at"] <= now:
                    raise ValueError("A waiting work wake_at must be in the future.")
            elif old is None or supplied.get("status") in ("ready", "done", "cancelled"):
                record["waiting_for"] = ""
                record["waiting_question_id"] = None
                record["waiting_question_ids"] = []
                record["wake_at"] = None
            record["updated_at"] = now
            if record.get("publish") or record.get("published"):
                # Reconciled by board.publish_drain on the engine's next step and at run finish.
                self.db.execute("INSERT INTO capture_meta(k,v) VALUES ('publish_dirty','1') "
                                "ON CONFLICT(k) DO UPDATE SET v='1'")
            self._save("works", record, "work.put")
            self._key("work", key, payload, record["id"])
            return record

    def work_get(self, id):
        work = self._work_view(self._get("works", self._resolve(id, "works")))
        # Docket P1a (C3): for each linked question, the other waiting works that share it. Present only when
        # some linked question is shared, so work_get still equals work_put's record otherwise.
        shared = {}
        for qid in work["waiting_question_ids"]:
            others = [other["id"] for other, linked in self._waiting_links()
                      if other["id"] != work["id"] and qid in linked]
            if others:
                shared[qid] = sorted(others)
        return dict(work, shared_with=shared) if shared else work

    def docket(self, person=None, now=None):
        """Docket P1a: read-only view of this home's own open questions to one person (default: default_human)."""
        from . import docket
        view = docket.build(self, person, now)
        return dict(view, text=docket.render(view))

    def _waiting_question(self, work):
        """Resolve pre-link records from their newest open question, never the label."""
        if not work or work["status"] != "waiting":
            return None
        if "waiting_question_id" in work:
            return work["waiting_question_id"]
        questions = [q for q in self._all("questions")
                     if q.get("work_id") == work["id"] and q["status"] == "open"]
        return questions[-1]["id"] if questions else None

    def _linked_ids(self, work):
        """M27 (iii): every open question a waiting work waits on (legacy records fold to their single link)."""
        if not work or work["status"] != "waiting":
            return []
        if "waiting_question_ids" in work:
            return list(work["waiting_question_ids"] or [])
        single = self._waiting_question(work)
        return [single] if single else []

    @staticmethod
    def _recipient(question):
        """P3: the canonical recipient - the routed identity (to_id) when known, else the address asked."""
        return question.get("to_id") or question["to"]

    @staticmethod
    def _is_routine(work):
        """RTN P2-fix: a dedicated routine work (docs/routines.md) moves only via routine_rearm, the engine quarantine
        or the `routine` CLI; questions, reminders and work refs never change its status or wake_at."""
        return bool(work) and isinstance(work.get("routine"), dict)

    ROUTINE_LINK = ("%s is a %s routine: %s cannot be linked to it (it changes only through routine_rearm, the "
                    "quarantine or `synthetics routine`). Link the %s to the real assignment work, or leave it unlinked.")

    def _work_view(self, work):
        work.setdefault("waiting_question_id", self._waiting_question(work))
        work.setdefault("waiting_question_ids", self._linked_ids(work))
        return work

    def work_list(self, status=None):
        items = [self._work_view(work) for work in self._all("works")]
        if status == "open":
            items = [item for item in items if item["status"] in ("ready", "waiting")]
        elif status is not None:
            items = [item for item in items if item["status"] == status]
        return sorted(items, key=lambda item: (-item["priority"], item["updated_at"], item["id"]))

    def runnable_work(self):
        # RTN P1 (D1): a quarantined routine never starts a session; only routine_rearm or `routine restore` clears it.
        return [work for work in self.work_list("ready") if not (work.get("routine") or {}).get("quarantine")]

    def _send(self, to, body, work_id, key, question, thread=None, in_reply_to=None, recipient_work_id=None,
              project=None, work_ids=None, relays=None):
        to, body, key = _text(to, "to"), _text(body, "body"), _text(key, "key")
        work_id = self._resolve(work_id, "works")
        in_reply_to = self._resolve(in_reply_to)  # own message/question/event; recipient_work_id never (K5)
        payload = dict(to=to, body=body, work_id=work_id)
        if work_ids is not None:
            # Docket P1a (§2a): link several of my works to this one question at ask time (a shared blocker).
            if not question:
                raise ValueError("work_ids applies to questions only.")
            if not isinstance(work_ids, list) or not work_ids:
                raise ValueError("work_ids must be a non-empty list of your own work IDs.")
            resolved = []
            for item in work_ids:
                item = self._resolve(_text(item, "work_ids item"), "works")
                if item not in resolved:
                    resolved.append(item)
            payload["work_ids"] = resolved
            if work_id is None:
                work_id = payload["work_id"] = resolved[0]
        if relays is not None:
            # Docket P1b (§1a): asker-side IDs of received questions this question carries onward.
            if not question:
                raise ValueError("relays applies to questions only.")
            if not isinstance(relays, list) or not relays:
                raise ValueError("relays must be a non-empty list of question IDs from received messages.")
            payload["relays"] = list(dict.fromkeys(_text(item, "relays item") for item in relays))
        # VIS G3 delta (backlog 52): project="" opts out of the default tag taken from the sender's own work.
        opt_out = isinstance(project, str) and not project.strip()
        if opt_out:
            project = None
        if project is not None:
            # M21 phase 2 (D3.1 item 2): one project tag, carried to the recipient as structured data. The sender
            # must hold a write grant on it; its own body is captured into P as direction "out".
            project = _text(project, "project")
            if not capture.is_project_name(project):
                raise ValueError("project must be a project name (lowercase letters, digits, - and _).")
            if not capture.project_path(self, project).exists():
                raise ValueError("No project %s." % project)
            if not capture.sender_may_write(self, project, self.identity()["id"]):
                raise ValueError("You have no write grant on project %s, so you cannot tag a message for it."
                                 % project)
            payload["project"] = project
        if recipient_work_id is not None:
            # M23: the recipient's work, carried as an opaque hint. work_id stays the
            # sender's own work.
            recipient_work_id = _text(recipient_work_id, "recipient_work_id")
            if not WORK_ID.match(recipient_work_id):
                raise ValueError("recipient_work_id must be a work ID (work-<32 hex>) from the recipient.")
            payload["recipient_work_id"] = recipient_work_id
        # Omit absent additions: old operation keys were recorded without these
        # fields, and must still reconcile after an upgrade.
        if thread is not None:
            payload["thread"] = _text(thread, "thread")
        if in_reply_to is not None:
            payload["in_reply_to"] = _text(in_reply_to, "in_reply_to")
        namespace = "question" if question else "message"
        with self._tx():
            old = self._once(namespace, key, payload)
            if old:
                return self._get("questions" if question else "messages", old)
            if recipient_work_id is not None and self._find("works", recipient_work_id):
                raise ValueError("recipient_work_id %s is your own work; use work_id for your own work and "
                                 "recipient_work_id only for the recipient's." % recipient_work_id)
            links = [work_id] if work_id else []
            links += [item for item in payload.get("work_ids") or [] if item not in links]
            works = [self._work_view(self._get("works", item)) for item in links]
            work = works[0] if works else None
            if question and any(item["status"] in ("done", "cancelled") for item in works):
                raise ValueError("Cannot block finished work with a question.")
            for item in works:
                if question and self._is_routine(item):
                    raise ValueError(self.ROUTINE_LINK % (item["id"], item["routine"].get("kind"), "a question",
                                                          "question"))
            links = [self._relay_source(item) for item in payload.get("relays") or []]
            # VIS G3 delta: with no explicit project (and no "" opt-out), a message linked to the sender's own
            # work published to P is tagged P, by "work". Never refuses the send: when P is gone or the sender
            # cannot write P, the message goes untagged and the result says why (not stored).
            default = (self._work_project_default(work) if project is None and not opt_out and work is not None
                       else None)
            tag_by = "sender"
            extra = {}
            if default is not None and default["applied"]:
                project, tag_by = default["project"], "work"
                extra = {"project": project, "project_by": "work"}
            now = timestamp()
            question_id = "question-" + uuid.uuid4().hex if question else None
            message = dict(payload, **extra, id="message-" + uuid.uuid4().hex, sender=self.identity()["id"],
                           kind=namespace, question_id=question_id, created_at=now)
            self._save("messages", message, "outbox.queued")
            if question:
                result = dict(payload, **extra, id=question_id, message_id=message["id"], created_at=now,
                              status="open", answer_event_id=None)
                if links:
                    result["relays"] = links
                self._save("questions", result, "question.asked")
                self._queue_hops(result, "with-person", now)
                for work in works:
                    # M27 (iii): re-asking the same canonical recipient supersedes that link; questions to other
                    # recipients stay linked, so any of their answers wakes the work. Superseded questions stay open.
                    ids = []
                    for qid in self._linked_ids(work):
                        linked = self._find("questions", qid)
                        if linked and linked["status"] == "open" and self._recipient(linked) != self._recipient(result):
                            ids.append(qid)
                    ids.append(question_id)
                    work.update(status="waiting", waiting_for=", ".join(ids), waiting_question_id=question_id,
                                waiting_question_ids=ids, wake_at=None, updated_at=now)
                    self._save("works", work, "work.waiting")
            else:
                result = message
            self._key(namespace, key, payload, result["id"])
            # M21 A4/B3: our own message on a bound thread is captured as direction "out".
            tag = {"project": project, "by": tag_by} if project is not None else None
            captured = (capture.enqueue(self, dict(message, source="outbox"), "out", tag)
                        if thread is not None or tag is not None else [])
        if captured:
            try:
                capture.capture_drain(self)
            except Exception:  # noqa: BLE001 - the message is queued; pending rows retry and stay visible
                pass
        if default is not None:
            result = dict(result, project_default=default)
        return result

    def _work_project_default(self, work):
        """VIS G3 delta: the project a message linked to my own `work` defaults to. None when the work is not
        published; otherwise {work_id, project, applied, reason?} with reason unknown-project or no-write-grant."""
        project = work.get("publish") or None
        if not project:
            return None
        note = {"work_id": work["id"], "project": project, "applied": False}
        if not capture.is_project_name(project) or not capture.project_path(self, project).exists():
            return dict(note, reason="unknown-project")
        if not capture.sender_may_write(self, project, self.identity()["id"]):
            return dict(note, reason="no-write-grant")
        return dict(note, applied=True)

    def question_ask(self, to, body, work_id=None, key=None, thread=None, in_reply_to=None, recipient_work_id=None,
                     project=None, work_ids=None, relays=None):
        return self._with_route(self._send(to, body, work_id, key, True, thread, in_reply_to, recipient_work_id,
                                           project, work_ids, relays))

    def message_send(self, to, body, work_id=None, key=None, thread=None, in_reply_to=None, recipient_work_id=None,
                     project=None):
        return self._with_route(self._send(to, body, work_id, key, False, thread, in_reply_to, recipient_work_id,
                                           project))

    def _with_route(self, result):
        """Backlog 26 (Codex ruling event-ba3b2852): an unroutable `to` is still queued, never refused; the caller
        sees which channels carry it, and a warning when none do. Not stored; homes without channels unchanged."""
        routed = self.routed_by(result["to"])
        if routed is None:
            return result
        result = dict(result, routed_by=routed)
        if not routed:
            result["warning"] = ("No configured channel routes %s from this home, so this stays queued and "
                                 "undelivered until a route is added. Check the address (roster_get) or ask the "
                                 "manager about the route; do not resend." % result["to"])
        return result

    def outbox_list(self, status=None, limit=None):
        """Queued messages, each with `delivery` = the M25 summary state.

        status filters on that summary (pending|claimed is reported as pending,
        uncertain, failed, delivered); limit keeps the newest N (backlog item 6)."""
        items = []
        for message in self._all("messages"):
            message["delivery"] = self.delivery_summary(message)
            if status is None or message["delivery"] == status:
                items.append(message)
        if limit is not None:
            items = items[-max(0, int(limit)):] if int(limit) > 0 else []
        return items

    STUCK_AFTER_SECONDS = 600
    STUCK_WINDOW_SECONDS = 48 * 3600
    STUCK_ITEMS = 5

    def outbox_stuck(self, now=None):
        """M27 (ii): own outbox items still pending or uncertain after 10 minutes, newest first.

        Only items younger than 48 hours are listed (older leftovers are only counted), so
        pre-contract backlog does not crowd the snapshot. routed_by names every channel whose routes
        include the recipient; an empty list means no configured channel will ever carry it."""
        result = self.outbox_stuck_from(self._all("messages"), self._all("channels"), now)
        del result["items_all"]
        return result

    @classmethod
    def outbox_stuck_from(cls, messages, channels, now=None):
        """outbox_stuck over plain rows (doctor reads them read-only); items_all is uncapped."""
        now = now or datetime.now(timezone.utc)
        items, older = [], 0
        for message in messages:
            state = cls.delivery_summary(message)
            if state not in ("pending", "uncertain"):
                continue
            try:
                created = datetime.fromisoformat(message["created_at"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            age = (now - created).total_seconds()
            if age < cls.STUCK_AFTER_SECONDS:
                continue
            if age > cls.STUCK_WINDOW_SECONDS:
                older += 1
                continue
            items.append(dict(id=message["id"], to=message["to"], kind=message.get("kind"),
                              question_id=message.get("question_id"), state=state, age_minutes=int(age // 60),
                              routed_by=sorted(c["id"] for c in channels if message["to"] in (c.get("routes") or []))))
        items.sort(key=lambda item: item["age_minutes"])
        return dict(items=items[:cls.STUCK_ITEMS], count=len(items), older_than_48h=older, items_all=items)

    def question_routed(self, question_id, to_id, routed_by):
        """Record the authenticated identity a delivery adapter routed this question to.

        Adapter/CLI only, never MCP. A question addressed by alias (for example
        "codex-manager") can then be answered by the canonical sender ID that
        the adapter stamps on the reply. Idempotent; never overwrites a different
        to_id. Audited as question.routed.
        """
        question_id, to_id = _text(question_id, "question_id"), _text(to_id, "to_id")
        routed_by = _text(routed_by, "routed_by")
        with self._tx():
            return self._question_routed(question_id, to_id, routed_by)

    def _question_routed(self, question_id, to_id, routed_by):
        # Inside the caller's transaction (question_routed, or feed(route=...) for issue #9).
        question = self._get("questions", question_id)
        current = question.get("to_id")
        if current == to_id:
            return question
        if current:
            raise ValueError("Question %s is already routed to %s; refusing to re-stamp it as %s."
                             % (question_id, current, to_id))
        question.update(to_id=to_id, routed_by=routed_by, routed_at=timestamp())
        return self._save("questions", question, "question.routed")

    # -- Docket P1b: relay links and hop notices (design §1, Codex rulings Q1/Q2, C2/C4) --

    HOPS = ("with-manager", "with-person", "answered-pending-relay")
    RELAY_SOURCES = ("fieldpost",)  # C2: Fieldpost-sourced events may carry auth null (pre-M22 backfill)

    @staticmethod
    def _headers(body):
        """The provenance header block of a relayed event body ("key: value" lines before the first ---)."""
        headers = {}
        for line in (body or "").splitlines():
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                headers.setdefault(key.strip(), value.strip())
        return headers

    def _relay_source(self, question_id):
        """The received event that carried `question_id` (the asker's ID) into this home, as a relay link.

        It must be a message whose header names that question and comes from an authenticated channel, or from
        Fieldpost with auth null (C2: link_auth null, shown that way). Anything else is unknown_relay."""
        question_id = _text(question_id, "relays item")
        if not re.fullmatch(r"question-[0-9a-f]{32}", question_id):
            raise ValueError("unknown_relay: %s is not a full question ID (question-<32 hex>) from a received "
                             "message's question-id: header." % question_id)
        me = self.identity()["id"]
        found = None
        for row in self.db.execute("SELECT data FROM events ORDER BY seq"):
            event = json.loads(row["data"])
            if event.get("kind") != "message" or event.get("sender") in (None, me):
                continue
            if self._headers(event.get("body")).get("question-id") != question_id:
                continue
            if event.get("auth") is None and event.get("source") not in self.RELAY_SOURCES:
                continue
            found = event
        if found is None:
            raise ValueError("unknown_relay: no received message in this home carries question %s from an "
                             "authenticated channel (or Fieldpost)." % question_id)
        return dict(question_id=question_id, asker=found["sender"], event_id=found["id"],
                    link_auth=found.get("auth"))

    def _queue_hop(self, question, link, hop, now):
        """Q1: the store queues one structured hop notice to the original asker, in the caller's transaction.
        No free text: the body is a fixed rendering of the fields, and Fieldpost delivers only `hop`."""
        person = self._recipient(question) if hop == "with-person" else None
        notice = dict(question_id=link["question_id"], hop=hop, person=person, since=now, via=question["id"])
        body = "hop: %s %s%s since %s via %s" % (link["question_id"], hop, " " + person if person else "", now,
                                                 question["id"])
        message = dict(id="message-" + uuid.uuid4().hex, to=link["asker"], body=body, work_id=None,
                       sender=self.identity()["id"], kind="hop", question_id=None, hop=notice, created_at=now)
        self._save("messages", message, "outbox.queued")
        return message

    def _queue_hops(self, question, hop, now, links=None):
        for link in links if links is not None else question.get("relays") or []:
            self._queue_hop(question, link, hop, now)

    def question_relays(self, question_id, add):
        """Link already-received questions (asker-side IDs) to my own question, which then carries them onward.

        The question must be mine and open or answered (an answered one owes the relay: answered-pending-relay).
        Each new link queues one hop notice to its asker. Audited as question.relays."""
        question_id = self._resolve(_text(question_id, "question_id"), "questions")
        if not isinstance(add, list) or not add:
            raise ValueError("add must be a non-empty list of question IDs from received messages.")
        with self._tx():
            question = self._get("questions", question_id)
            asked = self._find("messages", question.get("message_id") or "")
            if asked is not None and asked.get("sender") not in (None, self.identity()["id"]):
                raise ValueError("Only the asker can add relay links to a question.")
            if question["status"] == "withdrawn":
                raise ValueError("Question %s is withdrawn; it carries nothing onward." % question_id)
            links = list(question.get("relays") or [])
            have = {link["question_id"] for link in links}
            new = []
            for item in add:
                link = self._relay_source(item)
                if link["question_id"] not in have:
                    have.add(link["question_id"])
                    new.append(link)
            if not new:
                return question
            question["relays"] = links + new
            self._save("questions", question, "question.relays")
            hop = "with-person" if question["status"] == "open" else "answered-pending-relay"
            self._queue_hops(question, hop, timestamp(), new)
            return question

    def question_hop(self, question_id, hop, since, via, sender, person=None):
        """Adapter/CLI only (like question_routed): record where my open question now is, as reported by its
        recipient. Never wakes work, never answers, never changes status (C4). Idempotent on (via, hop); a
        notice older than the current hop is ignored. A notice from anyone but the recipient, or for a question
        that is not open, is hop_rejected and changes nothing (C2). Audited as question.hop."""
        if self.run_id is not None:
            raise ValueError("Hop notices are adapter operations, not model tools.")
        question_id, sender = _text(question_id, "question_id"), _text(sender, "sender")
        via, since = _text(via, "via"), timestamp(_text(since, "since"))
        if hop not in self.HOPS:
            raise ValueError("hop_rejected: hop must be one of %s." % (self.HOPS,))
        if (hop == "with-person") != bool(person):
            raise ValueError("hop_rejected: person is required for with-person and only for it.")
        with self._tx():
            question = self._find("questions", question_id)
            if question is None:
                raise ValueError("hop_rejected: no question %s in this home." % question_id)
            if sender not in (question["to"], question.get("to_id")):
                raise ValueError("hop_rejected: %s is not the recipient of %s." % (sender, question_id))
            if question["status"] != "open":
                raise ValueError("hop_rejected: question %s is %s." % (question_id, question["status"]))
            history = list(question.get("hop_history") or [])
            if any(h.get("via") == via and h.get("hop") == hop for h in history):
                return dict(question, already=True)
            if question.get("hop_since") and since < question["hop_since"]:
                return dict(question, stale=True)
            history.append(dict(hop=hop, person=person, since=since, via=via, at=timestamp()))
            question.update(hop=hop, hop_person=person, hop_since=since, hop_via=via, hop_history=history)
            return self._save("questions", question, "question.hop")

    @staticmethod
    def hop_label(question):
        """with-david / with-manager / answered-pending-relay, or None when no notice has arrived."""
        hop = question.get("hop")
        if hop == "with-person":
            return "with-%s" % question.get("hop_person")
        return hop

    RELAY_OWED_WARN_MINUTES = 240  # Q4: doctor warns when an answered relay has waited 4 h

    def relay_owed(self, now=None):
        """Relays I still owe: my answered relay questions whose carried questions I have not replied to since."""
        return self.relay_owed_from(self._all("messages"), self._all("questions"), now)

    @staticmethod
    def relay_owed_from(messages, questions, now=None):
        """relay_owed over plain rows (doctor reads them read-only)."""
        now = now or datetime.now(timezone.utc)
        replies = {}
        for message in messages:
            if message.get("kind") == "message" and message.get("in_reply_to"):
                replies.setdefault(message["in_reply_to"], []).append(message["created_at"])
        items = []
        for question in questions:
            if question["status"] not in ("answered", "answered_by_proxy") or not question.get("relays"):
                continue
            answered = question.get("answered_at") or ""
            for link in question["relays"]:
                if any(at >= answered for at in replies.get(link["question_id"], [])):
                    continue
                try:
                    age = int((now - datetime.fromisoformat(answered.replace("Z", "+00:00"))).total_seconds() // 60)
                except ValueError:
                    age = None
                items.append(dict(question_id=link["question_id"], asker=link["asker"], via=question["id"],
                                  answered_at=answered, age_minutes=age, link_auth=link.get("link_auth")))
        items.sort(key=lambda item: item["answered_at"])
        return items

    def question_list(self, status=None):
        return [q for q in self._all("questions") if status is None or q["status"] == status]

    def reminders_list(self, include_fired=False):
        return [r for r in self._all("reminders") if include_fired or r["fired_at"] is None]

    def audit_note(self, action, record_id, data):
        """Record an audited CLI action taken in this home (e.g. a session's company write)."""
        with self._tx():
            self._audit(action, record_id, data)

    # -- M28 D14: mandatory lifecycle ledger for delegated launchd effects (never best effort) --

    def launch_op_begin(self, row):
        """Durably record a delegated launchd op as pending BEFORE any effect. Raises on any failure, so the caller
        executes nothing. While an op on the same label is pending a new one is refused (LaunchOpPending): an
        unsettled effect is reconciled from evidence, never blindly repeated."""
        with self._tx():
            for old in self._all("launch_ops"):
                if old.get("status") == "pending" and old.get("label") == row.get("label"):
                    raise LaunchOpPending(old["id"], old.get("label"))
            record = dict(row, id="op-" + uuid.uuid4().hex, status="pending", outcome=None,
                          created_at=timestamp(), settled_at=None)
            self._save("launch_ops", record, "launch_op.requested")
        return record

    def launch_op_settle(self, op_id, outcome, warrant=None, **data):
        """Record the outcome of a pending op (confirmed / failed / uncertain) in one transaction. With a
        ``warrant`` (D16a: a session reconciling via `authority settle`) the actor's run and delegation chain are
        re-checked inside the same write transaction; a refusal rolls back and the row stays pending."""
        if outcome not in ("confirmed", "failed", "uncertain"):
            raise ValueError("outcome must be confirmed, failed or uncertain")
        if warrant is None:
            return self._launch_op_settle_tx(op_id, outcome, None, data)
        from . import authority
        authority.attach(self.db, warrant)
        try:
            return self._launch_op_settle_tx(op_id, outcome, warrant, data)
        finally:
            authority.detach(self.db, warrant)

    def _launch_op_settle_tx(self, op_id, outcome, warrant, data):
        with self._tx():
            if warrant is not None:
                from . import authority
                authority.recheck(self.db, warrant)
            record = self._get("launch_ops", op_id)
            if record.get("status") != "pending":
                raise ValueError("%s is already settled (%s)" % (op_id, record.get("outcome")))
            record.update(data, status="settled", outcome=outcome, settled_at=timestamp())
            self._save("launch_ops", record, "launch_op.settled")
        return record

    def launch_op(self, op_id):
        return self._get("launch_ops", op_id)

    def launch_ops(self, pending_only=True, limit=50):
        rows = [r for r in self._all("launch_ops") if not pending_only or r.get("status") == "pending"]
        return list(reversed(rows))[0:limit]

    def audit_list(self, limit=30):
        rows = self.db.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [dict(seq=row["seq"], at=row["at"], action=row["action"], record_id=row["record_id"],
                     data=json.loads(row["data"])) for row in rows]

    def _event(self, payload):
        now = timestamp()
        event = dict(payload, id="event-" + uuid.uuid4().hex, created_at=now)
        event["occurred_at"] = payload.get("occurred_at") or now
        cursor = self.db.execute("INSERT INTO events(id,data) VALUES (?,?)", (event["id"], _json(event)))
        event["seq"] = cursor.lastrowid
        self.db.execute("UPDATE events SET data=? WHERE id=?", (_json(event), event["id"]))
        self._audit("event.received", event["id"], event)
        return event

    def feed(self, sender, body, kind="message", thread=None, reply_to=None, source="manual", external_id=None, occurred_at=None,
             in_reply_to=None, credential=None, recipient_work_id=None, project=None, project_by="feeder",
             route=None):
        """route: the routed_by label of a relayed answer (channel C1). The question_routed stamp is then
        made inside this feed's transaction, so a rejected feed leaves no stamp (issue #9)."""
        sender, body, source = _text(sender, "sender"), _text(body, "body"), _text(source, "source")
        if route is not None:
            route = _text(route, "route")
            if not reply_to:
                raise ValueError("route needs reply_to (the question being answered).")
        payload = dict(sender=sender, body=body, kind=_text(kind, "kind"), thread=thread, reply_to=reply_to,
                       source=source, external_id=external_id,
                       occurred_at=timestamp(occurred_at) if occurred_at is not None else None)
        if in_reply_to is not None:
            # Generic provider/message reply metadata does not answer a question.
            # Only the existing explicit reply_to question ID has that effect.
            payload["in_reply_to"] = _text(in_reply_to, "in_reply_to")
        if external_id is not None:
            external_id = _text(external_id, "external_id")
            payload["external_id"] = external_id
        if recipient_work_id is not None:
            # Present only when supplied, so pre-M23 feed keys still reconcile.
            payload["recipient_work_id"] = _text(recipient_work_id, "recipient_work_id")
        if project is not None:
            # M21 phase 2: per-message project tag. Present only when supplied (old feed keys reconcile).
            # project_by: "feeder" (operator/adapter `feed --project`) or "sender" (peer tag, needs a write grant).
            if project_by not in capture.TAG_BY:
                raise ValueError("project_by must be one of %s" % (capture.TAG_BY,))
            payload["project"] = _text(project, "project")
            payload["project_by"] = project_by
        with self._tx():
            channel = self._channel_for(credential, source) if credential is not None else None
            previous = self._once("feed:" + source, external_id, payload)
            if previous:
                return self._get("events", previous)
            if route is not None:
                self._question_routed(reply_to, sender, route)
            question = self._get("questions", reply_to) if reply_to else None
            if question and not self._may_answer(question, sender):
                raise ValueError("Reply must come from the question's recipient and answer an open question.")
            # Resolve legacy dependencies before closing the question: the migration
            # fallback deliberately considers only questions that are still open.
            waiting = self._waiting_links() if question else []
            # auth is outside the idempotency payload: the same input fed again
            # returns the first event, whatever its stamp.
            stamped = dict(payload, auth=dict(channel=channel["id"], at=timestamp()) if channel else None)
            ref = stamped.pop("recipient_work_id", None)
            tag_project, tag_by = stamped.pop("project", None), stamped.pop("project_by", None)
            if tag_project is not None:
                stamped["project_ref"] = None
            if ref is not None:
                # Placeholder so the stamp sits outside the idempotency payload like auth.
                stamped["work_ref"] = None
            event = self._event(stamped)
            if question:
                self._answer(question, event, waiting, "question.answered", event["created_at"])
            if ref is not None:
                event["work_ref"] = self._work_ref(ref, event, channel)
                self.db.execute("UPDATE events SET data=? WHERE id=?", (_json(event), event["id"]))
            self._key("feed:" + source, external_id, payload, event["id"])
            # M21: project bindings decide capture; the outbox row commits with the event.
            tag = {"project": tag_project, "by": tag_by} if tag_project is not None else None
            captured = capture.enqueue(self, event, "in", tag)
            if tag is not None:
                self.db.execute("UPDATE events SET data=? WHERE id=?", (_json(event), event["id"]))
        if captured:
            try:
                capture.capture_drain(self)
            except Exception:  # noqa: BLE001 - the event is delivered; pending rows retry and stay visible
                pass
        return event

    def board_feed(self, sender, body, *, project, external_id, thread, occurred_at=None, authenticated=False):
        """M27 (iv) Board wake event. Local only: no channel credential, no question answer, no work_ref (C4),
        never captured (C2). auth {channel: board} only for a fenced post (Q4). Dedup on (source, external_id)."""
        source = capture.BOARD_SOURCE + _text(project, "project")
        payload = dict(sender=_text(sender, "sender"), body=_text(body, "body"), kind="message", thread=thread,
                       reply_to=None, source=source, external_id=_text(external_id, "external_id"),
                       occurred_at=timestamp(occurred_at) if occurred_at is not None else None)
        with self._tx():
            previous = self._once("feed:" + source, external_id, payload)
            if previous:
                return self._get("events", previous)
            event = self._event(dict(payload, auth=dict(channel="board", at=timestamp()) if authenticated else None))
            self._key("feed:" + source, external_id, payload, event["id"])
        return event

    def _work_ref(self, raw, event, channel):
        """M23: interpret a sender's reference to this employee's work.

        The ref is a hint. It is always delivered. It changes state only when the
        event is channel-authenticated, from a foreign sender, and names waiting
        work that is not waiting on an open question: then waiting -> ready, and
        nothing else. The prior wait stays visible on the work (woken_by_ref).
        """
        if not WORK_ID.match(raw):
            ref = dict(raw=raw[:80], valid=False, found=False, effect="none")
            self._audit("event.work_ref_invalid", event["id"], ref)
            return ref
        work = self._find("works", raw)
        if work is None:
            ref = dict(id=raw, valid=True, found=False, status_at_receipt=None, effect="none")
            self._audit("event.work_ref_unknown", event["id"], ref)
            return ref
        work = self._work_view(work)
        ref = dict(id=raw, valid=True, found=True, status_at_receipt=work["status"], effect="none")
        if channel is None:
            ref["reason"] = "unauthenticated"
        elif event["sender"] == self.identity()["id"]:
            ref["reason"] = "own message"
        elif self._is_routine(work):
            # P2-fix (4): received as usual; the routine's state is unchanged.
            ref["reason"] = ("%s routine: changes only through routine_rearm, the quarantine or `synthetics routine`"
                             % work["routine"].get("kind"))
        elif work["status"] != "waiting":
            ref["reason"] = "work is %s" % work["status"]
        elif work.get("waiting_question_ids"):
            # P4: any open linked question keeps the work waiting for its answer.
            ref["reason"] = "waiting on %s; answer that question instead" % ", ".join(work["waiting_question_ids"])
        else:
            now = timestamp()
            work["woken_by_ref"] = dict(event=event["id"], sender=event["sender"], channel=channel["id"], at=now,
                                        prior_waiting_for=work["waiting_for"], prior_wake_at=work["wake_at"])
            work.update(status="ready", waiting_for="", waiting_question_id=None, waiting_question_ids=[], wake_at=None, updated_at=now)
            self._save("works", work, "work.awakened_by_ref")
            ref["effect"] = "awakened"
        self._audit("event.work_ref", event["id"], ref)
        return ref

    # ---- M22: authenticated channels -------------------------------------
    # A channel is an adapter or operator path that holds a key file outside
    # every employee grant. Each home stores only the key's sha256 and the
    # source prefixes the channel may write. feed(credential=…) stamps
    # event.auth; only stamped foreign events (and questions answered by
    # them) can be verified evidence. Bounded by the M22 threat model: a
    # deliberate forger with the host uid can still edit SQLite (MG-09).

    @staticmethod
    def credential_hash(credential):
        if isinstance(credential, str):
            credential = credential.strip().encode("utf-8")
        if not isinstance(credential, bytes) or len(credential) < 16:
            raise ValueError("A channel credential must be at least 16 bytes.")
        return hashlib.sha256(credential.strip()).hexdigest()

    def channel_register(self, name, key_sha256, source_prefixes, registered_by, routes=None):
        """Operator only: register (or rotate) a channel's key hash, source prefixes and routes.

        routes (M25 §2) lists the recipient addresses this channel delivers from this
        home. None keeps the existing routes on rotation; a channel without routes
        delivers nothing (fail closed)."""
        if self.run_id is not None:
            raise ValueError("Channel registration is an operator operation, not a model tool.")
        name, registered_by = _text(name, "name"), _text(registered_by, "registered_by")
        if not re.fullmatch(r"[a-f0-9]{64}", key_sha256 or ""):
            raise ValueError("key_sha256 must be a sha256 hex digest.")
        prefixes = sorted({_text(p, "source prefix") for p in source_prefixes or []})
        if not prefixes:
            raise ValueError("A channel needs at least one source prefix.")
        if routes is not None:
            routes = sorted({_text(r, "route") for r in routes})
        with self._tx():
            row = self.db.execute("SELECT data FROM channels WHERE id=?", (name,)).fetchone()
            old = json.loads(row["data"]) if row else None
            if routes is None:
                routes = list((old or {}).get("routes") or [])
            if (old and old["key_sha256"] == key_sha256 and old["source_prefixes"] == prefixes
                    and (old.get("routes") or []) == routes):
                return old
            for other in self._all("channels"):
                if other["id"] != name and other["key_sha256"] == key_sha256:
                    raise ValueError("That key is already registered for channel %s." % other["id"])
            record = dict(id=name, key_sha256=key_sha256, source_prefixes=prefixes, routes=routes,
                          registered_by=registered_by, registered_at=timestamp())
            if old and old.get("instance"):
                record["instance"] = old["instance"]
            # The audit row carries the hash, never key material.
            return self._save("channels", record, "channel.rotated" if old else "channel.registered")

    def channel_list(self):
        return self._all("channels")

    def routed_by(self, to):
        """Backlog 22: channels whose routes carry address `to` from this home.

        None means this home has no channel records at all (pre-contract, M25 C2): the caller
        keeps today's behaviour. An empty list means channels exist but none will ever carry it."""
        channels = self._all("channels")
        if not channels:
            return None
        return sorted(c["id"] for c in channels if to in (c.get("routes") or []))

    def _channel_for(self, credential, source):
        digest = self.credential_hash(credential)
        for channel in self._all("channels"):
            if channel["key_sha256"] == digest:
                if not any(source.startswith(prefix) for prefix in channel["source_prefixes"]):
                    raise ValueError("Channel %s may not write source %s." % (channel["id"], source))
                return channel
        raise ValueError("The credential does not match a registered channel.")

    def delivery_record(self, message_id, credential, status="delivered", receipt=None, detail=None):
        """An authenticated delivery adapter records what happened to an outbox message.

        Idempotent per (channel, status, receipt). Audited as message.delivery_recorded.
        """
        message_id, status = _text(message_id, "message_id"), _text(status, "status")
        if status not in ("delivered", "failed"):
            raise ValueError("Delivery status must be delivered or failed.")
        if receipt is not None:
            receipt = _text(receipt, "receipt")
        if detail is not None and not isinstance(detail, dict):
            raise ValueError("Delivery detail must be an object.")
        with self._tx():
            channel = self._channel_by_key(credential)
            message = self._routed_message(channel, message_id)
            deliveries = message.setdefault("deliveries", [])
            for item in deliveries:
                if (item.get("channel"), item.get("status"), item.get("receipt")) == (channel["id"], status, receipt):
                    return message
            deliveries.append(dict(channel=channel["id"], status=status, receipt=receipt,
                                   detail=detail or {}, at=timestamp()))
            # M25: the legacy in-process ack is a final outcome for this channel.
            state = self._chan_state(message, channel["id"])
            state.update(state=status, updated_at=timestamp(), receipt=receipt)
            return self._save("messages", message, "message.delivery_recorded")

    # ---- M25: per message x channel delivery state (docs/channel-contract.md §3) ----

    def _channel_by_key(self, credential):
        try:
            digest = self.credential_hash(credential)
        except ValueError as exc:
            raise ChannelError("auth_failed", str(exc))
        channel = next((c for c in self._all("channels") if c["key_sha256"] == digest), None)
        if channel is None:
            raise ChannelError("auth_failed", "The credential does not match a registered channel.")
        return channel

    def _routed_message(self, channel, message_id):
        message = self._find("messages", _text(message_id, "message_id"))
        if message is None:
            raise ChannelError("unknown_message", "No message %s." % message_id)
        if message["to"] not in (channel.get("routes") or []):
            raise ChannelError("not_routed", "Channel %s does not route %s." % (channel["id"], message["to"]))
        return message

    @staticmethod
    def _chan_state(message, channel_id):
        """The mutable state entry for one channel; legacy M22 acks read as final."""
        states = message.setdefault("channel_states", {})
        if channel_id not in states:
            legacy = [d for d in message.get("deliveries") or [] if d.get("channel") == channel_id]
            final = next((d["status"] for d in reversed(legacy) if d.get("status") in FINAL_STATES), None)
            states[channel_id] = dict(state=final or "pending", attempt=0)
        return states[channel_id]

    @staticmethod
    def delivery_summary(message):
        """Message-level outcome: delivered > uncertain > failed (all channels) > pending."""
        current = message.get("channel_states") or {}
        states = [s["state"] for s in current.values()]
        # Channels with no state entry: legacy M22 acks (delivered|failed) are final.
        states += [d.get("status") for d in message.get("deliveries") or []
                   if d.get("channel") not in current and d.get("status") in FINAL_STATES]
        if "delivered" in states:
            return "delivered"
        if "uncertain" in states:
            return "uncertain"
        if states and all(s == "failed" for s in states):
            return "failed"
        return "pending"

    def _expire_claims(self, channel, now):
        """Expired leases become uncertain, never pending (no blind resend)."""
        for message in self._all("messages"):
            state = (message.get("channel_states") or {}).get(channel["id"])
            if state and state["state"] == "claimed" and state.get("lease_until", "") <= now:
                state.update(state="uncertain", updated_at=now, reason="claim lease expired")
                self._save("messages", message, "message.delivery_uncertain")

    def _adopt_instance(self, channel, instance, now):
        """C4: a new adapter instance turns the previous instance's claims uncertain."""
        if instance is None or channel.get("instance") == instance:
            return channel
        previous = channel.get("instance")
        if previous is not None:
            for message in self._all("messages"):
                state = (message.get("channel_states") or {}).get(channel["id"])
                if state and state["state"] == "claimed" and state.get("instance") != instance:
                    state.update(state="uncertain", updated_at=now,
                                 reason="adapter instance %s replaced by %s" % (state.get("instance"), instance))
                    self._save("messages", message, "message.delivery_uncertain")
        channel = dict(channel, instance=instance, instance_at=now)
        return self._save("channels", channel, "channel.instance")

    def _contract_view(self, message, channel_id, state=None):
        entry = (message.get("channel_states") or {}).get(channel_id) or {}
        view = dict(v=1, message_id=message["id"], kind=message.get("kind", "message"),
                    question_id=message.get("question_id"), to=message["to"], body=message["body"],
                    thread=message.get("thread"), in_reply_to=message.get("in_reply_to"),
                    recipient_work_id=message.get("recipient_work_id"), project=message.get("project"),
                    sender=message["sender"], created_at=message["created_at"], attempt=entry.get("attempt", 0))
        if message.get("kind") == "hop":
            view["hop"] = message.get("hop")  # Docket P1b: structured; an adapter that doesn't know it acks failed
        if state is not None:
            view["state"] = state
        return view

    def _begin(self, credential, instance):
        if self.run_id is not None:
            raise ChannelError("in_session", "Channel commands are adapter/operator operations, not model tools.")
        now = timestamp()
        channel = self._adopt_instance(self._channel_by_key(credential), instance, now)
        self._expire_claims(channel, now)
        return channel, now

    def channel_pending(self, credential, limit=50, instance=None):
        limit = max(1, min(int(limit), 200))
        with self._tx():
            channel, now = self._begin(credential, instance)
            routes = set(channel.get("routes") or [])
            out = []
            for message in self._all("messages"):
                if message["to"] not in routes or self.delivery_summary(message) == "delivered":
                    continue
                state = self._chan_state(dict(message), channel["id"])
                if state["state"] == "pending" and state.get("retry_after", "") <= now:
                    out.append(self._contract_view(message, channel["id"]))
            out.sort(key=lambda m: m["created_at"])
            return out[:limit]

    def channel_claim(self, credential, message_id, instance=None):
        with self._tx():
            channel, now = self._begin(credential, instance)
            message = self._routed_message(channel, message_id)
            state = self._chan_state(message, channel["id"])
            if state["state"] == "claimed":
                raise ChannelError("conflict", "Message %s is already claimed on channel %s."
                                   % (message["id"], channel["id"]))
            if self.delivery_summary(message) == "delivered" or state["state"] != "pending":
                raise ChannelError("invalid_state", "Message %s is %s on channel %s, not pending." % (
                    message["id"], "delivered" if self.delivery_summary(message) == "delivered" else state["state"],
                    channel["id"]))
            if state.get("retry_after", "") > now:
                raise ChannelError("invalid_state", "Message %s may be retried after %s."
                                   % (message["id"], state["retry_after"]))
            lease = datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() + CLAIM_LEASE_SECONDS
            state.update(state="claimed", claimed_at=now, updated_at=now, instance=instance,
                         lease_until=timestamp(datetime.fromtimestamp(lease, timezone.utc)))
            state.pop("retry_after", None)
            self._save("messages", message, "message.delivery_claimed")
            return self._contract_view(message, channel["id"], "claimed")

    def channel_ack(self, credential, message_id, outcome, receipt=None, retryable=False, detail=None, instance=None):
        """Record the adapter's outcome for a claimed message. Returns (result, state)."""
        if outcome not in ("delivered", "failed", "uncertain"):
            raise ChannelError("bad_line", "outcome must be delivered, failed or uncertain.")
        if receipt is not None and (not isinstance(receipt, str) or not receipt.strip()):
            raise ChannelError("bad_line", "receipt must be nonempty text or null.")
        if detail is not None and not isinstance(detail, dict):
            raise ChannelError("bad_line", "detail must be an object.")
        with self._tx():
            channel, now = self._begin(credential, instance)
            message = self._routed_message(channel, message_id)
            state = self._chan_state(message, channel["id"])
            deliveries = message.setdefault("deliveries", [])
            # Issue #1: a repeat counts as duplicate only while no new claim is open. An ack always closes its
            # claim, so while the message is claimed every earlier delivery belongs to an older attempt, and the
            # same outcome+receipt (e.g. failed, retryable, receipt null) is this attempt's own outcome.
            if state["state"] != "claimed":
                for item in deliveries:
                    if (item.get("channel"), item.get("status"), item.get("receipt")) == (
                            channel["id"], outcome, receipt):
                        return "duplicate", state["state"]
            if state["state"] in FINAL_STATES:
                raise ChannelError("conflict", "Message %s already has final outcome %s on channel %s."
                                   % (message["id"], state["state"], channel["id"]))
            if state["state"] != "claimed":
                raise ChannelError("invalid_state", "Message %s is %s on channel %s; claim before ack%s." % (
                    message["id"], state["state"], channel["id"],
                    ", or reconcile an uncertain message" if state["state"] == "uncertain" else ""))
            deliveries.append(dict(channel=channel["id"], status=outcome, receipt=receipt, retryable=bool(retryable),
                                   detail=detail or {}, at=now, instance=instance))
            if outcome == "failed" and retryable:
                retry = datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() + RETRY_AFTER_SECONDS
                state.update(state="pending", attempt=state.get("attempt", 0) + 1,
                             retry_after=timestamp(datetime.fromtimestamp(retry, timezone.utc)))
            else:
                state["state"] = outcome
            state.update(updated_at=now, receipt=receipt)
            for key in ("lease_until", "claimed_at"):
                state.pop(key, None)
            self._save("messages", message, "message.delivery_acked")
            return "recorded", state["state"]

    def channel_reconcile(self, credential, message_id, outcome, receipt=None, note=None, by=None):
        """The only exit from uncertain (and the recorded way to retry a failed message)."""
        if outcome not in ("delivered", "failed"):
            raise ChannelError("bad_line", "reconcile outcome must be delivered or failed.")
        with self._tx():
            channel, now = self._begin(credential, None)
            message = self._routed_message(channel, message_id)
            state = self._chan_state(message, channel["id"])
            last = state.get("reconciled") or {}
            expected = "delivered" if outcome == "delivered" else "pending"
            if last.get("outcome") == outcome and last.get("receipt") == receipt and state["state"] == expected:
                return "duplicate", state["state"]
            allowed = ("uncertain",) if outcome == "delivered" else ("uncertain", "failed")
            if state["state"] not in allowed:
                raise ChannelError("invalid_state", "Message %s is %s on channel %s; reconcile %s needs %s." % (
                    message["id"], state["state"], channel["id"], outcome, " or ".join(allowed)))
            record = dict(outcome=outcome, receipt=receipt, note=note, by=by or channel["id"], at=now,
                          from_state=state["state"])
            if outcome == "delivered":
                state.update(state="delivered", receipt=receipt)
            else:
                state.update(state="pending", attempt=state.get("attempt", 0) + 1)
                state.pop("retry_after", None)
            state.update(updated_at=now, reconciled=record)
            message.setdefault("deliveries", []).append(dict(channel=channel["id"], status="reconciled:" + outcome,
                                                             receipt=receipt, detail={"note": note}, at=now))
            self._save("messages", message, "message.delivery_reconciled")
            return "recorded", state["state"]

    def channel_uncertain(self, credential, instance=None):
        with self._tx():
            channel, _ = self._begin(credential, instance)
            out = []
            for message in self._all("messages"):
                state = (message.get("channel_states") or {}).get(channel["id"])
                if state and state["state"] == "uncertain" and message["to"] in (channel.get("routes") or []):
                    view = self._contract_view(message, channel["id"], "uncertain")
                    view.update(reason=state.get("reason"), claim_instance=state.get("instance"))
                    out.append(view)
            return out

    EVENT_LOOKUP_HIDDEN = "No event with that source and external_id was accepted through channel %s."

    def channel_event_status(self, credential, event_id=None, source=None, external_id=None):
        """Accepted vs processed for an event this channel delivered, by event ID or (#25 P2 D2(b)) by the
        channel's own source + external_id. IDs and state only: question_id is the event's reply_to (the
        question it answered), in_reply_to its generic reply metadata; never a body or sender (D2 condition).
        A lookup is audited (channel.event_lookup, IDs only) and refused the same way whether the event is
        missing or came through another channel."""
        if self.run_id is not None:
            raise ChannelError("in_session", "Channel commands are adapter/operator operations, not model tools.")
        channel = self._channel_by_key(credential)
        by_lookup = event_id is None
        if by_lookup:
            if not (isinstance(source, str) and source.strip() and isinstance(external_id, str)
                    and external_id.strip()):
                raise ChannelError("bad_line", "give --event, or both --source and --external-id")
            found = self.db.execute("SELECT record_id FROM idempotency WHERE namespace=? AND key=?",
                                    ("feed:" + source, external_id)).fetchone()
            event_id = found["record_id"] if found else None
        elif source is not None or external_id is not None:
            raise ChannelError("bad_line", "give --event, or both --source and --external-id, not both")
        row = self.db.execute("SELECT data, processed_run FROM events WHERE id=?",
                              (_text(event_id, "event") if event_id is not None else "",)).fetchone()
        event = json.loads(row["data"]) if row else None
        visible = event is not None and (event.get("auth") or {}).get("channel") == channel["id"]
        if by_lookup:
            with self._tx():
                self._audit("channel.event_lookup", event["id"] if visible else "(not found)",
                            dict(channel=channel["id"], source=source, external_id=external_id,
                                 result="returned" if visible else "refused"))
            if not visible:
                raise ChannelError("not_routed", self.EVENT_LOOKUP_HIDDEN % channel["id"])
        elif not visible:
            raise ChannelError("not_routed", "Event %s was not accepted through channel %s." % (event_id, channel["id"]))
        return dict(v=1, event_id=event["id"], state="processed" if row["processed_run"] else "accepted",
                    run_id=row["processed_run"], question_id=event.get("reply_to"),
                    in_reply_to=event.get("in_reply_to"))

    QUESTION_FULL_ID = re.compile(r"^question-[0-9a-f]{32}$")
    QUESTION_HIDDEN = "No open question for the human of this home with that ID is visible to this channel."

    def channel_question(self, credential, question_id):
        """#25 D1: the full body of one open question addressed to this home's default_human, for a keyed
        channel registered in this home (the answer desk). Read-only. Every call is audited with the channel
        and question ID only, never the body. Every refusal is the same error, so the response never says
        whether the question exists."""
        if self.run_id is not None:
            raise ChannelError("in_session", "Channel commands are adapter/operator operations, not model tools.")
        channel = self._channel_by_key(credential)
        if not isinstance(question_id, str) or not question_id.strip():
            raise ChannelError("bad_line", "--id is required")
        from .capture import default_human
        # Only a full question ID is looked up or written to the audit row; anything else (a prefix, a long or
        # odd string) is refused the same way and audited without the ID (PR #67 review n2).
        well_formed = bool(self.QUESTION_FULL_ID.match(question_id))
        human = default_human(self)
        question = self._find("questions", question_id) if well_formed else None
        visible = bool(human and question and question["status"] == "open" and question["to"] == human
                       and question.get("to_id") in (None, human))
        with self._tx():
            self._audit("channel.question_read", question_id if well_formed else "(malformed id)",
                        dict(channel=channel["id"], result="returned" if visible else "refused"))
        if not visible:
            raise ChannelError("unknown_message", self.QUESTION_HIDDEN)
        return dict(v=1, question_id=question["id"], to=question["to"], body=question["body"],
                    work_ids=list(question.get("work_ids") or ([question["work_id"]] if question.get("work_id")
                                                                else [])),
                    created_at=question["created_at"], thread=question.get("thread"), error=None)

    def event_auth_backfill(self, event_id, credential, basis):
        """Operator/adapter only: stamp a legacy event whose delivery the channel's own
        journal independently records. Never overwrites an existing stamp."""
        if self.run_id is not None:
            raise ValueError("Auth backfill is an operator operation, not a model tool.")
        event_id, basis = _text(event_id, "event_id"), _text(basis, "basis")
        with self._tx():
            event = self._get("events", event_id)
            if event.get("auth"):
                return event
            channel = self._channel_for(credential, event.get("source") or "")
            event["auth"] = dict(channel=channel["id"], at=timestamp(), backfill=True, basis=basis)
            self.db.execute("UPDATE events SET data=? WHERE id=?", (_json(event), event_id))
            self._audit("event.auth_backfilled", event_id, dict(channel=channel["id"], basis=basis))
            return event

    @staticmethod
    def _with_auth(record):
        """Show the model whether an input was channel-authenticated (null = not)."""
        if isinstance(record, dict) and "auth" not in record:
            return dict(record, auth=None)
        return record

    @staticmethod
    def _may_answer(question, sender):
        # The addressee may be recorded as the alias used to ask (to) and, once a
        # delivery adapter has routed it, as the authenticated identity (to_id).
        return question["status"] == "open" and sender in (question["to"], question.get("to_id"))

    def _waiting_links(self):
        return [(work, self._linked_ids(work)) for work in self._all("works") if work["status"] == "waiting"]

    def _answer(self, question, event, waiting, action, answered_at, status="answered", **extra):
        question.update(status=status, answer_event_id=event["id"], answered_at=answered_at, **extra)
        self._save("questions", question, action)
        self._queue_hops(question, "answered-pending-relay", answered_at)  # Docket P1b: tell each original asker
        for work, linked in waiting:
            if question["id"] in linked and self._is_routine(work):
                # P2-fix (5): a routine that somehow holds a link loses only the link; status and wake_at stay.
                rest = [qid for qid in linked if qid != question["id"]]
                work.update(waiting_question_ids=rest, waiting_question_id=rest[-1] if rest else None,
                            updated_at=question["answered_at"])
                self._save("works", work, "work.routine_link_dropped")
            elif question["id"] in linked:
                # P2: any linked answer wakes the work; the rest stay open and visible for a cheap re-wait.
                remaining = [qid for qid in linked if qid != question["id"]
                             and (self._find("questions", qid) or {}).get("status") == "open"]
                work["woken_by_answer"] = dict(question=question["id"], event=event["id"], at=question["answered_at"],
                                               remaining_open=remaining, prior_waiting_for=work["waiting_for"],
                                               prior_wake_at=work.get("wake_at"))
                work.update(status="ready", waiting_for="", waiting_question_id=None, waiting_question_ids=[],
                            wake_at=None, updated_at=question["answered_at"])
                self._save("works", work, "work.awakened")
            elif "waiting_question_ids" not in work:
                work["waiting_question_id"] = linked[-1] if linked else None
                work["waiting_question_ids"] = linked
                self._save("works", work, "work.question_link_recovered")

    def question_resolve(self, question_id, event_id):
        """The asker records that an already received message answers its open question.

        For answers that arrived uncorrelated (ordinary message, or adapter fallback).
        The event must be a message from the question's addressee (to or to_id), received
        after the question was asked. Wakes linked work like a correlated reply.
        Audited as question.resolved.
        """
        question_id, event_id = _text(question_id, "question_id"), _text(event_id, "event_id")
        question_id, event_id = self._resolve(question_id, "questions"), self._resolve(event_id, "events")
        with self._tx():
            question = self._get("questions", question_id)
            event = self._get("events", event_id)
            if question.get("answer_event_id") == event_id and question["status"] != "open":
                # M27 (iii) C: the same answer recorded twice (double intake) is not an error.
                return dict(question, already=True)
            if event.get("kind") != "message":
                raise ValueError("Only a received message can answer a question.")
            if question["status"] != "open":
                raise ValueError("Question %s is already %s (answer %s)." % (
                    question_id, question["status"], question.get("answer_event_id")))
            if not self._may_answer(question, event.get("sender")):
                raise ValueError("The answer must be a message from the question's recipient (%s) and the question "
                                 "must still be open." % (question.get("to_id") or question["to"]))
            if event.get("created_at", "") < question.get("created_at", ""):
                raise ValueError("The message was received before the question was asked.")
            self._answer(question, event, self._waiting_links(), "question.resolved", timestamp(),
                         resolved_by_run=self.run_id)
            return question

    def question_close(self, question_id, reason, event_id=None):
        """M27 (iii): the asker closes its own open question without the recipient's answer.

        With event_id (someone else answered for the recipient, Rowan R6): status answered_by_proxy;
        the event must be a message in this home, received after the ask, from neither the asker nor
        the recipient (the recipient's reply is question_resolve). The answer's auth is recorded as
        received, never upgraded; the question is marked secondhand and never supports verified
        evidence or a human's own words. Wakes linked work like an answer.
        Without event_id: status withdrawn; never wakes work. A waiting work whose question set
        becomes empty keeps its status and is listed in work_unlinked (P6).
        """
        question_id, reason = _text(question_id, "question_id"), _text(reason, "reason")
        question_id = self._resolve(question_id, "questions")
        event_id = self._resolve(event_id, "events")
        with self._tx():
            question = self._get("questions", question_id)
            me = self.identity()["id"]
            asked = self._find("messages", question.get("message_id") or "")
            if asked is not None and asked.get("sender") not in (None, me):
                raise ValueError("Only the asker can close a question.")
            if question["status"] != "open":
                raise ValueError("Question %s is already %s." % (question_id, question["status"]))
            now = timestamp()
            if event_id is not None:
                event_id = _text(event_id, "event_id")
                event = self._get("events", event_id)
                if event.get("kind") != "message":
                    raise ValueError("Only a received message can answer a question by proxy.")
                if event.get("created_at", "") < question.get("created_at", ""):
                    raise ValueError("The message was received before the question was asked.")
                sender = event.get("sender")
                if sender == me:
                    raise ValueError("Your own message cannot answer your question.")
                if sender in (question["to"], question.get("to_id")):
                    raise ValueError("This message is from the recipient; use question_resolve.")
                auth = event.get("auth")
                self._answer(question, event, self._waiting_links(), "question.closed", now,
                             status="answered_by_proxy", answered_by=sender, answer_auth=auth,
                             secondhand=True, unauthenticated=not auth, close_reason=reason,
                             closed_by_run=self.run_id)
                return dict(question, work_unlinked=[], unlinked_from=[])
            question.update(status="withdrawn", withdrawn_at=now, close_reason=reason, closed_by_run=self.run_id)
            self._save("questions", question, "question.withdrawn")
            self._queue_hops(question, "with-manager", now)  # Docket P1b: back with me, never "answered"
            unlinked, unlinked_from = [], []
            for work, linked in self._waiting_links():
                if question_id not in linked:
                    continue
                rest = [qid for qid in linked if qid != question_id]
                work.update(waiting_question_ids=rest, waiting_question_id=rest[-1] if rest else None,
                            waiting_for=work["waiting_for"].replace(question_id, "(withdrawn)"), updated_at=now)
                self._save("works", work, "work.question_unlinked")
                unlinked_from.append(work["id"])  # Docket P1a (C3): every work the question was removed from
                if not rest:
                    unlinked.append(work["id"])
            return dict(question, work_unlinked=unlinked, unlinked_from=unlinked_from)

    def event_pending(self, limit=50):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
            raise ValueError("Event limit must be from 1 to 10000.")
        rows = self.db.execute("SELECT data FROM events WHERE processed_run IS NULL ORDER BY seq LIMIT ?", (limit,))
        return [json.loads(row["data"]) for row in rows]

    def admission_held(self, event):
        """RTN F1: a work_due input for a routine that is quarantined or no longer ready never starts a session.
        It stays pending (processed_run NULL) as evidence; only routine_rearm consumes it."""
        if event.get("kind") != "work_due" or not event.get("work_id"):
            return False
        work = self._find("works", event["work_id"])
        if not work or not isinstance(work.get("routine"), dict):
            return False
        return bool(work["routine"].get("quarantine")) or work["status"] != "ready"

    def event_admissible(self, limit=50):
        """Pending inputs that may start or join a session (event_pending minus admission_held)."""
        return self.event_pending_select(lambda e: not self.admission_held(e), limit=limit)

    def event_pending_count(self):
        """How many inputs are pending in total (event_pending returns at most its limit)."""
        return self.db.execute("SELECT COUNT(*) AS n FROM events WHERE processed_run IS NULL").fetchone()["n"]

    def event_pending_select(self, accept, limit=50, page=200):
        """The oldest `limit` pending events for which accept(event) is true, scanning every pending event in seq
        order (issue #6: a daily-limit pause must find an admissible input behind any number of held ones)."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
            raise ValueError("Event limit must be from 1 to 10000.")
        found, after = [], -1
        while len(found) < limit:
            rows = self.db.execute("SELECT seq, data FROM events WHERE processed_run IS NULL AND seq > ? "
                                   "ORDER BY seq LIMIT ?", (after, page)).fetchall()
            if not rows:
                break
            for row in rows:
                event = json.loads(row["data"])
                if accept(event):
                    found.append(event)
                    if len(found) == limit:
                        break
            after = rows[-1]["seq"]
        return found

    def event_get(self, id, body_offset=None, body_chars=None):
        """Resolve source references even after an input has been acknowledged.

        F2: with body_offset and/or body_chars, return that slice of the saved original body with body_len,
        body_offset and body_next (offset of the next chunk, null at the end), so a large body is read in
        ID-keyed chunks. Without them the event is returned unchanged."""
        event = self._with_auth(self._get("events", self._resolve(_text(id, "id"), "events")))
        if body_offset is None and body_chars is None:
            return event
        for name, value, low in (("body_offset", body_offset, 0), ("body_chars", body_chars, 1)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < low):
                raise ValueError("%s must be an integer of at least %d." % (name, low))
        body = event.get("body") if isinstance(event.get("body"), str) else ""
        start = min(body_offset or 0, len(body))
        end = len(body) if body_chars is None else min(len(body), start + body_chars)
        return dict(event, body=body[start:end], body_len=len(body), body_offset=start,
                    body_next=end if end < len(body) else None)

    def event_search(self, query="", thread=None, limit=50, since=None, cursor=None, body_chars=None,
                     max_bytes=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Event search limit must be from 1 to 500.")
        if not isinstance(query, str):
            raise ValueError("Event search query must be text.")
        self._check_compact(body_chars, max_bytes)
        compact = body_chars is not None or max_bytes is not None
        budget = (max_bytes, self._json_size, self._cut_to_fit) if max_bytes is not None else None
        words = query.casefold().split()
        after = self._since_cursor(cursor, "e") if cursor is not None else None
        if since is not None or after:
            # P2 since=: events received at or after since (created_at), oldest first, with a capped flag.
            # H2: ordered by (created_at, seq) in SQL, so ties have a stable order and the scan can stop at
            # limit+1; a cursor resumes strictly after (created_at, seq).
            since = timestamp(since) if since is not None else after[0]
            found = []
            rows = self.db.execute("SELECT seq, data FROM events WHERE json_extract(data,'$.created_at') >= ? "
                                   "ORDER BY json_extract(data,'$.created_at'), seq", (since,))
            for row in rows:
                event = json.loads(row["data"])
                if after and (str(event.get("created_at", "")), row["seq"]) <= after:
                    continue
                if thread is not None and event.get("thread") != thread:
                    continue
                if all(word in _search_text(event).casefold() for word in words):
                    found.append(dict(event, _seq=row["seq"]))
                    if len(found) > limit:
                        break
            if compact:
                # F2: items are sized with their _seq tie-break still attached (a few bytes over, never under).
                found = [self._preview(self._with_auth(e), body_chars) for e in found]
                page = self._since_page(found, since, limit, "events", "e", lambda e: e["_seq"], budget)
                page["events"] = [{k: v for k, v in e.items() if k != "_seq"} for e in page["events"]]
                return page
            page = self._since_page(found, since, limit, "events", "e", lambda e: e["_seq"])
            page["events"] = [self._with_auth({k: v for k, v in e.items() if k != "_seq"}) for e in page["events"]]
            return page
        found = []
        for row in self.db.execute("SELECT data FROM events ORDER BY seq DESC"):
            event = json.loads(row["data"])
            if thread is not None and event.get("thread") != thread:
                continue
            if all(word in _search_text(event).casefold() for word in words):
                found.append(self._with_auth(event))
                if len(found) >= limit:
                    break
        if compact:
            # F2 C1: query/thread mode under compact options returns {events, capped, truncated_from};
            # capped = max_bytes cut the result (narrow the query, or page with since=).
            found = [self._preview(event, body_chars) for event in found]
            page = self._fit(found, *budget) if budget else found
            return {"events": page, "capped": len(page) < len(found),
                    "truncated_from": next((e.get("created_at") for e in page if e.get("body_truncated")), None)}
        return found

    def thread_history(self, thread, limit=50, before=None):
        """Read a chronological page of incoming and outgoing conversation.

        The first page contains the latest items; next_before retrieves the
        preceding page. Creation-audit sequences give stable cursors even when
        provider timestamps are equal or messages arrive out of order. A model
        bound to a run sees its captured inputs and earlier processed history,
        never a later or uncaptured pending message through this API.
        """
        run = self.run_get(self.run_id) if self.run_id else None
        return self._thread_history(thread, limit, before, run)

    def thread_import(self, thread, items, source):
        """Operator-only import of visible conversation history, oldest first.

        This creates neither pending input nor outgoing messages or questions.
        Preserve source provenance rather than presenting an imported assistant
        message as an action performed by this employee's current model.
        """
        if self.run_id is not None:
            raise ValueError("Conversation history import is an operator operation, not a model tool.")
        thread, source = _text(thread, "thread"), _text(source, "source")
        if not isinstance(items, list):
            raise ValueError("Conversation history items must be a list.")
        records = []
        with self._tx():
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Each conversation history item must be an object.")
                external_id = _text(item.get("id"), "item.id")
                direction = item.get("direction")
                if direction not in ("incoming", "outgoing"):
                    raise ValueError("Imported direction must be incoming or outgoing.")
                provenance = item.get("provenance", {})
                if not isinstance(provenance, dict):
                    raise ValueError("Imported provenance must be an object.")
                if not isinstance(item.get("body"), str):
                    raise ValueError("Imported body must be text; attachment references belong in provenance.")
                payload = dict(thread=thread, source=source, external_id=external_id,
                               sender=_text(item.get("sender"), "item.sender"), direction=direction,
                               body=item["body"], provenance=provenance,
                               occurred_at=timestamp(item["occurred_at"]) if item.get("occurred_at") is not None else None)
                if item.get("turn_id") is not None:
                    payload["turn_id"] = _text(item["turn_id"], "item.turn_id")
                namespace = "thread_import:" + source
                previous = self._once(namespace, external_id, payload)
                if previous:
                    records.append(self._get("thread_records", previous))
                    continue
                record = dict(payload, id="thread-record-" + uuid.uuid4().hex, historical=True,
                              created_at=timestamp())
                self._save("thread_records", record, "thread.imported")
                self._key(namespace, external_id, payload, record["id"])
                records.append(record)
        return records

    def _thread_history(self, thread, limit=50, before=None, run=None):
        thread = _text(thread, "thread")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Thread history limit must be from 1 to 500.")
        if before is not None and (not isinstance(before, int) or isinstance(before, bool) or before < 1):
            raise ValueError("Thread history before must be a positive sequence number.")
        captured = set(run["event_ids"]) if run else None
        through = run.get("history_sequence") if run else None
        if run and through is None:
            # Older run records predate the explicit history snapshot marker.
            row = self.db.execute("SELECT seq FROM audit WHERE action='run.started' AND record_id=? ORDER BY seq LIMIT 1",
                                  (run["id"],)).fetchone()
            through = row["seq"] if row else 0
        rows = self.db.execute("""
            SELECT a.seq AS sequence, 'incoming' AS direction, e.data AS data, e.processed_run AS processed_run
              FROM audit a JOIN events e ON a.record_id=e.id WHERE a.action='event.received'
            UNION ALL
            SELECT a.seq AS sequence, 'outgoing' AS direction, m.data AS data, NULL AS processed_run
              FROM audit a JOIN messages m ON a.record_id=m.id WHERE a.action='outbox.queued'
            UNION ALL
            SELECT a.seq AS sequence, NULL AS direction, h.data AS data, NULL AS processed_run
              FROM audit a JOIN thread_records h ON a.record_id=h.id WHERE a.action='thread.imported'
            ORDER BY sequence
        """)
        items = []
        for row in rows:
            # A bound model can see its own newly queued outgoing messages. Only
            # incoming events and operator-imported past history are snapshotted.
            if through is not None and row["direction"] != "outgoing" and row["sequence"] > through:
                continue
            record = json.loads(row["data"])
            if record.get("thread") != thread:
                continue
            if (captured is not None and row["direction"] == "incoming"
                    and row["processed_run"] is None and record["id"] not in captured):
                continue
            if row["direction"] == "incoming":
                record.setdefault("auth", None)
            items.append(dict(record, direction=row["direction"] or record["direction"], sequence=row["sequence"]))
        total = len(items)
        if before is not None:
            items = [item for item in items if item["sequence"] < before]
        truncated = len(items) > limit
        page = items[-limit:]
        return dict(thread=thread, items=page, total=total, truncated=truncated,
                    next_before=page[0]["sequence"] if truncated else None)

    def reminder_set(self, at, reason, work_id=None, key=None):
        at, reason, key = timestamp(at), _text(reason, "reason"), _text(key, "key")
        work_id = self._resolve(work_id, "works")
        payload = dict(at=at, reason=reason, work_id=work_id)
        with self._tx():
            previous = self._once("reminder", key, payload)
            if previous:
                return self._get("reminders", previous)
            if work_id:
                linked = self.work_get(work_id)
                if self._is_routine(linked):
                    raise ValueError(self.ROUTINE_LINK % (work_id, linked["routine"].get("kind"), "a reminder",
                                                          "reminder"))
            record = dict(payload, id="reminder-" + uuid.uuid4().hex, created_at=timestamp(), fired_at=None)
            self._save("reminders", record, "reminder.set")
            self._key("reminder", key, payload, record["id"])
            return record

    def due_events(self, now=None):
        now = timestamp(now)
        count = 0
        with self._tx():
            for reminder in self._all("reminders"):
                if reminder["fired_at"] is not None or reminder["at"] > now:
                    continue
                event = self._event(dict(sender="clock", body=reminder["reason"], kind="reminder", source="clock",
                                         external_id=reminder["id"], work_id=reminder["work_id"], thread=None, reply_to=None))
                reminder.update(fired_at=now, event_id=event["id"])
                self._save("reminders", reminder, "reminder.fired")
                if reminder["work_id"]:
                    # Not work_get: its shared_with view field must never be saved into the record.
                    work = self._work_view(self._get("works", reminder["work_id"]))
                    # P2-fix (2): a reminder linked before the refusal existed never wakes a routine.
                    if work["status"] == "waiting" and not self._is_routine(work):
                        work.update(status="ready", waiting_for="", waiting_question_id=None, waiting_question_ids=[], wake_at=None, updated_at=now)
                        self._save("works", work, "work.awakened")
                count += 1
            for work in self._all("works"):
                if work["status"] != "waiting" or not work.get("wake_at") or work["wake_at"] > now:
                    continue
                self._event(dict(sender="clock", body="Scheduled work is due: " + work["title"], kind="work_due", source="clock",
                                 external_id=work["id"] + ":" + work["wake_at"], work_id=work["id"], thread=None, reply_to=None))
                if isinstance(work.get("routine"), dict):
                    # RTN P1: diagnostics only (never the dream window, D2). anchor_at stays the reviewed cycle.
                    work["routine"] = dict(work["routine"], previous_fired_at=work["routine"].get("last_fired_at"),
                                           last_fired_at=now)
                work.update(status="ready", waiting_for="", waiting_question_id=None, waiting_question_ids=[], wake_at=None, updated_at=now)
                self._save("works", work, "work.awakened")
                count += 1
        return count

    # -- RTN P1 routines (docs/routines.md) -----------------------------------------------------------
    # A routine is a work record with work["routine"]. Only these methods and due_events write that field.

    def _routines(self, kind=None):
        return [w for w in self._all("works") if isinstance(w.get("routine"), dict)
                and (kind is None or w["routine"].get("kind") == kind)]

    def _routine_open(self, kind):
        found = [w for w in self._routines(kind) if w["status"] in ("ready", "waiting")]
        if not found:
            raise ValueError("No open %s routine in this home (seed it with `synthetics routine seed`)." % kind)
        return found[-1]

    def _routine_save(self, work, routine, now, action, **fields):
        work.update(fields, routine=routine, updated_at=now)
        if work.get("publish") or work.get("published"):
            self.db.execute("INSERT INTO capture_meta(k,v) VALUES ('publish_dirty','1') "
                            "ON CONFLICT(k) DO UPDATE SET v='1'")
        return self._save("works", work, action)

    @staticmethod
    def _routine_waiting(routine, wake):
        return dict(status="waiting", wake_at=wake, waiting_question_id=None, waiting_question_ids=[],
                    waiting_for="routine %s: next review at %s" % (routine["kind"], wake))

    def _operator_only(self, what):
        if self.run_id is not None:
            raise ValueError("%s is a CLI action (owner-only, docs/routines.md); inside a session re-arm with "
                             "routine_rearm." % what)

    def routine_route(self):
        """E2: where this home's routine reports go (runtime limit_notify + roster), or why nobody resolves."""
        from . import routines
        config = routines.configured(self.home)
        from . import roster
        try:
            # F4: the OFFICIAL company people/alias fold, never this home's private self/views fold.
            state = roster.company_state_for_home(self.home)
        except Exception:  # noqa: BLE001 - no roster means no canonicalisation, not no manager
            state = None
        try:
            default = capture.humans(self).get("default_human")
        except (OSError, ValueError):
            default = None
        return routines.route(config, state, self.identity()["id"], default)

    def routine_seed(self, kinds, every=None, dream_at=None, now=None, first=None):
        """Idempotent: a kind counts as existing if any non-cancelled routine work carries it (done included,
        never touched; restore it instead). Returns created, existing and the manager route (E2)."""
        from . import routines
        self._operator_only("routine seed")
        every = dict(every or {})
        kinds = list(dict.fromkeys(kinds or []))
        if not kinds:
            raise ValueError("Name at least one routine kind (%s)." % ", ".join(routines.KINDS))
        for kind in list(kinds) + list(every):
            routines.check_every(kind, every.get(kind, routines.DEFAULT_EVERY.get(kind, 60)))
        moment = routines.parse(timestamp(now))
        first = {kind: routines.parse_first(value, moment, every.get(kind, routines.DEFAULT_EVERY.get(kind, 60)), kind)
                 for kind, value in (first or {}).items()}
        created, existing = [], []
        with self._tx():
            stamp = timestamp(moment)
            for kind in kinds:
                found = [w for w in self._routines(kind) if w["status"] != "cancelled"]
                if found:
                    existing.append({"kind": kind, "work_id": found[-1]["id"], "status": found[-1]["status"],
                                     **({"first_ignored": True} if kind in first else {})})
                    continue
                minutes = every.get(kind, routines.DEFAULT_EVERY[kind])
                regular = routines.first_wake(kind, minutes, moment, dream_at or routines.DEFAULT_DREAM_AT)
                wake, anchor = timestamp(regular), timestamp(regular)
                if kind in first:
                    # G1: an explicit first wake sets wake_at only; the anchor is the grid point before the regular
                    # first wake, so the first routine_rearm lands back on the regular grid (dream: next --dream-at).
                    wake = timestamp(first[kind])
                    anchor = timestamp(regular - routines.timedelta(minutes=minutes))
                routine = dict(v=1, kind=kind, every_minutes=minutes, anchor_at=anchor, seeded_at=stamp,
                               last_fired_at=None, previous_fired_at=None, last_rearmed_at=None, rearmed_by_run=None,
                               last_review=None, quarantine=None, failures=[], covered_through=None, coverage=None)
                work = dict(id="work-" + uuid.uuid4().hex, title="Routine: %s" % kind,
                            description="RTN routine (docs/routines.md). Guidance: prompts/routines.md ## %s. Fires "
                                        "every %d min; re-arm only with routine_rearm after the review." % (kind, minutes),
                            priority=0, note="", created_at=stamp)
                self._routine_save(work, routine, stamp, "routine.seeded", **self._routine_waiting(routine, wake))
                created.append({"kind": kind, "work_id": work["id"], "wake_at": wake, "every_minutes": minutes})
        return {"created": created, "existing": existing, "route": self.routine_route()}

    def routine_cadence(self, kind, minutes):
        from . import routines
        self._operator_only("routine cadence")
        routines.check_every(kind, minutes)
        with self._tx():
            work = self._routine_open(kind)
            routine = dict(work["routine"], every_minutes=minutes)
            return self._routine_save(work, routine, timestamp(), "routine.cadence")

    RESTORED = "routine.restored"  # processed_run marker for a held work_due that `routine restore` consumed

    def routine_restore(self, kind, now=None):
        """Clear quarantine (failures stay) or reopen a closed routine, waiting at the next anchor target."""
        from . import routines
        self._operator_only("routine restore")
        if kind not in routines.KINDS:
            raise ValueError("Unknown routine kind %r." % kind)
        moment = routines.parse(timestamp(now))
        with self._tx():
            found = [w for w in self._routines(kind) if not w["routine"].get("torn_down_at")]
            if not found:
                raise ValueError("No %s routine to restore (a torn-down routine is seeded again)." % kind)
            work = found[-1]
            stamp = timestamp(moment)
            wake = timestamp(routines.rearm_target(work["routine"], moment))
            routine = dict(work["routine"], anchor_at=wake, quarantine=None, restored_at=stamp)
            # n3 (P2): the restore is the repair, so it consumes the held work_due (never run) like routine_rearm.
            self.db.execute("UPDATE events SET processed_run=? WHERE processed_run IS NULL AND id IN (SELECT id FROM "
                            "events WHERE json_extract(data,'$.kind')='work_due' AND json_extract(data,'$.work_id')=?)",
                            (self.RESTORED, work["id"]))
            return self._routine_save(work, routine, stamp, "routine.restored", **self._routine_waiting(routine, wake))

    def routine_teardown(self, kinds=None, reason="routine teardown"):
        """Cancel open routines (all when kinds is None) with a reason; no routine_closed flag afterwards."""
        self._operator_only("routine teardown")
        reason = _text(reason, "reason")
        done = []
        with self._tx():
            stamp = timestamp()
            for work in self._routines():
                if work["status"] not in ("ready", "waiting") or (kinds and work["routine"]["kind"] not in kinds):
                    continue
                routine = dict(work["routine"], torn_down_at=stamp)
                self._routine_save(work, routine, stamp, "routine.torn_down", status="cancelled", wake_at=None,
                                   waiting_for="", waiting_question_id=None, waiting_question_ids=[],
                                   cancellation=dict(reason=reason, recorded_at=stamp, recorded_by_run=None))
                done.append({"kind": routine["kind"], "work_id": work["id"]})
        return {"torn_down": done}

    def routine_list(self, now=None):
        from . import routines
        moment = routines.parse(timestamp(now))
        running = any(r["status"] == "running" for r in self._all("runs"))
        works = self._routines()
        rows = [dict(work_id=w["id"], kind=w["routine"]["kind"], status=w["status"], wake_at=w.get("wake_at"),
                     every_minutes=w["routine"].get("every_minutes"), last_fired_at=w["routine"].get("last_fired_at"),
                     last_rearmed_at=w["routine"].get("last_rearmed_at"), rearmed_by_run=w["routine"].get("rearmed_by_run"),
                     quarantined=bool(w["routine"].get("quarantine")), failures=len(w["routine"].get("failures") or []),
                     covered_through=w["routine"].get("covered_through")) for w in works]
        return {"routines": rows, "flags": routines.flags(works, moment, running), "route": self.routine_route()}

    def routine_rearm(self, work_id, review, cycle=None, covered_through=None, omissions=None):
        """The only proof of a routine review (C2): stores the review and re-arms at the next anchor target in one
        transaction. Accepted only while routines_due lists the routine (fired, quarantined or due within 15 min).
        Dream: covered_through T needs previous covered_through <= T <= this run's start (the evidence cutoff, E1)
        and T <= the earliest omission `from`; omitted T leaves the checkpoint where it is (D2)."""
        from . import routines
        if self.run_id is None:
            raise ValueError("routine_rearm is a session tool (its run is the proof of review); operators use "
                             "`synthetics routine restore`.")
        work_id = self._resolve(work_id, "works")
        review = _text(review, "review")
        if len(review) > routines.REVIEW_MAX:
            raise ValueError("review is at most %d characters: goals, blockers and next steps checked, or "
                             "\"no change\" and why." % routines.REVIEW_MAX)
        omitted = routines.check_omissions(omissions)
        with self._tx():
            work = self._get("works", work_id)
            routine = work.get("routine")
            if not isinstance(routine, dict):
                raise ValueError("%s is not a routine; use work_put." % work_id)
            if work["status"] in ("done", "cancelled"):
                raise ValueError("%s routine is %s; the operator restores it with `synthetics routine restore %s`."
                                 % (routine["kind"], work["status"], routine["kind"]))
            stamp = timestamp()
            moment = routines.parse(stamp)
            # Addendum (a): compare-and-set on the cycle routines_due showed (anchor_at). A repeat or stale call
            # names a cycle that an earlier re-arm already closed and is refused, never applied twice.
            if not isinstance(cycle, str) or not cycle.strip():
                raise ValueError("cycle is required: pass routines_due[].cycle for this routine.")
            if timestamp(cycle) != routine.get("anchor_at"):
                last = routine.get("last_review") or {}
                raise ValueError("stale re-arm: cycle %s was already closed (re-armed by run %s at %s); nothing to "
                                 "do for it." % (timestamp(cycle), last.get("run") or routine.get("rearmed_by_run"),
                                                 routine.get("last_rearmed_at")))
            state = routines.due_state(work, moment)
            if state is None:
                raise ValueError("%s routine is not due (next fire %s). routine_rearm is accepted only while "
                                 "routines_due lists it: fired, quarantined, or due within %d min."
                                 % (routine["kind"], work.get("wake_at"), routines.FOLD_IN_MINUTES))
            run = self._get("runs", self.run_id)
            routine = dict(routine)
            if covered_through is not None or omitted:
                if routine["kind"] != "dream":
                    raise ValueError("covered_through and omissions belong to the dream routine only.")
                cutoff = run["started_at"]
                through = routine.get("covered_through")
                if covered_through is not None:
                    through = timestamp(covered_through)
                    previous = routine.get("covered_through")
                    if through > cutoff:
                        raise ValueError("covered_through %s is after this run's evidence cutoff %s (its start); "
                                         "a dream covers only material that existed when it began." % (through, cutoff))
                    if previous and through < previous:
                        raise ValueError("covered_through %s is before the previous checkpoint %s; it never moves "
                                         "back." % (through, previous))
                    if any("from" not in o for o in omitted) and through != previous:
                        raise ValueError("an omission has no known earliest unread time, so covered_through cannot "
                                         "advance past the previous checkpoint %s (F3)." % previous)
                    earliest = min((o["from"] for o in omitted if "from" in o), default=None)
                    if earliest is not None and through > timestamp(earliest):
                        raise ValueError("covered_through %s passes unread material from %s (an omission); "
                                         "advance it at most to %s." % (through, timestamp(earliest), timestamp(earliest)))
                    routine["covered_through"] = through
                routine["coverage"] = dict(run=self.run_id, at=stamp, cutoff=cutoff, through=through,
                                           advanced=covered_through is not None, omissions=omitted)
            wake = timestamp(routines.rearm_target(routine, moment))
            routine.update(anchor_at=wake, last_rearmed_at=stamp, rearmed_by_run=self.run_id, quarantine=None,
                           last_review=dict(run=self.run_id, at=stamp, state=state, text=review))
            self.db.execute("UPDATE events SET processed_run=? WHERE processed_run IS NULL AND id IN (SELECT id FROM "
                            "events WHERE json_extract(data,'$.kind')='work_due' AND json_extract(data,'$.work_id')=?)",
                            (self.run_id, work_id))
            return self._routine_save(work, routine, stamp, "routine.rearmed", **self._routine_waiting(routine, wake))

    def _quarantine_left_ready(self, run, how):
        """D1: inside the run-end transaction, a routine this run captured (its work_due event) that is still
        ready is quarantined: no wake, out of runnable_work, a failure entry that is never removed."""
        captured = {e.get("work_id") for e in run.get("events") or [] if e.get("kind") == "work_due" and e.get("work_id")}
        # P2-fix (2): any wake kind. A routine this run captured any input for (reminder, work ref), or one that is
        # ready with no pending input of its own left (nothing else will hand it to a session), counts as left ready.
        for e in run.get("events") or []:
            ref = e.get("work_ref") if isinstance(e.get("work_ref"), dict) else {}
            for work_id in (e.get("work_id"), ref.get("id")):
                if work_id:
                    captured.add(work_id)
        for work in self._routines():
            if work["status"] != "ready" or work["routine"].get("quarantine") or work["id"] in captured:
                continue
            if not self._pending_for_work(work["id"]):
                captured.add(work["id"])
        stamp = timestamp()
        for work_id in sorted(captured):
            work = self._find("works", work_id)
            if not work or not isinstance(work.get("routine"), dict) or work["status"] != "ready":
                continue
            reason = "left ready when the run %s" % how
            entry = dict(run=run["id"], at=stamp, reason=reason)
            notice = self._quarantine_notice(work, entry, stamp)
            routine = dict(work["routine"], quarantine=dict(entry, notice=notice),
                           failures=list(work["routine"].get("failures") or []) + [entry])
            self._routine_save(work, routine, stamp, "routine.quarantined")

    def _pending_for_work(self, work_id):
        """Pending (unprocessed) inputs that name this work: work_due/reminder work_id or a work ref."""
        row = self.db.execute("SELECT 1 FROM events WHERE processed_run IS NULL AND (json_extract(data,'$.work_id')=? "
                              "OR json_extract(data,'$.work_ref.id')=?) LIMIT 1", (work_id, work_id)).fetchone()
        return row is not None

    def _quarantine_notice(self, work, entry, stamp):
        """N3: inside the run-end transaction, queue ONE outbox notice per quarantined cycle to routine_route's
        report_to. Keyed on work ID + anchor_at: a repeat, a restart or a second failure of the same cycle finds the
        key and sends nothing. No recipient resolved: nothing is sent (doctor shows routine_no_manager)."""
        from . import routines
        cycle = work["routine"].get("anchor_at")
        key = routines.quarantine_key(work["id"], cycle)
        row = self.db.execute("SELECT record_id FROM idempotency WHERE namespace='message' AND key=?",
                              (key,)).fetchone()
        if row:
            return dict(key=key, sent=True, message_id=row["record_id"], repeat=True)
        route = self.routine_route()
        to = route.get("report_to") if route.get("resolved") else None
        if not to:
            return dict(key=key, sent=False, reason=route.get("reason") or "no report_to resolved")
        body = routines.quarantine_notice(work["routine"]["kind"], self.identity()["name"])
        payload = dict(to=to, body=body, work_id=work["id"])
        message = dict(payload, id="message-" + uuid.uuid4().hex, sender=self.identity()["id"], kind="message",
                       question_id=None, created_at=stamp)
        self._save("messages", message, "outbox.queued")
        self._key("message", key, payload, message["id"])
        return dict(key=key, sent=True, message_id=message["id"], to=to,
                    recovery=routines.quarantine_recovery(work["routine"]["kind"], work["id"], self.home))

    def routines_due(self, run=None):
        """Every snapshot (A2-C2): routines fired, quarantined or due within 15 min, each with its ready-made
        re-arm target and guidance. Absorbing one means doing its review and calling routine_rearm."""
        from . import routines
        moment = routines.parse(timestamp())
        out, route = [], None
        for work in self._routines():
            state = routines.due_state(work, moment)
            if state is None:
                continue
            r = work["routine"]
            if route is None:
                route = self.routine_route()
            item = dict(work_id=work["id"], kind=r["kind"], state=state, cycle=r.get("anchor_at"),
                        every_minutes=r.get("every_minutes"),
                        rearm_wake_at=timestamp(routines.rearm_target(r, moment)), wake_at=work.get("wake_at"),
                        last_fired_at=r.get("last_fired_at"), previous_fired_at=r.get("previous_fired_at"),
                        report_to=route.get("report_to"), guidance=routines.guidance(r["kind"]))
            if not route.get("resolved") or route.get("reason"):
                item["route_problem"] = route.get("reason")
            if state == "quarantined":
                item.update(quarantine=r["quarantine"], failures=len(r.get("failures") or []))
            if r["kind"] == "dream":
                item.update(covered_through=r.get("covered_through"),
                            evidence_cutoff=(run or {}).get("started_at") or timestamp(moment))
            out.append(item)
        return out

    # -- M19 runtime requests ------------------------------------------------
    # The model only records a request; the engine carries it out at an idle point.

    def _runtime_request(self, origin, settings, reason, requested_by, key, delegation=None, warrant=None):
        from . import runtime  # Lazy: store.py stays importable on its own (experiment fixtures copy it alone).
        settings = runtime.parse_settings(settings)
        reason = _text(reason, "reason")
        state = runtime.read_state(self.home)
        if state is None:
            raise ValueError("This worker was not started with --runtime-config, so its runtime cannot be "
                             "changed by request; an operator must change its launch agent.")
        policy, effective = state["policy"], state["effective"]
        if origin == "self":
            runtime.check_self_service(settings, effective, policy["baseline"])
        proposed = runtime.validate(runtime.merge(effective, settings), policy["allowed_models"],
                                    policy["allowed_claude"])
        payload = dict(origin=origin, settings=settings, reason=reason, requested_by=requested_by)
        if delegation:
            payload["delegation"] = delegation
        if warrant is not None:  # M28 D4: re-check actor run + delegation chain inside this write transaction
            from . import authority
            authority.attach(self.db, warrant)
        try:
            return self._runtime_request_tx(payload, key, origin, requested_by, reason, settings, proposed,
                                            delegation, warrant)
        finally:
            if warrant is not None:
                authority.detach(self.db, warrant)

    def _runtime_request_tx(self, payload, key, origin, requested_by, reason, settings, proposed, delegation,
                            warrant):
        from . import runtime
        with self._tx():
            if warrant is not None:
                from . import authority
                authority.recheck(self.db, warrant)
            old = self._once("runtime", key, payload)
            if old:
                return self._get("runtime_requests", old)
            now = timestamp()
            for earlier in self._all("runtime_requests"):
                if earlier["status"] == "requested":
                    earlier.update(status="superseded", updated_at=now, outcome="superseded by a later request")
                    self._save("runtime_requests", earlier, "runtime.superseded")
            record = dict(id="runtime-" + uuid.uuid4().hex, status="requested", origin=origin,
                          requested_by=requested_by, run_id=self.run_id, reason=reason, settings=settings,
                          proposed=proposed, proposed_sha=runtime.digest(proposed), created_at=now, updated_at=now)
            if delegation:
                record["delegation"] = delegation
            self._save("runtime_requests", record, "runtime.requested")
            if warrant is not None:
                self._audit("authority.receipt", record["id"], authority.receipt(warrant, key))
            self._key("runtime", key, payload, record["id"])
            return record

    def runtime_restart(self, reason, key, settings=None):
        """MCP: ask for this employee's own worker to restart, optionally with changed settings."""
        if self.run_id is None:
            raise ValueError("runtime_restart is an employee session tool; operators use `runtime request --by`.")
        return self._runtime_request("self", settings, reason, self.identity()["id"], _text(key, "key"))

    def runtime_request(self, settings, reason, by, key=None, origin="operator", delegation=None, warrant=None):
        """CLI only: the owner (origin operator) or a delegated session (M28: origin delegated, by = the actor's
        identity, delegation = the covering entry, checked by authority.require before this call)."""
        if self.run_id is not None:
            raise ValueError("runtime_request is an operator path, not a session tool.")
        if origin not in ("operator", "delegated") or (origin == "delegated") != bool(delegation):
            raise ValueError("origin delegated needs the delegation ID (and only it).")
        return self._runtime_request(origin, settings, reason, _text(by, "by"), key, delegation=delegation,
                                     warrant=warrant)

    def runtime_requests(self, limit=10):
        return list(reversed(self._all("runtime_requests")))[0:limit]

    def runtime_record(self, record, action):
        """Engine-side lifecycle transition, audited as runtime.<action>."""
        if self.run_id is not None:
            raise ValueError("Runtime lifecycle changes belong to the worker.")
        with self._tx():
            record = dict(record, updated_at=timestamp())
            return self._save("runtime_requests", record, "runtime." + action)

    def runtime_audit(self, action, record_id, data):
        with self._tx():
            self._audit("runtime." + action, record_id, data)

    def runtime_view(self):
        try:
            from . import runtime
        except ImportError:
            return None
        state = runtime.read_state(self.home)
        requests = self.runtime_requests(3)
        if state is None and not requests:
            return None
        view = {}
        if state is not None:
            view = {name: state.get(name) for name in ("effective", "source", "sha", "pid", "label",
                                                        "started_at", "problems", "probation")}
            policy = state["policy"]
            view["allowed"] = dict(models=policy.get("allowed_models"), claude=policy.get("allowed_claude"),
                                   self_service=list(runtime.SELF_SERVICE), max_self_budget=policy["baseline"]["budget"])
        view["requests"] = [{name: r.get(name) for name in ("id", "status", "origin", "requested_by", "reason",
                                                             "settings", "outcome", "updated_at")} for r in requests]
        return view

    # -- M28 D10c: target-side idle hold ----------------------------------------------------------------
    HOLD_KEY = "worker_hold"
    HOLD_SECONDS = 120

    def _hold_row(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS capture_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        row = self.db.execute("SELECT v FROM capture_meta WHERE k=?", (self.HOLD_KEY,)).fetchone()
        if row is None:
            return None
        hold = json.loads(row["v"])
        return hold if hold.get("expires_at", "") > timestamp() else None

    def worker_hold(self):
        """The unexpired idle hold on this home, or None."""
        return self._hold_row()

    def worker_hold_acquire(self, holder, seconds=None):
        """D10c: atomically check that no run is running and hold the idle boundary. While the hold is
        unexpired, run_begin refuses (WorkerHeld) so the worker starts no session. Returns the hold, or None
        when the home is busy (a running run or another unexpired hold). Expiry bounds a crashed holder."""
        if self.run_id is not None:
            raise ValueError("worker holds are taken by the CLI, not by a session tool.")
        with self._tx():
            if any(run["status"] == "running" for run in self._all("runs")) or self._hold_row() is not None:
                return None
            now = datetime.now(timezone.utc)
            hold = dict(holder, token=uuid.uuid4().hex, at=timestamp(now),
                        expires_at=timestamp(now + timedelta(seconds=seconds or self.HOLD_SECONDS)))
            self.db.execute("INSERT OR REPLACE INTO capture_meta(k,v) VALUES (?,?)", (self.HOLD_KEY, json.dumps(hold)))
            self._audit("worker.hold", hold["token"], hold)
            return hold

    def worker_hold_release(self, token):
        with self._tx():
            self.db.execute("CREATE TABLE IF NOT EXISTS capture_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            row = self.db.execute("SELECT v FROM capture_meta WHERE k=?", (self.HOLD_KEY,)).fetchone()
            if row is not None and json.loads(row["v"]).get("token") == token:
                self.db.execute("DELETE FROM capture_meta WHERE k=?", (self.HOLD_KEY,))
                self._audit("worker.hold_released", token, {})
                return True
            return False

    def run_begin(self, event_ids=None):
        """Start a run. event_ids, when given, restricts the captured inputs to those pending events."""
        with self._tx():
            if any(run["status"] == "running" for run in self._all("runs")):
                raise ValueError("Another run is active; recover interrupted runs after acquiring the worker lock.")
            held = self._hold_row()
            if held is not None:
                raise WorkerHeld("worker.manage %s holds this home's idle boundary until %s"
                                 % (held.get("op"), held.get("expires_at")))
            now = timestamp()
            if event_ids is None:
                events = self.event_admissible()
            else:
                # Exactly the named events that are still pending, in seq order, wherever they sit in the queue
                # (issue #6: not only those inside the default 50-event window).
                wanted = list(dict.fromkeys(event_ids))
                events = []
                for start in range(0, len(wanted), 500):
                    chunk = wanted[start:start + 500]
                    events.extend(self.db.execute(
                        "SELECT seq, data FROM events WHERE processed_run IS NULL AND id IN (%s)"
                        % ",".join("?" * len(chunk)), chunk).fetchall())
                events = [json.loads(row["data"]) for row in sorted(events, key=lambda row: row["seq"])]
                events = [e for e in events if not self.admission_held(e)]
            history_sequence = self.db.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM audit").fetchone()["seq"]
            record = dict(id="run-" + uuid.uuid4().hex, status="running", started_at=now, created_at=now,
                          event_ids=[e["id"] for e in events], events=events, history_sequence=history_sequence,
                          finished_at=None, summary="", error=None, cost=None)
            return self._save("runs", record, "run.started")

    def run_get(self, id):
        return self._get("runs", id)

    def run_finish(self, run_id, summary, cost=None):
        with self._tx():
            run = self._get("runs", run_id)
            if run["status"] != "running":
                raise ValueError("Only an active run can finish.")
            run.update(status="finished", summary=summary, cost=cost, finished_at=timestamp())
            run["receipt_chain"] = self._receipt_chain(run_id)
            self._stream_chain(run, run_id)
            for event_id in run["event_ids"]:
                self.db.execute("UPDATE events SET processed_run=? WHERE id=? AND processed_run IS NULL", (run_id, event_id))
            self._quarantine_left_ready(run, "finished")
            return self._save("runs", run, "run.finished")

    def run_fail(self, run_id, error, cost=None):
        """Record a failed run. cost is the session's reported cost when known."""
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            cost = None
        with self._tx():
            run = self._get("runs", run_id)
            if run["status"] != "running":
                raise ValueError("Only an active run can fail.")
            run.update(status="failed", error=str(error), cost=cost, finished_at=timestamp())
            run["receipt_chain"] = self._receipt_chain(run_id)
            self._stream_chain(run, run_id)
            self._quarantine_left_ready(run, "failed")
            return self._save("runs", run, "run.failed")

    def run_cost_backfill(self):
        """Recover cost for failed runs from their retained Claude output. Idempotent."""
        recovered = []
        with self._tx():
            for run in self._all("runs"):
                if run.get("status") != "failed" or _known_cost(run.get("cost")) is not None:
                    continue
                record_id = str(run.get("id", ""))
                if not record_id or not all(c.isalnum() or c in "-_" for c in record_id):
                    continue
                path = self.home / "runs" / (record_id + ".claude.json")
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                cost = _known_cost(raw.get("total_cost_usd") if isinstance(raw, dict) else None)
                if cost is None:
                    continue
                run["cost"] = cost
                self._save("runs", run, "run.cost_backfilled")
                recovered.append({"run_id": run["id"], "cost": cost})
        return {"recovered": recovered, "total_usd": round(sum(r["cost"] for r in recovered), 6)}

    def usage(self, day=None, days=7):
        """Per-UTC-day session cost. Unknown costs are counted, never treated as zero."""
        if day is None:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        days = max(1, int(days))
        totals = {}
        for run in self._all("runs"):
            started = run.get("started_at") or run.get("created_at") or ""
            key = str(started)[:10]
            if not key:
                continue
            entry = totals.setdefault(key, {"day": key, "cost_usd": 0.0, "runs": 0, "unknown_cost_runs": 0, "running": 0})
            entry["runs"] += 1
            if run.get("status") == "running":
                entry["running"] += 1
                continue
            cost = _known_cost(run.get("cost"))
            if cost is None:
                entry["unknown_cost_runs"] += 1
            else:
                entry["cost_usd"] += cost
        anchor = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        recent = []
        for offset in range(days):
            key = datetime.fromtimestamp(anchor.timestamp() - offset * 86400, timezone.utc).strftime("%Y-%m-%d")
            entry = dict(totals.get(key) or {"day": key, "cost_usd": 0.0, "runs": 0, "unknown_cost_runs": 0, "running": 0})
            entry["cost_usd"] = round(entry["cost_usd"], 6)
            recent.append(entry)
        return {"timezone": "UTC", "today": recent[0], "recent": recent}

    def _receipt_chain(self, run_id):
        """i17 E1: anchor the head of runs/<run>.mcp.jsonl in the run record (and so in its run.finished /
        run.failed / run.interrupted audit row), outside runs/. Never fails the run transition."""
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
            return None
        try:
            return ledgerline.chain_head(self.home / "runs" / (run_id + ".mcp.jsonl")) or {"head_sha256": None, "lines": 0}
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__}

    def _stream_chain(self, run, run_id):
        """E4: anchor the head of runs/<run>.stream.jsonl too, only when the executor stream exists (a session on
        the stream_output: false path leaves the run record exactly as before). Never fails the transition."""
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
            return
        try:
            head = ledgerline.chain_head(self.home / "runs" / (run_id + ".stream.jsonl"))
        except Exception as exc:  # noqa: BLE001
            head = {"error": type(exc).__name__}
        if head is not None:
            run["stream_chain"] = head

    RUNS_TERMINAL = ("finished", "failed", "interrupted")
    RUN_CITE = re.compile(r"(?<![0-9A-Za-z_-])run-([0-9a-f]{8,32})(?![0-9a-f])")

    def runs_pins(self):
        """E4 retention: {run_id: reason} for runs whose files are never pruned: routine fields, work evidence
        (a ref, or `run-<hex>` in a ref or note) and any memory's `source` (active or superseded; full IDs and
        8+ hex prefixes). Runs named only in other prose (work notes, messages, views, files) are not pinned."""
        pins = {}
        cited = set()
        for work in self._all("works"):
            routine = work.get("routine") if isinstance(work.get("routine"), dict) else {}
            for reason, value in (("routine coverage.run", (routine.get("coverage") or {}).get("run")),
                                  ("routine rearmed_by_run", routine.get("rearmed_by_run")),
                                  ("routine last_review.run", (routine.get("last_review") or {}).get("run"))):
                if isinstance(value, str):
                    pins.setdefault(value, reason)
            for item in work.get("evidence") or []:
                ref = item.get("ref") if isinstance(item, dict) else None
                if isinstance(ref, str) and ref.startswith("run-"):
                    pins.setdefault(ref, "work evidence")
                if isinstance(item, dict):  # E4-r1 G1: a run named inside an evidence ref or note
                    for text in (item.get("ref"), item.get("note")):
                        cited.update(("work evidence", m) for m in self.RUN_CITE.findall(str(text or "")))
        for memory in self._all("memories"):  # E4-r1 G1 (Rowan, Codex event-9c211ffd): memory sources cite runs
            cited.update(("memory source", m) for m in self.RUN_CITE.findall(str(memory.get("source") or "")))
        heads = {}
        for reason, prefix in sorted(cited):
            heads.setdefault(prefix[:8], []).append((prefix, reason))
        runs = self._all("runs")
        for run in runs:
            run_id = str(run.get("id") or "")
            for prefix, reason in heads.get(run_id[4:12], ()):  # a short prefix (8+ hex) pins every run it names
                if run_id[4:].startswith(prefix):
                    pins.setdefault(run_id, reason)
        for run in runs:
            if run.get("status") == "failed" and _known_cost(run.get("cost")) is None:
                pins.setdefault(run.get("id"), "failed run without known cost (claude.json backfill)")
        return pins

    def runs_prune(self, keep_days=30.0, max_mb=200.0, keep_newest=50, dry_run=False, now=None):
        """E4 retention of runs/ (Codex Q2, C3). Deletes only regular files named <run-id>.<suffix> directly in
        runs/ (never a symlink, a directory or anything outside runs/) for finished/failed/interrupted runs older
        than keep_days, then oldest first while runs/ is over max_mb. Never: a running run, the newest
        keep_newest runs, a pinned run (runs_pins). The run rows and their receipt_chain anchors stay. Audited
        runs.pruned unless dry_run."""
        if isinstance(keep_days, bool) or not isinstance(keep_days, (int, float)) or keep_days < 1:
            raise ValueError("keep_days must be at least 1.")
        if isinstance(max_mb, bool) or not isinstance(max_mb, (int, float)) or max_mb <= 0:
            raise ValueError("max_mb must be positive.")
        folder = self.home / "runs"
        moment = datetime.fromisoformat(timestamp(now).replace("Z", "+00:00"))
        horizon = timestamp(moment - timedelta(days=float(keep_days)))
        runs = sorted(self._all("runs"), key=lambda r: (str(r.get("started_at") or ""), str(r.get("id"))))
        newest = {r.get("id") for r in runs[-keep_newest:]} if keep_newest > 0 else set()
        status = {r.get("id"): r for r in runs}
        pins = self.runs_pins()
        files = {}
        total = 0
        unremovable = 0  # E4-r2 (Rowan L2): bytes no prune can remove (unknown runs, other names)
        try:
            entries = list(os.scandir(str(folder)))
        except FileNotFoundError:
            entries = []
        for entry in entries:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            run_id = entry.name.split(".", 1)[0]
            size = entry.stat(follow_symlinks=False).st_size
            total += size
            if "." in entry.name and run_id in status and ledgerline.RUN_ID.fullmatch(run_id):
                files.setdefault(run_id, []).append((entry.name, size))
            else:
                unremovable += size
        kept_pinned = []
        candidates = []
        for run in runs:
            run_id = run.get("id")
            if run_id not in files:
                continue
            if run.get("status") not in self.RUNS_TERMINAL:
                continue
            if run_id in pins:
                kept_pinned.append(run_id)
                continue
            if run_id in newest:
                continue
            candidates.append(run)
        chosen = []
        remaining = total
        limit = float(max_mb) * 1024 * 1024
        # E4-r2 (Rowan L2): the size bound applies to the files a prune manages. Bytes it can never remove (files
        # of unknown runs, other names) don't count, so they can't make the size pass delete young runs; they are
        # reported as unremovable_bytes, and doctor warns runs_over_bound when runs/ as a whole is over.
        for run in candidates:  # oldest first
            old = str(run.get("started_at") or "") < horizon
            if old or remaining - unremovable > limit:
                chosen.append(run)
                remaining -= sum(size for _, size in files[run["id"]])
        removed_bytes = 0
        removed = []
        for run in chosen:
            for name, size in files[run["id"]]:
                if not dry_run:
                    path = folder / name
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        path.unlink()
                    except FileNotFoundError:
                        continue
                removed_bytes += size
                removed.append(name)
        result = {"runs": len(chosen), "files": len(removed), "bytes": removed_bytes, "dry_run": bool(dry_run),
                  "oldest": chosen[0].get("started_at") if chosen else None,
                  "newest": chosen[-1].get("started_at") if chosen else None,
                  "kept_pinned": len(kept_pinned), "size_before": total, "size_after": total - removed_bytes,
                  "unremovable_bytes": unremovable, "over_bound_after": total - removed_bytes > limit,
                  "keep_days": float(keep_days), "max_mb": float(max_mb), "keep_newest": keep_newest}
        if dry_run:
            result["would_remove"] = [r.get("id") for r in chosen]
        elif chosen:  # a prune that removes nothing leaves no row (the worker checks once a day)
            with self._tx():
                self._audit("runs.pruned", "runs", result)
        return result

    def recover_runs(self):
        count = 0
        with self._tx():
            for run in self._all("runs"):
                if run["status"] == "running":
                    run.update(status="interrupted", error="Worker stopped before completing this run; inspect existing effects before retrying.",
                               finished_at=timestamp())
                    run["receipt_chain"] = self._receipt_chain(run["id"])
                    self._stream_chain(run, run["id"])
                    self._quarantine_left_ready(run, "interrupted")
                    self._save("runs", run, "run.interrupted")
                    count += 1
        return count

    def run_list(self, limit=10):
        return list(reversed(self._all("runs")))[0:limit]

    FAILED_TOOLS_LIMIT = 5

    def _failed_tools(self, run):
        """Failed MCP calls of one run, read from its runs/<id>.mcp.jsonl receipt log.

        A missing log means no tool calls were recorded. An unreadable or corrupt
        line is reported with failed_tools_unavailable rather than hidden.
        """
        result = dict(run, failed_tools=[], failed_tool_count=0)
        run_id = run.get("id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
            return result
        path = self.home / "runs" / (run_id + ".mcp.jsonl")
        if not path.exists():
            return result
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            result["failed_tools_unavailable"] = True
            return result
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                result["failed_tools_unavailable"] = True
                continue
            if not isinstance(record, dict) or record.get("success", True):
                continue
            result["failed_tool_count"] += 1
            if len(result["failed_tools"]) < self.FAILED_TOOLS_LIMIT:
                result["failed_tools"].append(dict(at=record.get("at"), tool=record.get("tool"),
                                                   error=str(record.get("error") or "")[:200]))
        return result

    def _board_section(self, run_id):
        """M27 (iv): board/board_open/board_more; absent (zero bytes) when no readable project has posts."""
        try:
            return board.section(self, capture.home_id(self), run_id=run_id)
        except Exception:  # noqa: BLE001 - a broken project log must not block the session snapshot
            return {}

    def context(self, run_id=None):
        identity = self.identity()
        run_id = run_id or self.run_id
        run = self.run_get(run_id) if run_id else None
        events = [self._with_auth(e) for e in (run["events"] if run else self.event_pending())]
        thread_ids = sorted({event["thread"] for event in events
                             if isinstance(event.get("thread"), str) and event["thread"].strip()})
        # Only currently captured conversations belong in a fresh session. The
        # rest remain durable and can be retrieved explicitly when relevant.
        threads = {thread: self._thread_history(thread, limit=30, run=run) for thread in thread_ids}
        active_ids = {memory["id"] for memory in self._all("memories") if not memory.get("superseded_by")}
        active_count = len(active_ids)
        work, questions = self.work_list("open"), self.question_list("open")
        roster_sections = self._roster_sections(identity, events, work, questions)
        # C1: self_memories is gone from the snapshot. Self-subject memories listed as an ID+gist index
        # inside `self` (self.md) are left out of both `memories` and memory_index, so each memory appears
        # exactly once (review-P1 R1). The full record stays one memory_search(query=<id>) away.
        self_index = roster_sections.pop("_self_index")
        in_self = {m["id"] for m in self_index}
        # D3 (P2): the dream's working_context has its own key, outside `memories`, the index and truncation.
        working = self._working_context()
        apart = in_self | ({working["id"]} if working else set())
        memories = [m for m in self.memory_search(limit=min(500, 60 + len(apart))) if m["id"] not in apart][:60]
        memory_index, index_truncated = self._memory_index(memories + self_index + ([working] if working else []))
        # Issue #10: truncated means some active memory is in neither `memories` nor the self index.
        hidden = active_ids - {m["id"] for m in memories} - apart
        return dict(identity=identity, **roster_sections,
                    entities=self._snapshot_entities(), memories=memories,
                    working_context=(dict(id=working["id"], kind=working["kind"], value=working["value"],
                                          created_at=working["created_at"], source=working.get("source"),
                                          replaces=working.get("replaces"))
                                     if working else None),
                    access_grants=self.access_list(),
                    memory_count=active_count, memory_context_truncated=bool(hidden),
                    memory_index=memory_index, memory_index_truncated=index_truncated,
                    work=work, events=events, threads=threads, questions=questions,
                    recent_runs=[self._failed_tools(r) for r in self.run_list(5)], usage=self.usage(days=3),
                    runtime=self.runtime_view(), outbox_stuck=self.outbox_stuck(), relay_owed=self.relay_owed(),
                    project_bindings=capture.binding_lines(self), project_capture=capture.capture_status(self), publish_failing=board.publish_failing(self),
                    project_uncovered=capture.uncovered_counts(self, capture.home_id(self)),
                    routines_due=self.routines_due(run),
                    **self._board_section(run_id))

"""M27 (iv) Board: work -> backlog projection, snapshot sections and wake scan.

The backlog in a project log is a projection of each owner's own work records, never a second list to edit.
work_put(publish=<project>) marks a work; publish_drain reconciles: it appends one ``backlog`` entry whenever the
projected fields change. Only title, origin, status, waiting_for, evidence refs and priority are projected —
never description, note or waiting_question_ids (design §3, C7).

Idempotency: key ``backlog:<home>:<work_id>:<n>:<hash>`` where n is the work's publish generation. A crash between
the project append and storing the new generation replays the same key (no duplicate); moving A -> B -> A gets a
new generation, so the board never shows a stale replayed state.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from . import capture
from .projects import BACKLOG_TITLE_MAX, BACKLOG_WAITING_MAX, POST_LIST_MAX, ProjectStore, utf8_cut

DIRTY_KEY = "publish_dirty"
RETRY_BACKOFF = 60.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _cut(text: str, limit: int) -> str:
    data = (text or "").encode("utf-8")
    if len(data) <= limit:
        return text or ""
    return data[:utf8_cut(data, limit)].decode("utf-8")


BLOCKED_BY = re.compile(r"question-[0-9a-f]{32} [a-z0-9@._:-]{1,80}")


def blocked_by(store, work: dict) -> list:
    """Docket P1b (Q2): "<question ID> <hop>" for each open question a waiting work links, never its body.

    hop = the recipient's last reported hop (with-david, answered-pending-relay, with-manager) when one has
    arrived, else with-<address> once delivered, else queued."""
    if store is None or work.get("status") != "waiting":
        return []
    out = []
    for qid in store._linked_ids(work):
        question = store._find("questions", qid)
        if not question or question.get("status") != "open":
            continue
        hop = store.hop_label(question)
        if not hop:
            message = store._find("messages", question.get("message_id") or "") or {}
            hop = "with-%s" % question["to"] if store.delivery_summary(message) == "delivered" else "queued"
        line = "%s %s" % (qid, re.sub(r"[^a-z0-9@._:-]", "-", hop.lower())[:80])
        if BLOCKED_BY.fullmatch(line):
            out.append(line)
    return out[:POST_LIST_MAX]


def projection(work: dict, store=None) -> dict:
    """The only fields of a work that ever leave the owner's home."""
    refs = [item.get("ref") for item in (work.get("evidence") or [])
            if isinstance(item, dict) and isinstance(item.get("ref"), str) and item.get("ref")]
    body = {"work_id": work["id"], "title": _cut(work.get("title") or "", BACKLOG_TITLE_MAX),
            "origin": work.get("origin"), "status": work["status"],
            "waiting_for": _cut(work.get("waiting_for") or "", BACKLOG_WAITING_MAX),
            "evidence": [_cut(r, 200) for r in refs[-POST_LIST_MAX:]], "priority": int(work.get("priority") or 0)}
    blockers = blocked_by(store, work)
    if blockers:  # absent when empty, so rows of works without linked questions keep their hash
        body["blocked_by"] = blockers
    return body


def projection_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def publish_pending(store) -> bool:
    row = store.db.execute("SELECT v FROM capture_meta WHERE k=?", (DIRTY_KEY,)).fetchone()
    return row is not None and row[0] == "1"


def _set_dirty(store, value: str) -> None:
    store.db.execute("INSERT INTO capture_meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (DIRTY_KEY, value))


def _default_opener(store):
    def opener(project):
        path = capture.project_path(store, project)
        if not path.exists():
            raise LookupError("unknown_project: no project %s" % project)
        return ProjectStore(path, project)
    return opener


def _record(store, work_id: str, **fields) -> None:
    """Update only the publish bookkeeping of a work (re-read inside the transaction; the session may write too)."""
    with store._tx():
        rec = store._find("works", work_id)
        if rec is None:
            return
        for k, v in fields.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v
        store._save("works", rec, "work.published")


def publish_drain(store, opener=None, backoff: float = 0.0) -> dict:
    """Reconcile every published (or previously published) work with its project's backlog. Never raises."""
    opener = opener or _default_opener(store)
    with store._tx():
        _set_dirty(store, "0")  # a concurrent work_put sets it again
    home = capture.home_id(store)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=backoff)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    appended, failed, deferred = 0, 0, 0
    for work in store._all("works"):
        target = work.get("publish") or ""
        pub = dict(work.get("published") or {})
        if not target and not pub.get("project"):
            if work.get("publish_failing"):  # unpublished before it ever reached the board: nothing to retry
                _record(store, work["id"], publish_failing=None)
            continue
        failing = work.get("publish_failing") or {}
        if backoff and failing.get("last_attempt") and failing["last_attempt"] > cutoff:
            deferred += 1
            continue
        steps = []
        n = int(pub.get("n") or 0)
        if pub.get("project") and pub["project"] != target:
            body = dict(projection(work), status="unpublished", waiting_for="", evidence=[])
            steps.append((pub["project"], body, "unpublished", None))
        if target:
            body = projection(work, store)
            h = projection_hash(body)
            if pub.get("project") != target or pub.get("hash") != h:
                steps.append((target, body, h, target))
        for project, body, tag, now_published in steps:
            key = "backlog:%s:%s:%d:%s" % (home, work["id"], n + 1, tag)
            try:
                with opener(project) as ps:
                    ps.append([{"kind": "backlog", "body": body}], author=home, key=key)
            except Exception as exc:  # noqa: BLE001 - visible as publish_failing, retried with backoff
                failed += 1
                err = "%s: %s" % (type(exc).__name__, exc)
                attempts = int(failing.get("attempts") or 0) + 1 if failing.get("project") == project else 1
                _record(store, work["id"], publish_failing={"project": project, "error": err[:200],
                                                            "attempts": attempts, "last_attempt": _now()})
                break
            n += 1
            pub = {"project": now_published, "hash": tag if now_published else None, "n": n}
            _record(store, work["id"], published=pub, publish_failing=None)
            failing = {}
            appended += 1
    if failed or deferred:
        with store._tx():
            _set_dirty(store, "1")
    return {"appended": appended, "failed": failed, "deferred": deferred}


def publish_failing(store) -> list:
    """C8: owner's snapshot problem lines for refused or failing publications."""
    out = []
    for work in store._all("works"):
        f = work.get("publish_failing")
        if f:
            out.append({"work_id": work["id"], "project": f.get("project"), "error": f.get("error"),
                        "attempts": f.get("attempts")})
    return out


# ---- step 3: snapshot sections `board` and `board_open` (design §4) ------------------------------------------

SECTION_BYTES = 3072
SECTION_ITEMS = 15
OPEN_ITEMS = 5


def readable_projects(store, me: str, opener=None):
    opener = opener or _default_opener(store)
    root = capture.projects_root(store)
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.name in capture.RESERVED_PROJECTS or not (d / "memory.sqlite3").exists():
            continue
        try:
            with opener(d.name) as ps:
                if ps.may_read(me):
                    out.append(d.name)
        except Exception:  # noqa: BLE001 - an unreadable project is not the reader's problem here
            continue
    return out


def _meta(store, k):
    row = store.db.execute("SELECT v FROM capture_meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else None


def section(store, me: str, run_id=None, opener=None) -> dict:
    """Gist-only entries newer than this home's cursor, plus open threads addressed to me. {} when no posts."""
    opener = opener or _default_opener(store)
    items, open_threads, heads = [], [], {}
    for name in readable_projects(store, me, opener):
        with opener(name) as ps:
            st = ps.state()
            head = ps.head_seq()
        posts, rows = st.get("posts") or {}, st.get("backlog") or {}
        if not posts and not rows:
            continue
        heads[name] = head
        seen = int(_meta(store, "board_seen:" + name) or 0)
        for p in posts.values():
            if p["seq"] > seen:
                items.append({"project": name, "seq": p["seq"], "post": p["id"], "author": p["author"],
                              "topic": p["topic"], "gist": p["gist"], "reply_to": p["reply_to"],
                              "mentions_me": me in (p.get("mentions") or [])})
        for r in rows.values():
            if r["seq"] > seen:
                items.append({"project": name, "seq": r["seq"], "owner": r["owner"], "work_id": r["work_id"],
                              "title": r["title"], "status": r["status"]})
        mine = {pid for pid, p in posts.items() if p["author"] == me}
        for p in posts.values():
            if p["author"] == me or not (me in (p.get("mentions") or []) or p.get("reply_to") in mine):
                continue
            replied = any(posts.get(c, {}).get("author") == me for c in _descendants(posts, p["id"]))
            if not replied:
                open_threads.append({"project": name, "post": p["id"], "author": p["author"], "topic": p["topic"],
                                     "seq": p["seq"]})
    if not heads:
        return {}
    items.sort(key=lambda i: -i["seq"])
    shown, size = [], 0
    for item in items:
        n = len(json.dumps(item, ensure_ascii=False))
        if len(shown) >= SECTION_ITEMS or size + n > SECTION_BYTES:
            break
        shown.append(item)
        size += n
    if run_id is not None:
        with store._tx():
            store.db.execute("INSERT INTO capture_meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                             ("board_snap:" + run_id, json.dumps(heads)))
    open_threads.sort(key=lambda i: -i["seq"])
    out = {"board": shown, "board_open": open_threads[:OPEN_ITEMS]}
    if len(items) > len(shown):
        out["board_more"] = len(items) - len(shown)
    return out


def _descendants(posts, pid):
    stack, seen = list(posts.get(pid, {}).get("replies") or []), []
    while stack:
        c = stack.pop()
        seen.append(c)
        stack.extend(posts.get(c, {}).get("replies") or [])
    return seen


def advance(store, run_id: str, finished: bool) -> None:
    """At run end: a finished run moves board_seen:<p> to the heads captured in its snapshot; a failed run doesn't."""
    k = "board_snap:" + run_id
    raw = _meta(store, k)
    if raw is None:
        return
    with store._tx():
        if finished:
            for name, head in json.loads(raw).items():
                store.db.execute("INSERT INTO capture_meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET "
                                 "v=CAST(MAX(CAST(v AS INTEGER), CAST(excluded.v AS INTEGER)) AS TEXT)",
                                 ("board_seen:" + name, str(head)))
        store.db.execute("DELETE FROM capture_meta WHERE k=?", (k,))


# ---- step 4: wake scan (design §5; C1-C4, Q4) ------------------------------------------------------------------
WAKE_MENTIONS = 3        # C3: only the first 3 mentions of a post wake
THREAD_WAKES_PER_DAY = 3  # C1: per home, per thread, per UTC day; then the thread shows only in board_open


def _wake_reason(post: dict, entry: dict, posts: dict, owned: set, me: str):
    if entry["author"] == me:
        return None
    if me in list(post.get("mentions") or [])[:WAKE_MENTIONS]:
        return "mention"
    parent = posts.get(post.get("reply_to") or "")
    if parent and parent["author"] == me:
        return "reply"
    if owned & set(post.get("refs") or []):
        return "owned_work"
    return None


def scan(store, me: str | None = None, opener=None, now: datetime | None = None) -> list:
    """Feed one local event per new post that concerns this employee. Cursor capture_meta board_scan:<p> moves
    past every scanned entry (a capped or irrelevant post never wakes later). Returns the event ids created."""
    opener = opener or _default_opener(store)
    me = me or store.identity()["id"]
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    prune(store, day)
    created = []
    for name in readable_projects(store, me, opener):
        k = "board_scan:" + name
        with opener(name) as ps:
            head = ps.head_seq()
            cursor = _meta(store, k)
            if cursor is None:
                # First scan of a project: start at its head; old discussion shows in the board section, never wakes.
                _put_meta(store, k, str(head))
                continue
            cursor = int(cursor)
            if head <= cursor:
                continue
            fresh = [e for e in ps.entries() if e["seq"] > cursor and e["kind"] == "post"]
            st = ps.state() if fresh else {}
        posts = st.get("posts") or {}
        owned = {r["work_id"] for r in (st.get("backlog") or {}).values() if r["owner"] == me}
        for e in fresh:
            post = posts.get(e["id"])
            if post is None or post.get("redacted") or e["body"].get("redacted"):
                continue  # retracted by its author, or redacted by the operator, before we saw it
            reason = _wake_reason(post, e, posts, owned, me)
            if reason is None:
                continue
            thread = "board:%s:%s" % (name, post["root"])
            ck = "board_wakes:%s:%s" % (day, thread)
            count = int(_meta(store, ck) or 0)
            if count >= THREAD_WAKES_PER_DAY:
                continue  # C1: ping-pong guard; the thread stays visible in board_open
            body = ("Board post %s in project %s (%s) by %s\ntopic: %s\nreply_to: %s\nrefs: %s\n\n%s" % (
                e["id"], name, reason, e["author"], e["body"].get("topic", ""), e["body"].get("reply_to") or "-",
                ", ".join(e["body"].get("refs") or []) or "-", e["body"].get("body", "")))
            event = store.board_feed(e["author"], body, project=name, external_id=e["id"], thread=thread,
                                     occurred_at=e["at"], authenticated=bool(e.get("run")))
            _put_meta(store, ck, str(count + 1))
            created.append(event["id"])
        _put_meta(store, k, str(head))
    return created


def prune(store, day: str) -> dict:
    """M27 (iv) N3: at most once per UTC day, drop board_wakes:<d>:* counters of earlier days and board_snap:<run>
    heads whose run is no longer running (a crashed run never reaches advance)."""
    if _meta(store, "board_pruned") == day:
        return {}
    running = {r["id"] for r in store._all("runs") if r.get("status") == "running"}
    with store._tx():
        wakes = store.db.execute("DELETE FROM capture_meta WHERE k LIKE 'board_wakes:%' AND substr(k, 13, 10) < ?",
                                 (day,)).rowcount
        snaps = [k for (k,) in store.db.execute("SELECT k FROM capture_meta WHERE k LIKE 'board_snap:%'")
                 if k[len("board_snap:"):] not in running]
        for k in snaps:
            store.db.execute("DELETE FROM capture_meta WHERE k=?", (k,))
        store.db.execute("INSERT INTO capture_meta(k,v) VALUES ('board_pruned',?) ON CONFLICT(k) DO UPDATE SET "
                         "v=excluded.v", (day,))
    return {"board_wakes": wakes, "board_snap": len(snaps)}


def _put_meta(store, k: str, v: str) -> None:
    with store._tx():
        store.db.execute("INSERT INTO capture_meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

"""M21 MCP tools for project memory (r7 D3.6, D4, D5, r6 3.3).

Every write is fenced on the session run (ProjectStore.append(fence=<employee Store>, run=<session run>)) and is
attributed to the session's employee ID, never to an argument. Reads are gated by the project's read grant.
Source entries enter a project only by binding capture or by ``project_promote`` (byte-exact substring check);
``project_memory_append`` refuses ``source`` and operator kinds. Grants, bindings and redaction are not MCP tools.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import capture, projectview, roster
from .projects import POST_LIST_MAX, ProjectError, ProjectStore, utf8_cut

PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
APPEND_KINDS = ("intent", "lesson", "relate", "status", "retract", "confirm", "considered", "post")
# M27 (iv): "backlog" is never appendable by a model; only the work_put projection (board.publish_drain) writes it.
BOARD_READ_BYTES = 8192
# #64 S1e: the serialized plain board_read (and each backlog_cursor page) stays under the search tools' maximum. Same
# margin, not a measured line (docs/ledgerline.md). A row over BACKLOG_ROW_BYTES, which no projected work reaches, is
# cut and marked, so the posts, the meeting notice and one row always fit.
BOARD_RESULT_BYTES = 32000
BACKLOG_ROW_BYTES = 16384
BACKLOG_FIELD_BYTES = 200  # never shorter than the projection's own caps (BACKLOG_TITLE_MAX, BACKLOG_WAITING_MAX)
BACKLOG_FIELDS = ("owner", "work_id", "title", "origin", "status", "waiting_for", "priority", "blocked_by")
BACKLOG_CLOSED = ("done", "cancelled")
BACKLOG_CURSOR = re.compile(r"(0|[1-9][0-9]{0,6})-([0-9a-f]{64})")


def _text(description=""):
    return {"type": "string", "description": description}


def _int(minimum, maximum=None, description=""):
    field = {"type": "integer", "minimum": minimum, "description": description}
    if maximum is not None:
        field["maximum"] = maximum
    return field


def _tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False}}


PROJECT = _text("Project name, e.g. synthetics.")
KEY = _text("Stable descriptive key for this intended operation. Reuse after interruption; a different payload "
            "with the same key is rejected.")
AS_OF = _int(0, description="Read the project as of this log seq (default: head). Responses carry as_of_seq.")
# Backlog 17: the per-kind body fields, so a writer needn't discover them from errors.
ENTRY_BODIES = (
    "intent {title, claims [{cid, text}], sources [pm-… human source], scope_note?, open?}; "
    "lesson {statement, applies_when, uncertainty (all required; write \"none known\"), title?, lesson_kind?, "
    "evidence? [{ref, level, note}]}; "
    "relate {rel revises|refines|carries|context|disputes, from pm-…#cN, target pm-…[#cN], expected_head for revises}; "
    "status {target lesson, to accepted|disputed|retired, reason}; retract {target, reason}; "
    "confirm {target pm-…#cN, source}; considered {source pm-…, reason, outcome? captured|not-captured}; "
    "post {topic, body, mentions?, refs?, reply_to?}.")
ENTRY = {"type": "object", "additionalProperties": True,
         "description": "{kind, body}; every other field goes inside body. Bodies: " + ENTRY_BODIES}

TOOLS = [
    _tool("project_memory_append",
          "Append human-intent claims, team lessons, relations, status, retractions, confirmations or 'considered' "
          "coverage to a project's shared log, atomically. Attributed to you and fenced on this session. An intent "
          "must cite source entries whose role is human (David's own words); a revision needs expected_head. "
          "Sources enter only by binding capture or project_promote.",
          {"project": PROJECT, "entries": {"type": "array", "items": ENTRY, "description": "One batch."},
           "key": KEY, "expected_seq": _int(0, description="Reject if the log has moved past this seq.")},
          ["project", "entries", "key"]),
    _tool("project_memory_get",
          "Read one project record or source entry, bounded to ~8 KB: claims, each claim's current head, nearest "
          "history and a quote excerpt; for a lesson also applies_when, uncertainty and its evidence refs. "
          "evidence_truncated flags omitted evidence notes or refs; evidence_count keeps the stored count. "
          "part='quote' pages a source's full bytes with cursor.",
          {"project": PROJECT, "id": _text("pm-… record or source ID."), "history": _int(0, 20),
           "cursor": _int(0), "part": {"type": "string", "enum": ["quote"]}, "as_of": AS_OF},
          ["project", "id"]),
    _tool("project_memory_search",
          "Search a project's current intent and lesson records (superseded/retired excluded unless status asks). "
          "Results are gists; fetch with project_memory_get before relying on one.",
          {"project": PROJECT, "query": _text(), "kind": {"type": "string", "enum": ["intent", "lesson"]},
           "status": _text(), "limit": _int(1, 50), "as_of": AS_OF},
          ["project", "query"]),
    _tool("project_uncovered",
          "List project sources nobody has marked 'considered' (projects you can read) and human messages in your "
          "own home that were neither captured, promoted nor skipped. Private part is visible only to you.",
          {"project": PROJECT, "limit": _int(1, 50), "cursor": _int(0)}),
    _tool("project_skip",
          "Mark a human message in your own home as handled without promoting it. Stored privately in your home only.",
          {"event_id": _text(), "reason": _text()}, ["event_id", "reason"]),
    _tool("project_promote",
          "Copy an exact byte excerpt of an event in your home, or of a file under a granted folder, into a project "
          "as a source entry (max 64 KB, never truncated). Optional considered reason and extra entries (e.g. an "
          "intent citing it) join the same atomic batch; refer to the new source as '$source'.",
          {"project": PROJECT, "text": _text("Exact excerpt."), "event_id": _text(), "file": _text(),
           "considered": _text("Optional reason; appends a 'considered' entry for the new source."),
           "entries": {"type": "array", "items": ENTRY}, "key": KEY},
          ["project", "text", "key"]),
 _tool("board_post",
          "Post to a project's Board (needs a post or write grant): share an insight, proposal or status with the "
          "team. Only the first 3 mentions wake their recipients; a reply wakes the parent's author. Use "
          "question_ask for anything you need answered. Retract your own post with project_memory_append "
          "{kind: retract, body: {target: <post id>, reason}}.",
          {"project": PROJECT, "topic": _text("≤120 bytes."), "body": _text("≤2 KB."),
           "mentions": {"type": "array", "items": {"type": "string"},
                        "description": "≤8 people (ID, name or alias; resolved to canonical IDs)."},
           "reply_to": _text("Post ID (pm-…) this replies to."),
           "refs": {"type": "array", "items": {"type": "string"},
                    "description": "≤8 related IDs (work, project records); context only, never a lesson's source."},
           "key": KEY},
          ["project", "topic", "body", "key"]),
    _tool("board_read",
          "Read Board posts of one thread (thread = any post ID in it) or since a log seq, oldest first, bounded "
          "to ~8 KB. With neither, also the project's published backlog: open rows (ready, waiting) only, whole rows "
          "until the result would pass 32000 bytes. backlog_total and backlog_left_out count open and closed rows; "
          "while backlog_next is not null, pass it as backlog_cursor for the next rows, without posts: the rest of "
          "the open ones first, then the closed ones (done, cancelled), newest change first. An empty backlog with a "
          "backlog_next that is not null means closed rows only.",
          {"project": PROJECT, "thread": _text("A post ID; returns its whole thread."), "since": _int(0),
           "backlog_cursor": _text("backlog_next of the previous page; not together with thread or since. Refused "
                                   "once any backlog row has changed: then start again without it.")}),
]
NAMES = {t["name"] for t in TOOLS}
MUTATIONS = {"project_memory_append", "project_skip", "project_promote", "board_post"}


def _resolve_mentions(store, batch):
    """Design §1: post mentions resolve through the company roster to canonical IDs; unknown → unknown_person.
    With no company people at all (no roster yet) mentions are kept verbatim."""
    company = roster.company_state_for_home(store.home)
    if not company.get("people"):
        return batch
    for e in batch:
        if e["kind"] != "post" or not isinstance(e["body"], dict):
            continue
        out = []
        for m in e["body"].get("mentions") or []:
            pid = store._company_person(m, company) if isinstance(m, str) else None
            if pid is None:
                raise ProjectError("unknown_person", "mention %r names no company person" % (m,), name=m)
            out.append(pid)
        if "mentions" in e["body"]:
            e["body"]["mentions"] = out
    return batch


def _dump(value):
    """The text tools/call sends for this value (mcp.Server.respond)."""
    return json.dumps(value, ensure_ascii=False)


def _size(value):
    return len(_dump(value).encode("utf-8"))


def _cut_field(value):
    """A value of an oversized row, cut; the same value when nothing had to go."""
    if isinstance(value, str):
        data = value.encode("utf-8")
        return value if len(data) <= BACKLOG_FIELD_BYTES else data[:utf8_cut(data, BACKLOG_FIELD_BYTES)].decode("utf-8")
    if isinstance(value, list):
        return [_cut_field(v) for v in value[:POST_LIST_MAX]]
    return value if _size(value) <= BACKLOG_FIELD_BYTES else None


def _backlog_row(r):
    row = {k: r.get(k) for k in BACKLOG_FIELDS if k != "blocked_by" or r.get("blocked_by")}
    if _size(row) <= BACKLOG_ROW_BYTES:
        return row
    short = {k: _cut_field(v) for k, v in row.items()}
    return dict(short, cut=[k for k in row if short[k] != row[k]])


def _backlog_page(ps, rows, cursor, result, notice):
    """Add backlog rows to `result` without passing BOARD_RESULT_BYTES. A page holds open rows or closed rows, never
    both; the cursor is a position and a hash of the project and its rows, so any change of the backlog refuses it."""
    opened = sorted((r for r in rows if r.get("status") not in BACKLOG_CLOSED),
                    key=lambda r: (projectview.priority_rank(r.get("priority")), r["owner"], r["work_id"]))
    closed = sorted((r for r in rows if r.get("status") in BACKLOG_CLOSED), key=lambda r: -r["seq"])
    rows = opened + closed
    digest = hashlib.sha256(_dump([ps.project, rows]).encode("utf-8")).hexdigest()
    start, stop = 0, len(opened)
    if cursor is not None:
        match = BACKLOG_CURSOR.fullmatch(cursor)
        if not match:
            raise ProjectError("bad_cursor", "backlog_cursor is not a backlog_next value")
        if match.group(2) != digest:
            raise ProjectError("stale_cursor", "backlog_cursor is not for the current backlog of project %s (it "
                               "changed, or the cursor is another project's): start again without it" % ps.project)
        start = int(match.group(1))
        if start >= len(rows):
            raise ProjectError("bad_cursor", "backlog_cursor is not a backlog_next value")
        stop = len(opened) if start < len(opened) else len(rows)

    def page(end, taken):
        value = dict(result, backlog=taken,
                     backlog_total={"open": len(opened), "closed": len(closed)},
                     backlog_left_out={"open": max(0, len(opened) - end), "closed": len(rows) - max(end, len(opened))},
                     backlog_next="%d-%s" % (end, digest) if end < len(rows) else None)
        return value, _size(value if notice is None else dict(value, **{"yield": notice}))

    taken = []
    value, _ = page(start, taken)
    for end in range(start + 1, stop + 1):
        more = taken + [_backlog_row(rows[end - 1])]
        trial, size = page(end, more)
        if size > BOARD_RESULT_BYTES:
            break
        value, taken = trial, more
    return value


def _board_read(ps, thread=None, since=None, cursor=None, notice=None):
    """`notice` is the meeting notice the server adds to the result: counted here, never added here."""
    st = ps.state()
    posts = st.get("posts") or {}
    rows = list((st.get("backlog") or {}).values())
    if cursor is not None:
        return _backlog_page(ps, rows, cursor, {"project": ps.project, "as_of_seq": ps.head_seq()}, notice)
    if thread is not None:
        if thread not in posts:
            raise ProjectError("unknown_target", "%s is not a live post" % thread)
        root = posts[thread]["root"]
        want = {pid for pid, p in posts.items() if p["root"] == root}
    else:
        want = {pid for pid, p in posts.items() if p["seq"] > int(since or 0)}
    out, used, more = [], 0, 0
    for e in ps.entries():
        if e["kind"] != "post" or e["id"] not in want:
            continue
        item = {"id": e["id"], "seq": e["seq"], "at": e["at"], "author": e["author"],
                "topic": e["body"].get("topic", ""), "body": e["body"].get("body", ""),
                "mentions": e["body"].get("mentions", []), "reply_to": e["body"].get("reply_to"),
                "refs": e["body"].get("refs", []), "replies": posts[e["id"]].get("replies", [])}
        size = len(json.dumps(item))
        if used + size > BOARD_READ_BYTES:
            more += 1
            continue
        used += size
        out.append(item)
    result = {"project": ps.project, "posts": out, "more": more, "as_of_seq": ps.head_seq()}
    if thread is None and since is None:
        return _backlog_page(ps, rows, None, result, notice)
    return result


def _project(name):
    if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
        raise ValueError("Project names are lower-case letters, digits, dash or underscore.")
    if name in capture.RESERVED_PROJECTS:
        raise ValueError("%s is a reserved name, not a project." % name)
    return name


def _me(store):
    return store.identity()["id"]


def _open(store, project, *, must_exist=True):
    path = capture.project_path(store, _project(project))
    if must_exist and not path.exists():
        raise ProjectError("not_found", "no project %s" % project)
    return ProjectStore(path, project)


def _readable(ps, me):
    if not ps.may_read(me):
        raise ProjectError("no_grant", "%s has no read grant on project %s" % (me, ps.project), need="read")


def _entries(entries):
    out = []
    for n, raw in enumerate(entries or []):
        if not isinstance(raw, dict) or set(raw) - {"kind", "body", "id"}:
            raise ProjectError("bad_entry", "entry %d must be {kind, body}; put fields such as %s inside body"
                              % (n, ", ".join(sorted(set(raw) - {"kind", "body", "id"})) if isinstance(raw, dict)
                                 else "these"))
        if raw.get("kind") not in APPEND_KINDS:
            raise ProjectError("bad_kind", "entry %d: kind %r is not appendable here" % (n, raw.get("kind")))
        out.append({"kind": raw["kind"], "body": raw.get("body", {})})
    return out


def call(store, name, arguments, notice=None):
    """`notice`: the meeting notice the server will add to this result, so board_read can count its bytes."""
    me = _me(store)
    run = store.run_id
    if name == "project_memory_append":
        batch = _resolve_mentions(store, _entries(arguments["entries"]))
        with _open(store, arguments["project"], must_exist=False) as ps:
            return ps.append(batch, author=me, run=run, fence=store.path,
                             key="mcp:%s:%s" % (me, arguments["key"]), expected_seq=arguments.get("expected_seq"))
    if name == "project_memory_get":
        with _open(store, arguments["project"]) as ps:
            _readable(ps, me)
            return ps.get(arguments["id"], history=arguments.get("history", 5), cursor=arguments.get("cursor", 0),
                          part=arguments.get("part"), as_of=arguments.get("as_of"))
    if name == "project_memory_search":
        with _open(store, arguments["project"]) as ps:
            _readable(ps, me)
            return ps.search(arguments["query"], kind=arguments.get("kind"), status=arguments.get("status"),
                             limit=arguments.get("limit", 10), as_of=arguments.get("as_of"))
    if name == "project_uncovered":
        projects = [_project(arguments["project"])] if arguments.get("project") else None
        return capture.uncovered(store, me, projects, limit=arguments.get("limit", 20),
                                 cursor=arguments.get("cursor", 0))
    if arguments.get("event_id") is not None:
        arguments = dict(arguments, event_id=store._resolve(arguments["event_id"], "events"))  # backlog 24
    if name == "project_skip":
        return capture.skip(store, arguments["event_id"], arguments["reason"], run=run)
    if name == "project_promote":
        if ("event_id" in arguments) == ("file" in arguments):
            raise ProjectError("bad_origin", "name exactly one of event_id or file")
        project = _project(arguments["project"])
        roots = [g["path"] for g in store.access_list()]
        # '$source' in extra entries is resolved by capture.promote once the new source's ID is chosen.
        considered = ({"outcome": "not-captured", "reason": arguments["considered"]}
                      if arguments.get("considered") else None)
        return capture.promote(store, project, text=arguments["text"], author=me,
                               event_id=arguments.get("event_id"), file=arguments.get("file"),
                               granted_roots=roots, considered=considered,
                               extra=_entries(arguments.get("entries")), run=run, fence=store.path,
                               key="mcp:%s:%s" % (me, arguments["key"]))
    if name == "board_post":
        body = {"topic": arguments["topic"], "body": arguments["body"],
                "mentions": list(arguments.get("mentions") or []), "refs": list(arguments.get("refs") or []),
                "reply_to": arguments.get("reply_to") or None}
        batch = _resolve_mentions(store, [{"kind": "post", "body": body}])
        with _open(store, arguments["project"]) as ps:
            return ps.append(batch, author=me, run=run, fence=store.path,
                             key="mcp:%s:%s" % (me, arguments["key"]))
    if name == "board_read":
        if arguments.get("thread") and arguments.get("since") is not None:
            raise ProjectError("bad_args", "name thread or since, not both")
        cursor = arguments.get("backlog_cursor")
        if cursor is not None and (arguments.get("thread") is not None or arguments.get("since") is not None):
            raise ProjectError("bad_args", "backlog_cursor cannot be combined with thread or since")
        with _open(store, arguments["project"]) as ps:
            _readable(ps, me)
            return _board_read(ps, arguments.get("thread"), arguments.get("since"), cursor, notice)
    raise ValueError("Unknown tool: %s" % name)

"""Project memory: pure fold over an append-only entry log (M21; design r7 + r7a + r7b).

``fold(entries)`` is deterministic: no I/O, clock, randomness or messages. Entries are dicts with
``seq, id, kind, author, run, at, body``; they are folded in seq order. The project store keeps the
materialised result of this fold and tests assert a full re-fold equals it.
"""

from __future__ import annotations

import copy

ENTRY_KINDS = (
    "source", "intent", "lesson", "relate", "status", "retract",
    "confirm", "considered", "proposal", "redact", "binding", "post", "backlog",
)
# M27 (iv) Board: discussion posts and the projected backlog (owner work records, never edited here).
BOARD_KINDS = ("post", "backlog")
BACKLOG_STATUSES = ("ready", "waiting", "done", "cancelled", "unpublished")
BACKLOG_ORIGINS = ("self", "assigned")
POST_GIST = 160
CLAIM_RELS = ("revises", "refines", "carries", "context", "disputes")
LESSON_STATUSES = ("proposed", "accepted", "disputed", "superseded", "retired")
SOURCE_ROLES = ("human", "secretary", "out", "other")


def split_ref(ref: str) -> tuple[str, str | None]:
    """'pm-x#c2' -> ('pm-x', 'c2'); 'pm-x' -> ('pm-x', None)."""
    if "#" in ref:
        rid, cid = ref.split("#", 1)
        return rid, cid
    return ref, None


def active_entries(entries: list[dict]) -> list[dict]:
    """Entries whose effects apply: a retract removes its target unless the retract is itself retracted."""
    ordered = sorted(entries, key=lambda e: e["seq"])
    retracted_by: dict[str, list[str]] = {}
    for e in ordered:
        if e["kind"] == "retract":
            retracted_by.setdefault(e["body"]["target"], []).append(e["id"])
    active: dict[str, bool] = {}
    for e in reversed(ordered):
        active[e["id"]] = not any(active.get(r, False) for r in retracted_by.get(e["id"], ()))
    return [e for e in ordered if active[e["id"]]]


def _ceiling(source: dict) -> str:
    if source.get("redacted"):
        return "none"
    if source.get("quote_source") == "event" and source.get("auth"):
        return "verified"
    return "observed"


def fold(entries: list[dict]) -> dict:
    """Return the project state derived from ``entries``."""
    state: dict = {
        "through": max((e["seq"] for e in entries), default=0),
        "sources": {},
        "records": {},
        "claims": {},      # "pm-x#c1" -> {record, cid, text, head, superseded_by, confirmed, links}
        "coverage": {},    # source id -> [considered entry ids]
        "bindings": {},
        "posts": {},       # post id -> {author, topic, gist, mentions, reply_to, root, refs, seq, at, replies}
        "backlog": {},     # "<owner>|<work_id>" -> latest projected row
        "retracted": sorted(e["body"]["target"] for e in entries if e["kind"] == "retract"),
    }
    live = active_entries(entries)
    redacted = {e["body"]["target"] for e in live if e["kind"] == "redact"}
    for e in live:
        kind, body = e["kind"], e["body"]
        if kind == "source":
            origin = body.get("origin", {})
            role = origin.get("role", "other")
            qs = body.get("quote_source", "event")
            if role == "secretary":
                qs = "secondhand"
            src = {
                "id": e["id"], "seq": e["seq"], "role": role, "direction": origin.get("direction", "in"),
                "thread": origin.get("thread"), "auth": copy.deepcopy(origin.get("auth")),
                "quote_source": qs, "capture": copy.deepcopy(body.get("capture", {})),
                "part": copy.deepcopy(body.get("part")), "redacted": e["id"] in redacted,
            }
            src["ceiling"] = _ceiling(src)
            state["sources"][e["id"]] = src
            state["coverage"].setdefault(e["id"], [])
        elif kind in ("intent", "lesson"):
            rec = {
                "id": e["id"], "kind": kind, "seq": e["seq"], "author": e["author"],
                "title": body.get("title", ""), "sources": list(body.get("sources", [])),
                "flags": [], "claims": [],
            }
            if kind == "intent":
                rec["act"] = body.get("act")
                rec["open"] = copy.deepcopy(body.get("open"))
                for c in body.get("claims", []):
                    ref = f"{e['id']}#{c['cid']}"
                    state["claims"][ref] = {
                        "record": e["id"], "cid": c["cid"], "text": c["text"], "head": ref,
                        "superseded_by": None, "confirmed": None, "links": [],
                    }
                    rec["claims"].append(c["cid"])
            else:
                rec["lesson_kind"] = body.get("lesson_kind")
                rec["statement"] = body.get("statement", "")
                rec["status"] = "proposed"
                rec["disputed_with"] = []
            state["records"][e["id"]] = rec
        elif kind == "relate":
            _apply_relate(state, body, e)
        elif kind == "status":
            rec = state["records"].get(body["target"])
            if rec and rec["kind"] == "lesson":
                rec["status"] = body["to"]
                if body["to"] != "disputed":
                    rec["disputed_with"] = []
        elif kind == "confirm":
            claim = state["claims"].get(body["target"])
            if claim:
                claim["confirmed"] = {"entry": e["id"], "source": body.get("source")}
        elif kind == "considered":
            state["coverage"].setdefault(body["source"], []).append(e["id"])
        elif kind == "binding":
            state["bindings"][body["binding_id"]] = copy.deepcopy(body)
        elif kind == "post":
            parent = state["posts"].get(body.get("reply_to") or "")
            gone = bool(body.get("redacted")) or e["id"] in redacted  # backlog 27: placeholder keeps the thread
            state["posts"][e["id"]] = {
                "id": e["id"], "author": e["author"], "topic": "(redacted)" if gone else body.get("topic", ""),
                "gist": "" if gone else _post_gist(body.get("body", "")),
                "mentions": [] if gone else list(body.get("mentions", [])),
                "reply_to": body.get("reply_to"), "root": parent["root"] if parent else e["id"],
                "refs": [] if gone else list(body.get("refs", [])), "seq": e["seq"], "at": e["at"], "replies": [],
                "redacted": gone,
            }
            if parent:
                parent["replies"].append(e["id"])
        elif kind == "backlog":
            row = {k: copy.deepcopy(body.get(k)) for k in (
                "work_id", "title", "origin", "status", "waiting_for", "evidence", "priority")}
            row.update({"owner": e["author"], "entry": e["id"], "seq": e["seq"], "at": e["at"]})
            k = f"{e['author']}|{body.get('work_id')}"
            if body.get("status") == "unpublished":
                state["backlog"].pop(k, None)
            else:
                state["backlog"][k] = row
    _finish(state)
    return state


def _post_gist(text: str) -> str:
    data = " ".join(str(text).split()).encode("utf-8")
    if len(data) <= POST_GIST:
        return data.decode("utf-8")
    return data[:POST_GIST].decode("utf-8", "ignore") + "…"


def _head(state: dict, ref: str) -> str:
    seen = set()
    while ref in state["claims"] and state["claims"][ref]["head"] != ref and ref not in seen:
        seen.add(ref)
        ref = state["claims"][ref]["head"]
    return ref


def _apply_relate(state: dict, body: dict, e: dict) -> None:
    rel, src, target = body["rel"], body["from"], body["target"]
    tid, tcid = split_ref(target)
    if rel == "revises" and tcid is not None:
        claim = state["claims"].get(target)
        if claim is not None:
            claim["head"] = src
            claim["superseded_by"] = src
    elif rel == "revises":
        rec = state["records"].get(tid)
        if rec and rec["kind"] == "lesson":
            rec["status"] = "superseded"
    elif rel == "disputes":
        sid, _ = split_ref(src)
        for a, b in ((tid, sid), (sid, tid)):
            rec = state["records"].get(a)
            if rec and rec["kind"] == "lesson" and rec["status"] not in ("superseded", "retired"):
                rec["status"] = "disputed"
                rec["disputed_with"].append(b)
    if tcid is not None and target in state["claims"]:
        state["claims"][target]["links"].append({"rel": rel, "from": src, "entry": e["id"]})


def _finish(state: dict) -> None:
    sources = state["sources"]
    for ref, claim in state["claims"].items():
        claim["head"] = _head(state, ref)
        claim["status"] = "current" if claim["head"] == ref else "superseded"
    for rec in state["records"].values():
        cited = [sources[s] for s in rec["sources"] if s in sources]
        flags = set()
        if cited and all(s["redacted"] for s in cited):
            flags.add("withdrawn")
        if any(s["quote_source"] == "secondhand" for s in cited):
            flags.add("secondhand")
        live = [s for s in cited if not s["redacted"]]
        rec["ceiling"] = "verified" if live and all(s["ceiling"] == "verified" for s in live) else (
            "observed" if live else "none")
        if rec["ceiling"] == "observed":
            flags.add("observed-only")
        if rec["kind"] == "intent":
            current = [c for c in rec["claims"] if state["claims"][f"{rec['id']}#{c}"]["status"] == "current"]
            rec["status"] = "withdrawn" if "withdrawn" in flags else ("current" if current else "superseded")
            if rec.get("open"):
                flags.add("open")
            threads = {s["thread"] for s in cited}
            if len(threads) == 1 and None not in threads:
                (thread,) = threads
                last = max(s["seq"] for s in cited)
                if any(s["thread"] == thread and s["seq"] > last and not state["coverage"].get(sid)
                       for sid, s in sources.items()):
                    flags.add("newer-source-exists")
        elif rec["status"] == "disputed":
            flags.add("disputed")
        rec["flags"] = sorted(flags)

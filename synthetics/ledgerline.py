"""Ledgerline (issue #17): checkable evidence of what MCP tools returned in a run.

E1: each receipt line in runs/<run>.mcp.jsonl also carries `result` facts ({bytes, sha256} of the text the
server sends back, plus IDs and timestamps for read tools, never bodies) and `prev_sha256`, the sha256 of the
previous line, so an edit inside the file breaks the chain. The run record anchors the chain head outside
runs/ (`receipt_chain {head_sha256, lines}` in run.finished / run.failed / run.interrupted audit data).

A receipt proves what the server returned, not what the model attended to. A session can strip its
environment and rewrite its own runs/ files; the anchor makes a rewrite visible, it is not a sandbox
(docs/authority.md §0).
"""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

IDS_CAP = 200
TAIL_CHUNK = 65536
SEARCH_TOOLS = ("event_search", "memory_search")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_facts(text):
    data = text.encode("utf-8")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _items(value):
    """The record list of a search result: a bare list (store API) or {events|memories: [...]} (MCP)."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for name in ("events", "memories"):
            if isinstance(value.get(name), list):
                return value[name]
    return []


def result_facts(tool, value, text):
    """E1 result summary: sizes, hashes, IDs and timestamps only. `text` is exactly what tools/call sends."""
    facts = text_facts(text)
    if tool in SEARCH_TOOLS:
        items = [item for item in _items(value) if isinstance(item, dict)]
        ids = [item.get("id") for item in items]
        facts.update(count=len(items), ids=ids[:IDS_CAP],
                     created_first=items[0].get("created_at") if items else None,
                     created_last=items[-1].get("created_at") if items else None)
        if len(ids) > IDS_CAP:
            facts["ids_cut"] = True
        if isinstance(value, dict):
            for name in ("capped", "unread_from", "next_cursor", "truncated_from", "since"):
                if name in value:
                    facts[name] = value[name]
        truncated = [{"id": item.get("id"), "created_at": item.get("created_at"), "body_len": item.get("body_len")}
                     for item in items if item.get("body_truncated")]
        if truncated:
            facts["truncated"] = truncated[:IDS_CAP]
    elif tool == "event_get" and isinstance(value, dict):
        body = value.get("body")
        facts.update(id=value.get("id"), created_at=value.get("created_at"),
                     body_len=value.get("body_len", len(body) if isinstance(body, str) else None),
                     body_offset=value.get("body_offset", 0),
                     returned_chars=len(body) if isinstance(body, str) else 0)
    elif tool != "employee_context" and isinstance(value, dict) and isinstance(value.get("id"), str):
        facts["id"] = value["id"]
    return facts


def last_line(path):
    """The last non-empty line of a file as text (without its newline), or None for a missing/empty file."""
    try:
        with open(str(path), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            data = b""
            position = end
            while position > 0:
                step = min(TAIL_CHUNK, position)
                position -= step
                handle.seek(position)
                data = handle.read(step) + data
                stripped = data.rstrip(b"\n")
                if b"\n" in stripped:
                    return stripped.rsplit(b"\n", 1)[1].decode("utf-8")
            stripped = data.rstrip(b"\n")
            return stripped.decode("utf-8") if stripped else None
    except FileNotFoundError:
        return None


def chain_head(path):
    """{head_sha256, lines} of a receipt file (lines = non-empty lines), or None when it doesn't exist."""
    try:
        with open(str(path), "rb") as handle:
            lines = [line for line in handle.read().split(b"\n") if line]
    except FileNotFoundError:
        return None
    return {"head_sha256": hashlib.sha256(lines[-1]).hexdigest() if lines else None, "lines": len(lines)}


def check_chain(path, anchor):
    """chain_ok / chain_broken / unanchored for a receipt file against the run's anchored head."""
    try:
        with open(str(path), "rb") as handle:
            lines = [line for line in handle.read().split(b"\n") if line]
    except FileNotFoundError:
        return {"chain": "no_receipts"}
    previous = None
    for number, raw in enumerate(lines, 1):
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"chain": "chain_broken", "line": number, "reason": "unparseable line"}
        if not isinstance(record, dict) or "prev_sha256" not in record:
            return {"chain": "unanchored", "line": number, "reason": "line written before the E1 chain"}
        if record["prev_sha256"] != previous:
            return {"chain": "chain_broken", "line": number, "reason": "prev_sha256 does not match line %d" % (number - 1)}
        previous = hashlib.sha256(raw).hexdigest()
    if not isinstance(anchor, dict):
        return {"chain": "unanchored", "lines": len(lines),
                "reason": "run record has no receipt_chain (run still running, or finished before E1)"}
    if anchor.get("lines") != len(lines) or anchor.get("head_sha256") != previous:
        return {"chain": "chain_broken", "lines": len(lines), "anchor": anchor,
                "reason": "file head differs from the head anchored in the run record"}
    return {"chain": "chain_ok", "lines": len(lines)}


# ---------------------------------------------------------------------------------------------------------
# E2: export runs. An allow-list projection: named structural fields survive; free text becomes {bytes, sha256};
# anything unnamed is dropped or hashed. Never bodies, never credentials (static test: CREDENTIAL_NAMES).

CREDENTIAL_NAMES = r"token|key_file|credential|secret|password|key_sha256"
# Argument keys kept as they are (IDs, references, enums, numbers, timestamps).
ARG_KEEP = frozenset({
    "id", "work_id", "work_ids", "question_id", "event_id", "in_reply_to", "replaces", "subject", "thread",
    "project", "to", "person", "target", "status", "kind", "priority", "limit", "match", "body_offset",
    "body_chars", "max_bytes", "mode", "since", "cursor", "wake_at", "at", "covered_through", "key", "cycle",
    "section", "certainty", "origin", "publish", "include_superseded", "expected_seq", "as_of", "history",
    "part", "reply_to", "recipient_work_id", "relays", "add", "reconciles", "mentions", "refs", "file",
})
# Argument keys whose value is free text: exported as {bytes, sha256} only (Q2: query and title too).
ARG_TEXT = frozenset({
    "body", "value", "note", "description", "text", "reason", "review", "query", "excerpt", "title",
    "predicate", "statement", "waiting_for", "cancel_reason", "topic", "name",
})

# Audit actions: per-prefix allow-list of fields (IDs, status, codes). Unlisted actions export only the base.
AUDIT_FIELDS = (
    ("authority.", ("actor", "run", "delegation", "chain", "action", "code", "stage", "outcome", "op_id", "label",
                    "target", "what", "self_raise")),
    ("run.", ("id", "status", "started_at", "finished_at", "cost", "event_ids", "receipt_chain")),
    ("event.received", ("id", "kind", "sender", "source", "seq", "auth.channel")),
    ("work.", ("id", "status", "priority", "wake_at", "waiting_question_ids")),
    ("roster.view", ("person", "section", "seq")),
    ("roster.self", ("person", "section", "seq")),
    ("channel.registered", ("id",)),
    ("routine.", ("id", "status", "wake_at", "routine.kind", "routine.anchor_at", "routine.covered_through",
                  "routine.last_fired_at", "routine.rearmed_by_run", "routine.coverage.run", "routine.coverage.cutoff",
                  "routine.coverage.through", "routine.coverage.advanced", "routine.coverage.omissions[].from")),
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _scalar_or_ids(value):
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= 300
    if isinstance(value, list):
        return all(isinstance(v, (str, int, float, bool)) and (not isinstance(v, str) or len(v) <= 300) for v in value)
    return False


def project_args(arguments):
    """Receipt arguments → allow-listed structure. Free text and anything unnamed → {bytes, sha256}."""
    if not isinstance(arguments, dict):
        return text_facts(canonical(arguments))
    out = {}
    for key, value in arguments.items():
        if key in ARG_KEEP and _scalar_or_ids(value):
            out[key] = value
        elif key == "omissions" and isinstance(value, list):
            out[key] = [{"from": item.get("from") if isinstance(item, dict) else None,
                         "reason": text_facts(canonical(item.get("reason") if isinstance(item, dict) else item))}
                        for item in value]
        else:
            out[key] = text_facts(value if isinstance(value, str) and key in ARG_TEXT else canonical(value))
    return out


def _error_code(error):
    import re
    match = re.match(r"^([a-z][a-z0-9_]{2,40}):", error or "")
    return {"error_code": match.group(1)} if match else {"error": text_facts(error or "")}


def export_receipt(home_id, run_id, seq, record, raw):
    """One runs/<run>.mcp.jsonl line → one exported mcp.call line."""
    arguments = record.get("arguments")
    line = {"kind": "mcp.call", "home_id": home_id, "run_id": run_id, "seq": seq, "at": record.get("at"),
            "tool": record.get("tool"), "success": record.get("success"), "args": project_args(arguments),
            "args_sha256": sha256_text(canonical(arguments)), "line_sha256": hashlib.sha256(raw).hexdigest()}
    if "prev_sha256" in record:
        line["prev_sha256"] = record["prev_sha256"]
    if record.get("error") is not None:
        line.update(_error_code(record["error"]))
    if isinstance(record.get("result"), dict):
        line["result"] = record["result"]  # E1 facts are already IDs, timestamps, sizes and hashes only
    if record.get("result_error"):
        line["result_error"] = record["result_error"]
    return line


def _pick(data, path):
    """Dotted path with one optional list step `name[]`; returns (found, value)."""
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        items = data.get(head[:-2]) if isinstance(data, dict) else None
        if not isinstance(items, list):
            return False, None
        return True, [_pick(item, rest)[1] for item in items]
    if not isinstance(data, dict) or head not in data:
        return False, None
    return (True, data[head]) if not rest else _pick(data[head], rest)


def _set(out, path, value):
    parts = path.replace("[]", "").split(".")
    for part in parts[:-1]:
        out = out.setdefault(part, {})
    out[parts[-1]] = value


def export_audit(home_id, row):
    data = row["data"]
    line = {"kind": "audit", "home_id": home_id, "seq": row["seq"], "at": row["at"], "action": row["action"],
            "record_id": row["record_id"], "data_sha256": sha256_text(data)}
    try:
        parsed = json.loads(data)
    except ValueError:
        return line
    for prefix, names in AUDIT_FIELDS:
        if row["action"] == prefix or (prefix.endswith(".") and row["action"].startswith(prefix)):
            fields = {}
            for name in names:
                found, value = _pick(parsed, name)
                chain = name == "receipt_chain" and isinstance(value, dict) and set(value) <= {"head_sha256", "lines", "error"}
                if found and (_scalar_or_ids(value) or chain):
                    _set(fields, name, value)
            line["fields"] = fields
            break
    return line


def _connect(home):
    from . import sqlro
    conn = sqlro.connect_ro(Path(home) / "state.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def _home_id(conn):
    row = conn.execute("SELECT data FROM identity LIMIT 1").fetchone() if _has_table(conn, "identity") else None
    try:
        return json.loads(row["data"]).get("id") if row else None
    except ValueError:
        return None


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _receipt_lines(path):
    try:
        with open(str(path), "rb") as handle:
            raws = [line for line in handle.read().split(b"\n") if line]
    except FileNotFoundError:
        return None
    out = []
    for number, raw in enumerate(raws, 1):
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError:
            record = None
        out.append((number, raw, record if isinstance(record, dict) else None))
    return out


def export_runs(home, run=None, since=None, audit=False):
    """E2: yield exported lines for the home's runs (one run, runs started at/after `since`, or all)."""
    home = Path(home)
    conn = _connect(home)
    try:
        home_id = _home_id(conn)
        runs = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM runs ORDER BY rowid")]
        if run is not None:
            runs = [r for r in runs if r.get("id") == run]
            if not runs:
                raise ValueError("No run %s in %s." % (run, home))
        elif since is not None:
            runs = [r for r in runs if str(r.get("started_at") or "") >= since]
        for record in runs:
            run_id = record.get("id")
            yield {"kind": "run", "home_id": home_id, "run_id": run_id, "status": record.get("status"),
                   "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
                   "cost": record.get("cost"), "event_ids": record.get("event_ids"),
                   "receipt_chain": record.get("receipt_chain")}
            if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
                continue
            for number, raw, receipt in _receipt_lines(home / "runs" / (run_id + ".mcp.jsonl")) or []:
                if receipt is None:
                    yield {"kind": "mcp.call", "home_id": home_id, "run_id": run_id, "seq": number,
                           "unparseable": True, "line_sha256": hashlib.sha256(raw).hexdigest()}
                else:
                    yield export_receipt(home_id, run_id, number, receipt, raw)
        if audit:
            query, params = "SELECT seq, at, action, record_id, data FROM audit", ()
            if run is not None:
                started = runs[0].get("started_at") or ""
                finished = runs[0].get("finished_at") or "9999"
                query, params = query + " WHERE at >= ? AND at <= ?", (started, finished)
            elif since is not None:
                query, params = query + " WHERE at >= ?", (since,)
            for row in conn.execute(query + " ORDER BY seq", params):
                yield export_audit(home_id, row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------------
# E3: routine verify RUN. Checks a dream's covered_through against its since= pages and event_get chunks.

EPOCH = "1970-01-01T00:00:00Z"
SPILL_BYTES = 32000  # F2's MCP max_bytes default; the client's display threshold is predicted, not observed (C2)
SPILL_NOTE = ("predicted, not observed: a page over %d bytes is assumed spilled by the client; only the executor "
              "stream (E4, not built) would show what the session displayed")
CHAIN_NOTE = ("the receipt file can be rewritten by a session; its head is anchored in the run record "
              "(receipt_chain), which a session could also rewrite by stripping its environment (authority.md §0)")


def _ts(value):
    """Normalise ISO timestamps for comparison (Z and +00:00 forms, microseconds)."""
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        from datetime import datetime, timezone
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None


def _rearm_rows(conn, run_id):
    rows = []
    for row in conn.execute("SELECT seq, record_id, data FROM audit WHERE action='routine.rearmed' ORDER BY seq"):
        try:
            data = json.loads(row["data"])
        except ValueError:
            continue
        routine = data.get("routine") if isinstance(data, dict) else None
        if isinstance(routine, dict) and routine.get("kind") == "dream":
            rows.append((row["seq"], row["record_id"], routine))
    mine = [r for r in rows if r[2].get("rearmed_by_run") == run_id]
    if not mine:
        return None, None
    seq, work_id, routine = mine[-1]
    earlier = [r for r in rows if r[1] == work_id and r[0] < seq]
    previous = earlier[-1][2].get("covered_through") if earlier else None
    return routine, previous


def verify_dream(home, run_id, spill_bytes=SPILL_BYTES):
    """E3 verdict for one dream run: consistent, possibly_spilled, overclaimed or no_evidence."""
    home = Path(home)
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("run must be a run ID.")
    if not isinstance(spill_bytes, int) or isinstance(spill_bytes, bool) or spill_bytes < 1:
        raise ValueError("spill_bytes must be a positive integer.")
    out = {"run": run_id, "spill_check": SPILL_NOTE % spill_bytes, "spill_bytes": spill_bytes, "chain_note": CHAIN_NOTE}
    conn = _connect(home)
    try:
        row = conn.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return dict(out, verdict="no_evidence", reason="no such run")
        run = json.loads(row["data"])
        routine, previous = _rearm_rows(conn, run_id)
    finally:
        conn.close()
    path = home / "runs" / (run_id + ".mcp.jsonl")
    out["chain"] = check_chain(path, run.get("receipt_chain"))
    if routine is None:
        return dict(out, verdict="no_evidence", reason="no routine.rearmed row for a dream by this run")
    coverage = routine.get("coverage") if isinstance(routine.get("coverage"), dict) else {}
    if coverage.get("run") != run_id:
        return dict(out, verdict="no_evidence", reason="the dream's re-arm recorded no coverage for this run")
    start = _ts(previous) or _ts(EPOCH)  # C3: a first dream starts at 1970
    cutoff = _ts(coverage.get("cutoff"))
    through = _ts(coverage.get("through"))
    omissions = [_ts(o.get("from")) for o in coverage.get("omissions") or [] if isinstance(o, dict)]
    out.update(previous=start, cutoff=cutoff, through=through, omission_from=[o for o in omissions])
    lines = _receipt_lines(path)
    if lines is None:
        return dict(out, verdict="no_evidence", reason="no receipt file for this run")
    receipts = [r for _, _, r in lines if r is not None and r.get("success")]
    chunks = {}
    for r in receipts:
        facts = r.get("result") or {}
        if r.get("tool") == "event_get" and facts.get("id"):
            begin = facts.get("body_offset") or 0
            chunks.setdefault(facts["id"], []).append((begin, begin + (facts.get("returned_chars") or 0)))
    streams = {}
    breaks = []
    for tool, name in (("event_search", "events"), ("memory_search", "memories")):
        pages = [(n, r) for n, (_, _, r) in enumerate(lines, 1)
                 if r is not None and r.get("success") and r.get("tool") == tool
                 and isinstance(r.get("arguments"), dict)
                 and (r["arguments"].get("since") is not None or r["arguments"].get("cursor") is not None)]
        stream = _chain(pages, start, cutoff, spill_bytes, chunks, breaks, name)
        streams[name] = stream
    out["streams"] = streams
    if any(s.get("no_result_facts") for s in streams.values()):
        return dict(out, verdict="no_evidence", reason="since= receipts written before E1 carry no result facts")
    bound = min([s["end"] for s in streams.values()] + [o for o in omissions if o])
    out["bound"] = bound
    if through is None:
        return dict(out, verdict="no_evidence", reason="coverage has no through")
    if through > bound:
        first = min([b for b in breaks if b["from"]] or [{"from": bound}], key=lambda b: b["from"])
        return dict(out, verdict="overclaimed", first_unread=bound, breaking=first)
    unrecorded = [b for b in breaks if b["from"] and not any(o and o <= b["from"] for o in omissions)]
    if unrecorded:
        return dict(out, verdict="possibly_spilled" if any(b["why"] == "possibly_spilled" for b in unrecorded)
                    else "unread_truncated", unrecorded=unrecorded,
                    reason="a page or body in the read chain was not seen and no omission records it")
    return dict(out, verdict="consistent")


def _covered(spans, length):
    position = 0
    for begin, end in sorted(spans):
        if begin > position:
            return False
        position = max(position, end)
    return position >= (length or 0)


def _chain(pages, start, cutoff, spill_bytes, chunks, breaks, name):
    """Extend the contiguous chain from `start` through the stream's since=/cursor pages (fixed point)."""
    end = start
    last_cursor = None
    used = set()
    stopped = False
    stream = {"pages": len(pages), "end": end}
    changed = True
    while changed and not stopped:
        changed = False
        for number, receipt in pages:
            if number in used:
                continue
            args, facts = receipt["arguments"], receipt.get("result")
            since = _ts(args.get("since"))
            continues = (args.get("cursor") is not None and args.get("cursor") == last_cursor) or \
                        (args.get("cursor") is None and since is not None and since <= end)
            if not continues:
                continue
            used.add(number)
            if not isinstance(facts, dict):
                stream["no_result_facts"] = True
                stopped = True
                break
            first = _ts(facts.get("created_first"))
            if facts.get("bytes", 0) > spill_bytes:
                # The page wasn't seen. Items before its first one don't exist (oldest first, since inclusive).
                end = max(end, first or end)
                breaks.append({"stream": name, "line": number, "from": first, "why": "possibly_spilled",
                               "bytes": facts.get("bytes")})
                stopped = True
                break
            page_end = _ts(facts.get("created_last")) if facts.get("capped") else cutoff
            for item in facts.get("truncated") or []:
                if not _covered(chunks.get(item.get("id"), []), item.get("body_len")):
                    page_end = min(page_end or _ts(item.get("created_at")), _ts(item.get("created_at")))
                    breaks.append({"stream": name, "line": number, "from": _ts(item.get("created_at")),
                                   "why": "unread_truncated", "id": item.get("id")})
                    stopped = True
                    break
            if page_end and page_end > end:
                end = page_end
            last_cursor = facts.get("next_cursor")
            changed = True
            if stopped or not facts.get("capped"):
                stopped = True
                break
    stream["end"] = end
    return stream

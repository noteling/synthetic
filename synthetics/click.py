"""Stage 2 "one approved, named click" (P1: the engine slice).

An employee asks to press ONE named control on the human's front browser page. It happens only after the home's
default human explicitly approves that exact control, and at most once.

    screen_click_request {name, role?, app?, why, work_id?}
        Noteling checks the control (``--click NAME --prepare``): exact name, exactly one, visible, enabled, no
        sheet, guard class safe. If so the ENGINE asks the default human one tracked question in a fixed form
        ("... Reply "yes NAME" to allow it ..."). Nothing is pressed.
    screen_click {request}
        Reads the answer from records only. Fires (``--click NAME --fire``) only when every approval rule holds, and
        only after a compare-and-set asked -> firing, so a request fires at most once, ever.

Approval rules (P0 §2; Codex review C1/C2, owner-decisions.md):
- The request's own question (created by the engine, marked ``click_id``; never one from question_ask) has status
  ``answered`` through a CORRELATED reply: the answer event's reply_to is that question. question_resolve and
  question_close(event_id) refuse click questions outright, so an unrelated message never binds as approval.
- The answer is a message from the home's default_human (humans.json), with non-null auth, and its role is human:
  operator and secretary channels (e.g. codex-desktop relays) never count, whatever the sender field says.
- The text, normalized, is "yes " + the asked name (every approval names the control). Anything else declines,
  finally.
- The answer came within ASK_WINDOW of the ask, and the fire starts within FIRE_WINDOW of the answer.

Refused outright in v1, whatever any answer says: irreversible-class names (DENY_WORDS: Noteling's confirm words
plus order, book, donate, subscribe, place, reserve, agree, accept, ...) and any control Noteling's
IrreversibleGuard classes as confirm or forbidden.

Privacy: as Glance (counts + find). The stored record keeps the asked name/role/why, the site's host, match kind,
count, unique, visible/enabled, the guard class, state and result codes, and Noteling's opaque per-request HMAC
tokens. Never a hit's label, frame, the URL path or a DOM id.

The approval gate is the engine's and is independent of who presses: the reader seam (``click.reader``) is the
actor. A later OS-level identity for the employee (its own account or machine) replaces the actor, not the gate.

Opt-in: offered only in a home whose ``click.json`` is {"enabled": true} (separate from glance.json; off by
default). See docs/click.md.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

from . import capture, glance

OPT_IN = "click.json"
ASK_WINDOW = timedelta(minutes=30)   # ask -> answer
FIRE_WINDOW = timedelta(minutes=10)  # answer -> fire
FIRE_STALE = timedelta(minutes=2)    # a `firing` record older than this is a crash: becomes `unknown`, never retried
MIN_VERSION = (0, 10, 6)             # the Noteling release that brings --click (P2)
WHY_MAX = 200
RECENT = 5

# Whole words (case-insensitive, any whitespace inside phrases). Noteling IrreversibleGuard.confirmWords at 1695a69,
# plus Codex's v1 list (owner-decisions.md) and review D4. v1 refuses all of them outright.
NOTELING_CONFIRM = ("send", "submit", "pay", "purchase", "buy", "checkout", "confirm", "delete", "remove", "discard",
                    "sign", "approve", "reject", "publish", "post", "transfer", "unsubscribe", "reply all")
EXTRA = ("order", "book", "donate", "subscribe", "place", "reserve", "agree", "accept", "check out")
DENY_WORDS = NOTELING_CONFIRM + EXTRA
DENY = re.compile(r"(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])"
                  % "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in DENY_WORDS), re.IGNORECASE)

STATES_FINAL = ("refused", "declined", "expired", "done", "failed", "unknown")
TOKEN = re.compile(r"[0-9a-f]{64}\Z")

# Noteling --click exits (HeadlessRead.Exit 0-5, plus the P2 additions).
EXITS = {0: "ok", 1: "not_found", 2: "usage", 3: "no_browser", 4: "no_permission", 5: "partial",
         6: "page_changed", 7: "guard_refused", 8: "not_unique_or_not_exact", 9: "not_front"}
# Reader failures that happen before anything is launched: nothing can have been pressed.
NOT_LAUNCHED = ("noteling_missing", "noteling_too_old", "busy", "lock_failed")

SENTENCES = {
    "disabled": "Clicking isn't enabled for this home.",
    "denied_word": "That control looks irreversible (buy, order, pay, send, delete, ...). Clicks like that are refused "
                   "in this version, whatever the human answers.",
    "not_exact": "No control has exactly that name on the page, so nothing was asked.",
    "not_unique": "More than one control has that name, so nothing was asked.",
    "not_visible": "The control isn't visible, so nothing was asked.",
    "disabled_control": "The control is disabled, so nothing was asked.",
    "sheet_open": "A sheet or dialog is open over the page, so nothing was asked.",
    "guard": "Noteling classes that control as irreversible or forbidden, so it is refused in this version.",
    "no_human": "This home has no default human (humans.json), so there is nobody who can approve a click.",
    "request_open": "Another click request of this home is still open; finish or let it expire first.",
    "bad_report": "Noteling's answer was incomplete, so nothing was asked.",
    "waiting": "The human hasn't answered yet. Nothing was pressed; call screen_click again after the answer.",
    "declined": "The human didn't approve this exact control. The request is closed; nothing was pressed.",
    "not_approved": "The answer doesn't count as the human's own approval. The request is closed; nothing was pressed.",
    "expired": "The approval window passed (30 min to answer, then 10 min to click). Nothing was pressed.",
    "already_used": "This request was already used. A new click needs a new request and a new answer.",
    "unknown": "A fire started but its outcome was never recorded (crash or timeout). It is never retried; tell the "
               "human and glance at the page to see what happened.",
    "page_changed": "The page or the control changed since the human approved it, so nothing was pressed.",
    "not_front": "The browser wasn't the frontmost app, so nothing was pressed.",
}


class ClickRefused(Exception):
    """A request refused before any question; ``reason`` is a stable code."""

    def __init__(self, reason):
        super().__init__(SENTENCES.get(reason, reason))
        self.reason = reason


def enabled(home):
    """True only when <home>/click.json is a JSON object with "enabled": true. Off by default."""
    try:
        with open(os.path.join(str(home), OPT_IN), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("enabled") is True


QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u201c": '"', "\u201d": '"'})


def _fold(text):
    return unicodedata.normalize("NFKC", text or "").casefold().translate(QUOTES)


def norm(text):
    """Case-folded words: NFKC, punctuation and quotes become spaces, whitespace collapsed."""
    text = "".join(c if c.isalnum() else " " for c in _fold(text))
    return " ".join(text.split())


def _marks(text):
    """The punctuation and symbols of a text, in order (letters, digits and spaces dropped)."""
    return [c for c in _fold(text) if not c.isalnum() and not c.isspace()]


def _subsequence(needle, haystack):
    rest = iter(haystack)
    return all(c in rest for c in needle)


def names_control(body, name):
    """True when an answer reads "yes <name>": case and spacing are ignored and punctuation the human adds
    ("Yes, add to cart!") is ignored, but every mark in the name itself must be typed, in order, so "yes C"
    never approves "C++" (Vesper #73 n1)."""
    return norm(body) == norm("yes " + name) and _subsequence(_marks(name), _marks(body))


def denied(name):
    return bool(DENY.search(unicodedata.normalize("NFKC", name or "")))


def check(name, role=None, app=None, why=None):
    """The same flag-safety rules as Glance (a value must never become a Noteling flag), plus a short why."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must name one control, e.g. \"Add to cart\".")
    glance.check(find=name, role=role, app=app)
    if not norm(name):
        raise ValueError("name must contain letters or digits.")
    if not isinstance(why, str) or not why.strip():
        raise ValueError("why must say in one line why you want to press it.")
    if len(why) > WHY_MAX:
        raise ValueError("why is at most %d characters." % WHY_MAX)
    if any(unicodedata.category(c) in glance.UNPRINTABLE for c in why):
        raise ValueError("why can't contain line breaks, control characters or invisible format characters.")


def _base(name, role, app):
    args = ["--click", name] + (["--role", role] if role is not None else [])
    return args


def prepare_arguments(name, role, app, token):
    args = _base(name, role, app) + ["--prepare", "--json", "--token", token]
    return args + (["--app", app] if app is not None else [])


def fire_arguments(record):
    args = _base(record["name"], record.get("role"), record.get("browser")) + [
        "--fire", "--json", "--token", record["token"],
        "--expect", "%s,%s" % (record["page_token"], record["hit_token"]),
        "--named-approval", record["name"], "--require-front"]
    return args + (["--app", record["browser"]] if record.get("browser") is not None else [])


class LaunchReader(glance.LaunchReader):
    """Glance's launcher (open -n -g -W, the host-wide lock, timeout kill) with the --click minimum version."""

    def __call__(self, args):
        if not os.path.isdir(self.app):
            raise glance.GlanceError("noteling_missing", "Noteling isn't installed at %s." % self.app)
        version = glance.app_version(self.app)
        if version is None or version < MIN_VERSION:
            # 0.10.5 has no --click: an unknown flag would start the full app, so never launch it.
            raise glance.GlanceError("noteling_too_old", "Noteling too old for clicks (need ≥ 0.10.6).")
        held = self._hold()
        try:
            return self._launch(args)
        finally:
            held.close()


reader = None  # None: LaunchReader(glance.DEFAULT_APP). Tests set a fake.


def _read(args):
    return (reader or LaunchReader())(args)


def _now():
    return datetime.now(timezone.utc)


def _parse(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _hit_ok(found):
    """(ok, reason) for a prepare's find: exact, exactly one, visible, enabled."""
    if not isinstance(found, dict):
        return False, "bad_report"
    hits = [h for h in found.get("hits") or [] if isinstance(h, dict)]
    if found.get("match") != "exact":
        return False, "not_exact"
    if found.get("count") != 1 or found.get("unique") is not True or len(hits) != 1:
        return False, "not_unique"
    if hits[0].get("visible") is not True:
        return False, "not_visible"
    if hits[0].get("enabled") is not True:
        return False, "disabled_control"
    return True, None


def _summary(found):
    stored = glance.stored_find(found, None, None) or {}
    return {k: stored.get(k) for k in ("match", "count", "unique", "hits")}


def question_body(identity, record):
    """The fixed form of the approval question. The host is the only thing from the page (owner-decisions Q6)."""
    role = record.get("role") or "control"
    return ("%s asks to press the %s \"%s\" on %s, once.\nWhy: %s\n"
            "To allow it, reply: yes %s\n"
            "Anything else declines. The ask expires %d minutes after it was sent, and an approval is good for %d "
            "minutes after your answer. Only this one press is allowed."
            % (identity.get("name") or identity["id"], role, record["name"], record.get("host") or "an unknown site",
               record["why"], record["name"], ASK_WINDOW.seconds // 60, FIRE_WINDOW.seconds // 60))


def _open_request(store, now=None):
    """The home's one live request: asked and still inside its windows, or firing and not yet stale."""
    now = now or _now()
    for row in store.db.execute("SELECT data FROM clicks").fetchall():
        record = json.loads(row["data"])
        if record.get("state") == "asked" and now - _parse(record["asked_at"]) <= ASK_WINDOW + FIRE_WINDOW:
            return record
        if record.get("state") == "firing" and now - _parse(record["firing_at"]) <= FIRE_STALE:
            return record
    return None


def _save(store, record, action):
    from .store import timestamp
    record["updated_at"] = timestamp()
    store._save("clicks", record, action)
    return record


def _finish(store, record, state, reason):
    """Set a final state inside the caller's transaction. An expired request also withdraws its question if it is
    still open, so it never sits in the human's Docket as a stale "Needs you" (#73 L1). A work the withdrawal leaves
    with no linked question gets wake_at=now, so the next due tick makes it ready for a re-check instead of leaving it
    waiting on nothing (#75 L1); that wake is the ordinary clock wake, never an answer. Returns the IDs of those
    works (empty unless the request expired)."""
    from .store import timestamp
    record["state"], record["reason"] = state, reason
    _save(store, record, "click." + state)
    unlinked = []
    if state == "expired":
        question = store._find("questions", record.get("question_id") or "")
        if question is not None and question.get("click_id") == record["id"] and question.get("status") == "open":
            now = timestamp()
            unlinked = store._question_withdraw(question, "click request expired", now)["work_unlinked"]
            for work_id in unlinked:
                work = store._get("works", work_id)
                if work.get("status") == "waiting" and not work.get("wake_at"):
                    work.update(wake_at=now, updated_at=now)
                    store._save("works", work, "work.click_recheck")
    return unlinked


def expire_stale(store, now=None):
    """Settle every asked request that can no longer be approved and withdraw its open question (#73 L1): expired
    ones at once, any other final verdict (e.g. its question is gone, #75 n1) once both windows passed. Runs before a
    new request and at each session start, so an abandoned request (never fired) is closed too. A row that can't be
    read is skipped and counted in one click.expire_skipped audit (#75 L2). Returns the IDs it settled."""
    now = now or _now()
    done, skipped = [], 0
    with store._tx():
        for row in store.db.execute("SELECT data FROM clicks").fetchall():
            try:
                record = json.loads(row["data"])
                if record.get("state") != "asked":
                    continue
                verdict, detail = approval(store, record, now)
                past = now - _parse(record["asked_at"]) > ASK_WINDOW + FIRE_WINDOW
            except Exception:  # noqa: BLE001 - one unreadable row never stops the others from settling
                skipped += 1
                continue
            if verdict == "final" and (detail[0] == "expired" or past):
                _finish(store, record, *detail)
                done.append(record["id"])
        if skipped:
            store._audit("click.expire_skipped", "clicks", {"rows": skipped})
    return done


def _result(record, reason=None, **extra):
    out = dict(request=record["id"], state=record["state"], reason=reason or record.get("reason"),
               question_id=record.get("question_id"))
    out.update(extra)
    sentence = SENTENCES.get(out["reason"])
    if sentence:
        out["explanation"] = sentence
    return out


def screen_click_request(store, name, why, role=None, app=None, work_id=None):
    if not enabled(store.home):
        raise ValueError("Unknown tool: screen_click_request")
    check(name, role=role, app=app, why=why)
    from .store import timestamp
    record = dict(id="click-" + uuid.uuid4().hex, run_id=store.run_id, name=name, role=role, browser=app,
                  why=why, state="refused", reason=None, created_at=timestamp())
    human = capture.default_human(store)
    reason = None
    if denied(name):
        reason = "denied_word"
    elif not human:
        reason = "no_human"
    else:
        expire_stale(store)
        if _open_request(store) is not None:
            reason = "request_open"
    if reason is not None:
        record["reason"] = reason
        with store._tx():
            _save(store, record, "click.refused")
        return _result(record)
    token = secrets.token_hex(32)
    try:
        report = _read(prepare_arguments(name, role, app, token))
    except glance.GlanceError as exc:
        record["reason"] = exc.reason
        with store._tx():
            _save(store, record, "click.refused")
        return dict(_result(record), explanation=str(exc))
    found = report.get("find")
    record["find"] = _summary(found)
    page = report.get("page") if isinstance(report.get("page"), dict) else {}
    record["host"] = glance.host(report.get("host")) or glance.host(page.get("site"))
    record["guard"] = report.get("guard") if report.get("guard") in ("safe", "confirm", "forbidden") else None
    code = report.get("exit")
    if code == 0:
        ok, reason = _hit_ok(found)
    elif code in (1, 8):
        ok, reason = False, (_hit_ok(found)[1] if isinstance(found, dict) else None) or "not_exact"
    elif code == 7 and report.get("reason") == "sheet_open":
        ok, reason = False, "sheet_open"       # #75 n2: a sheet at prepare isn't a guard refusal
    else:
        ok, reason = False, EXITS.get(code, "bad_report")
    if ok and (page.get("sheetOpen") is True or report.get("sheetOpen") is True):
        ok, reason = False, "sheet_open"
    if ok and record["guard"] != "safe":
        ok, reason = False, "guard" if record["guard"] else "bad_report"
    tokens = (report.get("page_token"), report.get("hit_token"))
    if ok and not all(isinstance(t, str) and TOKEN.match(t) for t in tokens):
        ok, reason = False, "bad_report"
    if not ok:
        record["reason"] = reason
        with store._tx():
            _save(store, record, "click.refused")
        return _result(record)
    record.update(token=token, page_token=tokens[0], hit_token=tokens[1], state="asked", human=human)
    body = question_body(store.identity(), record)
    question = store._with_route(store._send(human, body, work_id, "click-question-" + record["id"], True,
                                             marks={"click_id": record["id"]}))
    record.update(question_id=question["id"], asked_at=question["created_at"])
    with store._tx():
        if _open_request(store) is not None:
            # A concurrent request won the race: withdraw ours (never approvable) and refuse.
            q = store._get("questions", question["id"])
            q.update(status="withdrawn", withdrawn_at=timestamp(), close_reason="click request_open")
            store._save("questions", q, "question.withdrawn")
            record.update(state="refused", reason="request_open")
            _save(store, record, "click.refused")
            return _result(record)
        _save(store, record, "click.asked")
    return _result(record, host=record["host"], guard=record["guard"], routed_by=question.get("routed_by"),
                   warning=question.get("warning"))


def approval(store, record, now):
    """(verdict, reason) from records only. verdict: approved | waiting | final (state, reason)."""
    question = store._find("questions", record.get("question_id") or "")
    asked = _parse(record["asked_at"])
    if question is None or question.get("click_id") != record["id"]:
        return "final", ("refused", "not_approved")
    status = question.get("status")
    if status == "open":
        if now - asked > ASK_WINDOW:
            return "final", ("expired", "expired")
        return "waiting", "waiting"
    if status != "answered" or question.get("resolved_by_run") or question.get("secondhand"):
        return "final", ("declined", "not_approved")
    event = store._find("events", question.get("answer_event_id") or "")
    if event is None or event.get("reply_to") != question["id"] or event.get("kind") != "message":
        return "final", ("declined", "not_approved")      # C1: only a correlated reply approves
    cfg = capture.humans(store)
    if (not event.get("auth") or event.get("sender") != record.get("human")
            or event.get("sender") != cfg.get("default_human")
            or capture.role_of(event, "in", cfg) != "human"):
        return "final", ("declined", "not_approved")      # C2: relays/operators/unauthenticated never count
    if not names_control(event.get("body"), record["name"]):
        return "final", ("declined", "declined")
    answered = _parse(event["created_at"])
    if answered - asked > ASK_WINDOW or now - answered > FIRE_WINDOW:
        return "final", ("expired", "expired")
    return "approved", None


def screen_click(store, request):
    if not enabled(store.home):
        raise ValueError("Unknown tool: screen_click")
    if not isinstance(request, str) or not re.fullmatch(r"click-[0-9a-f]{32}", request):
        raise ValueError("request is a click request ID (click-<32 hex>) from screen_click_request.")
    from .store import _json, timestamp
    with store._tx():
        record = store._find("clicks", request)
        if record is None:
            raise ValueError("No click request %s in this home." % request)
        now = _now()
        state = record["state"]
        if state == "firing":
            if now - _parse(record["firing_at"]) > FIRE_STALE:
                record.update(state="unknown", reason="unknown")
                _save(store, record, "click.unknown")
                return _result(record)
            return _result(record, reason="already_used")
        if state in ("done", "unknown", "failed"):
            return _result(record, reason="already_used")
        if state != "asked":
            return _result(record)
        verdict, detail = approval(store, record, now)
        if verdict == "waiting":
            return _result(record, reason="waiting")
        if verdict == "final":
            unlinked = _finish(store, record, *detail)
            if record["state"] == "expired":
                return _result(record, work_unlinked=unlinked)
            return _result(record)
        # Compare-and-set asked -> firing inside this BEGIN IMMEDIATE transaction: exactly one caller gets here.
        record.update(state="firing", reason=None, firing_at=timestamp(), firing_run=store.run_id)
        changed = store.db.execute(
            "UPDATE clicks SET data=? WHERE id=? AND json_extract(data,'$.state')='asked'",
            (_json(record), record["id"])).rowcount
        if changed != 1:
            return _result(record, reason="already_used")
        store._audit("click.firing", record["id"], record)
    try:
        report = _read(fire_arguments(record))
        code = report.get("exit")
        reason = EXITS.get(code, "bad_report") if code != 0 else None
        pressed = report.get("pressed") is True and code == 0
        final = ("done", None) if pressed else (("failed", reason or "bad_report") if code in EXITS else
                                                ("unknown", "unknown"))
        result = dict(pressed=pressed, changed=report.get("changed") if isinstance(report.get("changed"), bool)
                      else None, exit=code)
    except glance.GlanceError as exc:
        final = ("failed", exc.reason) if exc.reason in NOT_LAUNCHED else ("unknown", "unknown")
        result = dict(pressed=False if exc.reason in NOT_LAUNCHED else None, changed=None, exit=None,
                      error=exc.reason)
    except Exception as exc:  # noqa: BLE001 - #73 n2: any other reader failure is unknown at once, never retried
        final = ("unknown", "unknown")
        result = dict(pressed=None, changed=None, exit=None, error=type(exc).__name__)
    with store._tx():
        record = store._get("clicks", request)
        record.update(state=final[0], reason=final[1], result=result, finished_at=timestamp())
        _save(store, record, "click." + final[0])
    return _result(record, **result)


STATUS_KEYS = ("id", "run_id", "created_at", "updated_at", "name", "role", "browser", "host", "state", "reason",
               "question_id", "guard", "find", "result")


def recent(store, limit=RECENT):
    """Newest click requests, whitelisted (no tokens, no why), newest first."""
    rows = store.db.execute("SELECT data FROM clicks ORDER BY rowid DESC LIMIT ?", (int(limit),)).fetchall()
    return [{k: v for k, v in json.loads(row["data"]).items() if k in STATUS_KEYS} for row in rows]


def _text(description=""):
    return {"type": "string", "description": description}


TOOLS = [{
    "name": "screen_click_request",
    "description": "Ask to press ONE named control on the human's front browser page. Noteling checks it is the "
                   "only exact, visible, enabled match; then the engine asks the human, in a fixed form, to reply "
                   "\"yes <name>\". Nothing is pressed now. Irreversible-looking controls (buy, order, pay, send, "
                   "delete, ...) are refused. Returns a request ID for screen_click.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["name", "why"], "properties": {
        "name": _text("The control's name as a person would say it, e.g. \"Add to cart\"."),
        "why": _text("One line (≤200 chars) the human sees: why you want to press it."),
        "role": _text("Optional role, e.g. button, link."),
        "app": _text("Optional browser bundle ID (com.google.Chrome, com.apple.Safari)."),
        "work_id": _text("Optional: your work that waits on the human's answer."),
    }},
}, {
    "name": "screen_click",
    "description": "Press the control of an approved screen_click_request, at most once. Fires only if the human "
                   "answered that request's own question with \"yes <name>\" within the windows; otherwise says "
                   "waiting, declined, expired or not_approved. Never retried after a fire started.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["request"], "properties": {
        "request": _text("The click request ID from screen_click_request."),
    }},
}]
NAMES = {t["name"] for t in TOOLS}
MUTATIONS = set(NAMES)


def call(store, name, arguments):
    if name == "screen_click_request":
        return screen_click_request(store, **arguments)
    return screen_click(store, **arguments)

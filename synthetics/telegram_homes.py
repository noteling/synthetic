"""Telegram adapter, several homes (#100 P2 + P3): config v2, author header, route map, state migration, chooser.

A v1 config (today's ``employee_home``) keeps the single-home adapter in telegram.py unchanged. A v2 config
(``"homes": [...]``) serves several employee homes from one bot and one ``getUpdates`` poller, and reaches every
home, the primary one included, only through the channel contract (docs/channel-contract.md), never the Store
(Codex F2a). This module holds the pieces that don't touch the network:

* ``load_homes`` validates the per-home entries (label, button, pinned employee_id, colleague, thread, not_before).
* ``render_parts`` puts the home's label on every part of a message (Byline footer on the last only), each part
  at most 4096 characters, and drops a first body line equal to the label (F7).
* ``migrate_state`` turns a schema 3 state (``question_map``, deliveries without a home) into schema 4
  (``route_map``, deliveries tagged with the primary home). Idempotent.
* ``resolve_reply`` decides where a Telegram Reply goes from the route map alone (§2 rules 2-3); no inference.
* P3 chooser: ``keyboard`` (persistent reply keyboard, ✓ on the saved selection), ``selection_target`` (a message
  that is exactly a button's text), ``parse_directive`` (optional ``for <shortcut>``), ``inline_chooser`` (held
  messages only).

The header is rendered from the home whose outbox row is delivered, never from model text (§1).
"""
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_MESSAGE_LENGTH = 4096
HEADER_SEPARATOR = "\n"
FOOTER_SEPARATOR = "\n\n"
LABEL_MAX = 64
BUTTON_MAX = 64
STATE_SCHEMA = 4
ROUTE_MAP_CAP = 2000   # newest Telegram message ids kept in route_map (about 300 KB of state); older Replies are held
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
_EMPLOYEE = re.compile(r"^employee-[0-9a-f]{32}$")
HOME_FIELDS = ("key", "home", "employee_id", "colleague_id", "label", "button", "thread", "primary", "not_before",
               "shortcut")
_SHORTCUT = re.compile(r"[a-z0-9]{1,8}")
MARK = "✓ "                       # the current selection's button (§3.3)
PLACEHOLDER_MAX = 64              # Telegram input_field_placeholder: 1-64 characters
PLACEHOLDER_NONE = "Choose who to talk to"
DIRECTIVE_SEPARATORS = "\n:,.-—"  # F13: end of text, newline or one of : , . - — ; a plain space is not one


class HomesConfigError(ValueError):
    """The v2 ``homes`` list is invalid; the adapter refuses to start (nothing is guessed)."""


def _one_line(value: Any, field: str, limit: int, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HomesConfigError("home %s: %s must be nonempty text" % (key, field))
    if "\n" in value or "\r" in value or len(value) > limit or value != value.strip():
        raise HomesConfigError("home %s: %s must be one line of at most %d characters, no surrounding space"
                               % (key, field, limit))
    return value


def _iso(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise HomesConfigError("home %s: not_before must be an ISO 8601 time" % key)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HomesConfigError("home %s: not_before %r is not an ISO 8601 time" % (key, value))
    return value


def load_homes(entries: Any, base: Path) -> List[Dict[str, Any]]:
    """Validate a v2 ``homes`` list. Exactly one entry is ``primary``; keys, homes, labels and buttons are unique.

    ``home`` is resolved against the config file's directory. Unknown fields are refused, so a typo can't silently
    disable a check."""
    if not isinstance(entries, list) or not entries:
        raise HomesConfigError("homes must be a nonempty list")
    homes: List[Dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise HomesConfigError("each homes entry must be an object")
        key = raw.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise HomesConfigError("home key %r must match [a-z][a-z0-9_-]{0,15}" % (key,))
        unknown = sorted(set(raw) - set(HOME_FIELDS))
        if unknown:
            raise HomesConfigError("home %s: unknown field(s) %s" % (key, ", ".join(unknown)))
        for field in ("home", "employee_id", "colleague_id"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise HomesConfigError("home %s: %s is required" % (key, field))
        if not _EMPLOYEE.fullmatch(raw["employee_id"]):
            raise HomesConfigError("home %s: employee_id must be a full employee ID" % key)
        thread = raw.get("thread")
        if thread is not None and (not isinstance(thread, str) or not thread.strip()):
            raise HomesConfigError("home %s: thread must be nonempty text or null" % key)
        shortcut = raw.get("shortcut")
        if shortcut is not None and (not isinstance(shortcut, str) or not _SHORTCUT.fullmatch(shortcut)):
            raise HomesConfigError("home %s: shortcut must be 1-8 characters [a-z0-9]" % key)
        primary = raw.get("primary", False)
        if not isinstance(primary, bool):
            raise HomesConfigError("home %s: primary must be true or false" % key)
        homes.append(dict(
            key=key,
            home=str((base / Path(raw["home"]).expanduser()).resolve()),
            employee_id=raw["employee_id"],
            colleague_id=raw["colleague_id"],
            label=_one_line(raw.get("label"), "label", LABEL_MAX, key),
            button=_one_line(raw.get("button"), "button", BUTTON_MAX, key),
            thread=thread,
            primary=primary,
            not_before=_iso(raw["not_before"], key) if raw.get("not_before") is not None else None,
            shortcut=shortcut,
        ))
        button = homes[-1]["button"]
        if button.startswith(MARK.strip()) or button.startswith("/"):
            raise HomesConfigError("home %s: button must not start with %r or '/'" % (key, MARK.strip()))
        if len(placeholder(homes[-1])) > PLACEHOLDER_MAX:
            raise HomesConfigError("home %s: button too long for the input placeholder %r (at most %d characters)"
                                   % (key, placeholder(homes[-1]), PLACEHOLDER_MAX))
    for field in ("key", "home", "employee_id", "label", "button"):
        values = [h[field] for h in homes]
        if len(set(values)) != len(values):
            raise HomesConfigError("homes: %s must be unique" % field)
    shortcuts = [h["shortcut"] for h in homes if h["shortcut"]]
    if len(set(shortcuts)) != len(shortcuts):
        raise HomesConfigError("homes: shortcut must be unique")
    for home in homes:
        # §3.2 rule 3 / P0 r1 carry: a button that also parses as a directive is ambiguous. Stop with a clear
        # message rather than skip anything silently.
        if parse_directive(home["button"], homes) is not None:
            raise HomesConfigError("home %s: button %r reads as a `for <shortcut>` directive; change the button or "
                                   "the shortcut" % (home["key"], home["button"]))
        if any(home["button"].casefold() == (other["shortcut"] or "").casefold() for other in homes):
            raise HomesConfigError("home %s: button equals a shortcut" % home["key"])
    primaries = [h for h in homes if h["primary"]]
    if len(primaries) != 1:
        raise HomesConfigError("homes: exactly one entry must be primary (found %d)" % len(primaries))
    colleagues = {h["colleague_id"] for h in homes}
    if len(colleagues) != 1:
        # One paired Telegram user is one human; every home must name the same colleague for him.
        raise HomesConfigError("homes: every entry must name the same colleague_id")
    return homes


def placeholder(home: Optional[Dict[str, Any]]) -> str:
    """§3.3: ``To: <button>`` for the selected home (its leading emoji kept), else a fixed prompt."""
    return "To: %s" % home["button"] if home else PLACEHOLDER_NONE


def keyboard(homes: List[Dict[str, Any]], selected: Optional[str]) -> Dict[str, Any]:
    """§3.1 R1: the persistent reply keyboard, one row per home in config order, ✓ on the saved selection."""
    current = next((h for h in homes if h["key"] == selected), None)
    return dict(keyboard=[[dict(text=(MARK if h["key"] == selected else "") + h["button"])] for h in homes],
                is_persistent=True, resize_keyboard=True, one_time_keyboard=False,
                input_field_placeholder=placeholder(current))


def inline_chooser(homes: List[Dict[str, Any]], gen: int) -> Dict[str, Any]:
    """§3.4: inline buttons for a held message only; callback_data ``sel:<key>:<gen>`` (≤ 64 bytes: key ≤ 16)."""
    return dict(inline_keyboard=[[dict(text=h["button"], callback_data="sel:%s:%d" % (h["key"], gen))]
                                 for h in homes])


def selection_target(message: Dict[str, Any], homes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """§3.2 rule 2: the home whose button text the whole message is (surrounding space and the ✓ mark allowed).
    A caption, media, forward, via_bot, quote or Reply is never a selection; nothing else is inferred."""
    if len(homes) < 2:
        return None
    if any(message.get(k) for k in ("caption", "forward_origin", "forward_from", "forward_from_chat",
                                    "forward_date", "via_bot", "quote", "reply_to_message")):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith(MARK.strip()):
        text = text[len(MARK.strip()):].strip()
    return next((h for h in homes if h["button"] == text), None)


def parse_directive(text: Any, homes: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], str, str]]:
    """§3.5 rulings a-e, F13: ``for <shortcut>`` at the very start (leading whitespace allowed, case-insensitive),
    followed by end of text, a newline or one of ``: , . - —``. Returns (home, prefix, content): ``prefix`` is the
    exact consumed text (directive, separator, following whitespace), so prefix + content == text. None otherwise."""
    if not isinstance(text, str):
        return None
    shortcuts = {h["shortcut"]: h for h in homes if h.get("shortcut")}
    if not shortcuts:
        return None
    alternatives = "|".join(re.escape(k) for k in sorted(shortcuts, key=len, reverse=True))
    match = re.match(r"\s*for[ \t]+(%s)(?=\Z|[%s])[%s]?\s*" % (
        alternatives, re.escape(DIRECTIVE_SEPARATORS), re.escape(DIRECTIVE_SEPARATORS)), text, re.IGNORECASE)
    if not match:
        return None
    return shortcuts[match.group(1).lower()], match.group(0), text[match.end():]


def sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _split(text: str, max_len: int) -> List[str]:
    from .telegram import split_text
    return split_text(text, max_len)


def render_parts(body: str, label: str, footer: Optional[str] = None,
                 max_len: int = MAX_MESSAGE_LENGTH) -> Tuple[List[str], Dict[str, Any]]:
    """Every part = label + newline + chunk; the footer (Byline) on the last part only; each part <= max_len.

    F7: when the body's first line equals the label (after strip), that one line is dropped and the record says
    ``header_deduped``. Returns (parts, record fields: header sha8, header_deduped, footer sha8 when present)."""
    meta: Dict[str, Any] = {"header": sha8(label)}
    first, sep, rest = body.partition("\n")
    if first.strip() == label:
        body = rest if sep else ""
        meta["header_deduped"] = True
    head = label + HEADER_SEPARATOR
    tail = (FOOTER_SEPARATOR + footer) if footer else ""
    room = max_len - len(head) - len(tail)
    if room < 1:
        raise ValueError("label and footer leave no room for the message")
    chunks = _split(body, room) if body else [""]
    parts = [head + chunk for chunk in chunks]
    parts[-1] = parts[-1] + tail
    if footer:
        meta["footer"] = sha8(footer)
    return parts, meta


def receipt(chat_id: Any, ids: str, record: Dict[str, Any]) -> str:
    """telegram:<chat>:<ids>;header:<sha8>[;footer:<sha8>] (§1)."""
    text = "telegram:%s:%s" % (chat_id, ids)
    if record.get("header"):
        text += ";header:%s" % record["header"]
    if record.get("footer"):
        text += ";footer:%s" % record["footer"]
    return text


def migrate_state(d: Dict[str, Any], primary: Dict[str, Any]) -> bool:
    """Schema 3 -> 4 (§4). Returns True when anything changed. Idempotent.

    * every ``question_map`` entry becomes ``route_map[tg_id] = {home, employee_id, question, kind: "out",
      legacy: true}`` in the primary home, so a reply to an old question still closes that exact question;
    * every delivery without a ``home`` is tagged with the primary home;
    * ``question_map`` is kept as is (read by nothing in v2) so a rollback to schema 3 code still finds it.
    A pre-upgrade ordinary message has no route entry at all: a Reply to it is held for the chooser (F4)."""
    changed = False
    route_map = d.setdefault("route_map", {})
    for tg_id, question in sorted((d.get("question_map") or {}).items()):
        if str(tg_id) in route_map:
            continue
        route_map[str(tg_id)] = dict(home=primary["key"], employee_id=primary["employee_id"], message=None,
                                     question=question, kind="out", legacy=True)
        changed = True
    for record in (d.get("deliveries") or {}).values():
        if isinstance(record, dict) and not record.get("home"):
            record["home"] = primary["key"]
            changed = True
    if d.get("schema") != STATE_SCHEMA:
        d["schema"] = STATE_SCHEMA
        changed = True
    d.setdefault("selection", None)
    d.setdefault("held", [])
    return changed


HOLD = "hold"
_TO_COMMAND = re.compile(r"/to(?:@[A-Za-z0-9_]{1,64})?")


def resolve_reply(route_map: Dict[str, Any], reply_to_message_id: Optional[int],
                  homes: List[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Where a message goes by its Telegram Reply target (§2). Returns (verdict, home, entry):

    * ("none", None, None): not a Reply, or a Reply to a Router notice: the caller uses the current selection
      (P2: the primary home).
    * ("home", home, entry): a mapped target whose home is configured and whose pinned employee_id is unchanged.
      ``entry["question"]`` (may be None) is the tracked question of that home.
    * ("hold", None, entry-or-None): unmapped, legacy without a question (F4), home no longer configured or its
      employee changed. Never guessed, never broadcast."""
    if reply_to_message_id is None:
        return "none", None, None
    entry = route_map.get(str(reply_to_message_id))
    if entry is None:
        return HOLD, None, None
    if entry.get("kind") in ("notice", "select"):
        return "none", None, entry
    home = next((h for h in homes if h["key"] == entry.get("home")), None)
    if home is None or home["employee_id"] != entry.get("employee_id"):
        return HOLD, None, entry
    if entry.get("legacy") and not entry.get("question"):
        return HOLD, None, entry
    return "home", home, entry


def in_reply_to(entry: Optional[Dict[str, Any]], reply_to_message_id: Optional[int]) -> Optional[str]:
    """F3: the local message id of the replied-to outbound message when mapped, else ``tg:<message_id>``."""
    if reply_to_message_id is None:
        return None
    if entry and entry.get("message"):
        return entry["message"]
    if entry and entry.get("event"):
        return entry["event"]
    return "tg:%s" % reply_to_message_id


# ---------------------------------------------------------------------------
# Channel contract client (F2a): the adapter never opens a home's Store.
# ---------------------------------------------------------------------------

import json
import logging
import os
import secrets
import subprocess
import sys
import time
from datetime import timezone

logger = logging.getLogger(__name__)

CONTRACT_TIMEOUT = 60


class ContractError(RuntimeError):
    """A whole contract command failed (exit 2, timeout, unparsable output). ``code`` is the contract error code
    when the command printed one (``auth_failed``, ``in_session``), else ``"transport"``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Contract:
    """Runs ``python -m synthetics --home H channel CMD --key-file KEY --instance ID`` (like Postern)."""

    def __init__(self, key_file: str, instance: str, command: Optional[List[str]] = None):
        self.key_file = str(key_file)
        self.instance = instance
        self.command = list(command or [sys.executable, "-m", "synthetics"])

    def call(self, home: str, name: str, *args: str, lines: Optional[List[Dict[str, Any]]] = None,
             keyed: bool = True) -> List[Dict[str, Any]]:
        argv = self.command + ["--home", home, "channel", name]
        if keyed:
            argv += ["--key-file", self.key_file, "--instance", self.instance]
        argv += list(args)
        stdin = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines) if lines is not None else None
        env = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}
        try:
            out = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env,
                                 timeout=CONTRACT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ContractError("transport", "channel %s timed out after %d s" % (name, CONTRACT_TIMEOUT))
        try:
            rows = [json.loads(text) for text in out.stdout.split("\n") if text.strip()]
        except ValueError as exc:
            raise ContractError("transport", "channel %s printed a line that is not JSON: %s" % (name, exc))
        if out.returncode != 0:
            error = (rows[0].get("error") or {}) if rows and isinstance(rows[0], dict) else {}
            raise ContractError(error.get("code") or "transport", "channel %s failed (exit %s): %s" % (
                name, out.returncode, error.get("message") or out.stderr.strip()[-300:]))
        return rows

    def one(self, home: str, name: str, *args: str) -> Dict[str, Any]:
        """A single-object command; a rejection line raises ContractError with its code."""
        rows = self.call(home, name, *args)
        if not rows:
            raise ContractError("transport", "channel %s printed nothing" % name)
        row = rows[0]
        if row.get("result") == "rejected" or row.get("error"):
            error = row.get("error") or {}
            raise ContractError(error.get("code") or "rejected", error.get("message") or "rejected")
        return row


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The multi-home adapter (P2: header, route map, reply routing; non-reply -> primary)
# ---------------------------------------------------------------------------

ROUTER = "↪ Router"
HELD_CAP = 20
HELD_NOTICE = "Held: not sent to any manager yet (chooser not available)."   # single-home config only
CHOOSE_NOTICE = "Held: not sent to any manager yet. Who should get this?"
HELD_DOWN_NOTICE = ("Held: %s can't be reached through this chat right now, so this message was not sent to it. "
                    "It is kept here until the operator fixes it.")
HELD_DOWN_CHOOSE = ("Held: %s can't be reached through this chat right now, so this message was not sent to it. "
                    "Pick a manager below to send it to; until you choose one, it stays held here.")
HELD_REPLY_DOWN = ("Held: %s can't be reached through this chat right now, so this reply was not sent to it. "
                   "It stays held for it and is sent to it when you choose it again after the operator fixes it.")
TALKING_TO = "Talking to: %s"
SENT_TO = "Sent to: %s"
CALLBACK_WINDOW = 200            # handled callback ids kept (C2: replay beyond it is refused by the update cursor)
HELD_DONE_KEEP = 200
HELD_CAP_NOTICE = ("Not kept: this message was NOT sent to any manager and was NOT saved, because %d messages "
                   "are already waiting. Please send it again later.")


class HomesAdapter:
    """One bot, one poller, several homes through the channel contract (#100 P2 + P3).

    A plain message goes to the saved selection (P3 chooser); with none it is held. A two-home config is first
    enabled live at P4, with P2 and P3 merged and running together (F8, R4)."""

    def __init__(self, config_path: str, contract: Optional[Contract] = None, client: Any = None):
        from .telegram import TelegramChannelState
        path = Path(config_path).expanduser().resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 2:
            raise HomesConfigError("a homes config needs \"version\": 2")
        unknown = sorted(set(data) - {"version", "token_file", "credential_file", "state_file", "lock_file",
                                      "homes", "command"})
        if unknown:
            raise HomesConfigError("unknown config field(s) %s" % ", ".join(unknown))
        for field in ("token_file", "credential_file", "state_file", "lock_file"):
            if not isinstance(data.get(field), str) or not data[field]:
                # No defaults inside a home: the adapter belongs to no single home.
                raise HomesConfigError("%s is required in a homes config" % field)
        base = path.parent
        resolve = lambda value: (base / Path(value).expanduser()).resolve()
        self.token_file = resolve(data["token_file"])
        self.credential_file = resolve(data["credential_file"])
        self.state_file = resolve(data["state_file"])
        self.lock_file = resolve(data["lock_file"])
        self.homes = load_homes(data["homes"], base)
        self.primary = next(h for h in self.homes if h["primary"])
        self.colleague_id = self.primary["colleague_id"]
        self.state = _homes_state(TelegramChannelState)(self.state_file)
        self.instance = "telegram-" + secrets.token_hex(6)
        self.contract = contract or Contract(str(self.credential_file), self.instance, command=data.get("command"))
        self.client = client
        self.down: Dict[str, str] = {}
        self.features: List[str] = []
        self._lock_fd: Optional[int] = None
        self._next_activity_at = 0.0

    # ---- state ----

    @property
    def d(self) -> Dict[str, Any]:
        return self.state._d

    @property
    def route_map(self) -> Dict[str, Any]:
        return self.d.setdefault("route_map", {})

    def _thread(self, home: Dict[str, Any]) -> str:
        return home["thread"] or "telegram:%s" % self.state.bound_chat_id

    # ---- startup ----

    def start(self) -> None:
        """Checks that don't need the network beyond the contract: bindings, migration, identity pins.

        The bindings are checked before anything is written (Rowan #104 L1): a refused start leaves the state
        file exactly as it was, so a wrong config never stamps schema 4 on another employee's state."""
        bound = self._verify_bindings()
        if migrate_state(self.d, self.primary) or bound:
            self.state.save()
        self._pin_homes()
        self._baselines()
        self._recover_inflight()

    def _verify_bindings(self) -> bool:
        """Schema 3 bindings move into the primary entry and keep their refusals (§4). Raises before any write;
        returns True when it bound a fresh state (the caller saves)."""
        st = self.state
        if st.employee_id is not None and st.employee_id != self.primary["employee_id"]:
            raise RuntimeError("Employee identity mismatch: state was bound to %r, the primary home is pinned to "
                               "%r. This state file belongs to a different employee; do not transfer it."
                               % (st.employee_id, self.primary["employee_id"]))
        if st.bound_colleague_id is not None and st.bound_colleague_id != self.colleague_id:
            raise RuntimeError("Colleague ID mismatch: state was bound to %r, config names %r."
                               % (st.bound_colleague_id, self.colleague_id))
        if st.bound_colleague_id is not None and st.bound_local_thread != self.primary["thread"]:
            raise RuntimeError("Local thread mismatch: state was bound to %r, the primary home names %r. "
                               "Changing the conversation thread on an established channel is not allowed."
                               % (st.bound_local_thread, self.primary["thread"]))
        if st.employee_id is None or st.bound_colleague_id is None:
            st.employee_id = self.primary["employee_id"]
            st.bound_colleague_id = self.colleague_id
            st.bound_local_thread = self.primary["thread"]
            return True
        return False

    def _pin_homes(self) -> None:
        """C4/F11: each home's ``channel identity`` must equal its pinned employee_id. A home that fails is stopped
        alone (``self.down``); the others keep working. The primary failing stops nothing else either."""
        try:
            version = self.contract.call(self.primary["home"], "version", keyed=False)
            self.features = list((version[0] if version else {}).get("features") or [])
        except ContractError as exc:
            self.features = []
            logger.warning("channel version unavailable: %s", exc)
        for home in self.homes:
            if "identity" not in self.features:
                self.down[home["key"]] = "this Synthetics has no `channel identity` (P1); the home can't be pinned"
                continue
            try:
                row = self.contract.one(home["home"], "identity")
            except ContractError as exc:
                self.down[home["key"]] = "identity check failed (%s)" % exc.code
                continue
            if row.get("employee_id") != home["employee_id"]:
                self.down[home["key"]] = "identity mismatch: home reports %r, config pins %r" % (
                    row.get("employee_id"), home["employee_id"])
        for key, reason in self.down.items():
            logger.error("Home %s stopped: %s", key, reason)

    def _baselines(self) -> None:
        """No flood (§1): rows created before a home's cutoff are left pending. The cutoff is the later of the
        config's ``not_before`` and the state's baseline (the primary keeps schema 3 ``outbox_baseline_at``; a home
        first seen with neither gets now)."""
        baselines = self.d.setdefault("baselines", {})
        changed = False
        legacy = self.state.outbox_baseline_at
        if self.primary["key"] not in baselines and legacy is not None:
            baselines[self.primary["key"]] = legacy
            changed = True
        for home in self.homes:
            if home["key"] not in baselines and not home["not_before"] and self.state.paired:
                baselines[home["key"]] = _now()
                changed = True
        if changed:
            self.state.save()

    def _cutoff(self, home: Dict[str, Any]) -> Optional[datetime]:
        values = [_ts(home["not_before"]), _ts((self.d.get("baselines") or {}).get(home["key"]) or None)]
        values = [v for v in values if v is not None]
        return max(values) if values else None

    def _recover_inflight(self) -> None:
        for msg_id, rec in list(self.state.deliveries.items()):
            if rec.get("status") == "in-flight":
                rec = dict(rec, status="uncertain",
                           error="Recovered: in-flight state at restart; Telegram delivery unconfirmed")
                self.state.set_delivery(msg_id, rec)
                logger.warning("Recovered in-flight delivery %s -> uncertain", msg_id)

    # ---- outbound ----

    def process_outbox(self) -> List[str]:
        if not self.state.paired or self.state.bound_chat_id is None:
            return []
        self._notify_down()
        attempted: List[str] = []
        for home in self.homes:
            if home["key"] in self.down:
                continue
            try:
                attempted += self._process_home(home)
            except ContractError as exc:
                logger.warning("Outbox for home %s unavailable: %s", home["key"], exc)
        return attempted

    @property
    def chooser(self) -> bool:
        """The chooser exists only with two or more homes; a single-home v2 config sends no keyboard."""
        return len(self.homes) > 1

    def selected(self) -> Optional[Dict[str, Any]]:
        """The saved selection's home, if it is still configured with the same pinned employee (§3.8)."""
        sel = self.d.get("selection") or None
        if not isinstance(sel, dict):
            return None
        return next((h for h in self.homes if h["key"] == sel.get("key")
                     and h["employee_id"] == sel.get("employee_id")), None)

    def markup(self) -> Optional[Dict[str, Any]]:
        """T17: every bot send carries the keyboard; ✓ is the SAVED selection, never the sending home."""
        if not self.chooser:
            return None
        home = self.selected()
        return keyboard(self.homes, home["key"] if home else None)

    def _send(self, text: str, markup: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if markup is None:
            return self.client.send_message(self.state.bound_chat_id, text)
        return self.client.send_message(self.state.bound_chat_id, text, reply_markup=markup)

    def notice(self, text: str, markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """An adapter-authored text (§3.9): starts with the Router mark, never a manager's label, is not an outbox
        row, and is mapped ``kind: notice`` so a Reply to it uses the current selection. It carries the persistent
        keyboard unless ``markup`` (the held-message inline chooser) is given."""
        from .telegram import TelegramAPIError, TelegramNetworkError
        try:
            tg_mid = self._send(ROUTER + "\n" + text, markup if markup is not None else self.markup())["message_id"]
        except (TelegramAPIError, TelegramNetworkError) as exc:
            logger.warning("Router notice not sent: %s", type(exc).__name__)
            return None
        self.route_map[str(tg_mid)] = dict(kind="notice", at=_now())
        self.state.save()
        return tg_mid

    def _notify_down(self) -> None:
        """C4: once per stop, tell David which manager is unreachable (label only, never the reason's details)."""
        notified = self.d.setdefault("down_notified", {})
        changed = False
        for home in self.homes:
            key = home["key"]
            if key in self.down and key not in notified:
                if self.notice("%s can't be reached through this chat right now. Messages to it are not "
                               "delivered until the operator fixes it." % home["label"]) is not None:
                    notified[key] = _now()
                    changed = True
            elif key not in self.down and key in notified:
                del notified[key]
                changed = True
        if changed:
            self.state.save()

    def _process_home(self, home: Dict[str, Any]) -> List[str]:
        cutoff = self._cutoff(home)
        thread = self._thread(home)
        attempted = []
        for row in self.contract.call(home["home"], "pending"):
            if row.get("to") != home["colleague_id"]:
                continue
            created = _ts(row.get("created_at"))
            if cutoff is not None and (created is None or created <= cutoff):
                continue
            if row.get("thread") is not None and row.get("thread") != thread:
                continue
            msg_id = row["message_id"]
            existing = self.state.get_delivery(msg_id)
            if existing and existing.get("status") in ("accepted", "failed", "uncertain", "in-flight"):
                continue
            if row.get("sender") != home["employee_id"]:
                # The pending line names who wrote it; another employee in this home means the pin is wrong.
                self.down[home["key"]] = "outbox sender %r is not the pinned %r" % (row.get("sender"),
                                                                                    home["employee_id"])
                logger.error("Home %s stopped: %s", home["key"], self.down[home["key"]])
                return attempted
            try:
                self.contract.one(home["home"], "claim", "--message", msg_id)
            except ContractError as exc:
                logger.warning("Not sending %s: claim refused (%s)", msg_id, exc.code)
                continue
            if row.get("kind") not in (None, "message", "question"):
                # Hop notices are for adapters that relay between homes; Telegram never shows one (contract §4.1).
                self._ack(home, msg_id, {"status": "failed", "error": "kind %s not supported" % row.get("kind")})
                continue
            from . import byline
            record = self._deliver(home, msg_id, row, byline.footer_for(home["home"], "telegram"))
            self._ack(home, msg_id, record)
            attempted.append(msg_id)
        return attempted

    def _deliver(self, home: Dict[str, Any], message_id: str, row: Dict[str, Any],
                 footer: Optional[str]) -> Dict[str, Any]:
        from .telegram import (MAX_RATE_LIMIT_WAIT, PART_SEND_INTERVAL, RATE_LIMIT_RETRIES, TelegramAPIError,
                               TelegramNetworkError)
        chat_id = self.state.bound_chat_id
        parts, meta = render_parts(row.get("body") or "", home["label"], footer)
        question = row.get("question_id") or None
        record: Dict[str, Any] = dict(status="in-flight", started_at=_now(), telegram_message_ids=[],
                                      parts_total=len(parts), error=None, home=home["key"], **meta)
        self.state.set_delivery(message_id, record)
        sent: List[int] = []
        for i, part in enumerate(parts):
            if i > 0:
                time.sleep(PART_SEND_INTERVAL)
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    # R1: the keyboard goes on the last part only (header on every part).
                    tg_mid = self._send(part, self.markup() if i == len(parts) - 1 else None)["message_id"]
                except TelegramAPIError as exc:
                    if exc.error_code == 429 and attempt < RATE_LIMIT_RETRIES:
                        time.sleep(min(exc.retry_after if exc.retry_after is not None else 10, MAX_RATE_LIMIT_WAIT))
                        continue
                    status = "uncertain" if exc.error_code == 429 else "failed"
                    record.update(status=status, error=str(exc), telegram_message_ids=list(sent))
                    self.state.set_delivery(message_id, record)
                    return record
                except TelegramNetworkError as exc:
                    record.update(status="uncertain", error=str(exc), telegram_message_ids=list(sent))
                    self.state.set_delivery(message_id, record)
                    return record
                sent.append(tg_mid)
                record["telegram_message_ids"] = list(sent)
                # Route entry and receipt in one save (crash-safe, as question_map today).
                self.route_map[str(tg_mid)] = dict(home=home["key"], employee_id=home["employee_id"],
                                                   message=message_id, question=question, part=i,
                                                   parts=len(parts), kind="out")
                self.d.setdefault("deliveries", {})[message_id] = record
                self.state.save()
                break
        record.update(status="accepted", accepted_at=_now())
        self.state.set_delivery(message_id, record)
        self.d.setdefault("awaiting", {}).pop(home["key"], None)
        self.state.save()
        return record

    def _ack(self, home: Dict[str, Any], message_id: str, record: Dict[str, Any]) -> None:
        status = record.get("status")
        ids = ",".join(str(i) for i in record.get("telegram_message_ids") or [])
        if status == "accepted":
            outcome, rcpt = "delivered", receipt(self.state.bound_chat_id, ids, record)
        elif status == "failed":
            outcome, rcpt = "failed", None
        else:
            outcome, rcpt = "uncertain", receipt(self.state.bound_chat_id, ids, record) if ids else None
        detail = {k: record[k] for k in ("error", "parts_total", "accepted_at") if record.get(k) is not None}
        line = dict(v=1, message_id=message_id, outcome=outcome, receipt=rcpt, retryable=False, detail=detail)
        try:
            rows = self.contract.call(home["home"], "ack", lines=[line])
            if not rows or rows[0].get("result") == "rejected":
                logger.error("Ack %s for %s refused: %s", outcome, message_id, rows and rows[0].get("error"))
            else:
                # The local journal says the home recorded this outcome; an accepted record without it was not
                # mirrored and needs `channel reconcile` (the claim lapses to uncertain; nothing is resent).
                record["acked"] = outcome
                self.state.set_delivery(message_id, record)
        except ContractError as exc:
            logger.error("Could not record %s outcome for %s: %s", outcome, message_id, exc)

    # ---- inbound (§3.6 routing order) ----

    def process_update(self, update: Dict[str, Any]) -> Dict[str, Any]:
        """One Telegram update. Same gates as the single-home UpdateProcessor (private chat, no bots, pairing,
        paired user only). Result ``reason`` ``store_error`` means: don't advance the cursor."""
        update_id = update["update_id"]
        if update.get("callback_query"):
            return self._callback(update_id, update["callback_query"])
        message = update.get("message")
        if not message:
            return {"processed": False, "reason": "no_message_field", "event_id": None}
        chat = message.get("chat", {})
        sender = message.get("from", {})
        if chat.get("type") != "private":
            return {"processed": False, "reason": "non_private_chat", "event_id": None}
        if sender.get("is_bot"):
            return {"processed": False, "reason": "bot_sender", "event_id": None}
        text = message.get("text", "")
        if not self.state.paired and text.startswith("/start "):
            return self._pair(update_id, message, sender.get("id"), text)
        if (not self.state.paired or sender.get("id") != self.state.bound_user_id
                or chat.get("id") != self.state.bound_chat_id):
            return {"processed": False, "reason": "unrecognized_sender", "event_id": None}
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        # §3.6 routing order: (1) callback (above); (2) selection message or standalone directive; (3) directive
        # with content, checked against a Reply first; (4) Reply rules; (5) current selection; (6) none -> hold.
        target = selection_target(message, self.homes)
        if target is not None:
            return self._select(target, "keyboard", update_id, message)
        command = text.strip().split(None, 1) if isinstance(text, str) else []
        if self.chooser and command and _TO_COMMAND.fullmatch(command[0]):
            return self._command(update_id, message, command[1].strip() if len(command) > 1 else "")
        directive = parse_directive(text, self.homes) if self.chooser and not message.get("caption") else None
        verdict, home, entry = resolve_reply(self.route_map, reply_to, self.homes)
        extra: Dict[str, Any] = {}
        if directive is not None:
            named, prefix, content = directive
            if not content.strip():
                return self._select(named, "directive", update_id, message)
            if verdict == "home" and home["key"] != named["key"]:
                # (d) a Reply to X with a directive naming Y: never guess.
                return self._hold(update_id, message, reply_to, reason="conflict", body=content,
                                  directive=prefix[:64])
            extra = dict(body=content, directive=prefix[:64])
            if self._save_selection(named, "directive", update_id):
                self.notice(TALKING_TO % named["button"])
            # Rowan #105 n1: earlier held messages go first, so arrival order is kept as with a tap.
            if self._release(named).get("reason") == "store_error":
                return {"processed": False, "reason": "store_error", "event_id": None}
            if verdict != "home":
                verdict, home, entry = "home", named, None
        if verdict == "none":
            if not self.chooser:
                home = self.primary
            else:
                home = self.selected()
                if home is None:
                    return self._hold(update_id, message, reply_to, reason="no_selection")
        elif verdict == HOLD:
            if not self.chooser:
                home = self.primary                  # nothing to choose between (today's behaviour)
            else:
                return self._hold(update_id, message, reply_to)
        if home["key"] in self.down:
            if not self.chooser:
                # Rowan #104 L2: one home has no other manager to hold for. Keep the v1 behaviour: don't move the
                # cursor, retry with backoff (Telegram keeps the update up to 24 h), never hold and drop at 20.
                logger.error("Home %s is down (%s); update %d not fed, will retry", home["key"],
                             self.down[home["key"]], update_id)
                return {"processed": False, "reason": "store_error", "event_id": None, "home": home["key"]}
            return self._hold(update_id, message, reply_to, reason="home_down", home=home,
                              target=home["key"] if (verdict == "home" and entry) else None, **extra)
        question = entry.get("question") if (verdict == "home" and entry) else None
        return self._feed(home, update_id, message, reply_to, entry if verdict == "home" else None, question,
                          **extra)

    def _pair(self, update_id: int, message: Dict[str, Any], sender_id: Any, text: str) -> Dict[str, Any]:
        provided = text[len("/start "):].strip()
        if not self.state.pairing_nonce or provided != self.state.pairing_nonce:
            reason = "no_pending_nonce" if not self.state.pairing_nonce else "wrong_nonce"
            self.state.record_rejection(reason, update_id)
            return {"processed": False, "reason": reason, "event_id": None}
        self.state.bound_user_id = sender_id
        self.state.bound_chat_id = message.get("chat", {}).get("id")
        self.state.paired = True
        self.state.pairing_nonce = None
        baselines = self.d.setdefault("baselines", {})
        for home in self.homes:
            baselines.setdefault(home["key"], _now())
        self.state.save()
        fake = dict(message, text="[Telegram pairing confirmed. Bot is now connected to this conversation.]")
        fake.pop("reply_to_message", None)
        result = self._feed(self.primary, update_id, fake, None, None, None)
        if result["reason"] == "store_error":
            return {"processed": True, "reason": "paired_no_feed", "event_id": None}
        return {"processed": True, "reason": "paired", "event_id": result.get("event_id")}

    def _line(self, home: Dict[str, Any], update_id: int, message: Dict[str, Any], reply_to: Optional[int],
              entry: Optional[Dict[str, Any]], body: Optional[str] = None,
              directive: Optional[str] = None) -> Dict[str, Any]:
        from .telegram import UpdateProcessor, _tg_date_to_iso
        if body is None:
            body, _kind = UpdateProcessor._extract_content(None, message)  # type: ignore[arg-type]
        line: Dict[str, Any] = dict(v=1, external_id="tg:%s:%s" % (self.state.bot_id, update_id),
                                    sender=home["colleague_id"], body=body, source="telegram:%s" % self.state.bot_id,
                                    thread=self._thread(home))
        occurred = _tg_date_to_iso(message.get("date"))
        if occurred:
            line["occurred_at"] = occurred
        target = in_reply_to(entry, reply_to)
        if target:
            line["in_reply_to"] = target
        if "inbound.provider" in self.features:
            provider = {"message_id": message.get("message_id"), "chat_id": message.get("chat", {}).get("id")}
            if reply_to is not None:
                provider["reply_to_message_id"] = reply_to
            if directive and directive.strip():
                provider["directive"] = directive      # F14: original = directive + body
            line["provider"] = {k: v for k, v in provider.items() if v is not None}
        return line

    def _feed(self, home: Dict[str, Any], update_id: int, message: Dict[str, Any], reply_to: Optional[int],
              entry: Optional[Dict[str, Any]], question: Optional[str], body: Optional[str] = None,
              directive: Optional[str] = None) -> Dict[str, Any]:
        line = self._line(home, update_id, message, reply_to, entry, body, directive)
        try:
            row = self._inbound(home, dict(line, question_id=question) if question else line)
            if question and row.get("result") == "rejected" and (row.get("error") or {}).get("code") in (
                    "invalid_state", "unknown_message"):
                # Closed or answered elsewhere: the same line without question_id, context kept (§2 rule 2).
                # A refused line wrote no event, so the same external_id is reused.
                row = self._inbound(home, line)
        except ContractError as exc:
            if exc.code in ("auth_failed", "in_session"):
                from .telegram import ChannelConfigError
                raise ChannelConfigError("Telegram channel misconfigured for home %s: %s" % (home["key"], exc))
            logger.error("Failed to feed update %d to home %s: %s", update_id, home["key"], exc)
            return {"processed": False, "reason": "store_error", "event_id": None}
        if row.get("result") == "rejected":
            code = (row.get("error") or {}).get("code")
            if code == "source_not_allowed":
                from .telegram import ChannelConfigError
                raise ChannelConfigError("Telegram channel misconfigured for home %s: %s" % (home["key"], code))
            self.state._d.setdefault("feed_rejections", []).append(
                {"update_id": update_id, "at": _now(), "home": home["key"], "error": str(code)[:200]})
            del self.state._d["feed_rejections"][:-50]
            self.state._d["feed_rejections_total"] = int(self.state._d.get("feed_rejections_total") or 0) + 1
            self.state.save()
            return {"processed": False, "reason": "rejected", "event_id": None}
        message_id = message.get("message_id")
        if message_id is not None:
            self.route_map[str(message_id)] = dict(home=home["key"], employee_id=home["employee_id"],
                                                   event=row.get("event_id"), kind="in")
        self.d.setdefault("awaiting", {})[home["key"]] = _now()
        self.state.save()
        return {"processed": True, "reason": "fed", "event_id": row.get("event_id"), "home": home["key"]}

    def _inbound(self, home: Dict[str, Any], line: Dict[str, Any]) -> Dict[str, Any]:
        rows = self.contract.call(home["home"], "inbound", lines=[line])
        if len(rows) != 1:
            raise ContractError("transport", "channel inbound printed %d lines for 1" % len(rows))
        return rows[0]

    def _hold(self, update_id: int, message: Dict[str, Any], reply_to: Optional[int],
              reason: str = "ambiguous", body: Optional[str] = None,
              directive: Optional[str] = None, home: Optional[Dict[str, Any]] = None,
              target: Optional[str] = None) -> Dict[str, Any]:
        """F10: kept durably (cap 20) until a tap releases it (§3.4); never fed by itself. A hold is never silent:
        one Router notice per held message (with the inline chooser when there is a choice), and a message over
        the cap gets a distinct notice that it was NOT kept. ``target``: a Reply to a home that is down stays held
        for that home only (Rowan #105 L2); no other home's tap releases it, so its notice offers no choice."""
        held = self.d.setdefault("held", [])
        if any(h.get("update_id") == update_id for h in held):
            return {"processed": False, "reason": "held", "event_id": None}
        if len(held) >= HELD_CAP:
            self.notice(HELD_CAP_NOTICE % HELD_CAP)
            return {"processed": False, "reason": "held_cap", "event_id": None}
        record = dict(update_id=update_id, message=message, reply_to=reply_to, reason=reason, at=_now())
        if body is not None:
            record.update(body=body, directive=directive)
        if target is not None:
            record["target"] = target
        held.append(record)
        self.state.save()
        # A message for a manager that can't be reached says so honestly (Rowan #104 L2), not "not sent yet".
        down = reason == "home_down" and home is not None
        if self.chooser and target is not None:
            record["notice"] = self.notice(HELD_REPLY_DOWN % home["label"])
            self.state.save()
        elif self.chooser:
            text = HELD_DOWN_CHOOSE % home["label"] if down else CHOOSE_NOTICE
            record["notice"] = self.notice(text, markup=inline_chooser(self.homes, self._gen()))
            self.state.save()
        else:
            self.notice(HELD_DOWN_NOTICE % home["label"] if down else HELD_NOTICE)   # a failed notice only logs
        return {"processed": False, "reason": "held", "event_id": None}

    # ---- chooser (P3) ----

    def _gen(self) -> int:
        return int(self.d.get("chooser_gen") or 0)

    def _save_selection(self, home: Dict[str, Any], via: str, update_id: int) -> bool:
        """§3.8: saved before any reply or feed. Returns True when the selection changed."""
        current = self.selected()
        self.d["selection"] = dict(key=home["key"], employee_id=home["employee_id"], at=_now(), by_update=update_id,
                                   via=via)
        self.state.save()
        return current is None or current["key"] != home["key"]

    def _select(self, home: Dict[str, Any], via: str, update_id: int, message: Optional[Dict[str, Any]]
                ) -> Dict[str, Any]:
        """A keyboard tap, a standalone directive or ``/to <name>``: saved, journaled without body, never fed;
        one Router notice with the refreshed keyboard; held messages are released to that home."""
        self._save_selection(home, via, update_id)
        if message is not None and message.get("message_id") is not None:
            self.route_map[str(message["message_id"])] = dict(kind="select", key=home["key"], update_id=update_id,
                                                              at=_now())
            self.state.save()
        self.notice(TALKING_TO % home["button"])
        result = self._release(home)
        if result.get("reason") == "store_error":
            return result
        return {"processed": True, "reason": "selected", "event_id": None, "home": home["key"],
                "released": result.get("released", 0)}

    def _command(self, update_id: int, message: Dict[str, Any], arg: str) -> Dict[str, Any]:
        """§3.7 optional ``/to``: alone re-sends the keyboard; ``/to <shortcut|key|button>`` selects. Never fed."""
        if message.get("message_id") is not None:
            self.route_map[str(message["message_id"])] = dict(kind="select", key=None, update_id=update_id, at=_now())
            self.state.save()
        if arg:
            wanted = arg.casefold()
            home = next((h for h in self.homes if wanted in (h["key"].casefold(), (h["shortcut"] or "").casefold(),
                                                             h["button"].casefold())), None)
            if home is not None:
                return self._select(home, "command", update_id, None)
        current = self.selected()
        self.notice(TALKING_TO % current["button"] if current else "Choose who to talk to below.")
        return {"processed": True, "reason": "command", "event_id": None}

    def _held_for(self, record: Dict[str, Any], home: Dict[str, Any]) -> bool:
        """Rowan #105 L2: a held Reply whose home was down waits for that home only, while it is still configured.
        Every other hold (no selection, unknown target, conflict, plain message to a down home) goes to the
        chosen home."""
        target = record.get("target")
        return target is None or target == home["key"] or all(h["key"] != target for h in self.homes)

    def _release(self, home: Dict[str, Any]) -> Dict[str, Any]:
        """Feed the held messages meant for ``home`` (``_held_for``) in arrival order; the others stay held, in
        order. C1: once fed or refused, the body leaves the state; only ids stay (``held_done``). A transient
        failure keeps that message and the rest held and returns ``store_error``; the generation moves on only
        after a complete release, so a redelivered inline tap still matches (Rowan #105 L1)."""
        if home["key"] in self.down or not any(self._held_for(r, home) for r in self.d.get("held") or []):
            return {"released": 0}
        released = 0
        while True:
            record = next((r for r in self.d.get("held") or [] if self._held_for(r, home)), None)
            if record is None:
                break
            reply_to = record.get("reply_to")
            verdict, target, entry = resolve_reply(self.route_map, reply_to, self.homes)
            same = verdict == "home" and target["key"] == home["key"]
            result = self._feed(home, record["update_id"], record["message"], reply_to, entry if same else None,
                                entry.get("question") if (same and entry) else None, record.get("body"),
                                record.get("directive"))
            if result.get("reason") == "store_error":
                return {"released": released, "reason": "store_error"}
            self.d["held"].remove(record)
            done = self.d.setdefault("held_done", [])
            done.append(dict(update_id=record["update_id"], home=home["key"], event_id=result.get("event_id"),
                             outcome=result.get("reason"), at=_now()))
            del done[:-HELD_DONE_KEEP]
            self.state.save()
            released += 1
            if record.get("notice") is not None:
                self._edit(record["notice"], SENT_TO % home["button"])
        self.d["chooser_gen"] = self._gen() + 1     # every inline chooser shown before now is stale
        self.state.save()
        return {"released": released}

    def _edit(self, tg_mid: int, text: str) -> None:
        """Replace a held message's inline chooser with where it went (removes its buttons). Cosmetic: logs only."""
        try:
            self.client.edit_message_text(self.state.bound_chat_id, tg_mid, ROUTER + "\n" + text)
        except Exception as exc:
            logger.warning("Router notice not edited: %s", type(exc).__name__)

    def _callback(self, update_id: int, query: Dict[str, Any]) -> Dict[str, Any]:
        """§3.4 / r0 rule: paired user in the paired chat, not a bot, known key, current gen, callback id not seen.
        C2: the id window is the second guard; the first is the update cursor (poll_once never processes an
        update_id at or below it, and Telegram does not resend a confirmed update)."""
        sender = query.get("from") or {}
        chat = ((query.get("message") or {}).get("chat") or {})
        qid = str(query.get("id") or "")
        handled = self.d.setdefault("callbacks", [])
        reason = None
        home = None
        if not self.state.paired or sender.get("is_bot") or sender.get("id") != self.state.bound_user_id \
                or chat.get("id") != self.state.bound_chat_id:
            reason = "unrecognized_sender"
        elif not qid or qid in handled:
            reason = "callback_replayed"
        else:
            parts = str(query.get("data") or "").split(":")
            home = next((h for h in self.homes if len(parts) == 3 and parts[0] == "sel" and h["key"] == parts[1]),
                        None)
            if home is None:
                reason = "callback_unknown"
            elif parts[2] != str(self._gen()) or not self.d.get("held"):
                reason = "callback_stale"
            elif home["key"] in self.down:
                reason = "home_down"
        if qid and reason is not None and reason != "unrecognized_sender":
            handled.append(qid)
            del handled[:-CALLBACK_WINDOW]
        if reason is not None:
            self.state.record_rejection(reason, update_id)
            if reason != "unrecognized_sender":
                self._answer(qid, "Out of date. Use the buttons below the message box.")
            return {"processed": False, "reason": reason, "event_id": None}
        self._save_selection(home, "inline", update_id)
        self._answer(qid, None)
        self.notice(TALKING_TO % home["button"])
        result = self._release(home)
        if result.get("reason") == "store_error":
            # Rowan #105 L1: the id is not recorded, so Telegram's redelivery of this update finishes the release.
            return result
        handled.append(qid)
        del handled[:-CALLBACK_WINDOW]
        self.state.save()
        return {"processed": True, "reason": "selected", "event_id": None, "home": home["key"],
                "released": result.get("released", 0)}

    def _answer(self, qid: str, text: Optional[str]) -> None:
        if not qid:
            return
        try:
            self.client.answer_callback_query(qid, text)
        except Exception as exc:
            logger.warning("Callback not answered: %s", type(exc).__name__)

    # ---- typing indicator (F11: shared/status.json, never the Store) ----

    def service_activity(self) -> None:
        """Typing while a home whose session is running still owes David an answer: the adapter fed it a message
        and has delivered nothing from that home since. status.json missing or unreadable: typing is off."""
        from .telegram import CHAT_ACTION_INTERVAL
        now = time.monotonic()
        if now < self._next_activity_at or not self.state.paired or self.state.bound_chat_id is None:
            return
        awaiting = self.d.get("awaiting") or {}
        for home in self.homes:
            if home["key"] not in awaiting or home["key"] in self.down:
                continue
            try:
                status = json.loads((Path(home["home"]) / "shared" / "status.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if ((status.get("session") or {}).get("state") != "running"
                    or status.get("employee") not in (None, home["employee_id"])):
                continue
            self._next_activity_at = now + CHAT_ACTION_INTERVAL
            try:
                self.client.send_chat_action(self.state.bound_chat_id, "typing")
            except Exception as exc:  # cosmetic; never interrupts delivery
                logger.warning("Activity feedback unavailable: %s", type(exc).__name__)
            return

    # ---- main loop ----

    def run(self) -> None:
        import fcntl
        from .telegram import TelegramClient
        self.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.lock_file), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError("Another telegram adapter is running (lock: %s)" % self.lock_file)
        try:
            if self.client is None:
                self.token_file.chmod(0o600)
                self.client = TelegramClient(self.token_file.read_text(encoding="utf-8").strip())
            self._run_loop()
        except KeyboardInterrupt:
            logger.info("Interrupted; shutting down.")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def verify_bot(self) -> None:
        me = self.client.get_me()
        if self.state.bot_id is None:
            self.state.bot_id = me["id"]
            self.state.bot_username = me.get("username")
            self.state.save()
        elif self.state.bot_id != me["id"]:
            raise RuntimeError("Bot identity mismatch: state was bound to bot %s, the token identifies bot %s."
                               % (self.state.bot_id, me["id"]))
        if (self.client.get_webhook_info() or {}).get("url"):
            raise RuntimeError("Incompatible webhook is active on this bot. Remove it with deleteWebhook first.")

    def poll_once(self) -> bool:
        """One poll + updates + outbox + typing pass. Returns False when an update must be retried (store_error)."""
        from .telegram import OUTBOX_CHECK_INTERVAL
        cursor = self.state.update_cursor
        updates = self.client.get_updates(offset=cursor + 1 if cursor is not None else None,
                                          timeout=OUTBOX_CHECK_INTERVAL,
                                          allowed_updates=["message", "callback_query"] if self.chooser else None)
        ok = True
        for update in updates:
            if cursor is not None and update["update_id"] <= cursor:
                # C2: an update at or below the confirmed cursor is never processed again (cursor-based window).
                continue
            result = self.process_update(update)
            if result.get("reason") == "store_error":
                ok = False
                break
            self.state.update_cursor = update["update_id"]
            self.state.save()
        if self.state.paired:
            try:
                self.process_outbox()
            except Exception as exc:
                logger.warning("Outbox processing error: %s", exc)
            self.service_activity()
        return ok

    def _run_loop(self) -> None:
        from .telegram import (TRANSIENT_BACKOFF_INITIAL, TRANSIENT_BACKOFF_MAX, TelegramAPIError,
                               TelegramNetworkError)
        self.verify_bot()
        self.start()
        if not self.state.paired and not self.state.pairing_nonce:
            self.state.pairing_nonce = secrets.token_urlsafe(16)
            self.state.save()
        if not self.state.paired:
            name = self.state.bot_username or "your_bot"
            print("\n[PAIRING REQUIRED]\nSend the following command to @%s in Telegram:\n\n    /start %s\n"
                  % (name, self.state.pairing_nonce), file=sys.stderr, flush=True)
        backoff = TRANSIENT_BACKOFF_INITIAL
        while True:
            try:
                ok = self.poll_once()
            except TelegramNetworkError as exc:
                logger.warning("Poll failed (transient): %s", exc)
                ok = False
            except TelegramAPIError as exc:
                if exc.error_code != 429:
                    raise
                backoff = max(backoff, float(exc.retry_after or backoff))
                ok = False
            if ok:
                backoff = TRANSIENT_BACKOFF_INITIAL
                continue
            deadline = time.monotonic() + backoff
            while time.monotonic() < deadline:
                time.sleep(1.0)
                if self.state.paired:
                    try:
                        self.process_outbox()
                    except Exception as exc:
                        logger.warning("Outbox processing error during backoff: %s", exc)
            backoff = min(backoff * 2, TRANSIENT_BACKOFF_MAX)


def _homes_state(base: Any) -> Any:
    """The single-home state class with schema 4 on save (its own save would stamp schema 3 back)."""
    class HomesState(base):  # type: ignore[misc, valid-type]
        def save(self) -> None:
            from .telegram import _atomic_write
            self._d["schema"] = STATE_SCHEMA
            route_map = self._d.get("route_map")
            if isinstance(route_map, dict) and len(route_map) > ROUTE_MAP_CAP:
                # Bounded (Rowan #104 nit): the oldest entries go first (JSON keeps insertion order). A Reply to a
                # dropped target is unmapped, so it is held, never guessed.
                for stale in list(route_map)[:len(route_map) - ROUTE_MAP_CAP]:
                    del route_map[stale]
            _atomic_write(self.path, self._d)
            guard = self._guard_path()
            if not guard.exists():
                try:
                    guard.write_text(_now(), encoding="utf-8")
                    guard.chmod(0o600)
                except OSError:
                    pass
    return HomesState


def _now() -> str:
    from .telegram import _now_utc
    return _now_utc()

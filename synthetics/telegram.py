"""
Telegram private-chat adapter for Synthetics employee inboxes.

Delivers authenticated messages from a configured human colleague (David)
into an employee inbox via long polling. Proactively delivers the employee's
addressed outbox messages back to that colleague over Telegram.

Requires Python 3.9+. Standard library only (no third-party dependencies).
One adapter process per state file (enforced via flock).

Public API
----------
TelegramConfig(config_path)
    Load adapter config from a JSON file. Required keys: token_file,
    colleague_id, employee_home. Optional keys: state_file, lock_file,
    local_thread.

TelegramChannelState(path)
    Persistent adapter state: pairing, identity bindings, delivery tracking,
    reply correlation, rejection log. Loads from JSON on construction; saves
    atomically via save() with fsync and directory fsync for durability.
    Writes a .guard marker alongside the state file on first save so that a
    missing state on a previously established channel fails visibly on reload.

TelegramClient(token, api_base=TELEGRAM_API_BASE)
    Thin urllib wrapper for the Telegram Bot API. Token is never logged or
    included in error messages (redact_token is applied throughout).
    TelegramAPIError carries retry_after for 429 responses.

UpdateProcessor(state, colleague_id, local_thread=None)
    Classifies a single Telegram update and feeds it into the employee Store.
    Returns a result dict with keys: processed, reason, event_id.

OutboxDelivery(state, colleague_id, client, local_thread=None)
    Delivers employee outbox messages (addressed to colleague_id and routed
    through the private conversation thread) to Telegram. Maintains durable
    delivery states: pending → in-flight → accepted | failed | uncertain.
    In-flight records found on restart are recovered to uncertain (no auto-retry).
    Per-chat pacing honors Telegram's ~1 msg/sec limit. 429 responses are retried
    with retry_after backoff; exhausted retries → uncertain (not failed).
    Question mappings are written atomically with each accepted part receipt so
    that a crash between acceptance and mapping cannot lose correlation.
    rebuild_question_map() can reconstruct mappings from delivery receipts on startup.

TelegramAdapter(config_path)
    Main entry point. Acquires an exclusive process lock, verifies bot, employee,
    and config identity, then runs the polling/delivery loop until interrupted.

Pairing
-------
On first run with no existing state the adapter generates a one-time
high-entropy nonce and prints pairing instructions to stderr. The human must
send ``/start NONCE`` (or open the deep link) in Telegram. The adapter then
binds the sender's numeric user_id and chat_id and begins normal operation.
No other sender is ever accepted. Pairing rejection attempts are durably
recorded before the cursor is acknowledged.

Thread routing
--------------
If ``local_thread`` is specified in config, that value is used as the local
conversation thread ID for both incoming message tagging and outgoing message
filtering. Absent that override, the thread defaults to ``telegram:{chat_id}``.
Outgoing messages whose explicit thread does not match the private conversation
thread are silently skipped (they are not addressed to this channel).

Delivery states
---------------
accepted  — Telegram returned a message_id for every part.
failed    — Telegram API rejected the message permanently (operator must intervene).
uncertain — Network error at any point, or 429 retries exhausted. Telegram may
            or may not have accepted the message. No blind retries; operator must resolve.

Rate limiting
-------------
Telegram documents approximately 1 message per second per chat. Parts of a
split message are paced with PART_SEND_INTERVAL between sends. 429 responses
carry a retry_after value; the adapter sleeps that long (capped at
MAX_RATE_LIMIT_WAIT) and retries the same part up to RATE_LIMIT_RETRIES times.
If retries are exhausted the delivery is marked uncertain (not failed). Poll
429 is treated as a transient error with backoff.
"""

import fcntl
import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096
SCHEMA_VERSION = 3
POLL_TIMEOUT_SECONDS = 30
# Maximum seconds between outbox delivery checks. The inbound poll uses this
# as its timeout so the loop returns promptly to service pending outbound work
# even when the channel is quiet. Backoff sleeps are also broken into intervals
# of this length so a fresh outbox message is not starved during 429 recovery.
OUTBOX_CHECK_INTERVAL = 1
CHAT_ACTION_INTERVAL = 4.0
CHAT_ACTION_TIMEOUT = 2
TRANSIENT_BACKOFF_INITIAL = 1.0
TRANSIENT_BACKOFF_MAX = 60.0
# Per-chat send pacing (Telegram recommends ~1 msg/sec per chat)
PART_SEND_INTERVAL = 1.1
# Cap on retry_after honoring for 429 rate-limit responses
MAX_RATE_LIMIT_WAIT = 30.0
# Maximum retries on a single part when Telegram returns 429
RATE_LIMIT_RETRIES = 5
# Attachment types that arrive as Telegram message fields
_ATTACHMENT_TYPES = (
    "photo", "sticker", "document", "audio", "video", "voice",
    "video_note", "animation", "contact", "location", "venue",
    "poll", "dice", "game",
)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    """Return current UTC time as an ISO 8601 string with microseconds and Z suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _tg_date_to_iso(unix_ts: Optional[int]) -> Optional[str]:
    """Convert a Telegram Unix timestamp to ISO 8601 UTC string."""
    if unix_ts is None:
        return None
    return (
        datetime.utcfromtimestamp(unix_ts)
        .replace(tzinfo=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def split_text(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """
    Split *text* into parts of at most *max_len* characters each.

    Breaks on newline first, then space, then hard-cuts at max_len.
    Returns a list with at least one element.
    """
    if len(text) <= max_len:
        return [text]
    parts: List[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = text.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts or [text]


FOOTER_SEPARATOR = "\n\n"


def _footer(store: Any) -> Optional[str]:
    """Byline (#15) footer for this channel, or None (no attribution.json, invalid, or not in scope)."""
    home = getattr(store, "home", None)
    if home is None:
        return None
    from . import byline
    return byline.footer_for(home, "telegram")


def _receipt(chat_id: Any, ids: str, record: Dict[str, Any]) -> str:
    """telegram:<chat>:<ids>, plus ;footer:<sha8> when a Byline footer was sent (#15)."""
    from . import byline
    return byline.with_footer_receipt("telegram:%s:%s" % (chat_id, ids), (record or {}).get("footer"))


def redact_token(text: str, token: str) -> str:
    """Remove the bot token string from *text*. Safe to call with empty token."""
    if token and token in text:
        return text.replace(token, "[REDACTED]")
    return text


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    """
    Write *data* as JSON to *path* atomically (write→fsync→rename→dir-fsync).

    Mode 0o600. The fsync on the temp file and the directory ensures the
    rename is durable before claiming the cursor or delivery receipt is committed.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    with open(str(tmp), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.chmod(0o600)
    os.replace(str(tmp), str(path))
    # fsync the directory to persist the rename (best-effort on all platforms)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    path.chmod(0o600)


def _feed_event(store: Any, *, in_reply_to: Optional[str] = None,
                credential: Optional[bytes] = None, **kwargs: Any) -> Any:
    """
    Call store.feed() with optional in_reply_to and channel credential.

    The Store now supports in_reply_to natively (core-runtime-M03 and later).
    With a credential (M22) the Store stamps event.auth for the telegram channel.
    """
    if credential is not None:
        kwargs["credential"] = credential
    return store.feed(in_reply_to=in_reply_to, **kwargs)


class ChannelConfigError(RuntimeError):
    """The channel key is not registered in the home, or may not write this source (issue #5).

    Deterministic for every update, so the adapter stops instead of retrying or dropping messages."""


FEED_REJECTIONS_KEPT = 50


def channel_problem(store: Any, credential: Optional[bytes], source: str) -> Optional[str]:
    """None when *credential* is a registered channel of *store* that may write *source*.

    Otherwise a short reason. No credential means unauthenticated input (M22): nothing to check."""
    if credential is None:
        return None
    try:
        digest = store.credential_hash(credential)
    except ValueError as exc:
        return str(exc)
    for channel in store.channel_list():
        if channel.get("key_sha256") == digest:
            if any(source.startswith(prefix) for prefix in channel.get("source_prefixes") or []):
                return None
            return "channel %s may not write source %s" % (channel.get("id"), source)
    return "the credential does not match a registered channel"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TelegramConfig:
    """
    Immutable adapter configuration loaded from a JSON file.

    Required keys: token_file, colleague_id, employee_home.
    Optional keys: state_file, lock_file, local_thread.

    local_thread: stable local conversation thread ID override for this
    Telegram channel (e.g. "codex:<uuid>" to continue a savedchat history).
    Defaults to "telegram:{chat_id}" if absent.
    """

    def __init__(self, config_path: str):
        config_path = Path(config_path).expanduser().resolve()
        with config_path.open(encoding="utf-8") as fh:
            data = json.load(fh)

        self.token_file = Path(data["token_file"]).expanduser().resolve()
        self.colleague_id: str = str(data["colleague_id"])
        self.employee_home = Path(data["employee_home"]).expanduser().resolve()
        self.local_thread: Optional[str] = data.get("local_thread") or None
        # M22: channel key file; inputs fed with it carry event.auth.
        self.credential_file: Optional[Path] = (
            Path(data["credential_file"]).expanduser().resolve() if data.get("credential_file") else None)

        self.state_file = Path(
            data.get("state_file", self.employee_home / "telegram_state.json")
        ).expanduser().resolve()
        self.lock_file = Path(
            data.get("lock_file", self.employee_home / "telegram.lock")
        ).expanduser().resolve()

    def load_credential(self) -> Optional[bytes]:
        """Read the channel key (M22). None when not configured: inputs then stay unauthenticated."""
        if self.credential_file is None:
            logger.warning("No credential_file configured: Telegram inputs will be unauthenticated (M22).")
            return None
        from .store import read_credential_file
        return read_credential_file(self.credential_file)

    def load_token(self) -> str:
        """Read and return the bot token. Never cached."""
        self.token_file.chmod(0o600)
        return self.token_file.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Channel State
# ---------------------------------------------------------------------------

class TelegramChannelState:
    """
    Persistent adapter state: pairing, identity bindings, delivery tracking,
    reply correlation, and durable rejection log.

    Loads from a JSON file at construction; writes atomically on save() with
    fsync + directory fsync so cursor and receipt claims are durable.

    Schema version 3 adds bound_colleague_id and bound_local_thread to detect
    config changes that would route messages to a different local principal.

    Guard file: a .guard marker is written alongside the state file on first
    save. If the state file is later missing but the guard exists, _load()
    raises RuntimeError rather than silently creating fresh state.
    """

    def __init__(self, path: Path):
        self.path = path
        self._d: Dict[str, Any] = {}
        self._load()

    def _guard_path(self) -> Path:
        """Return the path of the guard marker file for this state."""
        return self.path.parent / (self.path.name + ".guard")

    def _load(self) -> None:
        if self.path.exists():
            self.path.chmod(0o600)
            try:
                self._d = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(
                    f"State file {self.path} is corrupt and cannot be parsed. "
                    "Operator must recover manually; inspect the file and "
                    "restore a backup if available. Do not delete — data may "
                    "be restorable."
                ) from exc
            # Ensure mutable nested dicts/lists exist for new fields
            self._d.setdefault("deliveries", {})
            self._d.setdefault("question_map", {})
            self._d.setdefault("pairing_rejections", [])
            self._d.setdefault("feed_rejections", [])
            # employee_id is new in schema v2 — may be absent in older state files
            self._d.setdefault("employee_id", None)
            # bound_colleague_id/bound_local_thread are new in schema v3
            self._d.setdefault("bound_colleague_id", None)
            self._d.setdefault("bound_local_thread", None)
        elif self._guard_path().exists():
            # Guard exists but state file is missing: previously established channel
            raise RuntimeError(
                f"State file {self.path} is missing but guard "
                f"{self._guard_path()} exists, indicating this channel was "
                "previously established. Operator must recover manually; do "
                "not start fresh — existing bindings and delivery history "
                "would be silently discarded."
            )
        else:
            # Genuine fresh start: no state file, no guard
            self._d = {
                "schema": SCHEMA_VERSION,
                "employee_id": None,
                "bot_id": None,
                "bot_username": None,
                "pairing_nonce": None,
                "paired": False,
                "bound_user_id": None,
                "bound_chat_id": None,
                "bound_colleague_id": None,
                "bound_local_thread": None,
                # ISO timestamp of the last outbox message (addressed to colleague) at
                # first connection; "" means outbox was empty; None means not yet set.
                "outbox_baseline_at": None,
                # message_id -> delivery record
                "deliveries": {},
                # str(telegram_message_id) -> local question_id
                "question_map": {},
                # list of rejected pairing attempt records
                "pairing_rejections": [],
                # updates the Store refused deterministically (issue #5), newest last, bounded
                "feed_rejections": [],
            }

    def save(self) -> None:
        """Persist current state atomically with fsync."""
        self._d["schema"] = SCHEMA_VERSION
        _atomic_write(self.path, self._d)
        # Write guard marker to detect missing state on restart (best-effort).
        # Guard is intentionally not synced — it is advisory only.
        guard = self._guard_path()
        if not guard.exists():
            try:
                with open(str(guard), "w", encoding="utf-8") as fh:
                    fh.write(_now_utc())
                guard.chmod(0o600)
            except OSError:
                pass  # best-effort; absence of guard just means missing-state detection is unavailable

    # ---- employee identity ----

    @property
    def employee_id(self) -> Optional[str]:
        return self._d.get("employee_id")

    @employee_id.setter
    def employee_id(self, v: Optional[str]) -> None:
        self._d["employee_id"] = v

    # ---- bot identity ----

    @property
    def bot_id(self) -> Optional[int]:
        return self._d.get("bot_id")

    @bot_id.setter
    def bot_id(self, v: Optional[int]) -> None:
        self._d["bot_id"] = v

    @property
    def bot_username(self) -> Optional[str]:
        return self._d.get("bot_username")

    @bot_username.setter
    def bot_username(self, v: Optional[str]) -> None:
        self._d["bot_username"] = v

    # ---- pairing ----

    @property
    def pairing_nonce(self) -> Optional[str]:
        return self._d.get("pairing_nonce")

    @pairing_nonce.setter
    def pairing_nonce(self, v: Optional[str]) -> None:
        self._d["pairing_nonce"] = v

    @property
    def paired(self) -> bool:
        return bool(self._d.get("paired"))

    @paired.setter
    def paired(self, v: bool) -> None:
        self._d["paired"] = v

    @property
    def bound_user_id(self) -> Optional[int]:
        return self._d.get("bound_user_id")

    @bound_user_id.setter
    def bound_user_id(self, v: Optional[int]) -> None:
        self._d["bound_user_id"] = v

    @property
    def bound_chat_id(self) -> Optional[int]:
        return self._d.get("bound_chat_id")

    @bound_chat_id.setter
    def bound_chat_id(self, v: Optional[int]) -> None:
        self._d["bound_chat_id"] = v

    # ---- config binding (schema v3) ----

    @property
    def bound_colleague_id(self) -> Optional[str]:
        return self._d.get("bound_colleague_id")

    @bound_colleague_id.setter
    def bound_colleague_id(self, v: Optional[str]) -> None:
        self._d["bound_colleague_id"] = v

    @property
    def bound_local_thread(self) -> Optional[str]:
        return self._d.get("bound_local_thread")

    @bound_local_thread.setter
    def bound_local_thread(self, v: Optional[str]) -> None:
        self._d["bound_local_thread"] = v

    # ---- polling cursor ----

    @property
    def update_cursor(self) -> Optional[int]:
        return self._d.get("update_cursor")

    @update_cursor.setter
    def update_cursor(self, v: Optional[int]) -> None:
        self._d["update_cursor"] = v

    # ---- outbox baseline ----

    @property
    def outbox_baseline_at(self) -> Optional[str]:
        return self._d.get("outbox_baseline_at")

    @outbox_baseline_at.setter
    def outbox_baseline_at(self, v: Optional[str]) -> None:
        self._d["outbox_baseline_at"] = v

    # ---- deliveries ----

    @property
    def deliveries(self) -> Dict[str, Any]:
        return self._d.setdefault("deliveries", {})

    def get_delivery(self, message_id: str) -> Optional[Dict[str, Any]]:
        return self.deliveries.get(message_id)

    def set_delivery(self, message_id: str, record: Dict[str, Any]) -> None:
        self.deliveries[message_id] = record
        self.save()

    def set_delivery_with_question(
        self,
        message_id: str,
        record: Dict[str, Any],
        tg_question_id: Optional[int] = None,
        question_id: Optional[str] = None,
    ) -> None:
        """
        Update the delivery record and optionally a question mapping in a single
        atomic save.

        Writing both in one save eliminates the crash window between persisting
        an accepted part receipt and writing the question correlation entry.
        Call this inside the send loop for each accepted part so that partial
        deliveries also get their mapping written durably.
        """
        self._d.setdefault("deliveries", {})[message_id] = record
        if tg_question_id is not None and question_id is not None:
            self._d.setdefault("question_map", {})[str(tg_question_id)] = question_id
        self.save()

    # ---- question map ----

    @property
    def question_map(self) -> Dict[str, str]:
        return self._d.setdefault("question_map", {})

    def add_question_mapping(self, telegram_message_id: int, question_id: str) -> None:
        """Record that *telegram_message_id* carries *question_id*."""
        self.question_map[str(telegram_message_id)] = question_id
        self.save()

    def resolve_question(self, telegram_message_id: int) -> Optional[str]:
        """Return the local question_id mapped to *telegram_message_id*, or None."""
        return self.question_map.get(str(telegram_message_id))

    # ---- durable rejection log ----

    def record_rejection(self, reason: str, update_id: int) -> None:
        """
        Persist a durable record of a rejected pairing attempt.

        Called BEFORE advancing the cursor so the record outlives any crash
        between rejection and acknowledgement.
        """
        rejections = self._d.setdefault("pairing_rejections", [])
        rejections.append({"reason": reason, "update_id": update_id, "at": _now_utc()})
        self.save()

    def record_feed_rejection(self, update_id: int, error: str) -> None:
        """Persist that the Store refused update *update_id* (issue #5), before the cursor moves past it.

        Keeps the last FEED_REJECTIONS_KEPT records; never stores the message body."""
        rejections = self._d.setdefault("feed_rejections", [])
        rejections.append({"update_id": update_id, "at": _now_utc(), "error": str(error)[:200]})
        del rejections[:-FEED_REJECTIONS_KEPT]
        self._d["feed_rejections_total"] = int(self._d.get("feed_rejections_total") or 0) + 1
        self.save()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class TelegramAPIError(Exception):
    """Telegram API returned ok=false."""

    def __init__(self, description: str, error_code: int = 0,
                 retry_after: Optional[int] = None):
        super().__init__(description)
        self.error_code = error_code
        # Seconds to wait before retrying, from Telegram's parameters.retry_after
        self.retry_after = retry_after


class TelegramNetworkError(Exception):
    """Transient network or timeout failure."""


class TelegramClient:
    """
    Thin urllib wrapper for the Telegram Bot API.

    The bot token is kept in memory and never written to logs or error strings.
    Caller supplies *api_base* to redirect requests (e.g. to a fake endpoint in tests).
    429 responses carry retry_after in TelegramAPIError.retry_after.
    """

    def __init__(self, token: str, api_base: str = TELEGRAM_API_BASE):
        self._token = token
        self._api_base = api_base.rstrip("/")

    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method}"

    def _call(self, method: str, params: Optional[Dict[str, Any]] = None,
              timeout: int = 10) -> Any:
        """Call *method* and return the result field."""
        url = self._url(method)
        data: Optional[bytes] = None
        if params:
            data = urllib.parse.urlencode(
                {k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                 for k, v in params.items()}
            ).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                safe_url = redact_token(url, self._token)
                raise TelegramAPIError(f"HTTP {exc.code} for {safe_url}") from exc
            if not body.get("ok"):
                params_field = body.get("parameters") or {}
                retry_after = params_field.get("retry_after")
                raise TelegramAPIError(
                    redact_token(body.get("description", "unknown error"), self._token),
                    error_code=body.get("error_code", 0),
                    retry_after=retry_after,
                ) from exc
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            safe = redact_token(str(exc), self._token)
            raise TelegramNetworkError(
                f"Network error calling {method}: {safe}"
            ) from exc
        if not body.get("ok"):
            params_field = body.get("parameters") or {}
            retry_after = params_field.get("retry_after")
            raise TelegramAPIError(
                redact_token(body.get("description", "unknown error"), self._token),
                error_code=body.get("error_code", 0),
                retry_after=retry_after,
            )
        return body["result"]

    # ---- API methods ----

    def get_me(self) -> Dict[str, Any]:
        return self._call("getMe")

    def get_webhook_info(self) -> Dict[str, Any]:
        return self._call("getWebhookInfo")

    def get_updates(self, offset: Optional[int] = None,
                    timeout: int = POLL_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, timeout=timeout + 10)

    def send_message(self, chat_id: int, text: str,
                     reply_to_message_id: Optional[int] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        return self._call("sendMessage", params)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        return self._call("sendChatAction", {"chat_id": chat_id, "action": action},
                          timeout=CHAT_ACTION_TIMEOUT)


# ---------------------------------------------------------------------------
# Inbox: update processor
# ---------------------------------------------------------------------------

class UpdateProcessor:
    """
    Classifies a single Telegram update and feeds it into the employee Store.

    Returns a result dict:
        processed  bool   — True if a store event was created or pairing occurred
        reason     str    — short label (e.g. "text", "paired", "bot_sender", …)
        event_id   str|None

    "store_error" reason means a transient failure; callers must NOT advance the
    provider cursor for this result so the update can be retried next poll.
    When a batch contains multiple updates, processing must stop at the first
    store_error so that no later update's cursor can skip over the failed one.
    """

    def __init__(self, state: TelegramChannelState, colleague_id: str,
                 local_thread: Optional[str] = None, credential: Optional[bytes] = None):
        self.state = state
        self.colleague_id = colleague_id
        self._local_thread = local_thread
        self.credential = credential

    # ---- namespace helpers ----

    def _thread_id(self) -> str:
        """Return the stable local conversation thread ID for this channel."""
        return self._local_thread or f"telegram:{self.state.bound_chat_id}"

    def _source_ns(self) -> str:
        return f"telegram:{self.state.bot_id}"

    def _external_id(self, update_id: int) -> str:
        return f"tg:{self.state.bot_id}:{update_id}"

    # ---- public entry point ----

    def process(self, update: Dict[str, Any], store: Any,
                outbox: Optional[Any] = None) -> Dict[str, Any]:
        """
        Process one Telegram update.

        *outbox* is an OutboxDelivery instance; pass it so that the pairing
        handler can establish the outbox baseline BEFORE feeding the wakeup
        event (preventing the race where an employee greeting queued in
        response to the wakeup gets excluded by a later baseline set).
        """
        update_id = update["update_id"]
        message = update.get("message")
        if not message:
            return {"processed": False, "reason": "no_message_field", "event_id": None}
        return self._handle_message(update_id, message, store, outbox=outbox)

    # ---- message dispatch ----

    def _handle_message(self, update_id: int, message: Dict[str, Any],
                        store: Any, outbox: Optional[Any] = None) -> Dict[str, Any]:
        chat = message.get("chat", {})
        from_user = message.get("from", {})

        # Only private chats
        if chat.get("type") != "private":
            logger.warning("Rejected non-private chat type=%s", chat.get("type"))
            return {"processed": False, "reason": "non_private_chat", "event_id": None}

        # Reject bots
        if from_user.get("is_bot"):
            logger.warning("Rejected bot sender user_id=%s", from_user.get("id"))
            return {"processed": False, "reason": "bot_sender", "event_id": None}

        sender_id = from_user.get("id")
        text = message.get("text", "")

        # Pairing window: check /start NONCE before the bound-sender check
        if not self.state.paired and text.startswith("/start "):
            return self._handle_pairing(update_id, message, sender_id, text, store,
                                        outbox=outbox)

        # Reject unrecognized senders
        if not self.state.paired or sender_id != self.state.bound_user_id:
            logger.warning(
                "Rejected sender user_id=%s (bound=%s, paired=%s)",
                sender_id, self.state.bound_user_id, self.state.paired,
            )
            return {"processed": False, "reason": "unrecognized_sender", "event_id": None}

        return self._feed_message(update_id, message, store)

    # ---- pairing ----

    def _handle_pairing(self, update_id: int, message: Dict[str, Any],
                        sender_id: int, text: str, store: Any,
                        outbox: Optional[Any] = None) -> Dict[str, Any]:
        provided = text[len("/start "):].strip()
        if not self.state.pairing_nonce:
            # Persist durable rejection BEFORE acknowledging the cursor
            self.state.record_rejection("no_pending_nonce", update_id)
            logger.warning("Pairing attempt with no pending nonce; ignoring.")
            return {"processed": False, "reason": "no_pending_nonce", "event_id": None}
        if provided != self.state.pairing_nonce:
            # Persist durable rejection BEFORE acknowledging the cursor
            self.state.record_rejection("wrong_nonce", update_id)
            logger.warning("Pairing attempt: wrong nonce from user_id=%s", sender_id)
            return {"processed": False, "reason": "wrong_nonce", "event_id": None}

        chat_id = message.get("chat", {}).get("id")
        self.state.bound_user_id = sender_id
        self.state.bound_chat_id = chat_id
        self.state.paired = True
        self.state.pairing_nonce = None   # consume one-time nonce
        self.state.save()
        logger.info("Paired with user_id=%s chat_id=%s", sender_id, chat_id)

        # Establish outbox baseline BEFORE feeding the wakeup event.
        # This prevents any employee reply to the wakeup from being erroneously
        # excluded because its timestamp equals the later-set baseline.
        if outbox is not None:
            outbox.establish_baseline(store)

        external_id = self._external_id(update_id)
        occurred_at = _tg_date_to_iso(message.get("date"))
        try:
            event = _feed_event(
                store,
                credential=self.credential,
                sender=self.colleague_id,
                body="[Telegram pairing confirmed. Bot is now connected to this conversation.]",
                kind="message",
                thread=self._thread_id(),
                source=self._source_ns(),
                external_id=external_id,
                occurred_at=occurred_at,
            )
        except Exception as exc:
            logger.error("Failed to feed pairing event: %s", exc)
            # Binding is durable; wakeup feed failed. The cursor WILL advance
            # (the binding is recorded; the wakeup is best-effort).
            return {"processed": True, "reason": "paired_no_feed", "event_id": None}
        return {"processed": True, "reason": "paired", "event_id": event["id"]}

    # ---- normal messages ----

    def _feed_message(self, update_id: int, message: Dict[str, Any],
                      store: Any) -> Dict[str, Any]:
        occurred_at = _tg_date_to_iso(message.get("date"))
        body, kind = self._extract_content(message)

        # Extract provider reply metadata (generic in_reply_to)
        tg_in_reply_to: Optional[str] = None
        reply_to_question_id: Optional[str] = None
        tg_reply = message.get("reply_to_message")
        if tg_reply:
            tg_reply_msg_id = tg_reply.get("message_id")
            if tg_reply_msg_id is not None:
                tg_in_reply_to = str(tg_reply_msg_id)
                reply_to_question_id = self.state.resolve_question(tg_reply_msg_id)
        # Plain messages must not answer arbitrary open questions
        # (reply_to_question_id is only set when the user explicitly replied
        # to the specific Telegram message that carried a known question)

        external_id = self._external_id(update_id)
        feed_kwargs: Dict[str, Any] = dict(
            sender=self.colleague_id,
            body=body,
            kind="message",
            thread=self._thread_id(),
            source=self._source_ns(),
            external_id=external_id,
            occurred_at=occurred_at,
        )

        try:
            if reply_to_question_id is not None:
                try:
                    event = _feed_event(
                        store,
                        credential=self.credential,
                        reply_to=reply_to_question_id,
                        in_reply_to=tg_in_reply_to,
                        **feed_kwargs,
                    )
                except ValueError:
                    # Question closed or sender mismatch: feed as ordinary message
                    # with generic reply metadata only (no question resolution).
                    logger.info(
                        "Question %s not open for reply; feeding update %d as plain message",
                        reply_to_question_id, update_id,
                    )
                    event = _feed_event(
                        store,
                        credential=self.credential,
                        reply_to=None,
                        in_reply_to=tg_in_reply_to,
                        **feed_kwargs,
                    )
            else:
                event = _feed_event(
                    store,
                    credential=self.credential,
                    reply_to=None,
                    in_reply_to=tg_in_reply_to,
                    **feed_kwargs,
                )
        except ValueError as exc:
            # Deterministic refusal (issue #5). If the channel itself is wrong (key unregistered
            # or source prefix not allowed) every later update fails the same way: stop the
            # adapter without advancing, so nothing is dropped and the operator sees why.
            problem = channel_problem(store, self.credential, self._source_ns())
            if problem is not None:
                raise ChannelConfigError("Telegram channel misconfigured: %s" % problem) from exc
            # Otherwise only this update is refused: record it and let the cursor advance,
            # so later updates from the paired human are not blocked forever.
            self.state.record_feed_rejection(update_id, str(exc))
            logger.error("Store refused update %d (recorded, skipped): %s", update_id, exc)
            return {"processed": False, "reason": "rejected", "event_id": None}
        except Exception as exc:
            logger.error("Failed to feed update %d to store: %s", update_id, exc)
            # Return store_error — caller must NOT advance cursor (retryable, with backoff).
            return {"processed": False, "reason": "store_error", "event_id": None}

        return {"processed": True, "reason": kind, "event_id": event["id"]}

    def _extract_content(self, message: Dict[str, Any]) -> Tuple[str, str]:
        """
        Return (body, kind_label).

        Text messages pass through verbatim.
        Unsupported attachments produce an explicit note so they are not silently lost.
        """
        text = message.get("text")
        if text:
            return text, "text"

        for atype in _ATTACHMENT_TYPES:
            if message.get(atype):
                caption = message.get("caption", "")
                note = f"[Unsupported attachment: {atype}]"
                if caption:
                    note += f" Caption: {caption}"
                return note, f"unsupported_{atype}"

        return "[Unsupported message type]", "unsupported_unknown"


# ---------------------------------------------------------------------------
# Outbox: delivery
# ---------------------------------------------------------------------------

class OutboxDelivery:
    """
    Delivers employee outbox messages (addressed to colleague_id) to Telegram.

    Only messages routed through the private conversation thread are delivered.
    If local_thread is configured, messages must have that exact thread; otherwise
    messages must have thread == "telegram:{bound_chat_id}" or no explicit thread.

    Delivery states:
        pending   — not yet attempted
        in-flight — attempt started; crash before receipt write
        accepted  — Telegram returned a message_id for every part
        failed    — Telegram API permanently rejected (no retry)
        uncertain — network error at any point, or 429 retries exhausted (operator
                    must resolve; no retry)

    In-flight records found on startup are recovered to uncertain (no retry).
    A timeout on the first send is also uncertain even if no response IDs were
    returned, because Telegram may have accepted the request.

    Rate limiting: parts are paced with PART_SEND_INTERVAL between sends.
    429 responses are retried with retry_after backoff (capped at
    MAX_RATE_LIMIT_WAIT) up to RATE_LIMIT_RETRIES times. If retries are
    exhausted the delivery is marked uncertain (not failed). Already-accepted
    parts are preserved; on restart only un-accepted parts need to be resent
    (use rebuild_question_map to restore correlation from durable receipts).

    Question mapping: written atomically with each accepted part receipt inside
    the send loop. This eliminates the crash window between acceptance and
    correlation. rebuild_question_map() can fill gaps in older state files.
    """

    def __init__(self, state: TelegramChannelState, colleague_id: str,
                 client: Any, local_thread: Optional[str] = None,
                 credential: Optional[bytes] = None, instance: Optional[str] = None):
        self.state = state
        self.colleague_id = colleague_id
        self.client = client
        self._local_thread = local_thread
        self.credential = credential
        # M25 C4: one adapter instance id per process; a new instance turns the
        # previous instance's Store claims uncertain (never back to pending).
        self.instance = instance or "telegram-" + secrets.token_hex(6)

    def _expected_thread(self) -> Optional[str]:
        """
        Return the thread ID expected for messages routed to this channel.

        None means the channel is not yet bound (not paired).
        """
        if self._local_thread:
            return self._local_thread
        if self.state.bound_chat_id is not None:
            return f"telegram:{self.state.bound_chat_id}"
        return None

    def establish_baseline(self, store: Any) -> None:
        """
        Record the timestamp of the most-recent outbox message at first connection.

        Messages at or before this timestamp will not be delivered. Call once
        after pairing, before the first delivery pass. Never overwrites an
        already-established baseline.
        """
        if self.state.outbox_baseline_at is not None:
            return  # already established; never overwrite

        messages = [
            m for m in store.outbox_list()
            if m.get("to") == self.colleague_id
        ]
        if messages:
            latest_at = max(m.get("created_at", "") for m in messages)
            self.state.outbox_baseline_at = latest_at
        else:
            self.state.outbox_baseline_at = ""  # empty outbox; deliver all future msgs
        self.state.save()
        logger.info("Outbox baseline: %r", self.state.outbox_baseline_at)

    def _is_new(self, msg: Dict[str, Any]) -> bool:
        """True if the message was added after the baseline was established."""
        baseline = self.state.outbox_baseline_at
        if baseline is None:
            return False  # baseline not established yet
        if baseline == "":
            return True   # outbox was empty at baseline — all current msgs are new
        return (msg.get("created_at") or "") > baseline

    def recover_inflight(self) -> None:
        """
        Convert any in-flight delivery records to uncertain.

        Call once at startup before the first process_outbox pass. A crash after
        Telegram accepted but before the receipt was written would cause a blind
        retry if in-flight records are not recovered; upgrading to uncertain
        requires operator resolution instead.
        """
        for msg_id, rec in list(self.state.deliveries.items()):
            if rec.get("status") == "in-flight":
                rec = dict(rec)
                rec["status"] = "uncertain"
                rec["error"] = "Recovered: in-flight state at restart; Telegram delivery unconfirmed"
                self.state.set_delivery(msg_id, rec)
                logger.warning("Recovered in-flight delivery %s → uncertain", msg_id)

    def rebuild_question_map(self, store: Any) -> None:
        """
        Rebuild question_map from outbox records and durable delivery receipts.

        For each outbox message with a question_id, examine its delivery receipt's
        telegram_message_ids (accepted and partial/uncertain). If a mapping is
        missing from question_map, add it. This is a safety net for state files
        that predate per-part atomic question mapping (crash recovery path).

        Call once at startup before the first process_outbox pass.
        """
        updated = False
        for msg in store.outbox_list():
            question_id = msg.get("question_id")
            if not question_id:
                continue
            msg_id = msg["id"]
            rec = self.state.get_delivery(msg_id)
            if not rec:
                continue
            for tg_mid in rec.get("telegram_message_ids", []):
                if str(tg_mid) not in self.state.question_map:
                    self.state._d.setdefault("question_map", {})[str(tg_mid)] = question_id
                    updated = True
        if updated:
            self.state.save()
            logger.info("Rebuilt question_map from delivery receipts")

    def record_receipts(self, store: Any) -> int:
        """
        M22: write accepted Telegram deliveries back to the employee's outbox
        message as authenticated delivery records (idempotent; also backfills
        receipts accepted before M22). No-op without a channel credential.
        """
        if self.credential is None:
            return 0
        recorded = 0
        for msg in store.outbox_list():
            if msg.get("to") != self.colleague_id:
                continue
            delivery = self.state.get_delivery(msg["id"]) or {}
            if delivery.get("status") != "accepted":
                continue
            if any(d.get("status") == "delivered" for d in msg.get("deliveries") or []):
                continue
            ids = ",".join(str(i) for i in delivery.get("telegram_message_ids") or [])
            try:
                store.delivery_record(
                    msg["id"], self.credential, "delivered",
                    receipt=_receipt(self.state.bound_chat_id, ids, delivery),
                    detail={"accepted_at": delivery.get("accepted_at")},
                )
                recorded += 1
            except ValueError as exc:
                logger.error("Could not record delivery for %s: %s", msg["id"], exc)
        return recorded

    def process_outbox(self, store: Any) -> List[str]:
        """
        Deliver qualifying pending outbox messages.

        Only messages addressed to colleague_id AND routed through the private
        conversation thread are delivered. Messages with an explicit thread that
        does not match are silently skipped.

        Returns a list of message IDs that were attempted this pass.
        """
        if not self.state.paired or self.state.bound_chat_id is None:
            return []
        self.record_receipts(store)

        expected_thread = self._expected_thread()
        footer: List[Optional[str]] = []   # Byline footer, looked up once per pass and only if something is sent

        attempted: List[str] = []
        for msg in store.outbox_list():
            if msg.get("to") != self.colleague_id:
                continue
            if not self._is_new(msg):
                continue

            # Thread check: skip messages explicitly routed to a different thread
            msg_thread = msg.get("thread")
            if msg_thread is not None and msg_thread != expected_thread:
                continue

            msg_id = msg["id"]
            existing = self.state.get_delivery(msg_id)
            status = (existing or {}).get("status")
            if status in ("accepted", "failed", "uncertain", "in-flight"):
                continue   # terminal or already in-flight from this run — do not retry

            if self.credential is not None and not self._claim(store, msg_id):
                continue
            if not footer:
                footer.append(_footer(store))
            self._deliver(msg_id, msg, footer=footer[0])
            if self.credential is not None:
                self._ack(store, msg_id)
            attempted.append(msg_id)
        return attempted

    def _claim(self, store: Any, message_id: str) -> bool:
        """M25: claim in the Store before any network side effect (contract §1.5).

        A refused claim (not routed to this channel, already claimed by another
        instance, delivered elsewhere, uncertain) means: do not send.
        """
        from .store import ChannelError
        try:
            store.channel_claim(self.credential, message_id, instance=self.instance)
            return True
        except ChannelError as exc:
            logger.warning("Not sending %s: Store claim refused (%s): %s", message_id, exc.code, exc)
            return False

    def _ack(self, store: Any, message_id: str) -> None:
        """M25: mirror the local journal outcome into the Store delivery state."""
        from .store import ChannelError
        record = self.state.get_delivery(message_id) or {}
        status = record.get("status")
        ids = ",".join(str(i) for i in record.get("telegram_message_ids") or [])
        if status == "accepted":
            outcome, receipt = "delivered", _receipt(self.state.bound_chat_id, ids, record)
        elif status == "failed":
            # The local journal treats an API rejection as terminal, so the Store
            # outcome is permanent too; an operator retry is `channel reconcile`.
            outcome, receipt = "failed", None
        else:
            outcome, receipt = "uncertain", _receipt(self.state.bound_chat_id, ids, record) if ids else None
        detail = {k: record[k] for k in ("error", "parts_total", "accepted_at") if record.get(k) is not None}
        try:
            store.channel_ack(self.credential, message_id, outcome, receipt=receipt, retryable=False,
                              detail=detail, instance=self.instance)
        except ChannelError as exc:
            # record_receipts() backfills accepted deliveries on the next pass.
            logger.error("Could not record %s outcome for %s: %s", outcome, message_id, exc)

    def _deliver(self, message_id: str, msg: Dict[str, Any], footer: Optional[str] = None) -> None:
        """
        Attempt to deliver *message_id* to Telegram.

        Per-chat pacing: sleeps PART_SEND_INTERVAL seconds between parts.
        429 handling: honors retry_after (capped at MAX_RATE_LIMIT_WAIT) and
        retries the failing part up to RATE_LIMIT_RETRIES times; exhausted
        retries produce uncertain (not failed).
        Atomic question mapping: each accepted part's question_map entry is
        written in the same save() call as that part's delivery receipt, so
        no crash window exists between acceptance and correlation.
        Byline (#15): *footer* (None unless this channel is in the home's
        attribution.json scope) is appended to the last part only; the body is
        split with room for it so every part stays within MAX_MESSAGE_LENGTH.
        The stored outbox body is unchanged; the record and receipt carry
        footer:<sha8>.
        """
        chat_id = self.state.bound_chat_id
        body = msg.get("body", "")
        if footer:
            sep = FOOTER_SEPARATOR + footer
            parts = split_text(body, MAX_MESSAGE_LENGTH - len(sep))
            parts[-1] = parts[-1] + sep
        else:
            parts = split_text(body, MAX_MESSAGE_LENGTH)
        question_id: Optional[str] = msg.get("question_id") or None

        # Mark in-flight durably before the first network call
        record: Dict[str, Any] = {
            "status": "in-flight",
            "started_at": _now_utc(),
            "telegram_message_ids": [],
            "parts_total": len(parts),
            "error": None,
        }
        if footer:
            from . import byline
            record["footer"] = byline.footer_sha8(footer)
        self.state.set_delivery(message_id, record)

        sent_ids: List[int] = []
        for i, part in enumerate(parts):
            # Per-chat pacing: sleep between parts (not before the very first)
            if i > 0:
                time.sleep(PART_SEND_INTERVAL)

            # Attempt this part with retry on 429
            part_ok = False
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    result = self.client.send_message(chat_id, part)
                    tg_mid = result["message_id"]
                    sent_ids.append(tg_mid)

                    # Persist accepted part ID and question mapping atomically.
                    # Both are written in a single save() so a crash between
                    # acceptance and correlation cannot lose the mapping.
                    record["telegram_message_ids"] = list(sent_ids)
                    self.state.set_delivery_with_question(
                        message_id, record,
                        tg_question_id=tg_mid if question_id else None,
                        question_id=question_id,
                    )
                    part_ok = True
                    break

                except TelegramAPIError as exc:
                    if exc.error_code == 429 and attempt < RATE_LIMIT_RETRIES:
                        wait = min(
                            exc.retry_after if exc.retry_after is not None else 10,
                            MAX_RATE_LIMIT_WAIT,
                        )
                        logger.warning(
                            "Rate limited (429) on part %d/%d for %s; "
                            "waiting %.1fs (attempt %d/%d)",
                            i + 1, len(parts), message_id, wait,
                            attempt + 1, RATE_LIMIT_RETRIES,
                        )
                        time.sleep(wait)
                        continue
                    if exc.error_code == 429:
                        # Exhausted retries on 429 — uncertain (may succeed after limit clears)
                        record.update(
                            status="uncertain",
                            error=f"Rate limited (429) after {RATE_LIMIT_RETRIES} retries: {exc}",
                            telegram_message_ids=list(sent_ids),
                        )
                        self.state.set_delivery(message_id, record)
                        logger.warning(
                            "Delivery uncertain for %s (429 retries exhausted after %d/%d parts)",
                            message_id, len(sent_ids), len(parts),
                        )
                        return
                    # Permanent API error: Telegram definitively rejected this part
                    record.update(status="failed", error=str(exc),
                                  telegram_message_ids=list(sent_ids))
                    self.state.set_delivery(message_id, record)
                    logger.error("Delivery failed (API) for %s: %s", message_id, exc)
                    return

                except TelegramNetworkError as exc:
                    # Network error (including timeout on the FIRST send) is always
                    # uncertain — Telegram may have accepted the request even with
                    # 0 parts acknowledged (the response may have been lost in transit).
                    record.update(status="uncertain", error=str(exc),
                                  telegram_message_ids=list(sent_ids))
                    self.state.set_delivery(message_id, record)
                    logger.warning(
                        "Delivery uncertain for %s after %d/%d parts: %s",
                        message_id, len(sent_ids), len(parts), exc,
                    )
                    return

            if not part_ok:
                # Guard: should not be reachable if loop logic is correct
                record.update(
                    status="uncertain",
                    error="Internal: part loop exited without success or error",
                    telegram_message_ids=list(sent_ids),
                )
                self.state.set_delivery(message_id, record)
                return

        # All parts accepted by Telegram
        record.update(status="accepted", telegram_message_ids=list(sent_ids),
                      accepted_at=_now_utc())
        self.state.set_delivery(message_id, record)
        logger.info("Delivered %s → tg message_id(s) %s", message_id, sent_ids)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class TelegramAdapter:
    """
    Main entry point.  Acquires an exclusive process lock, verifies bot, employee,
    and config identity, then runs the polling loop until interrupted.

    The loop:
    1. Poll getUpdates (long-polling)
    2. For each update: feed to employee Store; advance cursor durably.
       On "store_error" (transient): stop the batch immediately so the failed
       update is retried next poll; a later update must NOT advance the cursor
       past the failed one.
    3. Deliver pending outbox messages to Telegram
    4. Sleep only on transient errors (bounded exponential back-off)
    """

    def __init__(self, config_path: str):
        self.config = TelegramConfig(config_path)
        self.state = TelegramChannelState(self.config.state_file)
        self._lock_fd: Optional[int] = None
        self._next_activity_at = 0.0

    # ---- process lock ----

    def _acquire_lock(self) -> None:
        lock_path = self.config.lock_file
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError(
                f"Another telegram adapter is running (lock: {lock_path})"
            ) from exc
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    # ---- startup checks ----

    def _make_client(self) -> TelegramClient:
        return TelegramClient(self.config.load_token())

    def _make_client_with_base(self, api_base: str) -> TelegramClient:
        """Construct a client targeting *api_base* (used in tests / subclasses)."""
        return TelegramClient(self.config.load_token(), api_base=api_base)

    def _verify_bot(self, client: TelegramClient) -> None:
        """Confirm bot identity; bind on first run; fail on mismatch."""
        me = client.get_me()
        bot_id = me["id"]
        if self.state.bot_id is None:
            self.state.bot_id = bot_id
            self.state.bot_username = me.get("username")
            self.state.save()
            logger.info("Bot bound: id=%d @%s", bot_id, self.state.bot_username)
        elif self.state.bot_id != bot_id:
            raise RuntimeError(
                f"Bot identity mismatch: state was bound to bot {self.state.bot_id}, "
                f"but the loaded token identifies bot {bot_id}. "
                "Check that you are using the correct token file for this state."
            )

    def _verify_employee(self, store: Any) -> None:
        """
        Bind the employee Store identity on first run; fail on mismatch.

        Moving a state file to another employee's home must fail here rather
        than silently routing that employee's messages under old bindings.
        """
        actual_id = store.identity()["id"]
        if self.state.employee_id is None:
            self.state.employee_id = actual_id
            self.state.save()
            logger.info("Employee identity bound: %s", actual_id)
        elif self.state.employee_id != actual_id:
            raise RuntimeError(
                f"Employee identity mismatch: state was bound to employee "
                f"{self.state.employee_id!r}, but this Store reports {actual_id!r}. "
                "This state file belongs to a different employee; do not transfer it."
            )

    def _verify_config(self, colleague_id: str, local_thread: Optional[str]) -> None:
        """
        Bind colleague_id and local_thread on first run; fail on mismatch.

        Changing colleague_id on an established channel would route Telegram
        messages to a different local principal — rejected. Changing local_thread
        once bound is rejected in either direction. Moving an established
        conversation needs an explicit migration, not a configuration edit.
        """
        if self.state.bound_colleague_id is None:
            # First run: bind both
            self.state.bound_colleague_id = colleague_id
            if local_thread is not None:
                self.state.bound_local_thread = local_thread
            self.state.save()
            return

        if self.state.bound_colleague_id != colleague_id:
            raise RuntimeError(
                f"Colleague ID mismatch: state was bound to "
                f"{self.state.bound_colleague_id!r}, but config specifies "
                f"{colleague_id!r}. This would route Telegram messages to a "
                "different local principal; do not transfer this state file to "
                "a different colleague configuration."
            )

        # local_thread must match exactly what was bound on this channel.
        # Reject in all directions:
        #   non-null → null: UpdateProcessor would use telegram:<chat>, routing
        #                    messages away from the bound history thread.
        #   null → non-null: would silently split an established telegram:<chat>
        #                    history onto a different thread.
        #   non-null → different non-null: changes routing entirely.
        # Explicit migration is future work.
        if local_thread != self.state.bound_local_thread:
            raise RuntimeError(
                f"Local thread mismatch: state was bound to "
                f"{self.state.bound_local_thread!r}, but config specifies "
                f"{local_thread!r}. Changing the conversation thread on an "
                "established channel is not allowed; explicit migration is "
                "future work."
            )

    def _check_webhook(self, client: TelegramClient) -> None:
        """Raise if a webhook URL is set — incompatible with long polling."""
        info = client.get_webhook_info()
        url = info.get("url", "")
        if url:
            # Do not include the URL in the error — it can contain credentials.
            raise RuntimeError(
                "Incompatible webhook is active on this bot. "
                "Remove it with deleteWebhook before using long-polling mode."
            )

    def _ensure_pairing_nonce(self) -> None:
        if not self.state.paired and not self.state.pairing_nonce:
            nonce = secrets.token_urlsafe(16)
            self.state.pairing_nonce = nonce
            self.state.save()

    def _print_pairing_instructions(self) -> None:
        bot_username = self.state.bot_username or "your_bot"
        nonce = self.state.pairing_nonce
        print(
            f"\n[PAIRING REQUIRED]\n"
            f"Send the following command to @{bot_username} in Telegram:\n\n"
            f"    /start {nonce}\n\n"
            f"Or open:  https://t.me/{bot_username}?start={nonce}\n",
            file=sys.stderr,
            flush=True,
        )

    # ---- main loop helpers ----

    def _service_activity(self, client: TelegramClient, store: Any) -> None:
        """Best-effort, ephemeral feedback for a live run handling this human.

        This runs only on the adapter's serial loop. It neither authors a reply
        nor changes delivery state; Telegram expires the indicator itself.
        """
        now = time.monotonic()
        if now < self._next_activity_at or not self.state.paired:
            return
        try:
            from .engine import worker_status

            if self.state.bound_chat_id is None or not worker_status(self.config.employee_home)["running"]:
                return
            # Store.run_list currently loads all historical runs. Activity is a
            # frequent read, so select only the newest record without scanning
            # the employee's lifetime history on each poll.
            latest = store.db.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
            run = json.loads(latest["data"]) if latest else None
            if not run or run["status"] != "running":
                return
            # A replacement worker may hold its lock before recovering a stale
            # run. Do not animate a run begun by an earlier worker lifetime.
            started = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00")).timestamp()
            if (self.config.employee_home / "worker.lock").stat().st_mtime > started:
                return
            thread = self.config.local_thread or f"telegram:{self.state.bound_chat_id}"
            unanswered = {
                event["id"] for event in run.get("events", [])
                if event.get("id") in run.get("event_ids", [])
                and event.get("sender") == self.config.colleague_id
                and event.get("source") == f"telegram:{self.state.bot_id}"
                and event.get("thread") == thread
            }
            if not unanswered:
                return
            for message in store.outbox_list():
                if (message.get("to") != self.config.colleague_id
                        or message.get("thread") not in (None, thread)):
                    continue
                reply_to = message.get("in_reply_to")
                if reply_to:
                    unanswered.discard(reply_to)
                # Legacy employee replies may omit explicit reply correlation.
                # Only a new message on this same bound route can suppress it.
                elif message.get("created_at", "") >= run["started_at"]:
                    return
            if not unanswered:
                return
            self._next_activity_at = now + CHAT_ACTION_INTERVAL
            client.send_chat_action(self.state.bound_chat_id, "typing")
        except TelegramAPIError as exc:
            if exc.error_code == 429:
                try:
                    retry_after = float(exc.retry_after)
                except (TypeError, ValueError, OverflowError):
                    retry_after = CHAT_ACTION_INTERVAL
                wait = max(CHAT_ACTION_INTERVAL,
                           min(retry_after, TRANSIENT_BACKOFF_MAX))
                self._next_activity_at = now + wait
            logger.warning("Activity feedback unavailable: %s", type(exc).__name__)
        except Exception as exc:
            # Cosmetic feedback cannot interrupt inbox or outbox delivery.
            self._next_activity_at = now + CHAT_ACTION_INTERVAL
            logger.warning("Activity feedback unavailable: %s", type(exc).__name__)

    def _sleep_servicing_outbox(
        self, total_seconds: float, outbox: "OutboxDelivery", store: Any,
        activity_client: Optional[TelegramClient] = None,
    ) -> None:
        """
        Sleep for *total_seconds* but service the outbox at OUTBOX_CHECK_INTERVAL
        boundaries so a pending outgoing message is not held up for the full duration.

        Called during inbound backoff (network errors, 429) to keep outbound
        delivery responsive while respecting the inbound retry delay. No shared
        state is mutated concurrently — this runs synchronously in the adapter loop.
        """
        deadline = time.monotonic() + total_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(float(OUTBOX_CHECK_INTERVAL), remaining))
            if self.state.paired:
                try:
                    outbox.process_outbox(store)
                except Exception as exc:
                    logger.warning("Outbox processing error during backoff: %s", exc)
                if activity_client is not None:
                    self._service_activity(activity_client, store)

    # ---- main loop ----

    def run(self) -> None:
        """Block, running the adapter loop. Ctrl-C (KeyboardInterrupt) exits cleanly."""
        self._acquire_lock()
        try:
            self._run_loop()
        except KeyboardInterrupt:
            logger.info("Interrupted; shutting down.")
        finally:
            self._release_lock()

    def _run_loop(self) -> None:
        from synthetics.store import Store  # imported here to avoid circular import

        client = self._make_client()
        self._verify_bot(client)
        self._check_webhook(client)

        store = Store(str(self.config.employee_home))
        self._verify_employee(store)
        self._verify_config(self.config.colleague_id, self.config.local_thread)

        self._ensure_pairing_nonce()

        credential = self.config.load_credential()
        problem = channel_problem(store, credential, "telegram:%s" % self.state.bot_id)
        if problem is not None:
            raise ChannelConfigError("Telegram channel misconfigured: %s" % problem)
        processor = UpdateProcessor(
            self.state,
            self.config.colleague_id,
            local_thread=self.config.local_thread,
            credential=credential,
        )
        outbox = OutboxDelivery(
            self.state,
            self.config.colleague_id,
            client,
            local_thread=self.config.local_thread,
            credential=credential,
        )

        if not self.state.paired:
            self._print_pairing_instructions()
        else:
            outbox.establish_baseline(store)

        # Recover stale in-flight records and rebuild any missing question mappings
        outbox.recover_inflight()
        outbox.rebuild_question_map(store)

        backoff = TRANSIENT_BACKOFF_INITIAL
        store_backoff = TRANSIENT_BACKOFF_INITIAL

        while True:
            offset = (
                self.state.update_cursor + 1
                if self.state.update_cursor is not None
                else None
            )
            try:
                # Use OUTBOX_CHECK_INTERVAL as the poll timeout so the loop
                # returns promptly to service pending outbox work even when
                # the inbound channel is quiet.
                updates = client.get_updates(offset=offset,
                                             timeout=OUTBOX_CHECK_INTERVAL)
                backoff = TRANSIENT_BACKOFF_INITIAL
            except TelegramNetworkError as exc:
                logger.warning("Poll failed (transient): %s — retry in %.1fs",
                               exc, backoff)
                self._sleep_servicing_outbox(backoff, outbox, store, activity_client=client)
                backoff = min(backoff * 2, TRANSIENT_BACKOFF_MAX)
                continue
            except TelegramAPIError as exc:
                if exc.error_code == 429:
                    # Rate limited on poll: backoff with retry_after if available,
                    # but service outbox in short intervals so delivery is not
                    # starved for the full backoff duration.
                    wait = min(
                        exc.retry_after if exc.retry_after is not None else backoff,
                        TRANSIENT_BACKOFF_MAX,
                    )
                    logger.warning("Poll rate limited (429), waiting %.1fs", wait)
                    self._sleep_servicing_outbox(wait, outbox, store, activity_client=client)
                    backoff = min(wait * 2, TRANSIENT_BACKOFF_MAX)
                    continue
                logger.error("Poll failed (non-retryable API error): %s", exc)
                raise

            store_failed = False
            for update in updates:
                update_id = update["update_id"]
                result = processor.process(update, store, outbox=outbox)
                logger.debug("Update %d: %s", update_id, result["reason"])

                # "store_error" is retryable: stop the batch here so the failed
                # update is retried on the next poll. A later update must NOT
                # advance the cursor past the failed one — that would create an
                # unrecoverable inbox gap.
                if result.get("reason") == "store_error":
                    store_failed = True
                    break

                self.state.update_cursor = update_id
                self.state.save()

            if self.state.paired:
                try:
                    outbox.process_outbox(store)
                except Exception as exc:
                    logger.warning("Outbox processing error: %s", exc)
                self._service_activity(client, store)

            # Issue #5: a store_error is retried, but not in a hot loop.
            if store_failed:
                logger.warning("Store error; retrying the update in %.1fs", store_backoff)
                self._sleep_servicing_outbox(store_backoff, outbox, store, activity_client=client)
                store_backoff = min(store_backoff * 2, TRANSIENT_BACKOFF_MAX)
            else:
                store_backoff = TRANSIENT_BACKOFF_INITIAL


# ---------------------------------------------------------------------------
# Config generation helper
# ---------------------------------------------------------------------------

def generate_config(
    token_file: str,
    colleague_id: str,
    employee_home: str,
    state_file: Optional[str] = None,
    lock_file: Optional[str] = None,
    local_thread: Optional[str] = None,
    credential_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a config dict suitable for json.dump to a config file."""
    cfg: Dict[str, Any] = {
        "token_file": str(token_file),
        "colleague_id": colleague_id,
        "employee_home": str(employee_home),
    }
    if state_file:
        cfg["state_file"] = str(state_file)
    if lock_file:
        cfg["lock_file"] = str(lock_file)
    if local_thread:
        cfg["local_thread"] = local_thread
    if credential_file:
        cfg["credential_file"] = str(credential_file)
    return cfg

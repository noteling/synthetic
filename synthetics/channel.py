"""M25 channel contract v1: the CLI + JSON-lines surface adapters use (docs/channel-contract.md).

Adapters never import Store or SQLite; they run `synthetics --home H channel …`. Every request
and response line carries "v": 1. Every command except `version` takes --key-file; the key hash
selects the channel record in home H. Errors use the closed set in store.ChannelError.CODES.
Exit status: 0 when every line was processed (per-line rejections included), 2 on usage,
version or auth failure, or when refused inside an employee session.
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import __version__
from .capture import humans
from .store import SESSION_ENV, ChannelError, Store, read_credential_file

VERSION = 1
SUPPORTED = (1,)
INBOUND_FIELDS = ("external_id", "occurred_at", "sender", "body", "source", "thread", "reply_to", "in_reply_to",
                  "question_id", "recipient_work_id", "project", "kind", "routed", "provider")
# v1 additive features (#100 P1). An adapter sends `provider` or calls `identity` only when `channel version` lists
# the feature, so it keeps working unchanged against an older Synthetics.
FEATURES = ("inbound.provider", "identity")
PROVIDER_MAX_BYTES = 256
PROVIDER_MAX_KEYS = 8
PROVIDER_TEXT_MAX = 64
_PROVIDER_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class UsageFailure(Exception):
    """A whole-command failure: printed as one error line, exit 2."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _err(code, message):
    return {"code": code, "message": str(message)}


def _emit(out, obj):
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def _lines(stream):
    """Yield (parsed_or_None, error_or_None) for each nonblank input line."""
    for raw in stream:
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except ValueError as exc:
            yield None, _err("bad_line", "not JSON: %s" % exc)
            continue
        if not isinstance(item, dict):
            yield None, _err("bad_line", "each line must be a JSON object")
            continue
        if item.get("v") not in SUPPORTED:
            yield item, _err("bad_version", "unsupported v %r; supported %s" % (item.get("v"), list(SUPPORTED)))
            continue
        yield item, None


def _opt_text(item, field):
    value = item.get(field)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ChannelError("bad_line", "%s must be nonempty text or null" % field)
    return value


def _provider(item):
    """#100 P1: optional provider references for the message (e.g. Telegram message_id, reply_to_message_id,
    chat_id). Transport-neutral: a small flat object of short keys whose values are integers, short text or null.
    Stored on the event as `provider`; never interpreted by the engine and never echoed by event-status."""
    value = item.get("provider")
    if value is None:
        return None
    if not isinstance(value, dict) or not value or len(value) > PROVIDER_MAX_KEYS:
        raise ChannelError("bad_line", "provider must be a nonempty object of at most %d keys" % PROVIDER_MAX_KEYS)
    for key, ref in value.items():
        if not _PROVIDER_KEY.match(key):
            raise ChannelError("bad_line", "provider key %r must match [a-z][a-z0-9_]{0,31}" % key)
        if ref is None or (isinstance(ref, int) and not isinstance(ref, bool)):
            continue
        if isinstance(ref, str) and ref.strip() and len(ref) <= PROVIDER_TEXT_MAX:
            continue
        raise ChannelError("bad_line", "provider.%s must be an integer, nonempty text of at most %d characters, "
                           "or null" % (key, PROVIDER_TEXT_MAX))
    if len(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")) > PROVIDER_MAX_BYTES:
        raise ChannelError("bad_line", "provider is over %d bytes" % PROVIDER_MAX_BYTES)
    return value


def _inbound_one(store, channel, credential, item):
    for field in ("external_id", "sender", "body", "source"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ChannelError("bad_line", "%s is required nonempty text" % field)
    for field in ("occurred_at", "thread", "reply_to", "in_reply_to", "question_id", "recipient_work_id", "project"):
        _opt_text(item, field)
    provider = _provider(item)
    kind = item.get("kind") or "message"
    if kind != "message":
        raise ChannelError("bad_line", "kind must be \"message\" in contract v1")
    if not any(item["source"].startswith(prefix) for prefix in channel["source_prefixes"]):
        raise ChannelError("source_not_allowed", "Channel %s may not write source %s."
                           % (channel["id"], item["source"]))
    question_id = item.get("question_id")
    if item.get("routed") is not None and item.get("routed") is not True:
        raise ChannelError("bad_line", "routed must be true or absent")
    if item.get("routed") and not question_id:
        raise ChannelError("bad_line", "routed:true needs question_id")
    if item.get("routed") and item["sender"] not in humans(store)["operators"]["senders"]:
        # R2: only a configured operator may relay a human's answer; any other keyed channel would
        # otherwise be able to stamp to_id on a human-addressed question.
        raise ChannelError("invalid_state", "routed:true is accepted only from a sender in humans.json "
                           "operators.senders; %s is not one." % item["sender"])
    # Generic reply metadata never answers a question; only question_id does (M06/M18).
    in_reply_to = item.get("in_reply_to") or item.get("reply_to")
    before = store.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
    route = None
    if question_id and item.get("routed"):
        # C1: an operator relays a human's answer. Record who routed it to whom (audited
        # question.routed) so the answer is accepted from this sender; never silently.
        question = store._find("questions", question_id)
        if question is None:
            raise ChannelError("unknown_message", "No question %s in this home." % question_id)
        if question["status"] == "open" and item["sender"] not in (question["to"], question.get("to_id")):
            # Issue #9: the stamp is made inside the feed's transaction, so a rejected feed leaves none.
            if question.get("to_id"):
                raise ChannelError("conflict", "Question %s is already routed to %s; refusing to re-stamp it as %s."
                                   % (question_id, question["to_id"], item["sender"]))
            route = "channel:%s" % channel["id"]
    try:
        event = store.feed(item["sender"], item["body"], thread=item.get("thread"), reply_to=question_id,
                           source=item["source"], external_id=item["external_id"],
                           occurred_at=item.get("occurred_at"), in_reply_to=in_reply_to, credential=credential,
                           recipient_work_id=item.get("recipient_work_id"), project=item.get("project"),
                           route=route, provider=provider)
    except ChannelError:
        raise
    except ValueError as exc:
        text = str(exc)
        if "Reply must come from" in text:
            raise ChannelError("invalid_state", text + " (an operator relaying a human's answer sets \"routed\": true)")
        if "different" in text or "idempotency" in text.lower() or "already routed" in text:
            raise ChannelError("conflict", text)
        raise ChannelError("bad_line", text)
    result = "duplicate" if event.get("seq", before + 1) <= before else "accepted"
    return dict(v=VERSION, external_id=item["external_id"], result=result, event_id=event["id"], error=None)


def _ack_one(store, credential, item, instance):
    message_id = item.get("message_id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ChannelError("bad_line", "message_id is required")
    retryable = item.get("retryable", False)
    if not isinstance(retryable, bool):
        raise ChannelError("bad_line", "retryable must be true or false")
    result, state = store.channel_ack(credential, message_id, item.get("outcome"), receipt=item.get("receipt"),
                                      retryable=retryable, detail=item.get("detail"), instance=instance)
    return dict(v=VERSION, message_id=message_id, result=result, state=state, error=None)


def run(args, home, stdin=None, stdout=None):
    """Execute one `channel` contract command. Returns the exit status."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    command = args.channel_command
    if command == "version":
        _emit(stdout, dict(v=VERSION, supported=list(SUPPORTED), synthetics=__version__, features=list(FEATURES)))
        return 0
    try:
        if os.environ.get(SESSION_ENV):
            raise UsageFailure("in_session", "channel %s is an adapter/operator operation and is refused inside "
                               "an employee session (%s is set)." % (command, SESSION_ENV))
        if not args.key_file:
            raise UsageFailure("auth_failed", "--key-file is required")
        try:
            credential = read_credential_file(args.key_file)
        except (OSError, ValueError) as exc:
            raise UsageFailure("auth_failed", str(exc))
        with Store(home) as store:
            try:
                channel = store._channel_by_key(credential)
            except ChannelError as exc:
                raise UsageFailure(exc.code, str(exc))
            instance = getattr(args, "instance", None)
            if command == "inbound":
                for item, error in _lines(stdin):
                    external_id = item.get("external_id") if isinstance(item, dict) else None
                    if error is None:
                        try:
                            _emit(stdout, _inbound_one(store, channel, credential, item))
                            continue
                        except ChannelError as exc:
                            error = _err(exc.code, exc)
                    _emit(stdout, dict(v=VERSION, external_id=external_id, result="rejected", event_id=None,
                                       error=error))
                return 0
            if command == "ack":
                for item, error in _lines(stdin):
                    message_id = item.get("message_id") if isinstance(item, dict) else None
                    if error is None:
                        try:
                            _emit(stdout, _ack_one(store, credential, item, instance))
                            continue
                        except ChannelError as exc:
                            error = _err(exc.code, exc)
                    _emit(stdout, dict(v=VERSION, message_id=message_id, result="rejected", state=None, error=error))
                return 0
            try:
                if command == "pending":
                    for line in store.channel_pending(credential, limit=args.limit, instance=instance):
                        _emit(stdout, line)
                elif command == "uncertain":
                    for line in store.channel_uncertain(credential, instance=instance):
                        _emit(stdout, line)
                elif command == "claim":
                    _emit(stdout, store.channel_claim(credential, args.message, instance=instance))
                elif command == "reconcile":
                    result, state = store.channel_reconcile(credential, args.message, args.outcome,
                                                            receipt=args.receipt, note=args.note, by=args.by)
                    _emit(stdout, dict(v=VERSION, message_id=args.message, result=result, state=state, error=None))
                elif command == "event-status":
                    _emit(stdout, store.channel_event_status(credential, args.event, source=args.source,
                                                             external_id=args.external_id))
                elif command == "event-reply":
                    _emit(stdout, store.channel_event_reply(credential, args.event, args.index, args.offset, args.cursor))
                elif command == "question":
                    _emit(stdout, store.channel_question(credential, args.id))
                elif command == "identity":
                    # #100 P1 (Codex F11): the employee this home belongs to, from the home's own identity row,
                    # so an adapter can pin a configured home to its employee_id. Keyed, read-only.
                    try:
                        me = store.identity()
                    except ValueError as exc:
                        raise ChannelError("invalid_state", str(exc))
                    _emit(stdout, dict(v=VERSION, employee_id=me["id"], name=me.get("name"), error=None))
                else:
                    raise UsageFailure("bad_line", "unknown channel command %s" % command)
            except ChannelError as exc:
                # Single-object commands report the rejection as their one output line.
                _emit(stdout, dict(v=VERSION, result="rejected", error=_err(exc.code, exc)))
                return 0 if exc.code not in ("auth_failed", "in_session") else 2
            return 0
    except UsageFailure as exc:
        _emit(stdout, dict(v=VERSION, result="rejected", error=_err(exc.code, exc)))
        return 2


def add_parsers(channel_sub):
    """Contract v1 subcommands under `synthetics channel`."""
    channel_sub.add_parser("version", help="contract versions supported (no key needed)")

    def keyed(name, help_text):
        parser = channel_sub.add_parser(name, help=help_text)
        parser.add_argument("--key-file", help="channel key file (0600) from `channel provision`")
        parser.add_argument("--instance", help="adapter instance id; a new instance turns the old one's claims "
                            "uncertain (C4)")
        return parser

    keyed("inbound", "stdin JSONL messages → stdout JSONL results (accepted|duplicate|rejected)")
    status = keyed("event-status", "accepted vs processed for an event this channel delivered (IDs and state "
                   "only), by --event or by this channel's own --source and --external-id")
    status.add_argument("--event")
    status.add_argument("--source", help="with --external-id: find an event this channel fed (#25 P2, audited)")
    status.add_argument("--external-id", dest="external_id")
    reply = keyed("event-reply", "bounded explicitly correlated replies to an event sender")
    reply.add_argument("--event", required=True)
    reply.add_argument("--index", type=int, default=0)
    reply.add_argument("--offset", type=int, default=0)
    reply.add_argument("--cursor")
    question = keyed("question", "full text of one open question addressed to this home's default_human "
                     "(read-only, audited; #25 answer desk)")
    question.add_argument("--id", required=True)
    keyed("identity", "employee_id and name of this home's employee (read-only; #100 adapter home pin)")
    pending = keyed("pending", "outbox messages routed to this channel and pending on it")
    pending.add_argument("--limit", type=int, default=50)
    claim = keyed("claim", "claim a pending message before any external side effect")
    claim.add_argument("--message", required=True)
    keyed("ack", "stdin JSONL outcomes for claimed messages")
    reconcile = keyed("reconcile", "the only exit from uncertain; failed returns the message to pending")
    reconcile.add_argument("--message", required=True)
    reconcile.add_argument("--outcome", required=True, choices=("delivered", "failed"))
    reconcile.add_argument("--receipt")
    reconcile.add_argument("--note")
    reconcile.add_argument("--by")
    keyed("uncertain", "this channel's uncertain messages")

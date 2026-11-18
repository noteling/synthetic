"""The first communication surface and operator interface for a synthetic."""

import argparse
import json
import math
import os
import re
import signal
import sys
from pathlib import Path

from . import __version__, capture
from .engine import Engine, WorkerStopped, failure_streak, status, utc_day, worker_status, write_limit_override
from .runtime import EXIT_RESTART, SelfRestart
from . import channel as channel_contract
from .store import DEFAULT_CHANNEL_DIR, SESSION_ENV, WORK_ID, Store, read_credential_file


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))




def _recipient_work(args):
    """M23: a malformed recipient work ID typed at the CLI is an error, not a silent hint."""
    value = getattr(args, "recipient_work_id", None)
    if value is None:
        return {}
    if not WORK_ID.match(value):
        raise ValueError("--recipient-work-id must be a work ID (work-<32 hex>).")
    return dict(recipient_work_id=value)


def _queue_stop_alert(home, reason, failure_count, notify):
    """M25 §6: the three-failure stop alert is an ordinary outbox message to limit_notify.

    Whatever channel routes that address delivers it; the core makes no network call (C6: if the
    outbox write fails, log to stderr and return, with no fallback). Idempotent per last run."""
    if not notify:
        print("synthetics: worker stopped (%s); no limit_notify configured, alert not queued" % reason,
              file=sys.stderr)
        return None
    try:
        with Store(home) as store:
            try:
                name = store.identity().get("name") or "worker"
            except Exception:
                name = "worker"
            runs = store.run_list(limit=1)
            last = runs[0]["id"] if runs else "none"
            body = ("%s stopped after %d failed sessions. Last error: %s\nInspect runs, resolve the error, "
                    "then restart the worker." % (name, failure_count, str(reason)[:500]))
            return store.message_send(notify, body, key="worker-stop-%s" % last)
    except Exception as error:
        print("synthetics: stop alert to %s not queued: %s" % (notify, type(error).__name__), file=sys.stderr)
        return None

def _human_or_fail(home, value, flag):
    """M25 S3: an explicit value, else this home's humans.json default_human. Never a built-in name."""
    if value:
        return value
    found = capture.default_human(home)
    if not found:
        raise ValueError("no %s given and %s has no default_human; pass %s or set default_human in humans.json"
                         % (flag, capture.humans_path(home), flag))
    return found


def _init_humans(home, human, channels):
    """init --human writes humans.json for a fresh home; without it an empty v1 file (doctor warns)."""
    path = capture.humans_path(home)
    if channels and not human:
        raise ValueError("--human-channel needs --human")
    if path.exists():
        if human:
            raise ValueError("%s already exists; edit it instead of passing --human" % path)
        return str(path)
    Path(home).mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = {"senders": [], "channel_prefixes": []}
    if human:
        cfg = {"default_human": human, "senders": [human], "channel_prefixes": list(channels)}
    return str(capture.write_humans(home, cfg))


def _m28_gate(args, home):
    """M28: commands a session must never run (owner-only or credential), checked on the caller, not --home."""
    from . import authority
    cmd = args.command
    if cmd == "init":
        authority.owner_only("init")
    elif cmd == "handoff-codex":
        authority.owner_only("history import")
    elif cmd == "question-routed":
        authority.credential("question-routed")
    elif cmd == "access" and args.access_command in ("grant", "revoke"):
        authority.owner_only("access " + args.access_command)
    elif cmd == "work" and getattr(args, "override", None) is not None:
        authority.owner_only("work --override")
    elif cmd == "runtime" and args.runtime_command == "request":
        a = authority.actor()
        if not a["owner"] and Path(a["home"]) == Path(home):
            raise authority.AuthorityError("own_home", "a session changes its own worker with runtime_restart "
                                           "(M19), not runtime request")


def _delegated_runtime_request(store, home, args, runtime):
    """M28 item 10: owner, or a session holding runtime.request covering the target home (never its own)."""
    from . import authority
    a = authority.actor()
    by = authority.check_by(a, args.by)
    settings = runtime.parse_assignments(args.set)
    if not a["owner"] and "daily_limit" in settings:
        # D6a: the spend ceiling is limit.raise (P1b, max_usd_per_day <= 150), never a runtime.request side door.
        authority.audit(a["home"], "authority.refused", {"what": "runtime request daily_limit", "actor": a["id"],
                                                         "run": a["run"], "code": "use_limit_raise"})
        raise authority.AuthorityError("use_limit_raise", "daily_limit is not changed under runtime.request; it "
                                       "needs limit.raise (M28 P1b) or the owner")
    res = authority.require("runtime.request", home=a.get("home") or home, target_home=home,
                            what="runtime request %s" % home,
                            check=lambda sc: authority.homes_cover(sc["homes"], home, a.get("home")))
    if a["owner"]:
        return store.runtime_request(settings, args.reason, by, key=args.key)
    w = res["warrant"]
    return authority.fenced(w, lambda: store.runtime_request(settings, args.reason, a["id"], key=args.key,
                                                             origin="delegated", delegation=res["delegation"],
                                                             warrant=w))


def _finite_usd(text):
    """D18-r2: `limit raise` USD, owner and session, today and --persistent: NaN and +/-inf are refused at parse
    time, before any authorization or state change."""
    from . import authority
    try:
        return authority.finite_usd(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error))


def _delegated_limit(store, home, args):
    """M28 P1b/P1c limit.raise / limit.resume. Owner: unchanged (any day, any value, 0 disables). Session:
    limit.raise for today or persistent, 0 < usd <= min(the delegation's max_usd_per_day ("policy" = the ceiling,
    D18; a child is also held to its parent's bound), the effective
    ``limit.ceiling`` policy, initially $150 team policy); 0 only when that policy sets allow_disable. Every
    target, the actor's own home included, must be covered by the delegation's ``homes`` (D10a; a self-raise
    is audited and listed by doctor). limit.resume needs a scope covering the target home. File writes are
    D4-fenced."""
    from . import authority
    a = authority.actor()
    own = not a["owner"] and authority.same_home(home, a["home"])
    if args.limit_command == "resume":
        by = authority.check_by(a, args.by)
        res = authority.require("limit.resume", home=a.get("home") or home, target_home=home,
                                what="limit resume %s" % home,
                                check=lambda sc: authority.homes_cover(sc["homes"], home, a.get("home")))
        latest = failure_streak(store.run_list(3))
        fields = dict(resume_after_run=latest, resumed_by=by)
        if res["delegation"]:
            fields.update(delegation=res["delegation"], resumed_run=a["run"])
        return authority.fenced_file(res["warrant"], lambda: write_limit_override(home, **fields))
    if not math.isfinite(args.usd):  # D18-r2: defence in depth beside the argparse type
        raise ValueError("The limit must be a finite number.")
    if args.usd < 0:
        raise ValueError("The limit cannot be negative.")
    duration = "persistent" if args.persistent else "today"
    if args.persistent and args.date:
        raise ValueError("--persistent and --date are exclusive.")
    day = args.date or utc_day()
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        raise ValueError("--date must be a UTC day YYYY-MM-DD.")
    if not a["owner"]:
        from . import roster
        cap = authority.ceiling(roster.company_state_for_home(a.get("home") or home))
        if day != utc_day():
            authority.owner_only("limit raise")
        if (args.usd == 0 and not cap["allow_disable"]) or (cap["usd"] is not None and args.usd > cap["usd"]):
            authority.exceeds_ceiling(a, args.usd, cap)  # D18-r2 (4): team policy, not owner-only
    by = authority.check_by(a, args.by)
    extra = {"usd": args.usd, "duration": duration, "self_raise": own}
    res = authority.require("limit.raise", home=a.get("home") or home, target_home=home,
                            what="limit raise %s %s %s" % (args.usd, duration, home), extra=extra,
                            check=lambda sc: authority.homes_cover(sc["homes"], home, a.get("home"))
                            and (sc["max_usd_per_day"] is None or args.usd <= sc["max_usd_per_day"]) and duration in sc["durations"])
    if args.persistent:
        reason = args.reason or "limit raise %s persistent" % args.usd
        if a["owner"]:
            return store.runtime_request({"daily_limit": args.usd}, reason, by, key=args.key)
        w = res["warrant"]
        return authority.fenced(w, lambda: store.runtime_request({"daily_limit": args.usd}, reason, a["id"],
                                                                 key=args.key, origin="delegated",
                                                                 delegation=res["delegation"], warrant=w))
    fields = dict(date=day, limit_usd=args.usd, set_by=by)
    if res["delegation"]:
        fields.update(delegation=res["delegation"], set_run=a["run"], self_raise=own)
    return authority.fenced_file(res["warrant"], lambda: write_limit_override(home, **fields))


def _delegated_worker(home, args):
    """M28 P1b worker.manage. Owner: unchanged. Session: a delegation whose homes cover the member target and
    whose ops name this `worker` subcommand (D9a); never its own home (runtime_restart, M19); install only the
    standard worker plist (D9b); stop/restart/remove only while the target is idle (D9c); outcome recorded (D4)."""
    from . import authority, install
    a = authority.actor()
    op = args.worker_command
    if a["owner"]:
        return install.handle(args, home)
    what = "worker %s %s" % (op, home)

    def refuse(code, message):
        authority.audit(a["home"], "authority.refused", {"action": "worker.manage", "what": what, "op": op,
                                                         "target": str(Path(home).resolve()),
                                                         "actor": a["id"], "run": a["run"], "code": code})
        raise authority.AuthorityError(code, message, action="worker.manage")

    if authority.same_home(home, a["home"]):
        refuse("own_home", "a session changes its own worker with runtime_restart (M19), not worker %s" % op)
    res = authority.require("worker.manage", home=a["home"], target_home=home, what=what, extra={"op": op},
                            check=lambda sc: authority.homes_cover(sc["homes"], home, a["home"])
                            and install.op_covered(op, sc.get("ops")))
    params = None
    if op == "install":
        params = install.delegated_install_params(Path(home).resolve(), args, refuse)
    elif not install._agents_dir_allowed(getattr(args, "launch_agents_dir", None)):
        refuse("agents_dir", "--launch-agents-dir must be the default or a temporary test directory")
    if op != "install" and args.label and args.label != install.worker_label(install._identity(home)):
        refuse("label_not_standard", "worker.manage controls only the target's standard worker label")
    # P1c (c): an install over a loaded label replaces a running worker, so it holds the idle boundary too.
    gated = op in install.IDLE_GATED or (op == "install" and install.loaded(params["label"]) is not False)
    if not gated:
        return install.delegated_execute(home, args, res["warrant"], params)
    # D10c: hold the target's idle boundary through the launchd op; the worker starts no run while held.
    with Store(Path(home).resolve()) as target:
        hold = authority.fenced_file(res["warrant"], lambda: target.worker_hold_acquire(
            {"op": op, "actor": a["id"], "run": a["run"], "delegation": res["delegation"]}))
    if hold is None:
        if op == "restart":
            return _deferred_restart(home, a, res, what)
        # D11: refused, never deferred or retried; the hold was not taken and nothing else was written.
        if op == "install":
            refuse("not_idle", "target busy; ask again when idle (an install over a loaded worker is not deferred)")
        refuse("not_idle", "target busy; ask again when idle (%s is not deferred: a worker cannot record the "
                           "outcome of stopping itself)" % op)
    try:
        return install.delegated_execute(home, args, res["warrant"], params)
    finally:
        with Store(Path(home).resolve()) as target:
            target.worker_hold_release(hold["token"])


def _deferred_restart(home, a, res, what):
    """D10c: a busy target gets a deferred restart: an M19 runtime request with no settings, which the target's
    own runtime controller carries out at its next idle point and records (applied/failed). Needs a target
    worker started with --runtime-config; otherwise refused as not_idle with that reason."""
    from . import authority
    w = res["warrant"]
    try:
        with Store(Path(home).resolve()) as target:
            record = authority.fenced(w, lambda: target.runtime_request(
                {}, "worker.manage restart deferred (target busy)", a["id"], origin="delegated",
                delegation=res["delegation"], warrant=w))
    except authority.AuthorityError:
        raise
    except ValueError as error:
        authority.audit(a["home"], "authority.refused", {"action": "worker.manage", "what": what, "op": "restart",
                                                         "actor": a["id"], "run": a["run"], "code": "not_idle"})
        raise authority.AuthorityError("not_idle", "the target is busy and a deferred restart could not be "
                                                   "recorded: %s" % error, action="worker.manage")
    authority.audit(Path(home).resolve(), "authority.worker",
                    dict(authority.receipt(w), op="restart", stage="deferred", request=record.get("id")))
    return {"outcome": "deferred", "request": record.get("id"), "status": record.get("status"),
            "delegation": res["delegation"]}


def _delegated_adapter(home, args):
    """M28 P1b adapter.manage. Owner: unchanged. Session: a delegation whose labels name this
    com.synthetics.adapter.* label; only start, restart, or install that re-writes the installed plist with
    unchanged ProgramArguments/KeepAlive (D2). New executables, changed arguments, stop and remove: owner-only."""
    from . import authority, install
    a = authority.actor()
    op = args.adapter_command
    if a["owner"]:
        return install.handle(args, home)
    what = "adapter %s %s" % (op, args.label)

    def refuse(code, message):
        authority.audit(a["home"], "authority.refused", {"action": "adapter.manage", "what": what, "op": op,
                                                         "actor": a["id"], "run": a["run"], "code": code})
        raise authority.AuthorityError(code, message, action="adapter.manage")

    install._check_prefix(args.label, install.ADAPTER_PREFIX)
    if op == "install" and getattr(args, "review", None):
        # P1c-b adapter.install: a new or changed adapter whose code matches an accepted review's frozen sums
        from . import roster
        state = roster.company_state_for_home(a["home"])

        def refuse_install(code, message):
            authority.audit(a["home"], "authority.refused", {"action": "adapter.install", "what": what, "op": op,
                                                             "actor": a["id"], "run": a["run"], "code": code})
            raise authority.AuthorityError(code, message, action="adapter.install")
        plan = install.adapter_install_plan(home, args, state, refuse_install)
        res = authority.require("adapter.install", home=a["home"], what=what,
                                extra={"op": op, "label": args.label, "review": plan["review"]["sha256"]},
                                check=lambda sc: install.adapter_plan_within(plan, sc))
        return install.delegated_adapter_install(home, args, plan, res["warrant"], refuse_install)
    if op not in install.ADAPTER_DELEGATED_OPS:
        refuse("owner_only", "adapter %s is owner-only (it cuts a channel); delegated: %s"
               % (op, ", ".join(install.ADAPTER_DELEGATED_OPS)))
    res = authority.require("adapter.manage", home=a["home"], what=what, extra={"op": op, "label": args.label},
                            check=lambda sc: install.label_covered(args.label, sc.get("labels")))
    return install.delegated_adapter(home, args, res["warrant"], refuse)


CONTRACT_COMMANDS = ("version", "inbound", "event-status", "pending", "claim", "ack", "reconcile", "uncertain")


def _refuse_in_session(what):
    """Credential and channel operations are for operators and adapters, never a model session."""
    if os.environ.get(SESSION_ENV):
        raise ValueError("%s is an operator operation and is refused inside an employee session (%s is set)."
                         % (what, SESSION_ENV))


def channel_provision(name, source_prefixes, homes, key_dir=DEFAULT_CHANNEL_DIR, by="operator", routes=None):
    """Create NAME.key (32 random bytes, hex, 0600 in a 0700 dir) unless it exists, then register
    its hash in every home. Idempotent. Returns hashes and paths only, never key material."""
    import secrets
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name or ""):
        raise ValueError("Channel name must be letters, digits, dot, dash or underscore.")
    directory = Path(key_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key_path = directory / (name + ".key")
    created = False
    if not key_path.exists():
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(32) + "\n")
        created = True
    digest = Store.credential_hash(read_credential_file(key_path))
    registered = []
    for home in homes:
        with Store(Path(home).expanduser()) as store:
            store.identity()
            record = store.channel_register(name, digest, source_prefixes, by, routes=routes)
            registered.append(dict(home=str(store.home), source_prefixes=record["source_prefixes"],
                                   routes=record.get("routes") or []))
    return dict(channel=name, key_file=str(key_path), key_created=created, key_sha256=digest, homes=registered)


def project_command(store, args):
    """M21 CLI. bind/unbind/bindings/capture-drain/export may run in a session (E2/B2, audited with --by).
    M28: grant and redact need the owner or a delegation (project.grant / project.redact); import is owner-only."""
    from . import authority, capture
    from .projects import ProjectStore
    cmd = args.project_command
    if getattr(args, "project", None) in capture.RESERVED_PROJECTS:
        raise ValueError("%s is a reserved name, not a project." % args.project)
    if cmd == "bind":
        return capture.bind(store, args.project, thread=args.thread, source_prefixes=args.source_prefixes,
                            by=args.by, reason=args.reason)
    if cmd == "unbind":
        return capture.unbind(store, args.binding, by=args.by, reason=args.reason)
    if cmd == "bindings":
        return capture.bindings(store, include_ended=args.all)
    if cmd == "capture-drain":
        return capture.capture_drain(store, limit=args.limit)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project or ""):
        raise ValueError("Project names are lower-case letters, digits, dash or underscore.")
    path = capture.project_path(store, args.project)
    by = getattr(args, "by", None)
    warrant = None
    if cmd == "import":
        authority.owner_only("project import")
    elif cmd == "grant":
        by, warrant = _delegated_project_grant(store, args)
    elif cmd == "redact":
        a = authority.actor()
        by = authority.check_by(a, by)
        warrant = authority.require("project.redact", home=store.home,
                                    what="project redact %s %s" % (args.project, args.entry),
                                    check=lambda sc: args.project in sc["projects"] or "*" in sc["projects"])["warrant"]
    if cmd == "import":
        ProjectStore.import_jsonl(args.jsonl, path, args.project).close()
        with ProjectStore(path, args.project) as ps:
            return dict(project=args.project, head_seq=ps.head_seq())
    with ProjectStore(path, args.project) as ps:
        if cmd == "grant":
            return authority.fenced(warrant, lambda: ps.grant(args.employee, args.mode, by=by, warrant=warrant))
        if cmd == "grants":
            return ps.grants()
        if cmd == "redact":
            return authority.fenced(warrant, lambda: ps.redact(args.entry, reason=args.reason, by=by,
                                                               warrant=warrant))
        if cmd == "export":
            return dict(project=args.project, export_through=ps.export(rebuild=args.rebuild))
    raise ValueError("unknown project command %s" % cmd)


def _delegated_project_grant(store, args):
    """M28 item 1: owner, or a session holding project.grant for this project and mode; never to itself."""
    from . import authority, roster
    a = authority.actor()
    company = roster.company_state_for_home(store.home)
    by = authority.check_by(a, args.by, company)
    if not a["owner"]:
        grantee = roster.resolve(company, args.employee)["id"] or args.employee
        if grantee == a["id"]:
            authority.audit(a["home"], "authority.refused", {"what": "project grant", "actor": a["id"],
                                                             "run": a["run"], "code": "self_promotion"})
            raise authority.AuthorityError("self_promotion", "a session cannot grant itself project access")
    res = authority.require("project.grant", home=store.home,
                            what="project grant %s %s %s" % (args.project, args.employee, args.mode),
                            check=lambda sc: (args.project in sc["projects"] or "*" in sc["projects"])
                            and authority.MODES.index(args.mode) <= authority.MODES.index(sc["max_mode"]))
    return by, res["warrant"]


def parser():
    p = argparse.ArgumentParser(description="A continuing digital employee: identity, memory, and work.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--home", default=os.environ.get("SYNTHETICS_HOME", str(Path.home() / ".synthetics" / "default")),
                   help="employee home (default: SYNTHETICS_HOME or ~/.synthetics/default)")
    sub = p.add_subparsers(dest="command", required=True)
    from .telegram_cli import add_commands
    from .install import add_commands as add_install_commands
    add_commands(sub)
    add_install_commands(sub)
    from .doctor import add_command as add_doctor_command
    add_doctor_command(sub)
    from .postern import add_parser as add_postern_parser
    add_postern_parser(sub)
    init = sub.add_parser("init", help="provision one employee and private workspace")
    init.add_argument("--name", required=True)
    init.add_argument("--role", required=True)
    init.add_argument("--purpose", default="")
    init.add_argument("--by", help="initial colleague's stable ID (default: --human, else humans.json default_human)")
    init.add_argument("--colleague-name", help="initial colleague's display name (default: their ID)")
    init.add_argument("--human", help="write humans.json naming this human (sender and default_human) for a fresh home")
    init.add_argument("--human-channel", action="append", default=[], dest="human_channels", metavar="PREFIX",
                      help="source prefix of a channel that carries this human's words (repeatable; needs --human)")

    feed = sub.add_parser("feed", help="deliver a message or other event")
    feed.add_argument("body")
    feed.add_argument("--from", dest="sender", required=True)
    feed.add_argument("--kind", default="message")
    feed.add_argument("--thread")
    feed.add_argument("--reply-to", help="question ID to answer")
    feed.add_argument("--in-reply-to", help="message ID being discussed; does not answer a question")
    feed.add_argument("--source", default="manual")
    feed.add_argument("--external-id", help="source ID, deduplicated on redelivery")
    feed.add_argument("--occurred-at", help="original event time, including UTC offset")
    feed.add_argument("--credential-file", help="channel key file (operator/adapter only); stamps event auth")
    feed.add_argument("--recipient-work-id", help="the receiving employee's work this message concerns (work-<32 hex>)")
    feed.add_argument("--project", help="tag the message for one project; captured when the channel is unbound "
                      "(a binding decides on bound channels)")
    feed.add_argument("--project-by", choices=["feeder", "sender"], default="feeder",
                      help="who set the tag: the feeder (operator/adapter, default) or the peer sender, whose "
                      "write grant on the project is then required")
    ingest = sub.add_parser("ingest", help="read event objects as JSON lines from stdin")
    ingest.add_argument("--credential-file", help="channel key file (operator/adapter only); stamps event auth")
    channel = sub.add_parser("channel", help="authenticated channels: operator provisioning (M22) and the adapter "
                             "contract v1 (M25, docs/channel-contract.md)")
    channel_sub = channel.add_subparsers(dest="channel_command", required=True)
    provision = channel_sub.add_parser("provision", help="generate a channel key file (0600) and register its hash "
                                       "in each --home; never prints the key")
    provision.add_argument("name")
    provision.add_argument("--source-prefix", action="append", required=True, dest="source_prefixes")
    provision.add_argument("--home", action="append", required=True, dest="homes")
    provision.add_argument("--key-dir", default=DEFAULT_CHANNEL_DIR)
    provision.add_argument("--by", default="operator")
    provision.add_argument("--route", action="append", dest="routes", metavar="ADDR",
                           help="recipient address this channel delivers (repeatable); omitted keeps existing routes")
    channel_register = channel_sub.add_parser("register", help="register an existing key file's hash in this home")
    channel_register.add_argument("name")
    channel_register.add_argument("--source-prefix", action="append", required=True, dest="source_prefixes")
    channel_register.add_argument("--key-file", required=True)
    channel_register.add_argument("--by", default="operator")
    channel_register.add_argument("--route", action="append", dest="routes", metavar="ADDR",
                                  help="recipient address this channel delivers (repeatable); omitted keeps routes")
    channel_sub.add_parser("list", help="registered channels (hashes and prefixes only)")
    channel_contract.add_parsers(channel_sub)
    sub.add_parser("status", help="current work, pending questions, and platform limitations")
    sub.add_parser("stop", help="stop the active local worker without discarding work")
    sub.add_parser("outbox", help="read outgoing messages and questions (local delivery)")
    docket_parser = sub.add_parser("docket", help="read-only: this home's open questions to a person, oldest first")
    docket_parser.add_argument("--person", help="person ID, name or alias (default: humans.json default_human)")
    docket_parser.add_argument("--format", choices=["text", "json"], default="text")
    sub.add_parser("system-prompt", help="read-only: print the system prompt a new session of this home gets "
                   "(shipped prompt + host rules)")
    sub.add_parser("inbox", help="read events awaiting attention")
    send = sub.add_parser("send", help="queue an outgoing message for a configured delivery adapter")
    send.add_argument("to")
    send.add_argument("body")
    send.add_argument("--key", required=True, help="stable key for retry reconciliation")
    send.add_argument("--thread")
    send.add_argument("--in-reply-to")
    send.add_argument("--work-id", help="your own work this message belongs to")
    send.add_argument("--also-work", action="append", dest="work_ids", metavar="WORK",
                      help="with --question: another of your works this question also blocks (repeatable)")
    send.add_argument("--recipient-work-id", help="the recipient's work this message concerns (work-<32 hex>)")
    send.add_argument("--question", action="store_true", help="record a question awaiting an answer")
    send.add_argument("--project", help="tag for one project you hold a write grant on; your body is captured there")
    routed = sub.add_parser("question-routed",
                            help="adapter/operator: record the identity a question was routed to (not available to models)")
    routed.add_argument("id", help="question ID in this employee's store")
    routed.add_argument("--to-id", required=True, help="authenticated recipient identity")
    routed.add_argument("--by", required=True, help="adapter or operator recording the route")
    thread = sub.add_parser("thread", help="read both sides of a durable conversation")
    thread.add_argument("id")
    thread.add_argument("--limit", type=int, default=50)
    thread.add_argument("--before", type=int, help="cursor returned by an earlier page")
    handoff = sub.add_parser("handoff-codex", help="import an explicit saved chat as historical context, without replaying messages")
    handoff.add_argument("id", help="exact Codex conversation ID")
    handoff.add_argument("--snapshot", required=True, help="private JSON snapshot path (parent must exist)")
    handoff.add_argument("--thread", help="local conversation ID (default: codex:<id>)")
    handoff.add_argument("--human", help="local human colleague ID (default: humans.json default_human)")
    handoff.add_argument("--codex", default="codex", help="Codex executable")
    history = sub.add_parser("history", help="search received events, including already handled messages")
    history.add_argument("query", nargs="?", default="")
    history.add_argument("--thread")
    history.add_argument("--limit", type=int, default=50)
    event = sub.add_parser("event", help="read the original source of a remembered event")
    event.add_argument("id")
    usage = sub.add_parser("usage", help="per-UTC-day session cost (unknown costs counted separately)")
    usage.add_argument("--days", type=int, default=7)
    limit = sub.add_parser("limit", help="operator override for the worker's daily soft limit")
    limit_sub = limit.add_subparsers(dest="limit_command", required=True)
    limit_raise = limit_sub.add_parser("raise", help="set today's (or --date's) UTC soft limit; 0 disables it for that day")
    limit_raise.add_argument("usd", type=_finite_usd)
    limit_raise.add_argument("--date", help="UTC day YYYY-MM-DD (default today)")
    limit_raise.add_argument("--by", help="display label (owner default operator; a session must name itself)")
    limit_raise.add_argument("--persistent", action="store_true",
                             help="change the worker's persistent daily_limit (a runtime request applied at idle) "
                                  "instead of today's override")
    limit_raise.add_argument("--reason", help="reason recorded with a --persistent request")
    limit_raise.add_argument("--key", help="idempotency key for a --persistent request")
    limit_resume = limit_sub.add_parser("resume", help="clear a recent-failures pause after inspecting the runs")
    limit_resume.add_argument("--by", help="display label (owner default operator; a session must name itself)")
    sub.add_parser("backfill-cost", help="recover cost of failed runs from retained Claude output (idempotent)")
    runs = sub.add_parser("runs", help="read recent work-session outcomes")
    runs.add_argument("--limit", type=int, default=10)
    audit = sub.add_parser("audit", help="inspect recorded changes")
    audit.add_argument("--limit", type=int, default=30)

    entity = sub.add_parser("entity", help="introduce or update a colleague, project, or concept")
    entity.add_argument("id")
    entity.add_argument("--name", required=True)
    entity.add_argument("--kind", choices=["person", "project", "concept", "organization", "system"], required=True)
    entity.add_argument("--description", default="")
    access = sub.add_parser("access", help="provision or inspect intended filesystem access")
    access_sub = access.add_subparsers(dest="access_command", required=True)
    access_sub.add_parser("list")
    grant = access_sub.add_parser("grant")
    grant.add_argument("path")
    grant.add_argument("--mode", choices=["read", "write"], required=True)
    grant.add_argument("--purpose", required=True)
    grant.add_argument("--by", help="default: humans.json default_human")
    revoke = access_sub.add_parser("revoke")
    revoke.add_argument("id")
    revoke.add_argument("--by", help="default: humans.json default_human")
    entities = sub.add_parser("entities", help="find known people and other entities")
    entities.add_argument("query", nargs="?", default="")
    remember = sub.add_parser("remember", help="record a fact, belief, preference, or procedure")
    remember.add_argument("subject")
    remember.add_argument("predicate")
    remember.add_argument("value")
    remember.add_argument("--kind", choices=["fact", "belief", "preference", "procedure"], default="fact")
    remember.add_argument("--source", required=True)
    remember.add_argument("--replaces")
    recall = sub.add_parser("recall", help="search memory across projects")
    recall.add_argument("query", nargs="?", default="")
    recall.add_argument("--subject")
    recall.add_argument("--history", action="store_true")
    recall.add_argument("--match", choices=("any", "all"), default="any")

    work = sub.add_parser("work", help="assign or inspect ongoing work")
    ws = work.add_subparsers(dest="work_command", required=True)
    add = ws.add_parser("add")
    add.add_argument("title")
    add.add_argument("--description", default="")
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--key")
    ws.add_parser("list")
    get = ws.add_parser("get")
    get.add_argument("id")
    update = ws.add_parser("update")
    update.add_argument("id")
    update.add_argument("--status", choices=["ready", "waiting", "done", "cancelled"])
    update.add_argument("--note")
    update.add_argument("--waiting-for")
    update.add_argument("--wake-at")
    update.add_argument("--priority", type=int)
    update.add_argument("--evidence", action="append", nargs=2, metavar=("LEVEL", "REF"),
                        help="completion evidence (repeatable): level attempted|observed|verified and an event, run, "
                             "question, message, memory or work ID, a file path, or a receipt:/https: reference")
    update.add_argument("--cancel-reason", help="required with --status cancelled")
    update.add_argument("--override", metavar="REASON",
                        help="operator only: close without citable evidence; recorded as level operator-override")
    rt = sub.add_parser("runtime", help="inspect or request changes to persisted worker settings (M19)")
    rts = rt.add_subparsers(dest="runtime_command", required=True)
    rts.add_parser("show")
    rreq = rts.add_parser("request", help="operator or manager only: queue a restart of this home's worker")
    rreq.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                      help="claude, model, budget, timeout, poll, retry, daily_limit, limit_notify (none clears), "
                           "limit_allow (comma-separated)")
    rreq.add_argument("--reason", required=True)
    rreq.add_argument("--by", required=True, help="who is asking; recorded and notified of the outcome")
    rreq.add_argument("--key")
    wake = sub.add_parser("wake", help="request attention without creating an assignment")
    wake.add_argument("reason")
    remind = sub.add_parser("remind", help="schedule a future event (time needs UTC offset)")
    remind.add_argument("at")
    remind.add_argument("reason")
    remind.add_argument("--work-id")
    remind.add_argument("--key", required=True)

    from .rostercli import add_parser as add_roster_parser
    add_roster_parser(sub)
    from .authority import add_parser as add_authority_parser
    add_authority_parser(sub)
    proj = sub.add_parser("project", help="project memory (M21): bindings, capture, grants, redaction, export")
    ps = proj.add_subparsers(dest="project_command", required=True)
    pbind = ps.add_parser("bind", help="capture a thread/source into project P from now on (allowed in a session; "
                                       "audited; no back-dating)")
    pbind.add_argument("project")
    pbind.add_argument("--thread")
    pbind.add_argument("--source-prefix", dest="source_prefixes", action="append", default=[],
                       help="repeatable; an event matches if its source starts with any prefix")
    pbind.add_argument("--by", required=True)
    pbind.add_argument("--reason", required=True)
    punbind = ps.add_parser("unbind", help="end a binding (prospective; audited)")
    punbind.add_argument("binding")
    punbind.add_argument("--by", required=True)
    punbind.add_argument("--reason", required=True)
    pbindings = ps.add_parser("bindings", help="list this home's bindings")
    pbindings.add_argument("--all", action="store_true", help="include ended bindings")
    pdrain = ps.add_parser("capture-drain", help="retry pending captures from this home's outbox")
    pdrain.add_argument("--limit", type=int, default=50)
    from .projects import GRANT_MODES
    pgrant = ps.add_parser("grant", help="owner, or a session holding project.grant (docs/authority.md): grant an employee " + ", ".join(GRANT_MODES)
                           + " on project P")
    pgrant.add_argument("project")
    pgrant.add_argument("employee")
    pgrant.add_argument("--mode", choices=list(GRANT_MODES), required=True)
    pgrant.add_argument("--by", required=True)
    pgrants = ps.add_parser("grants", help="list a project's grants")
    pgrants.add_argument("project")
    predact = ps.add_parser("redact", help="owner, or a session holding project.redact: remove a source entry's bytes or a Board post's text "
                             "(hash tombstone kept; copies already fed into homes are not removed)")
    predact.add_argument("project")
    predact.add_argument("entry")
    predact.add_argument("--reason", required=True)
    predact.add_argument("--by", required=True)
    pexport = ps.add_parser("export", help="bring log.jsonl up to date (--rebuild rewrites it)")
    pexport.add_argument("project")
    pexport.add_argument("--rebuild", action="store_true")
    pimport = ps.add_parser("import", help="operator only: load a log.jsonl into an empty project store")
    pimport.add_argument("project")
    pimport.add_argument("jsonl")

    for name, help_text in [("run", "work for a bounded number of sessions"),
                            ("serve", "stay available; act on events, work, and follow-ups")]:
        runner = sub.add_parser(name, help=help_text)
        runner.add_argument("--claude", default="claude", help="Claude executable")
        runner.add_argument("--model", default="claude-opus-5-5")
        runner.add_argument("--timeout", type=float, default=300)
        runner.add_argument("--budget", type=float, default=100.0,
                            help="API-equivalent USD usage cap per model session; not a subscription charge")
        if name == "run":
            runner.add_argument("--turns", type=int, default=1)
        else:
            runner.add_argument("--poll", type=float, default=3)
            runner.add_argument("--retry", type=float, default=60)
            runner.add_argument("--daily-limit", type=float, default=75.0,
                                help="soft USD limit per UTC day checked before each session; 0 disables")
            runner.add_argument("--limit-notify", help="recipient of one pause notice per day")
            runner.add_argument("--limit-allow", action="append", default=[],
                                help="sender whose authenticated Telegram messages are admitted during a daily-limit pause")
            runner.add_argument("--runtime-config", metavar="HOME/runtime.json",
                                help="M19: read persisted settings from HOME/runtime.json (then runtime.last-good.json); "
                                     "the other options become the baseline")
            runner.add_argument("--allow-model", action="append", default=[],
                                help="model a runtime request may select (repeatable; default: the baseline --model)")
            runner.add_argument("--allow-claude", action="append", default=[],
                                help="claude executable a runtime request may select, compared by realpath "
                                     "(repeatable; default: the baseline --claude)")
            runner.add_argument("--restart-mode", choices=["kickstart", "exit"], default="kickstart",
                                help="how a runtime request restarts the job: launchctl kickstart -k, or exit 75 "
                                     "for KeepAlive{SuccessfulExit:false} to relaunch")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    previous_term = None
    stopped_by_signal = []
    try:
        if args.command == "telegram":
            from .telegram_cli import handle
            output(handle(args, home))
            return 0
        if args.command == "postern":
            # An adapter process: it never opens a Store, only the channel contract CLI.
            _refuse_in_session("postern run")
            from . import postern
            return postern.run(args.config, once=args.once, interval=args.interval)
        if args.command == "system-prompt":
            # Read-only, never opens a Store: reads only the company log and the home's prompt.local.md.
            from .brain import system_prompt
            print(system_prompt(Path(home).expanduser().resolve()), end="")
            return 0
        if args.command == "doctor":
            # Read-only: never opens a Store (which would create or migrate state).
            from . import doctor
            report = doctor.diagnose(home, label=args.label, compare=args.compare_legacy, key_files=args.key_file,
                                     collector_plists=args.collector_plist, collector_dirs=args.collector_dir,
                                     relay_routes=args.relay_routes, relay_channel=args.relay_channel)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) if args.json
                  else doctor.render(report))
            return 0 if report.get("ok") else 1
        if args.command == "worker":
            # M28 P1b worker.manage (D9); an employee changes its own worker only through runtime_restart (M19).
            output(_delegated_worker(home, args))
            return 0
        if args.command == "adapter":
            # M28 P1b adapter.manage (D2): start/restart/reinstall an existing label under delegation.
            output(_delegated_adapter(home, args))
            return 0
        if args.command == "stop":
            current = worker_status(home)
            if current["running"]:
                if not current["pid"]:
                    raise RuntimeError("The worker is starting; try stopping it again in a moment.")
                os.kill(current["pid"], signal.SIGTERM)
            output({"stop_requested": current["running"], "pid": current["pid"]})
            return 0
        if args.command in ("run", "serve"):
            from .brain import ClaudeBrain

            # A service manager's normal stop should clean up Claude/MCP children
            # just as Ctrl-C does. SIGKILL recovery uses the durable run fence.
            previous_term = signal.getsignal(signal.SIGTERM)
            def terminate(signum, frame):
                stopped_by_signal.append(signum)
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, terminate)
            brain = ClaudeBrain(home, executable=args.claude, model=args.model,
                                timeout=args.timeout, budget=args.budget)
            def update(event):
                print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
            if args.command == "run":
                engine = Engine(home, brain, on_update=update)
                output(engine.run(max_turns=args.turns))
                return 0
            controller = None
            if args.runtime_config:
                from . import runtime
                from .service import request_restart
                if Path(args.runtime_config).expanduser().resolve() != home / runtime.CONFIG_FILE:
                    raise ValueError("--runtime-config must be %s." % (home / runtime.CONFIG_FILE))
                baseline = dict(claude=args.claude, model=args.model, budget=args.budget, timeout=args.timeout,
                                poll=args.poll, retry=args.retry, daily_limit=args.daily_limit,
                                limit_notify=args.limit_notify, limit_allow=args.limit_allow)
                controller = runtime.Controller(home, baseline, allowed_models=args.allow_model,
                                                allowed_claude=args.allow_claude,
                                                label=os.environ.get("XPC_SERVICE_NAME"), restarter=request_restart,
                                                mode=args.restart_mode, argv=sys.argv)
            engine = Engine(home, brain, on_update=update, daily_limit=args.daily_limit,
                            limit_notify=args.limit_notify, limit_allow=args.limit_allow, runtime=controller)
            try:
                engine.serve(
                    poll_seconds=args.poll,
                    retry_seconds=args.retry,
                    on_stop=lambda reason, failures: _queue_stop_alert(
                        home, reason, failures, engine.limit_notify),
                )
            except WorkerStopped as error:
                # Deliberate stop: exit 0 so launchd KeepAlive{SuccessfulExit:false} leaves it stopped.
                print("synthetics: %s" % error, file=sys.stderr)
            except SelfRestart as error:
                print("synthetics: restarting for runtime request %s (exit %d)" % (error, EXIT_RESTART), file=sys.stderr)
                return EXIT_RESTART
            return 0
        if args.command == "status":
            output(status(home))
            return 0
        if args.command == "channel" and args.channel_command == "provision":
            _refuse_in_session("channel provision")
            output(channel_provision(args.name, args.source_prefixes, args.homes, args.key_dir, args.by, args.routes))
            return 0
        if args.command == "channel" and args.channel_command in CONTRACT_COMMANDS:
            return channel_contract.run(args, home)
        if args.command in ("init", "handoff-codex", "question-routed", "access", "limit", "work", "runtime"):
            _m28_gate(args, home)
        if args.command == "init":
            humans_written = _init_humans(home, args.human, args.human_channels)
            colleague = _human_or_fail(home, args.by or args.human, "--by")
        with Store(home) as store:
            if args.command == "init":
                result = store.initialize(args.name, args.role, purpose=args.purpose)
                store.entity_put(colleague, args.colleague_name or colleague, "person",
                                 "Initial colleague; provides onboarding and discusses work.")
                store.feed(colleague,
                           "Welcome, %s. Your role is %s. %s Read your identity and current situation, introduce yourself through the outbox, and ask for onboarding information you need. Your workspace starts empty. Access to a working copy and company accounts will be provided separately."
                           % (args.name, args.role, args.purpose),
                           kind="onboarding", source="provisioning", external_id="welcome")
                output({"identity": result, "home": str(home), "workspace": str(home / "workspace"),
                        "humans": humans_written})
            else:
                store.identity()
                if args.command == "feed":
                    credential = None
                    if args.credential_file:
                        _refuse_in_session("feed --credential-file")
                        credential = read_credential_file(args.credential_file)
                    output(store.feed(args.sender, args.body, kind=args.kind, thread=args.thread,
                                      reply_to=args.reply_to, source=args.source, external_id=args.external_id,
                                      occurred_at=args.occurred_at, in_reply_to=args.in_reply_to,
                                      credential=credential, project=args.project, project_by=args.project_by,
                                      **_recipient_work(args)))
                elif args.command == "channel":
                    if args.channel_command == "register":
                        _refuse_in_session("channel register")
                        digest = Store.credential_hash(read_credential_file(args.key_file))
                        output(store.channel_register(args.name, digest, args.source_prefixes, args.by,
                                                      routes=args.routes))
                    else:
                        output(store.channel_list())
                elif args.command == "ingest":
                    credential = None
                    if args.credential_file:
                        _refuse_in_session("ingest --credential-file")
                        credential = read_credential_file(args.credential_file)
                    allowed = {"sender", "body", "kind", "thread", "reply_to", "source", "external_id", "occurred_at", "in_reply_to",
                               "recipient_work_id", "project", "project_by"}
                    for number, line in enumerate(sys.stdin, 1):
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        if not isinstance(item, dict) or set(item) - allowed:
                            raise ValueError("event line %s has unsupported fields" % number)
                        if "sender" not in item or "body" not in item:
                            raise ValueError("event line %s needs sender and body" % number)
                        # One result per input line makes a retrying adapter easy to write.
                        print(json.dumps(store.feed(credential=credential, **item), ensure_ascii=False), flush=True)
                elif args.command == "outbox":
                    output(store.outbox_list())
                elif args.command == "docket":
                    view = store.docket(args.person)
                    if args.format == "json":
                        output(view)
                    else:
                        print(view["text"])
                elif args.command == "send":
                    method = store.question_ask if args.question else store.message_send
                    extra = dict(_recipient_work(args))
                    if args.work_ids:
                        if not args.question:
                            raise ValueError("--also-work applies to --question only.")
                        extra["work_ids"] = ([args.work_id] if args.work_id else []) + args.work_ids
                    output(method(args.to, args.body, key=args.key, thread=args.thread,
                                  in_reply_to=args.in_reply_to, work_id=args.work_id, project=args.project,
                                  **extra))
                elif args.command == "question-routed":
                    output(store.question_routed(args.id, args.to_id, args.by))
                elif args.command == "thread":
                    output(store.thread_history(args.id, limit=args.limit, before=args.before))
                elif args.command == "handoff-codex":
                    from .codex_history import CodexHistoryExporter

                    identity = store.identity()
                    snapshot = CodexHistoryExporter(executable=args.codex).export_to(
                        args.id, args.snapshot, human_sender=_human_or_fail(home, args.human, "--human"),
                        assistant_sender=identity["id"])
                    conversation = args.thread or snapshot["thread"]
                    imported = store.thread_import(conversation, snapshot["messages"],
                                                   source="codex:" + args.id)
                    # Keep the address available on future work/reminder turns,
                    # which need not have a newly captured conversation event.
                    pins = store.memory_search(subject=identity["id"], limit=200)
                    if not any(m["predicate"] == "conversation_thread" and m["value"] == conversation for m in pins):
                        store.memory_put(identity["id"], "conversation_thread", conversation,
                                         kind="procedure", source=str(Path(args.snapshot).expanduser().resolve()))
                    output({"thread": conversation, "imported": len(imported),
                            "snapshot": str(Path(args.snapshot).expanduser().resolve()),
                            "warnings": snapshot["warnings"], "delivery": "history only; no messages replayed"})
                elif args.command == "inbox":
                    output(store.event_pending())
                elif args.command == "history":
                    output(store.event_search(args.query, thread=args.thread, limit=args.limit))
                elif args.command == "event":
                    output(store.event_get(args.id))
                elif args.command == "usage":
                    output(store.usage(days=args.days))
                elif args.command == "backfill-cost":
                    output(store.run_cost_backfill())
                elif args.command == "limit":
                    output(_delegated_limit(store, home, args))
                elif args.command == "runs":
                    output(store.run_list(limit=args.limit))
                elif args.command == "audit":
                    output(store.audit_list(limit=args.limit))
                elif args.command == "entity":
                    output(store.entity_put(args.id, args.name, args.kind, args.description))
                elif args.command == "access":
                    if args.access_command == "grant":
                        output(store.access_grant(args.path, args.mode, args.purpose, granted_by=_human_or_fail(home, args.by, "--by")))
                    elif args.access_command == "revoke":
                        output(store.access_revoke(args.id, granted_by=_human_or_fail(home, args.by, "--by")))
                    else:
                        output(store.access_list())
                elif args.command == "entities":
                    output(store.entities(args.query))
                elif args.command == "remember":
                    output(store.memory_put(args.subject, args.predicate, args.value, kind=args.kind,
                                            source=args.source, replaces=args.replaces))
                elif args.command == "recall":
                    output(store.memory_search(args.query, subject=args.subject, include_superseded=args.history,
                                               match=args.match))
                elif args.command == "work":
                    if args.work_command == "add":
                        output(store.work_put(args.title, description=args.description, priority=args.priority, key=args.key))
                    elif args.work_command == "list":
                        output(store.work_list())
                    elif args.work_command == "get":
                        output(store.work_get(args.id))
                    else:
                        fields = {name: getattr(args, name) for name in ("status", "note", "waiting_for", "wake_at", "priority",
                                                                          "cancel_reason", "override")
                                  if getattr(args, name) is not None}
                        if args.evidence:
                            fields["evidence"] = [{"level": level, "ref": ref} for level, ref in args.evidence]
                        output(store.work_put(id=args.id, **fields))
                elif args.command == "runtime":
                    from . import runtime
                    if args.runtime_command == "show":
                        def peek(name):
                            try:
                                return json.loads((home / name).read_text(encoding="utf-8"))
                            except (OSError, ValueError) as error:
                                return {"unavailable": str(error)}
                        output(dict(state=runtime.read_state(home), file=peek(runtime.CONFIG_FILE),
                                    last_good=peek(runtime.LAST_GOOD_FILE), requests=store.runtime_requests(10)))
                    else:
                        output(_delegated_runtime_request(store, home, args, runtime))
                elif args.command == "authority":
                    from . import authority
                    output(authority.command(store, args))
                elif args.command == "project":
                    output(project_command(store, args))
                elif args.command == "roster":
                    from . import rostercli
                    output(rostercli.command(store, args))
                elif args.command == "wake":
                    output(store.feed("runtime", args.reason, kind="wake", source="manual"))
                elif args.command == "remind":
                    output(store.reminder_set(args.at, args.reason, work_id=args.work_id, key=args.key))
        return 0
    except KeyboardInterrupt:
        print("Worker stopped. Its work and pending messages are retained.", file=sys.stderr)
        # A service manager's SIGTERM is a deliberate stop for serve; Ctrl-C and `run` keep 130.
        return 0 if args.command == "serve" and stopped_by_signal else 130
    except (ValueError, RuntimeError, OSError) as error:
        print("synthetics: %s" % error, file=sys.stderr)
        return 1
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    raise SystemExit(main())

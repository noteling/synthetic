"""Operator commands for one employee's human conversation channel."""

import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

from .store import Store


def add_commands(subparsers):
    channel = subparsers.add_parser("telegram", help="connect a paired private human conversation")
    commands = channel.add_subparsers(dest="telegram_command", required=True)
    initialize = commands.add_parser("init", help="write private channel configuration; does not send messages")
    initialize.add_argument("--token-file", required=True)
    initialize.add_argument("--colleague", help="paired human's ID (default: humans.json default_human)")
    initialize.add_argument("--thread", required=True, help="durable conversation ID, independent of Telegram")
    initialize.add_argument("--credential-file", help="M22 channel key file (from `synthetics channel provision telegram`)")
    for name, description in (("serve", "receive and deliver messages until stopped"),
                              ("status", "inspect pairing and delivery state without reading credentials"),
                              ("install", "start a macOS background adapter, optionally the employee worker"),
                              ("stop", "stop the selected background services in this login session"),
                              ("restart", "ask launchd to restart installed services without a stop/start gap")):
        command = commands.add_parser(name, help=description)
        command.add_argument("--config", help="default: EMPLOYEE_HOME/telegram.json")
        if name in ("install", "stop"):
            command.add_argument("--with-worker", action="store_true")
        if name == "restart":
            selected = command.add_mutually_exclusive_group()
            selected.add_argument("--with-worker", action="store_true", help="restart adapter then worker")
            selected.add_argument("--worker-only", action="store_true", help="restart only the employee worker")
        if name == "install":
            command.add_argument("--model", default="claude-opus-5-5")
            command.add_argument("--budget", type=float, default=100.0,
                                 help="API-equivalent USD usage cap per model session; not a subscription charge")
            command.add_argument("--timeout", type=float, default=300)
            command.add_argument("--claude", help="Claude CLI executable for the worker plist "
                                 "(default: ~/.local/bin/claude when present, else PATH lookup)")
    initialize.add_argument("--config", help="default: EMPLOYEE_HOME/telegram.json")


def _configuration(args, home):
    path = Path(args.config).expanduser().resolve() if args.config else home / "telegram.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "homes" in data:
        # #100 P2: a v2 config belongs to its primary home (the --home that serves and installs it).
        primary = [h for h in data.get("homes") or [] if isinstance(h, dict) and h.get("primary")]
        if len(primary) != 1 or (path.parent / Path(str(primary[0].get("home"))).expanduser()).resolve() != home:
            raise ValueError("Channel configuration belongs to another employee home.")
        return path, data
    if Path(data["employee_home"]).expanduser().resolve() != home:
        raise ValueError("Channel configuration belongs to another employee home.")
    return path, data


def _service_labels(path, identity):
    channel = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    employee = hashlib.sha256(identity["id"].encode()).hexdigest()[:16]
    return "com.synthetics.telegram." + channel, "com.synthetics.worker." + employee


def _worker_claude(explicit, native=None):
    """Kept for callers and tests; the shared rule lives in install.resolve_claude (M25)."""
    from .install import resolve_claude

    return resolve_claude(explicit, native)


def _start_if_needed(path):
    from .install import start_if_needed

    return start_if_needed(path)


def _is_homes_config(path):
    """#100 P2: a config with a ``homes`` list is v2; anything else keeps the single-home adapter unchanged."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "homes" in data


def _homes_status(data, state):
    """#100 P2 extra status fields for a v2 config (labels and counts only, never bodies)."""
    if not isinstance(data, dict) or "homes" not in data:
        return {}
    return {"homes": [{"key": h.get("key"), "label": h.get("label"), "primary": bool(h.get("primary"))}
                      for h in data.get("homes") or [] if isinstance(h, dict)],
            "schema": state.get("schema"), "held": len(state.get("held") or []),
            "route_map": len(state.get("route_map") or {})}


def handle(args, home):
    if args.telegram_command != "status":
        # Issue #8: init takes a credential, serve is an adapter process, and install/stop/restart write or drive
        # launchd jobs (install --with-worker writes a worker plist with caller-chosen model, executable and
        # budget). None of that is a model session's to do; delegated worker changes go through
        # `synthetics worker` (M28 worker.manage). `status` stays readable.
        from .cli import _refuse_in_session
        _refuse_in_session("telegram " + args.telegram_command)
    home = Path(home).resolve()
    with Store(home) as store:
        identity = store.identity()
    if args.telegram_command == "init":
        token = Path(args.token_file).expanduser().resolve()
        if not token.is_file():
            raise ValueError("Token file does not exist.")
        if token.stat().st_mode & 0o077:
            raise ValueError("Token file must be private; use chmod 600 on it.")
        if not args.colleague:
            from . import capture
            args.colleague = capture.default_human(home)
            if not args.colleague:
                raise ValueError("no --colleague given and humans.json has no default_human")
        if not args.thread.strip() or not args.colleague.strip():
            raise ValueError("Conversation and colleague IDs must be nonempty.")
        path = Path(args.config).expanduser().resolve() if args.config else home / "telegram.json"
        data = dict(token_file=str(token), colleague_id=args.colleague, employee_home=str(home),
                    local_thread=args.thread, state_file=str(path.with_suffix(".state.json")))
        if args.credential_file:
            data["credential_file"] = str(Path(args.credential_file).expanduser().resolve())
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # An existing connection is never silently reconfigured or rebound.
        if path.exists():
            if json.loads(path.read_text()) != data:
                raise ValueError("Existing channel configuration differs and was preserved.")
        else:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        return {"config": str(path), "employee": identity["id"], "thread": args.thread,
                "next": "Start the adapter, then open its one-time pairing link."}

    path, data = _configuration(args, home)
    state_path = Path(data.get("state_file", home / "telegram_state.json")).expanduser().resolve()
    if isinstance(data, dict) and "homes" in data and data.get("state_file"):
        # #100 P2: a v2 config's paths are relative to the config file, as HomesAdapter reads them.
        state_path = (Path(path).parent / Path(data["state_file"]).expanduser()).resolve()
    if args.telegram_command == "status":
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        counts = {}
        for receipt in state.get("deliveries", {}).values():
            label = receipt.get("status", "unknown")
            counts[label] = counts.get(label, 0) + 1
        return {"employee": identity["id"], "config": str(path), "state": str(state_path),
                "thread": data.get("local_thread", data.get("thread")), "bot_username": state.get("bot_username"),
                "paired": state.get("paired", False), "user_id": state.get("bound_user_id"),
                "chat_id": state.get("bound_chat_id"), "update_cursor": state.get("update_cursor"),
                "delivery_counts": counts,
                "feed_rejections": int(state.get("feed_rejections_total") or len(state.get("feed_rejections", []))),
                "last_feed_rejection": (state.get("feed_rejections") or [None])[-1],
                "accepted_means": "provider accepted; human reading is unknown",
                **_homes_status(data, state)}
    if args.telegram_command == "serve":
        from .telegram import TelegramAdapter

        try:
            if _is_homes_config(path):
                # #100 P2: a v2 config (`homes`) serves several homes through the channel contract.
                from .telegram_homes import HomesAdapter
                HomesAdapter(str(path)).run()
            else:
                TelegramAdapter(str(path)).run()
        except KeyboardInterrupt:
            return {"stopped": True}
        except Exception as error:
            config_error = getattr(sys.modules.get(__package__ + ".telegram"), "ChannelConfigError", None)
            if config_error is not None and isinstance(error, config_error):
                # Issue #5: our own message (channel ID and source prefix only, no token or key).
                raise RuntimeError(str(error)) from None
            # Provider exception chains can include a token-bearing URL. Never
            # let a traceback or raw response escape the transport boundary.
            raise RuntimeError("Telegram transport stopped (%s); inspect private delivery state." %
                               type(error).__name__) from None
        return {"stopped": True}

    from .service import restart_launch_agent, stop_launch_agent, write_launch_agent

    adapter_label, worker_label = _service_labels(path, identity)
    if args.telegram_command == "restart":
        labels = [] if args.worker_only else [adapter_label]
        if args.worker_only or args.with_worker:
            labels.append(worker_label)
        # The caller may itself belong to the worker job. Restart it last; each
        # transition is owned by launchd, not by a child doing stop then start.
        requested = []
        for label in labels:
            try:
                requested.append(restart_launch_agent(label))
            except subprocess.CalledProcessError as error:
                raise RuntimeError("launchd restart request failed for %s (exit %s)." %
                                   (label, error.returncode)) from None
        return {"restart_requests": requested}
    if args.telegram_command == "stop":
        stopped = []
        if args.with_worker:
            stopped.append(stop_launch_agent(worker_label))
        stopped.append(stop_launch_agent(adapter_label))
        return {"stopped": stopped, "login_restart": "Plists remain installed; services start on next login."}

    if not math.isfinite(args.budget) or not math.isfinite(args.timeout) or args.budget <= 0 or args.timeout <= 0:
        raise ValueError("Worker budget and timeout must be positive.")
    if args.with_worker:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not state.get("paired"):
            raise ValueError("Pair the human conversation before installing its employee worker.")
    from .install import ROOT, module_prefix, service_environment, worker_argv, write_worker

    logs = home / "services"
    adapter = write_launch_agent(adapter_label, module_prefix(home) + ["telegram", "serve", "--config", str(path)],
                                 ROOT, logs, keep_alive=True, environment=service_environment())
    started = [_start_if_needed(adapter)]
    if args.with_worker:
        colleague = (json.loads(path.read_text()).get("colleague_id") if path.exists() else None) or ""
        # The paired human hears about a pause and can still reach the worker during it.
        argv = worker_argv(home, _worker_claude(args.claude), args.model, args.budget, args.timeout,
                           poll=2.0, daily_limit=75.0, limit_notify=colleague or None,
                           limit_allow=[colleague] if colleague else [])
        worker = write_worker(home, worker_label, argv)
        started.append(_start_if_needed(worker))
    return {"services": started, "logs": str(logs),
            "availability": "Logged-in, awake Mac with network access; worker retains its bounded failure stop."}

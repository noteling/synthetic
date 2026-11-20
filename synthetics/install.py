"""Transport-neutral launch-agent installation for workers and adapters (M25).

A worker is installed without any messaging adapter. Adapters (Telegram,
Postern, a company's own bridge) are separate launch agents. Both reuse the
owned-plist helpers in service.py; nothing here starts a model session.
"""

import hashlib
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


WORKER_PREFIX = "com.synthetics.worker."
ADAPTER_PREFIX = "com.synthetics.adapter."
ROOT = Path(__file__).resolve().parent.parent


def worker_label(identity):
    """Default label for an employee's worker; the M19 runtime controller expects this prefix."""
    return WORKER_PREFIX + hashlib.sha256(identity["id"].encode()).hexdigest()[:16]


def _check_prefix(label, prefix):
    if not isinstance(label, str) or not label.startswith(prefix) or len(label) == len(prefix):
        raise ValueError("label must start with %s" % prefix)
    return label


def service_environment():
    """Explicit plist environment: no ambient secrets, the native claude first on PATH."""
    user = pwd.getpwuid(os.getuid()).pw_name
    return {"HOME": str(Path.home()), "PYTHONPATH": str(ROOT),
            "PATH": str(Path.home() / ".local/bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "USER": user, "LOGNAME": user, "SHELL": "/bin/zsh", "TMPDIR": tempfile.gettempdir()}


def module_prefix(home, python=None):
    return [str(Path(python or sys.executable).absolute()), "-u", "-m", "synthetics", "--home", str(home)]


def resolve_claude(explicit, native=None):
    """Executable written into a worker plist. Always an explicit absolute path.

    The native install in ~/.local/bin is preferred over an ambient PATH lookup:
    on 2026-11-17 the Homebrew copy was held suspended at launch on this host
    (docs/human-channel.md), and a plist must not depend on the installer's PATH.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            raise ValueError("--claude is not an executable file: %s" % candidate)
        return str(candidate.absolute())
    native = Path(native) if native else Path.home() / ".local/bin/claude"
    if native.is_file() and os.access(native, os.X_OK):
        return str(native)
    found = shutil.which("claude")
    if not found:
        raise ValueError("Claude CLI was not found; the transport remains available.")
    return str(Path(found).absolute())


def resolve_codex(explicit=None):
    """Resolve the owner-selected Codex CLI without requiring a Claude installation."""
    if explicit:
        candidate = Path(explicit).expanduser()
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            raise ValueError("--codex is not an executable file: %s" % candidate)
        return str(candidate.absolute())
    found = shutil.which("codex")
    if not found:
        raise ValueError("Codex CLI was not found; the transport remains available.")
    return str(Path(found).absolute())


def worker_argv(home, claude, model, budget, timeout, poll=2.0, daily_limit=75.0, limit_notify=None,
                limit_allow=(), runtime_config=False, allow_model=(), allow_claude=(), python=None,
                brain="claude", codex=None, allow_codex=(), brain_config=None):
    """The `serve` argv for a worker plist. Order matches the pre-M25 telegram install."""
    import math

    for value, name in ((budget, "budget"), (timeout, "timeout"), (poll, "poll")):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Worker %s must be positive." % name)
    if not math.isfinite(daily_limit) or daily_limit < 0:
        raise ValueError("Worker daily limit must be zero or positive.")
    if brain_config is not None:
        from . import brainconfig
        if (brain != "claude" or claude not in (None, "", "claude") or codex or model is not None
                or allow_model or allow_claude or allow_codex):
            raise ValueError("--brain-config owns executor/model selection; provider/model overrides are not allowed.")
        configuration = brainconfig.load(brain_config)
        executable = ["--brain-config", str(configuration.path)]
    else:
        if brain not in ("claude", "codex"):
            raise ValueError("brain must be claude or codex.")
        executable = ["--claude", str(Path(claude).absolute())] if brain == "claude" else [
            "--brain", "codex", "--codex", resolve_codex(codex)]
        model = model or ("claude-opus-5-5" if brain == "claude" else "")
    # An omitted Codex model selects its CLI default. Launch-agent argv deliberately refuses empty
    # strings, so represent that choice by omitting --model rather than an empty argument.
    model_args = ["--model", model] if model else []
    argv = module_prefix(home, python) + ["serve"] + executable + model_args + [
                                  "--budget", str(budget), "--timeout", str(timeout),
                                  "--poll", ("%g" % poll), "--daily-limit", str(daily_limit)]
    if limit_notify:
        argv += ["--limit-notify", limit_notify]
    for sender in limit_allow:
        argv += ["--limit-allow", sender]
    if runtime_config:
        argv += ["--runtime-config", str(Path(home) / "runtime.json")]
    for item in allow_model:
        # The CLI's default model is recorded as an empty string, but launch-agent
        # argv forbids empty elements. argparse's assignment form preserves it.
        argv += ["--allow-model="] if item == "" else ["--allow-model", item]
    for item in allow_claude:
        argv += ["--allow-claude", item]
    for item in allow_codex:
        argv += ["--allow-codex", item]
    return argv


def write_worker(home, label, argv, launch_agents_dir=None):
    from .service import write_launch_agent

    _check_prefix(label, WORKER_PREFIX)
    # on-failure: a crash relaunches, the deliberate three-failure stop exits 0 and stays stopped.
    return write_launch_agent(label, argv, ROOT, Path(home) / "services", launch_agents_dir,
                              keep_alive="on-failure", environment=service_environment())


def write_adapter(home, label, argv, keep_alive=True, launch_agents_dir=None, cwd=None):
    """P1c-b: ``cwd`` sets the plist WorkingDirectory (a `-m module` adapter resolves its module there); default
    ROOT (main), as before."""
    from .service import write_launch_agent

    _check_prefix(label, ADAPTER_PREFIX)
    return write_launch_agent(label, list(argv), Path(cwd) if cwd else ROOT, Path(home) / "services",
                              launch_agents_dir, keep_alive=keep_alive, environment=service_environment())


def start_if_needed(path):
    """Bootstrap an unloaded plist, or start a loaded-but-stopped job; never kill a running one."""
    from .service import bootstrap_launch_agent, start_launch_agent

    target = "gui/%s/%s" % (os.getuid(), path.stem)
    check = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True)
    if check.returncode == 0:
        if re.search(r"^\s*state = running\s*$", check.stdout, re.MULTILINE):
            return {"label": path.stem, "already_loaded": True}
        return start_launch_agent(path.stem)
    return bootstrap_launch_agent(path)


UNLOAD_WAIT_SECONDS = 5.0


def wait_unloaded(label, wait=None, clock=None, sleep=None):
    """G3: True once launchd no longer has ``label`` loaded, polling up to ``wait`` seconds (bootout returns before
    the job is gone); False if still loaded, None if it cannot be determined."""
    import time

    wait = UNLOAD_WAIT_SECONDS if wait is None else wait
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + wait
    while True:
        state = loaded(label)
        if state is False:
            return True
        if clock() >= deadline:
            return None if state is None else False
        sleep(0.25)


def reload_if_loaded(path):
    """G1: start a freshly written plist so launchd runs the NEW definition. If the label is loaded, launchd keeps
    the old definition (start_if_needed would answer already_loaded), so boot it out, wait for the unload (G3) and
    bootstrap the new plist. Callers hold the target's idle boundary (workers) before calling this."""
    from .service import bootstrap_launch_agent, stop_launch_agent

    label = Path(path).stem
    if loaded(label) is not True:
        return start_if_needed(path)
    stop_launch_agent(label)
    if wait_unloaded(label) is not True:
        raise RuntimeError("%s did not unload after bootout; the new plist is written but not loaded" % label)
    return dict(bootstrap_launch_agent(path), reloaded=True)


def add_commands(sub):
    worker = sub.add_parser("worker", help="install or control this home's worker launch agent (no adapter needed)")
    ws = worker.add_subparsers(dest="worker_command", required=True)
    inst = ws.add_parser("install", help="write the worker plist and start it (operator)")
    inst.add_argument("--label", help="default: com.synthetics.worker.<sha256(employee id)[:16]>")
    inst.add_argument("--brain-config", metavar="CONFIG_JSON",
                      help="owner-provisioned executor and model connection JSON")
    inst.add_argument("--claude", help="Claude executable (default: ~/.local/bin/claude, then PATH)")
    inst.add_argument("--brain", choices=["claude", "codex"], default="claude", help="reasoning executor")
    inst.add_argument("--codex", help="Codex executable (default: PATH)")
    inst.add_argument("--model", help="executor model (default: Claude Opus, or Codex CLI default)")
    inst.add_argument("--budget", type=float, default=100.0)
    inst.add_argument("--timeout", type=float, default=600.0)
    inst.add_argument("--poll", type=float, default=2.0)
    inst.add_argument("--daily-limit", type=float, default=75.0)
    inst.add_argument("--limit-notify", help="recipient of pause and stop notices (via the outbox)")
    inst.add_argument("--limit-allow", action="append", default=[])
    inst.add_argument("--runtime-config", action="store_true", help="M19: read HOME/runtime.json at start")
    inst.add_argument("--allow-model", action="append", default=[])
    inst.add_argument("--allow-claude", action="append", default=[])
    inst.add_argument("--allow-codex", action="append", default=[],
                      help="owner-provisioned Codex executable paths runtime requests may select")
    inst.add_argument("--launch-agents-dir", help="write the plist here instead of ~/Library/LaunchAgents")
    inst.add_argument("--no-start", action="store_true", help="write the plist only")
    for name in ("start", "stop", "restart", "remove"):
        control = ws.add_parser(name)
        control.add_argument("--label")
        if name == "remove":
            control.add_argument("--launch-agents-dir")
            control.add_argument("--never-loaded", action="store_true",
                                 help="remove a plist that was written but never bootstrapped")
    adapter = sub.add_parser("adapter", help="install a generic adapter launch agent (com.synthetics.adapter.*)")
    asub = adapter.add_subparsers(dest="adapter_command", required=True)
    ainst = asub.add_parser("install", help="adapter install --label L [options] -- /abs/executable args...")
    ainst.add_argument("--label", required=True)
    ainst.add_argument("--keep-alive", choices=["always", "on-failure", "never"], default="always")
    ainst.add_argument("--launch-agents-dir")
    ainst.add_argument("--no-start", action="store_true")
    ainst.add_argument("--cwd", help="P1c-b: plist WorkingDirectory (default: the Synthetics checkout)")
    ainst.add_argument("--review", help="P1c-b, session: the frozen review SHA256SUMS file the code matches; it must "
                                        "be listed in the company `policy adapter.reviews` (adapter.install)")
    ainst.add_argument("argv", nargs="*")
    for name in ("start", "stop", "restart", "remove"):
        control = asub.add_parser(name)
        control.add_argument("--label", required=True)
        control.add_argument("--launch-agents-dir", help="where the installed plist is (default ~/Library/LaunchAgents)")
        if name == "remove":
            control.add_argument("--never-loaded", action="store_true")


def _control(kind, command, label, args):
    from .service import remove_launch_agent, restart_launch_agent, start_launch_agent, stop_launch_agent

    if command == "start":
        return start_launch_agent(label)
    if command == "stop":
        return stop_launch_agent(label)
    if command == "restart":
        return restart_launch_agent(label)
    removed = remove_launch_agent(label, args.launch_agents_dir and Path(args.launch_agents_dir).absolute(),
                                  stop=not args.never_loaded)
    return {"label": label, "removed": removed}


def handle(args, home):
    home = Path(home).resolve()
    if args.command == "adapter":
        _check_prefix(args.label, ADAPTER_PREFIX)
        if args.adapter_command != "install":
            return _control("adapter", args.adapter_command, args.label, args)
        argv = list(args.argv)
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            raise ValueError("adapter install needs the adapter argv after --")
        keep = {"always": True, "on-failure": "on-failure", "never": False}[args.keep_alive]
        agents = Path(args.launch_agents_dir).absolute() if args.launch_agents_dir else None
        cwd = None
        if getattr(args, "cwd", None):
            cwd = Path(args.cwd).expanduser().resolve()
            if not cwd.is_dir():
                raise ValueError("--cwd is not a directory: %s" % cwd)
        plist = write_adapter(home, args.label, argv, keep, agents, cwd=cwd)
        result = {"label": args.label, "plist": str(plist), "argv": argv}
        if cwd:
            result["cwd"] = str(cwd)
        if not args.no_start:
            result["service"] = start_if_needed(plist)
        return result

    from .store import Store

    with Store(home) as store:
        identity = store.identity()
    label = _check_prefix(args.label or worker_label(identity), WORKER_PREFIX)
    if args.worker_command != "install":
        return _control("worker", args.worker_command, label, args)
    selected = getattr(args, "brain", "claude")
    configuration_path = getattr(args, "brain_config", None)
    from .runtime import check_brain_config_options
    check_brain_config_options(args)
    claude = resolve_claude(args.claude) if selected == "claude" and configuration_path is None else ""
    codex = resolve_codex(getattr(args, "codex", None)) if selected == "codex" and configuration_path is None else None
    argv = worker_argv(home, claude, args.model, args.budget, args.timeout, args.poll, args.daily_limit,
                       args.limit_notify, args.limit_allow, args.runtime_config, args.allow_model, args.allow_claude,
                       brain=selected, codex=codex, allow_codex=getattr(args, "allow_codex", ()),
                       brain_config=configuration_path)
    agents = Path(args.launch_agents_dir).absolute() if args.launch_agents_dir else None
    plist = write_worker(home, label, argv, agents)
    result = {"employee": identity["id"], "label": label, "plist": str(plist), "argv": argv,
              "logs": str(home / "services")}
    if not args.no_start:
        result["service"] = start_if_needed(plist)
    return result


# ---- M28 P1b worker.manage (D9) --------------------------------------------------------------------------

WORKER_OPS = ("install", "start", "stop", "restart", "remove")  # D9a: exactly the `worker` subcommand names
OP_ALIASES = {"kickstart": "restart"}
MAX_DELEGATED_BUDGET = 100.0
IDLE_GATED = ("stop", "restart", "remove")  # D9c


def op_covered(op, scope_ops):
    """D9a: ``op`` is a `worker` subcommand; a scope lists subcommand names, "kickstart" (= restart) or "*"."""
    ops = set(scope_ops or [])
    return op in WORKER_OPS and ("*" in ops or op in ops or any(OP_ALIASES.get(o) == op for o in ops))


def _agents_dir_allowed(value):
    """D9b: the default ~/Library/LaunchAgents, or a directory under the system temp dir (tests)."""
    if not value:
        return True
    try:
        path = Path(value).expanduser().resolve()
        default = (Path.home() / "Library/LaunchAgents").resolve()
        temp = Path(tempfile.gettempdir()).resolve()
    except (OSError, RuntimeError):
        return False
    return path == default or temp in path.parents


def delegated_install_params(home, args, refuse):
    """D9b: a delegated `worker install` writes only the standard worker plist for ``home``. Returns the checked
    install kwargs; any other shape is refused through ``refuse(code, message)`` (audited by the caller)."""
    from . import authority, roster, runtime
    from .store import Store

    if (getattr(args, "brain_config", None) is not None or getattr(args, "brain", "claude") != "claude" or getattr(args, "codex", None)
            or getattr(args, "allow_codex", None)):
        refuse("executor_owner_only", "Configured/Codex worker installation and executor allow-lists are owner-only; "
               "a delegated worker install preserves the provisioned Claude executor.")
    if args.model is None:
        args.model = "claude-opus-5-5"
    with Store(home) as store:
        identity = store.identity()
    company = roster.company_state_for_home(home)
    label = worker_label(identity)
    if args.label and args.label != label:
        refuse("label_not_standard", "a delegated worker install writes only %s" % label)
    if not _agents_dir_allowed(args.launch_agents_dir):
        refuse("agents_dir", "--launch-agents-dir must be the default or a temporary test directory")
    state = runtime.read_state(home)
    policy = (state or {}).get("policy") or {}
    models, claudes = policy.get("allowed_models"), policy.get("allowed_claude")
    policy_source = "target runtime.state.json"
    if not models or not claudes:
        # P1c (c): a target that publishes no policy (never installed, or no --runtime-config) takes the company
        # ``policy worker.defaults``. No consent is missing here, only the capability without one.
        defaults = authority.worker_defaults(company)
        if defaults is None:
            refuse("no_policy", "the target publishes no runtime policy (runtime.state.json allowed lists) and the "
                   "company log has no `policy worker.defaults`: %s (set worker.defaults, M28 P1c)"
                   % authority.NOT_YET)
        try:  # D15 at use: an entry that dropped out of every published owner list is refused now
            authority.check_worker_defaults(roster.root_of_home(home), company, defaults["models"],
                                            defaults["claude"])
        except authority.AuthorityError as error:
            refuse(error.code, str(error))
        models, claudes = defaults["models"], defaults["claude"]
        policy_source = "company policy worker.defaults %s" % defaults["entry"]
    real = {os.path.realpath(c) for c in claudes}
    try:
        claude = resolve_claude(args.claude)
    except ValueError as error:
        refuse("foreign_executable", str(error))
    if os.path.realpath(claude) not in real:
        refuse("foreign_executable", "claude %s is not in the target's allowed list; any other executable stays "
               "owner-only (adapter install)" % claude)
    if args.model not in models:
        refuse("model_not_allowed", "model %s is not in the target's allowed models %s" % (args.model, models))
    if any(m not in models for m in args.allow_model) or any(os.path.realpath(c) not in real
                                                              for c in args.allow_claude):
        refuse("allow_list_widened", "--allow-model/--allow-claude must stay within the target's allowed lists")
    if not 0 < args.budget <= MAX_DELEGATED_BUDGET:
        refuse("budget_exceeds", "a delegated install's budget must be in (0, %g]" % MAX_DELEGATED_BUDGET)
    cap = authority.ceiling(company)["usd"]
    if not 0 < args.daily_limit or (cap is not None and args.daily_limit > cap):
        refuse("limit_exceeds", "a delegated install's daily limit must be in (0, %s] (the limit.ceiling policy)"
               % ("no ceiling" if cap is None else "%g" % cap))
    if args.limit_allow:
        refuse("limit_allow_owner_only", "--limit-allow lets senders bypass the daily limit: owner-only")
    python = installed_interpreter(label, args.launch_agents_dir)  # G2: keep the owner plist's interpreter
    argv = worker_argv(home, claude, args.model, args.budget, args.timeout, args.poll, args.daily_limit,
                       args.limit_notify, (), args.runtime_config, args.allow_model or models,
                       args.allow_claude or claudes, python=python)
    return {"identity": identity, "label": label, "argv": argv, "policy_source": policy_source,
            "interpreter": argv[0]}


def installed_interpreter(label, launch_agents_dir=None):
    """G2: the interpreter of the installed worker plist (argv[0] of a `-m synthetics` plist), when it is still an
    executable file; else None (the caller's sys.executable). A delegated reinstall must not switch an owner plist
    on CLT python3 to whatever Python the delegate's shell runs."""
    agents = Path(launch_agents_dir).absolute() if launch_agents_dir else None
    _, installed = _installed_plist(label, agents)
    argv = (installed or {}).get("ProgramArguments") if isinstance(installed, dict) else None
    if not argv or len(argv) < 4 or argv[1:4] != ["-u", "-m", "synthetics"]:
        return None
    exe = argv[0]
    return exe if os.path.isabs(exe) and os.path.isfile(exe) and os.access(exe, os.X_OK) else None


def is_idle(home):
    """D9c: no session is running in ``home`` (its newest runs hold no status running)."""
    from .store import Store

    with Store(home) as store:
        return not any(r.get("status") == "running" for r in store.run_list(10))


def loaded(label):
    """True/False: launchd has the job loaded in this GUI domain; None when that cannot be determined."""
    try:
        check = subprocess.run(["/bin/launchctl", "print", "gui/%s/%s" % (os.getuid(), label)],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return check.returncode == 0


def service_pid(label):
    """The live PID launchd reports for ``label``: an int, None when loaded without a process or not loaded,
    False when it cannot be determined. A PID counts only if the process exists (signal 0)."""
    import re as _re

    try:
        check = subprocess.run(["/bin/launchctl", "print", "gui/%s/%s" % (os.getuid(), label)],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if check.returncode != 0:
        return None
    found = _re.search(r"^\s*pid = (\d+)\s*$", check.stdout or "", _re.M)
    if not found:
        return None
    pid = int(found.group(1))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid


READY_WAIT_SECONDS = 10.0


def _ready(label, before, wait=None, clock=None, sleep=None):
    """D10b: start/restart is confirmed only by a live PID different from the one captured before the op."""
    import time

    wait = READY_WAIT_SECONDS if wait is None else wait
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + wait
    while True:
        pid = service_pid(label)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid != before:
            return pid
        if clock() >= deadline:
            return None
        sleep(0.5)


def _outcome(op, label, plist, args, result, before=None):
    """D4: confirmed when an independent read-back agrees with the request, else uncertain (never retried)."""
    import plistlib

    if op == "install":
        try:
            with open(plist, "rb") as handle:
                written = plistlib.load(handle)
        except (OSError, ValueError):
            return "uncertain"
        if written.get("Label") != label or written.get("ProgramArguments") != result["argv"]:
            return "uncertain"
        if args.no_start:
            return "confirmed"
        op = "start"
    elif op == "remove":
        if plist.exists():
            return "uncertain"
        if args.never_loaded:
            return "confirmed"
        expect = False
    elif op == "stop":
        expect = False
    if op in ("start", "restart"):
        if before is False:  # the pre-op PID is unknown, so no new PID can be proven
            result["pid"] = None
            return "uncertain"
        pid = _ready(label, before)
        result["pid"] = pid
        return "confirmed" if pid is not None else "uncertain"
    state = wait_unloaded(label)  # G3: expect is False here; bootout returns before the job is gone
    return "confirmed" if state is True else "uncertain"


def target_controller(home, pid):
    """M28 D12: which engine runs in the target, from its published runtime.state.json and the live launchd PID.
    "current": the state's pid is the live PID and lists the worker_hold capability (honours the D10c hold).
    "old_controller": the state's pid is the live PID but lacks it (started on pre-P1b code; the hold is not
    honoured, so the op runs on the old idle check only). "not_running": no live PID. "unknown": the PID could not
    be read, or no runtime state / a state for another PID (a worker without --runtime-config publishes none)."""
    from . import runtime

    if pid is False:
        return "unknown"
    if pid is None:
        return "not_running"
    state = runtime.read_state(home)
    if not state or state.get("pid") != pid:
        return "unknown"
    return "current" if "worker_hold" in (state.get("capabilities") or []) else "old_controller"


# ---- M28 D14: mandatory lifecycle ledger ---------------------------------------------------------------------

def ledger_begin(ledger_home, row):
    """D14a: write the pending op durably before any launchd effect. Any failure refuses the op (nothing ran)."""
    from . import authority
    from .store import LaunchOpPending, Store

    try:
        with Store(ledger_home) as store:
            return store.launch_op_begin(row)["id"]
    except LaunchOpPending as error:
        raise authority.AuthorityError("pending_op", str(error), op_id=error.op_id)
    except Exception as error:
        raise authority.AuthorityError("ledger_unavailable", "the request could not be recorded durably (%s); "
                                       "nothing was executed" % error)


def ledger_settle(ledger_home, op_id, outcome, **data) -> bool:
    """D14b: record the outcome. False when the write failed: the op stays pending and reconcilable."""
    from .store import Store

    try:
        with Store(ledger_home) as store:
            store.launch_op_settle(op_id, outcome, **data)
        return True
    except Exception:
        return False


PID_UNKNOWN = "unknown"


def pid_intent(before):
    """D16b: the pre-op PID as recorded intent: an int, None (no process: launchd has none) or "unknown"."""
    if isinstance(before, bool) or before is False:
        return PID_UNKNOWN
    return before if isinstance(before, int) else None


def _failure(ledger_home, audit_home, audit_action, row, error, started, warrant):
    """D16c: settle a delegated op whose execution raised.
    - Raised before the effect began (D4 refusal at the fence, or nothing ran): ``failed`` and the error is
      re-raised. If even that settle write fails the row stays pending and the error carries ``pending_op``.
    - Raised after the effect may have started (plist written, then launchctl raised): ``uncertain``, never
      ``failed`` — part of the effect may be in place. Returned as a result (no retry); a failed settle write
      leaves the row pending (``pending_op``) for `authority settle`."""
    from . import authority

    refused = isinstance(error, authority.AuthorityError) and not started
    outcome = "uncertain" if started else "failed"
    data = {"error": str(error)}
    if refused:
        data["refused"] = True
    settled = ledger_settle(ledger_home, row["op_id"], outcome, **data)
    if not refused:
        authority.audit(audit_home, audit_action, dict(row, stage="outcome", outcome=outcome, error=str(error)))
    if not started:
        if isinstance(error, authority.AuthorityError):
            error.detail.update(op_id=row["op_id"], **({} if settled else {"pending_op": row["op_id"]}))
        if not settled:
            raise authority.AuthorityError("pending_op", "%s; the failure could not be recorded, so %s stays "
                                           "pending (reconcile with `authority settle`)" % (error, row["op_id"]),
                                           op_id=row["op_id"], pending_op=row["op_id"]) from error
        raise error
    result = {"label": row["label"], "outcome": "uncertain", "error": str(error), "op_id": row["op_id"],
              "delegation": warrant["delegation"]}
    if not settled:
        result["pending_op"] = row["op_id"]
    return result


def op_evidence(record) -> dict:
    """D14b reconcile: judge a pending op from independent evidence only (plist read-back, launchd state, a new
    live PID). Anything short of proof is uncertain; nothing is executed."""
    import plistlib

    op, label = record.get("op"), record.get("label")
    path = Path(record.get("plist") or "")
    try:
        with open(path, "rb") as handle:
            plist = plistlib.load(handle)
    except FileNotFoundError:
        plist = None
    except (OSError, ValueError):
        plist = False
    evidence = {"plist": "missing" if plist is None else "unreadable" if plist is False else "present"}
    outcome = "uncertain"
    if op == "install":
        matches = isinstance(plist, dict) and plist.get("Label") == label and \
            (record.get("argv") is None or plist.get("ProgramArguments") == record.get("argv")) and \
            (record.get("cwd") is None or plist.get("WorkingDirectory") == record.get("cwd"))  # D17a
        evidence["plist_matches"] = bool(matches)
        if not matches:
            outcome = "uncertain"
        elif record.get("no_start") is True:
            outcome = "confirmed"
        else:  # D16b: install+start (or an intent without no_start) needs the same new-PID proof as start
            op = "start"
    if op == "remove":
        state = loaded(label)
        evidence["loaded"] = state
        outcome = "confirmed" if plist is None and state is False else "uncertain"
    elif op == "stop":
        state = loaded(label)
        evidence["loaded"] = state
        outcome = "confirmed" if state is False else "uncertain"
    elif op in ("start", "restart"):
        pid, before = service_pid(label), record.get("pid_before", PID_UNKNOWN)
        evidence["pid"] = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        # D16b: an unknown pre-op PID (or a row without one) proves nothing; an absent one (None) is
        # proven by any live PID, as in _ready.
        if before != PID_UNKNOWN and not isinstance(before, bool) and evidence["pid"] is not None \
                and evidence["pid"] != before:
            outcome = "confirmed"
    return {"outcome": outcome, "evidence": evidence}


def delegated_execute(home, args, warrant, params=None):
    """Execute one delegated worker op under the D4 file fence and record requested → confirmed/failed/uncertain
    in the target home's audit. A failure is reported, never blind-retried."""
    from . import authority

    home = Path(home).resolve()
    op = args.worker_command
    label = params["label"] if params else _check_prefix(args.label or worker_label(_identity(home)), WORKER_PREFIX)
    agents = Path(args.launch_agents_dir).absolute() if getattr(args, "launch_agents_dir", None) else None
    from .service import _agents_directory

    plist = _agents_directory(agents) / (label + ".plist")
    row = dict(authority.receipt(warrant), op=op, label=label, plist=str(plist))
    before = service_pid(label) if op in ("install", "start", "restart") else None
    row["pid_before"] = pid_intent(before)  # D16b: int, None (no process) or "unknown" (could not be read)
    if op != "install":  # D12: label the engine the op acts on (old-controller targets ignore worker_hold)
        row["controller"] = target_controller(home, before if op in ("start", "restart") else service_pid(label))
    if op == "install":
        row["argv"] = params["argv"]
        row["no_start"] = bool(args.no_start)  # D16b: the full intent, so reconcile judges the right effect
    row["op_id"] = ledger_begin(home, dict(row, kind="worker", home=str(home)))  # D14a: durable before any effect
    authority.audit(home, "authority.worker", dict(row, stage="requested"))

    started = []

    def run():
        started.append(True)  # D16c: from here an effect may have (partly) happened
        if op == "install":
            path = write_worker(home, label, params["argv"], agents)
            result = {"employee": params["identity"]["id"], "label": label, "plist": str(path),
                      "argv": params["argv"], "logs": str(home / "services"),
                      "policy_source": params["policy_source"]}
            if not args.no_start:
                result["service"] = reload_if_loaded(path)  # G1: the idle hold is taken by the caller
            return result
        return _control("worker", op, label, args)

    try:
        result = authority.fenced_file(warrant, run)
    except Exception as error:
        return _failure(home, home, "authority.worker", row, error, bool(started), warrant)
    if not isinstance(result, dict):
        result = {"label": label, "result": result}
    result["argv"] = result.get("argv") or (params or {}).get("argv")
    outcome = _outcome(op, label, plist, args, result, before=before)
    if result["argv"] is None:
        result.pop("argv")
    if not ledger_settle(home, row["op_id"], outcome, pid=result.get("pid")):
        outcome, result["pending_op"] = "uncertain", row["op_id"]  # D14b: unrecorded outcome is never confirmed
    authority.audit(home, "authority.worker", dict(row, stage="outcome", outcome=outcome, pid=result.get("pid")))
    result.update(outcome=outcome, delegation=warrant["delegation"], op_id=row["op_id"])
    if "controller" in row:
        result["controller"] = row["controller"]
    return result


def _identity(home):
    from .store import Store

    with Store(home) as store:
        return store.identity()


# ---- M28 P1b adapter.manage (D2) -------------------------------------------------------------------------

ADAPTER_DELEGATED_OPS = ("start", "restart", "install")  # install = reinstall with unchanged ProgramArguments


def label_covered(label, scope_labels):
    """adapter.manage / adapter.install scope: exact labels, "*" (root only, never in a child), or a prefix
    pattern ending in ".*" on a dot boundary (D19, authority.label_matches)."""
    from . import authority
    return authority.label_matches(label, scope_labels)


def _installed_plist(label, agents):
    import plistlib
    from .service import _agents_directory

    path = _agents_directory(agents) / (label + ".plist")
    try:
        with open(path, "rb") as handle:
            return path, plistlib.load(handle)
    except FileNotFoundError:
        return path, None
    except (OSError, ValueError):
        return path, False


def delegated_adapter(home, args, warrant, refuse):
    """D2: start/restart an EXISTING com.synthetics.adapter.* label, or reinstall it with ProgramArguments and
    KeepAlive equal to the installed plist. A new label, a new executable or changed arguments stay owner-only
    (`adapter install`); stop/remove stay owner-only (they cut a channel). Outcome per D4/D10b, recorded in the
    actor home's audit (an adapter belongs to no single employee home)."""
    import plistlib
    from . import authority

    op, label = args.adapter_command, args.label
    agents = Path(args.launch_agents_dir).absolute() if getattr(args, "launch_agents_dir", None) else None
    if agents is not None and not _agents_dir_allowed(args.launch_agents_dir):
        refuse("agents_dir", "--launch-agents-dir must be the default or a temporary test directory")
    path, installed = _installed_plist(label, agents)
    if installed is None:
        refuse("unknown_label", "%s has no installed plist; installing a new adapter is owner-only" % label)
    if installed is False or installed.get("Label") != label:
        refuse("plist_unreadable", "the installed plist for %s cannot be read back" % label)
    argv = None
    if op == "install":
        argv = list(args.argv)
        if argv and argv[0] == "--":
            argv = argv[1:]
        keep = {"always": True, "on-failure": {"SuccessfulExit": False}, "never": False}[args.keep_alive]
        if argv != installed.get("ProgramArguments") or keep != installed.get("KeepAlive"):
            refuse("argv_changed", "a delegated reinstall keeps the installed ProgramArguments and KeepAlive; "
                                   "a changed executable or arguments is owner-only `adapter install`")
    row = dict(authority.receipt(warrant), op=op, label=label, plist=str(path))
    before = service_pid(label)
    row["pid_before"] = pid_intent(before)  # D16b: int, None (no process) or "unknown" (could not be read)
    if op != "install":  # D12: label the engine the op acts on (old-controller targets ignore worker_hold)
        row["controller"] = target_controller(home, before if op in ("start", "restart") else service_pid(label))
    if op == "install":
        row["argv"] = argv
        row["no_start"] = bool(args.no_start)  # D16b
    row["op_id"] = ledger_begin(warrant["home"], dict(row, kind="adapter", home=str(Path(home).resolve())))  # D14a
    authority.audit(warrant["home"], "authority.adapter", dict(row, stage="requested"))

    started = []

    def run():
        started.append(True)  # D16c
        if op == "install":
            written = write_adapter(home, label, argv, {"always": True, "on-failure": "on-failure",
                                                        "never": False}[args.keep_alive], agents)
            result = {"label": label, "plist": str(written), "argv": argv}
            if not args.no_start:
                result["service"] = start_if_needed(written)
            return result
        return _control("adapter", op, label, args)

    try:
        result = authority.fenced_file(warrant, run)
    except Exception as error:
        return _failure(warrant["home"], warrant["home"], "authority.adapter", row, error, bool(started), warrant)
    if not isinstance(result, dict):
        result = {"label": label, "result": result}
    outcome = None
    if op == "install":
        try:
            with open(path, "rb") as handle:
                after = plistlib.load(handle)
        except (OSError, ValueError):
            after = {}
        if after.get("ProgramArguments") != installed.get("ProgramArguments"):
            outcome = "uncertain"
        elif args.no_start:
            outcome = "confirmed"
    if outcome is None:
        if before is False:
            result["pid"], outcome = None, "uncertain"
        else:
            result["pid"] = _ready(label, before)
            outcome = "confirmed" if result["pid"] is not None else "uncertain"
    if not ledger_settle(warrant["home"], row["op_id"], outcome, pid=result.get("pid")):
        outcome, result["pending_op"] = "uncertain", row["op_id"]  # D14b
    authority.audit(warrant["home"], "authority.adapter",
                    dict(row, stage="outcome", outcome=outcome, pid=result.get("pid")))
    result.update(outcome=outcome, delegation=warrant["delegation"], op_id=row["op_id"])
    return result


# ---- M28 P1c-b adapter.install -------------------------------------------------------------------------------

ADAPTER_EXECUTABLES = ("/usr/bin/python3", "/Library/Developer/CommandLineTools/usr/bin/python3")  # Q1
ADAPTER_FLAGS = ("-u",)
CREDENTIAL_FLAGS = ("--credential-file", "--key-file")


def _key_dir():
    from .store import DEFAULT_CHANNEL_DIR

    return Path(DEFAULT_CHANNEL_DIR).expanduser()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sums(path):
    """``<64 hex>  <path>`` lines (shasum format) -> [(sha, path)]; anything else is refused by the caller."""
    out = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            found = re.fullmatch(r"([0-9a-f]{64})  \*?(.+)", line)
            if not found:
                raise ValueError("not a SHA256SUMS line: %r" % line[:80])
            out.append((found.group(1), found.group(2)))
    return out


def _package_files(top, module, cwd, refuse):
    """D17b: the code a delegated adapter imports: every *.py of the enclosing local package ``top`` (None: only
    ``module``), each resolved and required to stay under ``cwd`` (a symlink out of it is refused)."""
    found = {Path(module).resolve()}
    if top is not None:
        found |= {p.resolve() for p in Path(top).rglob("*.py") if "__pycache__" not in p.parts}
    for f in found if top is not None else ():
        if cwd != f and cwd not in f.parents:
            refuse("bad_argv", "adapter code %s resolves outside --cwd %s" % (f, cwd))
    return sorted(found)


def adapter_install_plan(home, args, state, refuse):
    """P1c-b: check everything about a delegated `adapter install --review` that does not depend on the delegation's
    scope, and return the plan {label, argv, cwd, keep, files, review, credential_files}. ``files`` are the resolved
    code files the scope's ``roots`` must contain (the module or script, and every *.py of a package)."""
    from . import authority

    label = args.label
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        refuse("bad_argv", "adapter install needs the adapter argv after --")
    # Q1: the interpreter, compared after resolve(); no other executable (third-party stays with David, D13)
    try:
        allowed = {Path(e).resolve() for e in ADAPTER_EXECUTABLES}
        exe = Path(argv[0]).resolve()
    except (OSError, RuntimeError):
        exe, allowed = None, set()
    if not os.path.isabs(argv[0]) or exe not in allowed:
        refuse("foreign_executable", "a delegated adapter runs only %s (after resolve()); %s stays with David"
               % (" or ".join(ADAPTER_EXECUTABLES), argv[0]))
    if not args.cwd:
        refuse("bad_argv", "a delegated adapter install names its WorkingDirectory with --cwd")
    try:
        cwd = Path(args.cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        cwd = None
    if cwd is None or not cwd.is_dir():
        refuse("bad_cwd", "--cwd is not a directory: %s" % args.cwd)
    rest = argv[1:]
    while rest and rest[0] in ADAPTER_FLAGS:
        rest = rest[1:]
    files = []
    if rest[:1] == ["-m"] and len(rest) >= 2 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*",
                                                              rest[1]):
        parts = rest[1].split(".")
        base = cwd.joinpath(*parts)
        if (base / "__init__.py").is_file():
            module = base / "__init__.py"
        elif base.with_suffix(".py").is_file():
            module = base.with_suffix(".py")
        else:
            refuse("bad_argv", "module %s is not found under --cwd %s" % (rest[1], cwd))
        # D17b: the enclosing local package is the outermost dotted prefix with an __init__.py; importing the module
        # runs that package's __init__.py and every parent's, so every *.py in it must be reviewed.
        top = next((cwd.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)
                    if (cwd.joinpath(*parts[:i]) / "__init__.py").is_file()), None)
        files = _package_files(top, module, cwd, refuse)
        walk = (top, module)
        anchor = cwd
    elif rest and not rest[0].startswith("-") and rest[0].endswith(".py"):
        script = Path(rest[0]) if os.path.isabs(rest[0]) else cwd / rest[0]
        if not script.is_file():
            refuse("bad_argv", "script %s does not exist" % script)
        script = script.resolve()
        # D17b: a script inside a package (its directory has __init__.py) brings in that package; walk up while the
        # parent is still a package and stays under --cwd.
        top = None
        if (script.parent / "__init__.py").is_file() and (script.parent == cwd or cwd in script.parent.parents):
            top = script.parent
            while top.parent != cwd and cwd in top.parent.parents and (top.parent / "__init__.py").is_file():
                top = top.parent
        files = _package_files(top, script, cwd, refuse)
        walk = (top, script)
        anchor = cwd if cwd in script.parents else script.parent
    else:
        refuse("bad_argv", "a delegated adapter argv is <python> [-u] (-m <module> | <script.py>) [args]; "
                           "-c, other interpreter flags and other forms stay owner-only")
    # credential files: stat only; keys are minted host-side, never read or created here (D13)
    creds = []
    tail = rest[2:] if rest[0] == "-m" else rest[1:]
    for i, item in enumerate(tail):
        value = None
        for flag in CREDENTIAL_FLAGS:
            if item == flag:
                value = tail[i + 1] if i + 1 < len(tail) else ""
            elif item.startswith(flag + "="):
                value = item[len(flag) + 1:]
        if value is None:
            continue
        try:
            key = Path(value).expanduser()
            info = key.lstat()
            real = key.resolve()
            keydir = _key_dir().resolve()
        except (OSError, RuntimeError):
            refuse("bad_credential_file", "credential file %r does not exist" % value)
        import stat as _stat
        if not _stat.S_ISREG(info.st_mode) or keydir not in real.parents or info.st_uid != os.getuid() \
                or (info.st_mode & 0o077):
            refuse("bad_credential_file", "credential file %s must be an existing 0600 regular file of this user "
                   "under %s; keys are minted host-side" % (value, keydir))
        creds.append(str(real))
    # Q3: the review is an accepted frozen sums file (company `policy adapter.reviews`) and the code matches it
    if not args.review:
        refuse("no_review", "a delegated adapter install needs --review <SHA256SUMS of a merged review>")
    try:
        review = Path(args.review).expanduser().resolve()
        review_sha = _sha256(review)
    except (OSError, RuntimeError) as error:
        refuse("bad_review", "cannot read --review %s (%s)" % (args.review, error))
    entry = next((r for r in authority.adapter_reviews(state) if r["sums_sha256"] == review_sha
                  and Path(r["sums_path"]).resolve() == review), None)
    if entry is None:
        refuse("review_not_accepted", "%s (sha %s) is not listed in the company `policy adapter.reviews`"
               % (review, review_sha[:12]))
    try:
        listed = _read_sums(review)
    except (OSError, ValueError) as error:
        refuse("bad_review", "%s is not a SHA256SUMS file (%s)" % (review, error))
    shas = {}
    for f in files:
        rel = str(f.relative_to(anchor)) if anchor in f.parents else f.name
        # deviation 3 (accepted): an exact cwd-relative entry wins; otherwise a listed path ending in "/" + rel
        hits = [sha for sha, path in listed if path == rel] or \
            [sha for sha, path in listed if path.endswith("/" + rel)]
        if not hits:
            refuse("unreviewed_file", "%s is not listed in %s" % (rel, review))
        shas[str(f)] = _sha256(f)
        if shas[str(f)] not in hits:
            refuse("review_mismatch", "%s differs from the reviewed sums in %s" % (rel, review))
    keep = {"always": True, "on-failure": "on-failure", "never": False}[args.keep_alive]
    return {"label": label, "argv": argv, "cwd": cwd, "keep": keep, "files": [str(f) for f in files],
            "file_shas": shas, "walk": [None if walk[0] is None else str(walk[0]), str(walk[1])],
            "review": {"path": str(review), "sha256": review_sha, "entry": entry["id"],
                       "review_ref": entry["review_ref"]}, "credential_files": creds}


def _recheck_review(actor_home, plan, refuse):
    """D17c: re-check, just before the plist is written, what the plan checked at plan time: the
    ``policy adapter.reviews`` entry is still active in company state, the sums file still has the accepted sha,
    and every planned code file still has the sha it had. Any change → refuse (nothing written). The window
    between this check and launchd exec'ing the code remains a documented limit."""
    from . import authority, roster

    review = plan["review"]
    state = roster.company_state_for_home(actor_home)
    if not any(r.get("id") == review["entry"] and r.get("sums_sha256") == review["sha256"]
               for r in authority.adapter_reviews(state)):
        refuse("review_not_accepted", "review entry %s was retracted before the install" % review["entry"])
    try:
        current = _sha256(review["path"])
    except OSError:
        current = None
    if current != review["sha256"]:
        refuse("review_mismatch", "%s changed before the install" % review["path"])
    # D17d: re-enumerate the enclosing package the same way the plan did; an added or removed file refuses
    top, module = plan["walk"]
    now_files = {str(f) for f in _package_files(None if top is None else Path(top), Path(module),
                                                 Path(plan["cwd"]), refuse)}
    if now_files != set(plan.get("file_shas") or {}):
        changed = sorted(now_files ^ set(plan.get("file_shas") or {}))
        refuse("review_mismatch", "package file set changed before the install: %s" % ", ".join(changed))
    for f, sha in (plan.get("file_shas") or {}).items():
        try:
            now = _sha256(f)
        except OSError:
            now = None
        if now != sha:
            refuse("review_mismatch", "%s changed before the install" % f)


def adapter_plan_within(plan, scope) -> bool:
    """adapter.install scope: the label is listed (or "*" in a root) and cwd + every code file sit under ``roots``."""
    from . import authority

    if not label_covered(plan["label"], scope.get("labels")):
        return False
    roots = scope.get("roots") or []
    return authority.path_under(plan["cwd"], roots) and all(authority.path_under(f, roots) for f in plan["files"])


def delegated_adapter_install(home, args, plan, warrant, refuse):
    """P1c-b: write a com.synthetics.adapter.* plist from a checked plan, under the D4 file fence, ledgered in the
    actor home (D14). An install over a loaded label reloads it (G1). Outcome: plist read-back (Label,
    ProgramArguments, WorkingDirectory) and, unless --no-start, a new live PID (D10b). Never retried."""
    import plistlib
    from . import authority
    from .service import _agents_directory

    label = plan["label"]
    agents = Path(args.launch_agents_dir).absolute() if getattr(args, "launch_agents_dir", None) else None
    if agents is not None and not _agents_dir_allowed(args.launch_agents_dir):
        refuse("agents_dir", "--launch-agents-dir must be the default or a temporary test directory")
    path = _agents_directory(agents) / (label + ".plist")
    row = dict(authority.receipt(warrant), op="install", label=label, plist=str(path), argv=plan["argv"],
               cwd=str(plan["cwd"]), files=list(plan["files"]), no_start=bool(args.no_start), review=plan["review"],
               action="adapter.install")
    before = service_pid(label)
    row["pid_before"] = pid_intent(before)
    row["op_id"] = ledger_begin(warrant["home"], dict(row, kind="adapter", home=str(Path(home).resolve())))
    authority.audit(warrant["home"], "authority.adapter", dict(row, stage="requested"))
    started = []

    def run():
        _recheck_review(warrant["home"], plan, refuse)  # D17c: inside the fence, before any effect
        started.append(True)
        written = write_adapter(home, label, plan["argv"], plan["keep"], agents, cwd=plan["cwd"])
        result = {"label": label, "plist": str(written), "argv": plan["argv"], "cwd": str(plan["cwd"]),
                  "review": plan["review"]}
        if not args.no_start:
            result["service"] = reload_if_loaded(written)
        return result

    try:
        result = authority.fenced_file(warrant, run)
    except Exception as error:
        return _failure(warrant["home"], warrant["home"], "authority.adapter", row, error, bool(started), warrant)
    try:
        with open(path, "rb") as handle:
            after = plistlib.load(handle)
    except (OSError, ValueError):
        after = {}
    if after.get("Label") != label or after.get("ProgramArguments") != plan["argv"] \
            or after.get("WorkingDirectory") != str(plan["cwd"]):
        outcome = "uncertain"
    elif args.no_start:
        outcome = "confirmed"
    elif before is False:
        result["pid"], outcome = None, "uncertain"
    else:
        result["pid"] = _ready(label, before)
        outcome = "confirmed" if result["pid"] is not None else "uncertain"
    if not ledger_settle(warrant["home"], row["op_id"], outcome, pid=result.get("pid")):
        outcome, result["pending_op"] = "uncertain", row["op_id"]
    authority.audit(warrant["home"], "authority.adapter",
                    dict(row, stage="outcome", outcome=outcome, pid=result.get("pid")))
    result.update(outcome=outcome, delegation=warrant["delegation"], op_id=row["op_id"])
    return result

"""Small operator-owned macOS launch-agent helpers.

Writing a plist does not load it. Launch agents run in the current user's GUI
login session; they do not keep a sleeping/offline Mac available. Callers should
use keep_alive for an idle transport, and leave it false for bounded model
workers so their repeated-failure stop remains effective.
"""

import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


MANAGED_MARKER = b"<!-- Managed by Synthetics launch-agent helper v1. -->"
LABEL_PATTERN = re.compile(r"com\.synthetics\.[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _label(value):
    if not isinstance(value, str) or len(value) > 128 or not LABEL_PATTERN.fullmatch(value):
        raise ValueError("Launch-agent label must use com.synthetics.<lowercase letters, digits and separated segments>.")
    return value


def _absolute_path(value, name):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("%s must be an absolute filesystem path." % name)
    path = Path(value)
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("%s must be an absolute filesystem path." % name)
    return path


def _agents_directory(value):
    return _absolute_path(value if value is not None else Path.home() / "Library" / "LaunchAgents",
                          "launch_agents_dir")


def _managed_plist(path):
    """Reject unrelated files and symlinks before updating/loading/removing."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise FileExistsError("Refusing a launch-agent file not owned as a regular file by the current user: %s" % path)
    data = path.read_bytes()
    try:
        configuration = plistlib.loads(data)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise FileExistsError("Refusing an invalid existing launch-agent plist: %s" % path) from exc
    if (MANAGED_MARKER not in data or not isinstance(configuration, dict)
            or configuration.get("Label") != path.stem):
        raise FileExistsError("Refusing an unrelated launch-agent plist: %s" % path)
    _label(configuration["Label"])
    return configuration


def write_launch_agent(label, argv, working_directory, logs_directory, launch_agents_dir=None,
                       keep_alive=False, environment=None):
    """Write an owned, private plist atomically; do not install or start it.

    argv is an argument array, never shell text. The executable and all directory
    arguments are absolute. Environment is explicit: a minimal PATH is supplied
    unless overridden, and no ambient secrets/authentication variables are copied.
    A caller running Claude should explicitly supply its legitimate HOME, PATH
    and the module's PYTHONPATH as needed.
    """
    label = _label(label)
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)):
        raise ValueError("argv must be a nonempty list or tuple of nonempty strings, not shell text.")
    _absolute_path(argv[0], "argv[0]")
    working = _absolute_path(working_directory, "working_directory")
    logs = _absolute_path(logs_directory, "logs_directory")
    agents = _agents_directory(launch_agents_dir)
    if not working.is_dir():
        raise ValueError("working_directory must be an existing directory.")
    if logs == logs.parent:
        raise ValueError("logs_directory must not be a filesystem root.")
    if not isinstance(keep_alive, bool) and keep_alive != "on-failure":
        raise ValueError("keep_alive must be a boolean or \"on-failure\".")
    if environment is not None and not isinstance(environment, dict):
        raise ValueError("environment must be a dictionary of strings.")
    env = {"PATH": DEFAULT_PATH}
    for key, value in (environment or {}).items():
        if (not isinstance(key, str) or not key or "=" in key or "\x00" in key
                or not isinstance(value, str) or "\x00" in value):
            raise ValueError("environment must contain valid string names and values.")
        env[key] = value
    for path, name in ((logs, "logs_directory"), (agents, "launch_agents_dir")):
        if path.exists() and not path.is_dir():
            raise ValueError("%s must be a directory." % name)
    target = agents / (label + ".plist")
    try:
        _managed_plist(target)
    except FileNotFoundError:
        pass
    configuration = dict(Label=label, ProgramArguments=list(argv), WorkingDirectory=str(working),
                         StandardOutPath=str(logs / (label + ".stdout.log")),
                         StandardErrorPath=str(logs / (label + ".stderr.log")),
                         EnvironmentVariables=env, RunAtLoad=True, Umask=0o077,
                         # "on-failure": restart crashes only; a deliberate stop exits 0 and stays stopped.
                         KeepAlive={"SuccessfulExit": False} if keep_alive == "on-failure" else keep_alive)
    if keep_alive:
        configuration["ThrottleInterval"] = 30
    data = plistlib.dumps(configuration, fmt=plistlib.FMT_XML, sort_keys=True)
    data = data.replace(b"<plist version", MANAGED_MARKER + b"\n<plist version", 1)
    # Validate everything before creating directories or altering an existing file.
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs.chmod(0o700)
    agents.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="." + label + ".", suffix=".tmp", dir=str(agents))
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, str(target))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def _launchctl(arguments):
    if sys.platform != "darwin":
        raise RuntimeError("Starting or stopping a launch agent requires macOS launchctl.")
    return subprocess.run(["/bin/launchctl"] + arguments, check=True, capture_output=True, text=True)


def bootstrap_launch_agent(plist_path):
    """Load a managed plist into the current user's GUI login domain."""
    path = _absolute_path(plist_path, "plist_path")
    configuration = _managed_plist(path)
    domain = "gui/%s" % os.getuid()
    _launchctl(["bootstrap", domain, str(path)])
    return dict(label=configuration["Label"], domain=domain, plist=str(path))


def stop_launch_agent(label):
    """Unload only this Synthetics label in the current user's GUI domain."""
    label = _label(label)
    target = "gui/%s/%s" % (os.getuid(), label)
    _launchctl(["bootout", target])
    return dict(label=label, target=target, stopped=True)


def start_launch_agent(label):
    """Explicitly start a loaded job, without killing an already running one."""
    label = _label(label)
    target = "gui/%s/%s" % (os.getuid(), label)
    _launchctl(["kickstart", target])
    return dict(label=label, target=target, start_requested=True)


def restart_launch_agent(label):
    """Ask launchd to replace a loaded job in one request.

    launchd owns the transition after accepting the request, so the requesting
    process does not need to survive stopping its own service. This explicit
    action leaves the worker's bounded failure policy unchanged.
    """
    label = _label(label)
    target = "gui/%s/%s" % (os.getuid(), label)
    _launchctl(["kickstart", "-k", target])
    return dict(label=label, target=target, restart_requested=True)


def request_restart(label, wait_seconds=10.0):
    """A worker asks launchd to replace its own job (M19).

    Unlike restart_launch_agent, launchctl runs in its own session: launchd kills a
    job's whole process group when the job exits, and subprocess.run would kill the
    client if our SIGTERM arrives while waiting. The request belongs to launchd once
    sent, so this never kills launchctl; a SIGTERM (KeyboardInterrupt) simply ends
    the wait while launchd completes the replacement.
    """
    label = _label(label)
    if sys.platform != "darwin":
        raise RuntimeError("Restarting a launch agent requires macOS launchctl.")
    target = "gui/%s/%s" % (os.getuid(), label)
    process = subprocess.Popen(["/bin/launchctl", "kickstart", "-k", target], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    try:
        _, error = process.communicate(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        return dict(label=label, target=target, restart_requested=True, launchctl="still running")
    if process.returncode != 0:
        raise RuntimeError("launchctl kickstart -k %s exited %d: %s" % (target, process.returncode,
                                                                       (error or b"").decode(errors="replace").strip()[:300]))
    return dict(label=label, target=target, restart_requested=True)


def remove_launch_agent(label, launch_agents_dir=None, stop=True):
    """Remove a verified managed plist after unloading it.

    A failed bootout retains the plist and reports the error. Use stop=False
    explicitly to remove a plist that was written but never loaded.
    """
    label = _label(label)
    if not isinstance(stop, bool):
        raise ValueError("stop must be a boolean.")
    path = _agents_directory(launch_agents_dir) / (label + ".plist")
    try:
        _managed_plist(path)
    except FileNotFoundError:
        return False
    if stop:
        stop_launch_agent(label)
    path.unlink()
    return True

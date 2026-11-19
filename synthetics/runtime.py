"""Persisted worker runtime settings (M19).

A stable launch command carries a *baseline* configuration in argv. A worker
started with --runtime-config reads <home>/runtime.json, falling back to
runtime.last-good.json and finally to the baseline. Loading never raises: a
broken file must not stop a worker from starting. Every fallback is reported in
the returned record so the caller can audit and notify.
"""

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path


SCHEMA = 1
CONFIG_FILE = "runtime.json"
LAST_GOOD_FILE = "runtime.last-good.json"
FIELDS = ("claude", "model", "budget", "timeout", "poll", "retry", "daily_limit", "limit_notify", "limit_allow")
METADATA = ("version", "written_by", "request_id", "written_at")
# E4 (Ledgerline): optional settings. A config without them stays valid and keeps its digest; readers use
# setting(). stream_output false restores the pre-E4 `--output-format json` session path exactly (Codex C1).
OPTIONAL = {"stream_output": True, "runs_keep_days": 30.0, "runs_max_mb": 200.0}
OPTIONAL_BOUNDS = {"runs_keep_days": (1.0, 3650.0), "runs_max_mb": (1.0, 100000.0)}


def setting(config, name):
    """An optional setting's value in ``config``, or its default."""
    value = (config or {}).get(name) if isinstance(config, dict) else None
    return OPTIONAL[name] if value is None else value


def _optional(name, value):
    if name == "stream_output":
        if not isinstance(value, bool):
            raise ValueError("stream_output must be true or false.")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number." % name)
    low, high = OPTIONAL_BOUNDS[name]
    if value < low or value > high:
        raise ValueError("%s must be at least %s and at most %s." % (name, low, high))
    return float(value)


def _present(config):
    """The settings of ``config``: every FIELDS entry plus the optional ones it names."""
    names = list(FIELDS) + [name for name in OPTIONAL if name in config]
    return {name: config[name] for name in names}
MODEL_PATTERN = re.compile(r"claude-[a-z0-9.-]{1,57}\Z")
RECIPIENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
BOUNDS = {"budget": (0.0, 100.0, False), "timeout": (60.0, 3600.0, True), "poll": (0.5, 60.0, True),
          "retry": (5.0, 3600.0, True), "daily_limit": (0.0, 500.0, True)}
MAX_ALLOW = 8


def _number(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number." % name)
    low, high, low_inclusive = BOUNDS[name]
    if value > high or value < low or (value == low and not low_inclusive):
        raise ValueError("%s must be %s %s and at most %s." % (name, "at least" if low_inclusive else "above",
                                                                 low, high))
    return float(value)


def validate(config, allowed_models=None, allowed_claude=None):
    """Return a normalized copy of a complete config, or raise ValueError naming the field.

    allowed_models / allowed_claude: when given, the model must be listed and the claude
    executable's realpath must equal the realpath of a listed path (M19 R1, R2).
    """
    if not isinstance(config, dict):
        raise ValueError("Runtime config must be a JSON object.")
    unknown = set(config) - set(FIELDS) - set(OPTIONAL) - set(METADATA)
    if unknown:
        raise ValueError("Unsupported runtime fields: %s." % ", ".join(sorted(unknown)))
    missing = [name for name in FIELDS if name not in config]
    if missing:
        raise ValueError("Runtime config lacks: %s." % ", ".join(missing))
    if "version" in config and config["version"] != SCHEMA:
        raise ValueError("Runtime config version must be %d." % SCHEMA)
    result = {}
    claude = config["claude"]
    if not isinstance(claude, str) or "\x00" in claude or not os.path.isabs(claude):
        raise ValueError("claude must be an absolute path.")
    if not os.path.isfile(claude) or not os.access(claude, os.X_OK):
        raise ValueError("claude must be an existing executable file: %s" % claude)
    if allowed_claude is not None:
        permitted = {os.path.realpath(path) for path in allowed_claude}
        if os.path.realpath(claude) not in permitted:
            raise ValueError("claude %s is not an allowed executable: %s." % (claude, ", ".join(sorted(allowed_claude))))
    result["claude"] = claude
    model = config["model"]
    if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model):
        raise ValueError("model must look like claude-<name>.")
    if allowed_models is not None and model not in allowed_models:
        raise ValueError("model %s is not in the allowed models: %s." % (model, ", ".join(sorted(allowed_models))))
    result["model"] = model
    for name in ("budget", "timeout", "poll", "retry", "daily_limit"):
        result[name] = _number(name, config[name])
    notify = config["limit_notify"]
    if notify is not None and (not isinstance(notify, str) or not RECIPIENT_PATTERN.fullmatch(notify)):
        raise ValueError("limit_notify must be null or a recipient ID.")
    result["limit_notify"] = notify
    allow = config["limit_allow"]
    if (not isinstance(allow, list) or len(allow) > MAX_ALLOW
            or any(not isinstance(item, str) or not RECIPIENT_PATTERN.fullmatch(item) for item in allow)):
        raise ValueError("limit_allow must be a list of at most %d sender IDs." % MAX_ALLOW)
    result["limit_allow"] = sorted(set(allow))
    for name in OPTIONAL:
        if name in config:
            result[name] = _optional(name, config[name])
    return result


def digest(config):
    """Stable sha256 over the settings only (metadata excluded)."""
    settings = _present(config)
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_atomic(path, config, **metadata):
    """Write a private JSON file with tmp + fsync + replace. Returns the settings digest."""
    path = Path(path)
    value = _present(config)
    value.update(version=SCHEMA, **{k: v for k, v in metadata.items() if k in METADATA and v is not None})
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return digest(value)


def _read(path, allowed_models, allowed_claude=None):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError) as error:
        return None, "unreadable: %s" % error
    try:
        config = validate(raw, allowed_models, allowed_claude)
    except ValueError as error:
        return None, "invalid: %s" % error
    config["request_id"] = raw.get("request_id") if isinstance(raw.get("request_id"), str) else None
    return config, None


def load(home, baseline, allowed_models=None, allowed_claude=None):
    """Choose the effective config: runtime.json, then last-good, then baseline. Never raises.

    Returns {effective, source, sha, request_id, problems[]}. A missing runtime.json
    on first start is seeded from the baseline (both files), which is not a problem.
    """
    home = Path(home)
    problems = []
    current, error = _read(home / CONFIG_FILE, allowed_models, allowed_claude)
    if current is not None:
        request_id = current.pop("request_id")
        return dict(effective=current, source="file", sha=digest(current), request_id=request_id, problems=problems)
    seed = error == "absent"
    if not seed:
        problems.append("%s %s" % (CONFIG_FILE, error))
    good, good_error = _read(home / LAST_GOOD_FILE, allowed_models, allowed_claude)
    if good is not None:
        good.pop("request_id")
        if seed:
            # runtime.json vanished but last-good exists: restore it rather than reseed.
            problems.append("%s absent; restored from %s" % (CONFIG_FILE, LAST_GOOD_FILE))
            _safe_write(home / CONFIG_FILE, good, problems, written_by="fallback")
        return dict(effective=good, source="last-good", sha=digest(good), request_id=None, problems=problems)
    if good_error != "absent":
        problems.append("%s %s" % (LAST_GOOD_FILE, good_error))
    try:
        base = validate(dict(baseline), allowed_models, allowed_claude)
    except ValueError as error:
        # The launch argv itself is the operator's; run it as given but say so loudly.
        problems.append("baseline invalid: %s" % error)
        base = {name: baseline[name] for name in FIELDS}
        return dict(effective=base, source="baseline-unvalidated", sha=digest(base), request_id=None,
                    problems=problems)
    if seed:
        _safe_write(home / CONFIG_FILE, base, problems, written_by="seed")
        _safe_write(home / LAST_GOOD_FILE, base, problems, written_by="seed")
    return dict(effective=base, source="baseline", sha=digest(base), request_id=None, problems=problems)


def _safe_write(path, config, problems, **metadata):
    try:
        write_atomic(path, config, **metadata)
    except OSError as error:
        problems.append("could not write %s: %s" % (Path(path).name, error))


# ---------------------------------------------------------------------------
# Requests: who may change what (M19 R3), and the policy the worker publishes.

STATE_FILE = "runtime.state.json"
# What this engine honours, published in runtime.state.json so delegated ops can tell an engine started before
# a feature from a current one (M28 D12): "worker_hold" = run_begin refuses while a worker.manage hold is set.
CAPABILITIES = ("worker_hold",)
SELF_SERVICE = ("claude", "model", "budget", "timeout", "poll", "retry", "daily_limit")
NUMERIC = ("budget", "timeout", "poll", "retry", "daily_limit")
LABEL_PATTERN = re.compile(r"com\.synthetics\.worker\.[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}\Z")


def parse_settings(settings):
    """Check the shape of a partial settings change. Values are validated after merging."""
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object.")
    unknown = set(settings) - set(FIELDS) - set(OPTIONAL)
    if unknown:
        raise ValueError("Unsupported runtime settings: %s. Allowed: %s." % (", ".join(sorted(unknown)),
                                                                               ", ".join(FIELDS + tuple(OPTIONAL))))
    result = {}
    for name, value in settings.items():
        if name in NUMERIC:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("%s must be a number." % name)
            value = float(value)
        result[name] = value
    return result


def parse_assignments(pairs):
    """CLI --set name=value pairs into a settings dict (limit_allow comma-separated, limit_notify none)."""
    settings = {}
    for pair in pairs or []:
        name, separator, value = pair.partition("=")
        name = name.strip().replace("-", "_")
        if not separator or not name:
            raise ValueError("--set needs name=value, got %r." % pair)
        if name in NUMERIC:
            try:
                settings[name] = float(value)
            except ValueError:
                raise ValueError("%s must be a number." % name)
        elif name == "limit_allow":
            settings[name] = [item.strip() for item in value.split(",") if item.strip()]
        elif name == "limit_notify":
            settings[name] = None if value.strip().lower() in ("", "none", "null") else value.strip()
        elif name == "stream_output":
            flag = value.strip().lower()
            if flag not in ("true", "false"):
                raise ValueError("stream_output must be true or false.")
            settings[name] = flag == "true"
        elif name in OPTIONAL:
            try:
                settings[name] = float(value)
            except ValueError:
                raise ValueError("%s must be a number." % name)
        else:
            settings[name] = value
    return parse_settings(settings)


def _limit_rank(value):
    # 0 disables the daily limit, which is the most permissive setting.
    return math.inf if not value else value


def check_self_service(settings, effective, baseline):
    """Raise unless an employee may make this change to its own runtime (M19 R3)."""
    for name, value in settings.items():
        if name not in SELF_SERVICE:
            raise ValueError("%s is a manager or operator setting; it changes only through "
                             "`synthetics runtime request --by`." % name)
    budget = settings.get("budget")
    if budget is not None and budget != effective["budget"] and budget > baseline["budget"]:
        raise ValueError("budget may be at most the baseline $%s; raising it is a manager or operator action."
                         % baseline["budget"])
    limit = settings.get("daily_limit")
    if limit is not None and _limit_rank(limit) > _limit_rank(effective["daily_limit"]):
        raise ValueError("daily_limit may only be lowered by the employee (current %s; 0 disables it)."
                         % effective["daily_limit"])


def merge(effective, settings):
    merged = _present(effective)
    merged.update(settings)
    return merged


def write_json(path, value):
    """Atomic private JSON write for files other than configs."""
    path = Path(path)
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_state(home):
    """The running worker's published runtime state, or None when it does not use a runtime config."""
    try:
        value = json.loads((Path(home) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("policy"), dict) else None


# ---------------------------------------------------------------------------
# Worker-side lifecycle: reconcile at start, execute at idle, decide probation.

class SelfRestart(Exception):
    """Exit-code fallback (mode "exit"): serve exits EXIT_RESTART and KeepAlive{SuccessfulExit:false} relaunches."""


EXIT_RESTART = 75


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _restore(path, previous):
    """Put back the exact bytes runtime.json had before a failed restart (None: it did not exist)."""
    path = Path(path)
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(previous)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class Controller:
    """One worker's runtime settings. The engine calls start(), idle() and after_run().

    restarter(label) asks launchd to replace this job (service.request_restart); it is
    injected so tests never touch launchd. mode "kickstart" (default) or "exit" (R5 fallback).
    """

    GRACE_SECONDS = 60.0

    def __init__(self, home, baseline, allowed_models=None, allowed_claude=None, label=None, restarter=None,
                 mode="kickstart", argv=None, pid=None, clock=None):
        import time
        self.home = Path(home).resolve()
        self.baseline = {name: baseline[name] for name in FIELDS}
        self.allowed_models = sorted(set(allowed_models or [self.baseline["model"]]))
        self.allowed_claude = sorted(set(allowed_claude or [self.baseline["claude"]]))
        self.label = label
        self.restarter = restarter
        if mode not in ("kickstart", "exit"):
            raise ValueError("restart mode must be kickstart or exit.")
        self.mode = mode
        self.argv = list(argv or [])
        self.pid = pid or os.getpid()
        self.clock = clock or time.monotonic
        self.loaded = None
        self.state = None
        self.probation = None
        self.issued = None

    # -- start ---------------------------------------------------------------
    def load(self):
        self.loaded = load(self.home, self.baseline, self.allowed_models, self.allowed_claude)
        return self.loaded

    @property
    def effective(self):
        return self.loaded["effective"]

    def start(self, store):
        """Reconcile request records with what actually loaded, then publish and audit the runtime."""
        if self.loaded is None:
            self.load()
        loaded = self.loaded
        for record in store.runtime_requests(50):
            if record["status"] not in ("restarting", "applied_unconfirmed"):
                continue
            matches = (loaded["source"] == "file" and loaded["sha"] == record.get("proposed_sha")
                       and loaded["request_id"] == record["id"])
            if matches and record.get("revert"):
                record.update(status="applied", outcome="reverted to last-good settings", applied_pid=self.pid)
                store.runtime_record(record, "applied")
            elif matches:
                record.update(status="applied_unconfirmed", applied_pid=self.pid, applied_at=_now(),
                              outcome="restarted; first session is probation")
                store.runtime_record(record, "applied_unconfirmed")
                self.probation = record["id"]
            else:
                if loaded["source"] != "file":
                    reason = "fell back to %s settings: %s" % (loaded["source"], "; ".join(loaded["problems"]))
                else:
                    reason = "superseded_on_disk: runtime.json no longer holds this request's settings"
                record.update(status="failed", outcome=reason)
                store.runtime_record(record, "failed")
                self._notify(store, record, "failed", "Runtime request %s failed after restart: %s" % (record["id"], reason))
        self.state = dict(policy=dict(baseline=self.baseline, allowed_models=self.allowed_models,
                                      allowed_claude=self.allowed_claude),
                          effective=loaded["effective"], source=loaded["source"], sha=loaded["sha"],
                          request_id=loaded["request_id"], problems=loaded["problems"], pid=self.pid,
                          label=self.label, argv=self.argv, started_at=_now(), probation=self.probation,
                          restart_mode=self.mode, capabilities=list(CAPABILITIES))
        self._publish()
        store.runtime_audit("started", "worker-%d" % self.pid, self.state)
        if loaded["problems"]:
            key = hashlib.sha256(json.dumps([loaded["source"], loaded["problems"]]).encode()).hexdigest()[:16]
            store.runtime_audit("fallback", "worker-%d" % self.pid, dict(source=loaded["source"], problems=loaded["problems"]))
            self._send(store, self.effective.get("limit_notify"), "runtime-fallback-%s" % key,
                       "Worker %s started on %s settings because: %s. Inspect %s." % (
                           self.label or self.home, loaded["source"], "; ".join(loaded["problems"]), self.home / CONFIG_FILE))
        return self.state

    def _publish(self):
        try:
            write_json(self.home / STATE_FILE, self.state)
        except OSError:
            pass  # Context and requests then report "not started with --runtime-config"; audit still has it.

    # -- idle ----------------------------------------------------------------
    def idle(self, store, failure_paused=False):
        """Carry out a pending request. True means: do not start sessions (a restart is under way)."""
        if self.issued is not None:
            started, record, previous = self.issued
            if self.clock() - started < self.GRACE_SECONDS:
                return True
            self.issued = None
            _restore(self.home / CONFIG_FILE, previous)
            record.update(status="failed", outcome="launchd accepted the restart but did not replace the worker "
                                                    "within %d seconds; previous settings restored" % self.GRACE_SECONDS)
            store.runtime_record(record, "failed")
            self._notify(store, record, "failed", "Runtime request %s failed: %s." % (record["id"], record["outcome"]))
            return False
        pending = [r for r in store.runtime_requests(50) if r["status"] == "requested"]
        if not pending:
            return False
        record = pending[0]
        try:
            if failure_paused and not record.get("revert"):
                raise ValueError("paused_recent_failures: the worker is paused after repeated failed sessions; "
                                 "an operator must run `limit resume` first")
            if record.get("origin") == "self":
                check_self_service(record["settings"], self.effective, self.baseline)
            proposed = validate(merge(self.effective, record["settings"]), self.allowed_models, self.allowed_claude)
            if not self.label or not LABEL_PATTERN.fullmatch(self.label):
                raise ValueError("not launchd-managed: XPC_SERVICE_NAME is %r" % (self.label,))
            if self.mode == "kickstart" and self.restarter is None:
                raise ValueError("no restarter configured")
        except ValueError as error:
            record.update(status="failed", outcome=str(error))
            store.runtime_record(record, "failed")
            self._notify(store, record, "failed", "Runtime request %s was refused: %s." % (record["id"], error))
            return False
        path = self.home / CONFIG_FILE
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            previous = None
        write_atomic(path, proposed, written_by=record.get("requested_by"), request_id=record["id"], written_at=_now())
        last = store.run_list(1)
        record.update(status="restarting", proposed=proposed, proposed_sha=digest(proposed), old_sha=self.loaded["sha"],
                      label=self.label, pid=self.pid, mode=self.mode,
                      checkpoint=dict(last_run=last[0]["id"] if last else None, pending_events=store.event_pending_count()))
        # Committed before launchd is asked, so the replacement can reconcile it.
        store.runtime_record(record, "restarting")
        if self.mode == "exit":
            raise SelfRestart(record["id"])
        try:
            self.restarter(self.label)
        except Exception as error:
            _restore(path, previous)
            record.update(status="failed", outcome="restart request to launchd failed: %s" % error)
            store.runtime_record(record, "failed")
            self._notify(store, record, "failed", "Runtime request %s failed: %s. Previous settings kept."
                         % (record["id"], record["outcome"]))
            return False
        self.issued = (self.clock(), record, previous)
        return True

    # -- probation -------------------------------------------------------------
    def after_run(self, store, run_id, error=None):
        """The first finished or failed session under a new config decides it (M19 §2.5, R6)."""
        if self.probation is None:
            return
        record = next((r for r in store.runtime_requests(50) if r["id"] == self.probation), None)
        self.probation = None
        self.state["probation"] = None
        self._publish()
        if record is None or record["status"] != "applied_unconfirmed":
            return
        if error is None:
            _safe_write(self.home / LAST_GOOD_FILE, self.effective, [], written_by="probation", request_id=record["id"],
                        written_at=_now())
            record.update(status="applied", outcome="session %s finished under the new settings" % run_id,
                          confirmed_by_run=run_id)
            store.runtime_record(record, "applied")
            self._notify(store, record, "applied", "Runtime request %s applied: %s." % (record["id"], record["outcome"]),
                         requester_only=True)
            return
        good, _ = _read(self.home / LAST_GOOD_FILE, self.allowed_models, self.allowed_claude)
        if good is not None:
            good.pop("request_id")
            source = LAST_GOOD_FILE
        else:
            good, source = validate(dict(self.baseline)), "baseline"
        revert = dict(id="runtime-revert-%s" % record["id"][len("runtime-"):], status="requested", origin="revert",
                      requested_by="worker", run_id=None, reason="revert after failed probation of %s" % record["id"],
                      settings=_present(good), proposed=good, proposed_sha=digest(good),
                      revert=True, created_at=_now())
        # Revert the file at once: even if the restart cannot run, the next start loads good settings.
        write_atomic(self.home / CONFIG_FILE, good, written_by="revert", request_id=revert["id"], written_at=_now())
        record.update(status="failed", outcome="probation session %s failed: %s; reverting to %s"
                      % (run_id, str(error)[:300], source), failed_run=run_id)
        store.runtime_record(record, "failed")
        store.runtime_record(revert, "requested")
        self._notify(store, record, "failed", "Runtime request %s failed: %s." % (record["id"], record["outcome"]))

    # -- notices -----------------------------------------------------------------
    def _notify(self, store, record, status, body, requester_only=False):
        key = "runtime-%s-%s" % (record["id"], status)
        targets = []
        if not requester_only:
            targets.append(self.effective.get("limit_notify"))
        if record.get("origin") in ("operator", "delegated"):  # M28: the delegate hears the outcome too
            targets.append(record.get("requested_by"))
        for target in dict.fromkeys(t for t in targets if t):
            if store.routed_by(target) == []:
                # Backlog 22: no channel routes this address (e.g. requested_by "operator"), so a
                # queued notice would sit pending forever. The request record already holds the
                # outcome; leave an audit row instead of an undeliverable message.
                store.runtime_audit("notice_unroutable", record["id"], {"to": target, "status": status})
                continue
            self._send(store, target, "%s-%s" % (key, target), body)

    @staticmethod
    def _send(store, to, key, body):
        if not to:
            return
        try:
            store.message_send(to, body, key=key)
        except ValueError:
            pass  # Already queued under this key.

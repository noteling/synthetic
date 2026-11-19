"""Event-driven work sessions. No model calls are made while the employee is idle."""

import fcntl
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import board
from . import capture
from . import sharedstatus
from .store import Store, WorkerHeld


class WorkerBusy(RuntimeError):
    pass


class WorkerStopped(RuntimeError):
    """A deliberate stop after repeated failed sessions. serve exits 0 so launchd does not restart it."""


LIMIT_FILE = "usage_limit.json"
FAILURE_WINDOW = 3


def utc_day(moment=None):
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def read_limit_override(home):
    """Operator overrides in <home>/usage_limit.json. Missing or unreadable means none."""
    try:
        value = json.loads((Path(home) / LIMIT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_limit_override(home, **fields):
    path = Path(home).expanduser().resolve() / LIMIT_FILE
    value = read_limit_override(path.parent)
    value.update(fields)
    value["set_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return value


def failure_streak(runs, window=FAILURE_WINDOW):
    """Latest run id when the newest `window` runs all failed or were interrupted, else None."""
    recent = runs[:window]
    if len(recent) < window or any(run.get("status") not in ("failed", "interrupted") for run in recent):
        return None
    return recent[0]["id"]


class WorkerLock:
    """Kernel-owned lock: released on exit, including a killed process."""

    def __init__(self, home):
        self.home = Path(home).resolve()
        self.handle = None

    def __enter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.handle = (self.home / "worker.lock").open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise WorkerBusy("This employee already has a running worker.")
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *args):
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def worker_status(home):
    path = Path(home).expanduser().resolve() / "worker.lock"
    try:
        handle = path.open("r")
    except FileNotFoundError:
        return {"running": False, "pid": None}
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            content = handle.read().strip()
            return {"running": True, "pid": int(content) if content.isdigit() else None}
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return {"running": False, "pid": None}


class Engine:
    """daily_limit: soft USD cap per UTC day checked at admission (None/0 disables).
    limit_notify: recipient for one pause notice per day/limit. limit_allow: senders
    whose authenticated Telegram or desktop-chat messages are still admitted during a daily-limit pause.
    """

    def __init__(self, home, brain, on_update=None, daily_limit=None, limit_notify=None, limit_allow=(), runtime=None):
        self.home = Path(home).resolve()
        self.brain = brain
        self.on_update = on_update or (lambda value: None)
        self.daily_limit = float(daily_limit) if daily_limit else None
        if self.daily_limit is not None and self.daily_limit < 0:
            raise ValueError("The daily limit cannot be negative.")
        self.limit_notify = limit_notify or None
        self.limit_allow = frozenset(limit_allow or ())
        self.failure_pause = None  # latest failed run id when serve started in a failure streak
        self._last_pause = None
        self.runtime = runtime  # M19 runtime.Controller, or None for a fixed argv configuration
        self.retention = {"keep_days": 30.0, "max_mb": 200.0}  # E4 runs/ retention (runtime.json runs_keep_days/_max_mb)

    def apply_runtime(self, effective):
        """Use the effective persisted settings for brain, limits and polling."""
        self.brain.executable = str(effective["claude"])
        self.brain.model = effective["model"]
        self.brain.timeout = float(effective["timeout"])
        self.brain.budget = float(effective["budget"])
        from . import runtime as runtime_settings
        self.brain.stream_output = bool(runtime_settings.setting(effective, "stream_output"))  # E4 C1 switch
        self.retention = {"keep_days": float(runtime_settings.setting(effective, "runs_keep_days")),
                          "max_mb": float(runtime_settings.setting(effective, "runs_max_mb"))}
        self.daily_limit = float(effective["daily_limit"]) or None
        self.limit_notify = effective["limit_notify"] or None
        self.limit_allow = frozenset(effective["limit_allow"] or ())

    def pause_state(self, store):
        """Why admission is paused, or None. Re-read on every poll so operator overrides apply at once."""
        override = read_limit_override(self.home)
        if self.failure_pause and override.get("resume_after_run") != self.failure_pause:
            return {"reason": "recent_failures", "run_id": self.failure_pause}
        self.failure_pause = None
        today = utc_day()
        limit = self.daily_limit
        if override.get("date") == today and isinstance(override.get("limit_usd"), (int, float)):
            limit = float(override["limit_usd"]) or None
        if not limit:
            return None
        usage = store.usage(day=today, days=1)["today"]
        if usage["cost_usd"] >= limit:
            return {"reason": "daily_limit", "day": today, "limit_usd": limit, "cost_usd": usage["cost_usd"],
                    "unknown_cost_runs": usage["unknown_cost_runs"]}
        return None

    # M25 §4: which channels and senders may pass a limit pause is the home's humans.json
    # limit_bypass, intersected with runtime limit_allow, and always requires channel auth (M22).
    # Operator notices never bypass (C1).
    def _admissible_during_limit(self, event, cfg):
        return capture.may_bypass_limit(event, cfg, self.limit_allow)

    def _notify_pause(self, store, pause):
        if pause != self._last_pause:
            self._last_pause = pause
            self.on_update(dict(pause, type="paused"))
            sharedstatus.write(store, paused=pause)  # VIS C1: Lookout shows the pause
        if not self.limit_notify:
            return
        if pause["reason"] == "daily_limit":
            key = "daily-soft-limit-%s-%s" % (pause["day"], pause["limit_usd"])
            body = ("Daily soft limit reached for %s (UTC): known spend $%.2f of $%.2f; %d run(s) of unknown cost. "
                    "New sessions are paused until UTC midnight. To continue today: synthetics --home %s limit raise USD."
                    % (pause["day"], pause["cost_usd"], pause["limit_usd"], pause["unknown_cost_runs"], self.home))
        else:
            key = "worker-failure-pause-%s" % pause["run_id"]
            body = ("Worker restarted after its %d most recent sessions failed or were interrupted (latest %s). "
                    "It is paused and will not call the model until an operator inspects the runs and runs: "
                    "synthetics --home %s limit resume." % (FAILURE_WINDOW, pause["run_id"], self.home))
        try:
            store.message_send(self.limit_notify, body, key=key)
        except ValueError:
            pass  # Already queued under this key (content differed only in live spend).

    def _probation(self, store, run_id, error):
        try:
            self.runtime.after_run(store, run_id, error=error)
        except Exception as problem:  # Never mask the run's own outcome; stay visible.
            self.on_update({"type": "runtime_error", "run_id": run_id, "error": "%s: %s" % (type(problem).__name__, problem)})
            try:
                store.runtime_audit("error", run_id, {"error": str(problem)})
            except Exception:
                pass

    def _step(self):
        with Store(self.home) as store:
            store.identity()  # A worker must have been provisioned.
            store.due_events()
            if capture.has_pending(store):
                # M21: retry project captures that failed at feed time (backoff 60 s, never dropped).
                try:
                    capture.capture_drain(store, backoff=capture.RETRY_BACKOFF)
                except Exception:  # noqa: BLE001 - rows stay pending and visible in the snapshot
                    pass
            if board.publish_pending(store):
                # M27 (iv): project published works into their project backlog (refusals -> publish_failing).
                try:
                    board.publish_drain(store, backoff=board.RETRY_BACKOFF)
                except Exception:  # noqa: BLE001 - dirty flag stays; retried next step
                    pass
            try:
                # M27 (iv): wake only for posts that mention me, reply to my post or ref my published work.
                board.scan(store)
            except Exception:  # noqa: BLE001 - the cursor didn't move; retried next step
                pass
            if not store.event_admissible(limit=1) and not store.runnable_work():
                return None
            if store.worker_hold() is not None:
                return None  # M28 D10c: a delegated worker op holds the idle boundary
            pause = self.pause_state(store)
            if pause is None:
                if self._last_pause is not None:
                    sharedstatus.write(store, paused=None)  # VIS: the pause ended
                self._last_pause = None
                try:
                    run = store.run_begin()
                except WorkerHeld:
                    return None
            else:
                self._notify_pause(store, pause)
                allowed = []
                if pause["reason"] == "daily_limit":
                    try:
                        humans_cfg = capture.humans(store)
                    except (OSError, ValueError) as exc:  # malformed humans.json: nobody bypasses
                        print("synthetics: humans.json unreadable, no limit bypass: %s" % exc, file=sys.stderr)
                        humans_cfg = capture.empty_humans()
                    # Issue #6: scan every pending event, not the 50 oldest, so held peer traffic can't hide an
                    # admissible input; at most one ordinary batch (50) is admitted.
                    allowed = [e["id"] for e in store.event_pending_select(
                        lambda e: not store.admission_held(e) and self._admissible_during_limit(e, humans_cfg))]
                if not allowed:
                    return None
                try:
                    run = store.run_begin(event_ids=allowed)
                except WorkerHeld:
                    return None
        try:
            with Store(self.home) as store:
                context = store.context(run_id=run["id"])
                sharedstatus.write(store, paused=self._last_pause)  # VIS: session running
            # Inputs that arrive after this snapshot belong to the next session.
            events = run["events"]
            context["events"] = events
            context["pending_events"] = events
            context["run"] = run
            context["workspace"] = str(self.home / "workspace")
            self.on_update({"type": "started", "run_id": run["id"]})
            result = self.brain.run(context)
            with Store(self.home) as store:
                finished = store.run_finish(run["id"], result.summary, cost=result.cost)
                sharedstatus.write(store, paused=self._last_pause)  # VIS: session ended, idle
                try:
                    board.advance(store, run["id"], finished=True)
                except Exception:  # noqa: BLE001 - cursor stays; entries show again next time
                    pass
                if board.publish_pending(store):
                    try:  # the board is current when the session ends
                        board.publish_drain(store)
                    except Exception:  # noqa: BLE001 - retried by the next idle step
                        pass
        except BaseException as error:
            # Tool effects may already exist. Failed input is retained for reconciliation.
            with Store(self.home) as store:
                store.run_fail(run["id"], str(error) or type(error).__name__, cost=getattr(error, "cost", None))
                sharedstatus.write(store, paused=self._last_pause)  # VIS: session ended (failed), idle
                try:
                    board.advance(store, run["id"], finished=False)
                except Exception:  # noqa: BLE001
                    pass
                # A deliberate stop (KeyboardInterrupt) is not a probation verdict; the next start retries it.
                if self.runtime is not None and isinstance(error, Exception):
                    self._probation(store, run["id"], str(error) or type(error).__name__)
            self.on_update({"type": "failed", "run_id": run["id"], "error": str(error)})
            raise
        if self.runtime is not None:
            with Store(self.home) as store:
                self._probation(store, run["id"], None)
        self.on_update({"type": "finished", "run": finished})
        return finished

    def run(self, max_turns=1):
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        with WorkerLock(self.home):
            with Store(self.home) as store:
                store.identity()
                store.recover_runs()
            completed = []
            for _ in range(max_turns):
                result = self._step()
                if result is None:
                    break
                completed.append(result)
            return completed

    PRUNE_KEY = "runs_pruned_day"

    def prune_runs_if_due(self):
        """E4 retention: at an idle point (no session running), at most once per UTC day. Never raises."""
        today = utc_day()
        if getattr(self, "_runs_checked_day", None) == today:
            return None  # E4-r2 (Rowan L1): already checked today in this process; no Store open per idle poll
        try:
            with Store(self.home) as store:
                row = store.db.execute("SELECT v FROM capture_meta WHERE k=?", (self.PRUNE_KEY,)).fetchone()
                if row is not None and row["v"] == today:
                    self._runs_checked_day = today
                    return None
                result = store.runs_prune(**self.retention)
                with store._tx():
                    store.db.execute("INSERT INTO capture_meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET "
                                     "v=excluded.v", (self.PRUNE_KEY, today))
            self._runs_checked_day = today
            if result.get("runs"):
                self.on_update({"type": "runs_pruned", "runs": result["runs"], "bytes": result["bytes"]})
            return result
        except Exception as error:  # noqa: BLE001 - housekeeping never stops the worker
            self._runs_checked_day = today  # one failure report per day, not one per idle poll; retried tomorrow
            self.on_update({"type": "runs_prune_failed", "error": type(error).__name__})
            return None

    def serve(self, poll_seconds=3.0, retry_seconds=60.0, stop=None, on_stop=None):
        """Wait locally between events; failed sessions back off instead of hot-looping.

        on_stop: optional callable(last_error: str, failure_count: int) fired once,
        synchronously, immediately before the three-failure RuntimeError is raised.
        Any exception raised by on_stop is suppressed; the RuntimeError is always raised.
        Engine does not import or use Telegram; on_stop is wired externally (e.g. cli.py).
        """
        if poll_seconds <= 0 or retry_seconds <= 0:
            raise ValueError("poll and retry intervals must be positive")
        stop = stop or threading.Event()
        with WorkerLock(self.home):
            with Store(self.home) as store:
                store.identity()
                recovered = store.recover_runs()
                streak = failure_streak(store.run_list(FAILURE_WINDOW))
                if streak and read_limit_override(self.home).get("resume_after_run") != streak:
                    self.failure_pause = streak
                if self.runtime is not None:
                    state = self.runtime.start(store)
                    self.apply_runtime(state["effective"])
                    poll_seconds = state["effective"]["poll"]
                    retry_seconds = state["effective"]["retry"]
                    self.on_update(dict(type="runtime", **{k: state[k] for k in (
                        "pid", "label", "argv", "effective", "source", "sha", "request_id", "problems", "probation")}))
                sharedstatus.write(store, paused=None)  # VIS C1: written at worker start (after recovery)
            self.on_update({"type": "listening", "home": str(self.home), "recovered": recovered})
            failures = 0
            while not stop.is_set():
                self.prune_runs_if_due()
                if self.runtime is not None:
                    # Idle point: no session is running and the last one's inputs are settled.
                    with Store(self.home) as store:
                        self.pause_state(store)  # clears failure_pause once an operator resumed
                        restarting = self.runtime.idle(store, failure_paused=bool(self.failure_pause))
                    if restarting:
                        stop.wait(min(poll_seconds, 1.0))
                        continue
                try:
                    result = self._step()
                    failures = 0
                except Exception as error:
                    failures += 1
                    if failures >= 3:
                        if on_stop is not None:
                            try:
                                on_stop(str(error) or type(error).__name__, failures)
                            except Exception:
                                pass
                        raise WorkerStopped("Worker stopped after three failed sessions. Inspect runs and resolve the error before restarting.")
                    stop.wait(retry_seconds)
                    continue
                # Pending work continues on the next bounded session. A brief wait lets
                # new information join it and prevents rapid empty tool/model cycles.
                stop.wait(min(poll_seconds, 1.0) if result else poll_seconds)


def status(home):
    with Store(home) as store:
        identity = store.identity()
        work = store.work_list()
        runs = store.run_list(limit=5)
        return {
            "identity": identity,
            "home": str(Path(home).resolve()),
            "worker": worker_status(home),
            "access_grants": store.access_list(),
            "pending_events": store.event_pending(),
            "work": work,
            "questions": store.question_list(status="open"),
            "reminders": store.reminders_list(),
            "outbox": store.outbox_list(),
            "recent_runs": runs,
            "platform": {
                "execution": "trusted host account; workspace is not a sandbox",
                "model_auth": "Claude CLI's configured authentication; not a provisioned employee account",
                "communication": "durable inbox/outbox; delivery requires a configured adapter; queued does not mean delivered",
            },
        }

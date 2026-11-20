"""#98 D3-D5: a provider-refused session (quota/outage) is no verdict; the home pauses, retries, resumes by itself
and tells the owner once each way. Fail-first on ecb11b3 (the names below don't exist there)."""

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from synthetics import brain as brainmod
from synthetics import codexevents, engine as enginemod, runtime
from synthetics.brain import BrainError, ClaudeBrain, ProviderUnavailable
from synthetics.codexbrain import CodexBrain
from synthetics.engine import (PROVIDER_IDLE, Engine, ProviderStep, WorkerStopped, failure_streak,
                               write_limit_override, utc_day)
from synthetics.store import Store
from tests import test_rtn_p1_routines as rp1
from tests.humans_fixture import write_host_humans

PAUSE_WORDS = "refused a session"
RESUME_WORDS = "accepted sessions again"


def quota(reset_at=None, text="You've hit your weekly limit"):
    return ProviderUnavailable("provider_unavailable quota (usage_limit_reached/429): " + text, cost=0.0,
                               kind="quota", status=429, api_error="usage_limit_reached", text=text,
                               reset_at=reset_at)


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Seq:
    """A brain that plays a script: exceptions are raised, None returns success; past the end it succeeds and
    sets `stop` if given."""

    def __init__(self, script, stop=None):
        self.script = list(script)
        self.stop = stop
        self.contexts = []

    def run(self, context):
        self.contexts.append(context)
        step = self.script.pop(0) if self.script else None
        if not self.script and step is None and self.stop is not None:
            self.stop.set()
        if step is not None:
            raise step
        return SimpleNamespace(summary="ok", cost=0.01)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        write_host_humans(self.home)
        with Store(self.home) as store:
            store.initialize("Vesper", "Maintainer")
            store.entity_put("david", "David", "person")
            store.entity_put("codex-manager", "Codex", "system")
            run = store.run_begin()
            store.run_finish(run["id"], "welcomed", cost=0.0)

    def engine(self, brain, **kwargs):
        kwargs.setdefault("limit_notify", "codex-manager")
        return Engine(self.home, brain, **kwargs)

    def feed(self, text="please do the thing"):
        with Store(self.home) as store:
            return store.feed("david", text)

    def outbox(self, words=None):
        with Store(self.home) as store:
            items = store.outbox_list()
        return [m for m in items if words is None or words in m["body"]]

    def audit(self, prefix="provider."):
        with Store(self.home) as store:
            return [row for row in reversed(store.audit_list(500)) if row["action"].startswith(prefix)]

    def pause(self, engine):
        with Store(self.home) as store:
            return engine.pause_state(store)

    def pause_row(self, engine):
        with Store(self.home) as store:
            return engine._provider_pause_read(store)

    def age_pause(self, engine):
        """Time passes: the pause's next try is due."""
        with Store(self.home) as store:
            row = engine._provider_pause_read(store)
            row["next_try_at"] = iso(datetime.now(timezone.utc) - timedelta(seconds=1))
            engine._provider_pause_write(store, row, "test.aged", {})

    def runs(self):
        with Store(self.home) as store:
            return store.run_list(50)


class NoVerdict(Base):
    def test_t1_run_records_cause_and_provider_field(self):
        self.feed()
        result = self.engine(Seq([quota()]))._step()
        self.assertIsInstance(result, ProviderStep)
        run = self.runs()[0]
        self.assertEqual(run["status"], "failed")
        self.assertIn("You've hit your weekly limit", run["error"])
        self.assertEqual(run["provider"], {"kind": "quota", "status": 429, "api_error": "usage_limit_reached",
                                           "reset_at": None})

    def test_t3_failure_streak_skips_provider_runs(self):
        runs = [dict(id="r%d" % i, status="failed") for i in range(3)]
        self.assertEqual(failure_streak(runs), "r0")
        runs[1]["provider"] = {"kind": "quota"}
        self.assertIsNone(failure_streak(runs))
        runs.append(dict(id="r9", status="failed"))
        self.assertEqual(failure_streak(runs), "r0")

    def test_t3_three_provider_runs_never_stop_serve(self):
        self.feed()
        stop = threading.Event()
        brain = Seq([quota(), quota(), quota(), None], stop=stop)
        engine = self.engine(brain)
        engine.provider_backoff = (0.0,) * 5  # each try is due at once
        engine.serve(poll_seconds=0.01, retry_seconds=0.01, stop=stop)  # no WorkerStopped
        self.assertEqual(len(brain.contexts), 4)
        self.assertIsNone(failure_streak(self.runs()))

    def test_t3_p1_fail_provider_fail_fail_stops_serve(self):
        """Rowan P1 / Codex: a provider run neither counts nor resets serve's counter (agrees with failure_streak)."""
        self.feed()
        stop = threading.Event()
        crash = BrainError("crash")
        brain = Seq([crash, quota(), crash, crash, None], stop=stop)
        engine = self.engine(brain)
        engine.provider_backoff = (0.0,) * 5
        with self.assertRaises(WorkerStopped):
            engine.serve(poll_seconds=0.01, retry_seconds=0.01, stop=stop)
        self.assertEqual(len(brain.contexts), 4)
        self.assertIsNotNone(failure_streak(self.runs()))

    def test_t3_p1_paused_polls_keep_the_counter(self):
        """A poll that finds the provider pause in force is not an idle success either."""
        self.feed()
        engine = self.engine(Seq([quota()]))
        engine._step()
        self.assertIs(engine._step(), PROVIDER_IDLE)
        self.assertFalse(PROVIDER_IDLE)

    def test_c3_t9_inputs_stay_pending_and_replay_once_after_the_pause(self):
        event = self.feed()
        brain = Seq([quota(), None])
        engine = self.engine(brain)
        engine._step()
        with Store(self.home) as store:
            self.assertIn(event["id"], [e["id"] for e in store.event_pending()])
        self.assertIs(engine._step(), PROVIDER_IDLE)  # paused: not replayed yet
        self.assertEqual(len(brain.contexts), 1)
        self.age_pause(engine)
        finished = engine._step()
        self.assertEqual(finished["status"], "finished")
        self.assertIn(event["id"], [e["id"] for e in brain.contexts[1]["events"]])
        with Store(self.home) as store:
            self.assertEqual(store.event_pending(), [])
        self.assertIsNone(engine._step())


class Quarantine(rp1.RoutineCase):
    def test_t4_ready_routine_is_not_quarantined_by_a_provider_run(self):
        work, run, sess = self.fire_and_capture()
        sess.close()
        self.s.run_fail(run["id"], "provider_unavailable quota", provider={"kind": "quota", "status": 429})
        self.assertIsNone(self.s.work_get(work["id"])["routine"]["quarantine"])
        self.assertEqual(self.s.work_get(work["id"])["routine"]["failures"], [])


class PauseResume(Base):
    def test_t5_parsed_reset_sets_next_try_and_one_message_each_way(self):
        self.feed()
        reset = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=3)
        brain = Seq([quota(reset_at=iso(reset)), None])
        engine = self.engine(brain)
        engine._step()
        pause = self.pause(engine)
        self.assertEqual(pause["reason"], "provider_unavailable")
        self.assertEqual(pause["kind"], "quota")
        self.assertEqual(pause["attempts"], 1)
        self.assertEqual(enginemod._parse_iso(pause["next_try_at"]), reset + timedelta(minutes=2))
        sent = self.outbox(PAUSE_WORDS)
        self.assertEqual(len(sent), 1)
        self.assertIn(str(self.home), sent[0]["body"])  # C2: names the home
        self.assertIn("next try", sent[0]["body"])  # L2
        self.assertNotIn("resets at", sent[0]["body"])
        for _ in range(3):
            self.assertIs(engine._step(), PROVIDER_IDLE)
        self.assertEqual(len(brain.contexts), 1)
        self.assertEqual(len(self.outbox(PAUSE_WORDS)), 1)
        # The pause survives a worker restart.
        restarted = self.engine(brain)
        self.assertEqual(self.pause(restarted)["reason"], "provider_unavailable")
        self.age_pause(restarted)
        self.assertEqual(restarted._step()["status"], "finished")
        self.assertIsNone(self.pause_row(restarted))
        resumed = self.outbox(RESUME_WORDS)
        self.assertEqual(len(resumed), 1)
        self.assertIn(str(self.home), resumed[0]["body"])
        self.assertEqual([r["action"] for r in self.audit()][-1], "provider.resumed")
        self.feed("more")
        restarted._step()
        self.assertEqual(len(self.outbox(RESUME_WORDS)), 1)
        self.assertEqual(len(self.outbox(PAUSE_WORDS)), 1)

    def test_t5_retries_back_off_and_add_no_messages(self):
        self.feed()
        brain = Seq([quota(), quota(), quota()])
        engine = self.engine(brain)
        before = datetime.now(timezone.utc)
        for attempt in (1, 2, 3):
            engine._step()
            row = self.pause_row(engine)
            self.assertEqual(row["attempts"], attempt)
            delay = (enginemod._parse_iso(row["next_try_at"]) - before).total_seconds()
            expected = (300, 600, 1200)[attempt - 1]
            self.assertTrue(expected - 5 <= delay <= expected + 60, (attempt, delay))
            self.age_pause(engine)
        self.assertEqual(len(self.outbox(PAUSE_WORDS)), 1)
        self.assertEqual(len(self.outbox(RESUME_WORDS)), 0)

    def test_t5_backoff_schedule_and_cap(self):
        engine = self.engine(Seq([]))
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        delays = [(engine._provider_next_try(now, n, None) - now).total_seconds() for n in range(1, 8)]
        self.assertEqual(delays, [300, 600, 1200, 2400, 3600, 3600, 3600])

    def test_t5b_reset_in_past_or_beyond_8_days_uses_backoff(self):
        engine = self.engine(Seq([]))
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        for reset in (now - timedelta(minutes=1), now + timedelta(days=8, minutes=1), "garbage"):
            reset = reset if isinstance(reset, str) else iso(reset)
            self.assertEqual((engine._provider_next_try(now, 1, reset) - now).total_seconds(), 300, reset)
        within = now + timedelta(days=7)
        self.assertEqual(engine._provider_next_try(now, 1, iso(within)), within + timedelta(minutes=2))

    def test_t5_p3_limit_resume_tries_now_keeps_attempts_and_audits(self):
        self.feed()
        brain = Seq([quota(), None])
        engine = self.engine(brain)
        engine._step()
        self.assertIs(engine._step(), PROVIDER_IDLE)
        write_limit_override(self.home, resume_after_run=None, resumed_by="test-owner",
                             provider_resume_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self.assertIsNone(self.pause(engine))
        row = self.pause_row(engine)
        self.assertEqual(row["attempts"], 1)
        requested = [r for r in self.audit() if r["action"] == "provider.resume_requested"]
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]["data"]["by"], "test-owner")
        self.assertEqual(engine._step()["status"], "finished")
        self.assertEqual(len(self.outbox(RESUME_WORDS)), 1)
        # An old resume request does not end a later pause.
        self.feed("again")
        engine.brain = Seq([quota()])
        engine._step()
        self.assertEqual(self.pause(engine)["reason"], "provider_unavailable")

    def test_t5_cli_limit_resume_writes_the_provider_request(self):
        from synthetics import cli
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            cli.main(["--home", str(self.home), "limit", "resume", "--by", "test-owner"])
        override = json.loads((self.home / "usage_limit.json").read_text())
        self.assertIsNotNone(enginemod._parse_iso(override["provider_resume_at"]))

    def test_own_failure_ends_the_pause_and_counts_as_a_failure(self):
        self.feed()
        brain = Seq([quota(), BrainError("crash")])
        engine = self.engine(brain)
        engine._step()
        self.age_pause(engine)
        with self.assertRaises(BrainError):
            engine._step()
        self.assertIsNone(self.pause_row(engine))
        self.assertEqual(len(self.outbox(RESUME_WORDS)), 1)
        self.assertIn("(failed)", self.outbox(RESUME_WORDS)[0]["body"])

    def test_deliberate_stop_does_not_end_the_pause(self):
        self.feed()
        brain = Seq([quota(), KeyboardInterrupt()])
        engine = self.engine(brain)
        engine._step()
        self.age_pause(engine)
        with self.assertRaises(KeyboardInterrupt):
            engine._step()
        self.assertIsNotNone(self.pause_row(engine))
        self.assertEqual(self.outbox(RESUME_WORDS), [])

    def test_c4_precedence_failure_then_provider_then_daily_limit(self):
        self.feed()
        engine = self.engine(Seq([quota()]), daily_limit=0.5)
        engine._step()
        with Store(self.home) as store:
            run = store.run_begin(event_ids=[])
            store.run_finish(run["id"], "spent", cost=5.0)
        engine.failure_pause = "run-x"
        self.assertEqual(self.pause(engine)["reason"], "recent_failures")
        write_limit_override(self.home, resume_after_run="run-x", resumed_by="t")
        self.assertEqual(self.pause(engine)["reason"], "provider_unavailable")
        self.age_pause(engine)
        self.assertEqual(self.pause(engine)["reason"], "daily_limit")

    def test_provider_pause_admits_no_limit_bypass(self):
        with Store(self.home) as store:
            store.feed("david", "from Telegram", source="telegram:1")
        brain = Seq([quota()])
        engine = self.engine(brain, limit_allow=["david"])
        engine._step()
        self.assertIs(engine._step(), PROVIDER_IDLE)
        self.assertEqual(len(brain.contexts), 1)

    def test_lookout_paused_field_carries_the_provider_pause(self):
        from synthetics import sharedstatus
        self.feed()
        engine = self.engine(Seq([quota()]))
        engine._step()
        paused = sharedstatus.read(self.home)["session"]["paused"]
        self.assertEqual(paused["reason"], "provider_unavailable")
        self.assertEqual(paused["attempts"], 1)
        self.assertIn("next_try_at", paused)
        self.assertNotIn("text", paused)


FAKE_CLAUDE = r'''
import json, os, sys
args = sys.argv[1:]
sys.stdin.read()
mode = args[args.index("--output-format") + 1]
spec = json.loads(%(spec)r)
counter = spec["counter"]
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
step = spec["steps"][min(n, len(spec["steps"]) - 1)]
if mode == "stream-json":
    for line in step.get("lines", []):
        sys.stdout.write(line)
if step.get("result") is not None:
    print(json.dumps(step["result"]))
sys.stdout.flush()
raise SystemExit(step.get("exit", 0))
'''

OBSERVED = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "duration_ms": 181,
            "total_cost_usd": 0, "terminal_reason": "api_error", "api_error_status": 429,
            "api_error_type": "rate_limit_error", "api_error_origin": "first_party",
            "api_error": "usage_limit_reached", "result": "You've hit your weekly limit · resets 8am (America/New_York)"}
SUCCESS = {"type": "result", "subtype": "success", "is_error": False, "result": "Saved.", "total_cost_usd": 0.5}


def tool_turns():
    from tests.test_e4_stream import stream_lines
    return stream_lines()[:-1]


class FakeCli:
    def __init__(self, directory, steps):
        self.directory = Path(directory)
        self.counter = self.directory / "fake-count"
        self.path = self.directory / "fake-claude"
        self.path.write_text("#!" + sys.executable + "\n" + FAKE_CLAUDE % {
            "spec": json.dumps({"counter": str(self.counter), "steps": steps})})
        self.path.chmod(0o700)

    def calls(self):
        return int(self.counter.read_text()) if self.counter.exists() else 0


class BothModes(Base):
    """Rowan L4: T1 and T6 in both stream and json modes."""

    def outcome(self, steps, stream, run_id):
        fake = FakeCli(Path(self.temp.name), steps)
        if fake.counter.exists():
            fake.counter.unlink()
        brain = ClaudeBrain(self.home, executable=str(fake.path), timeout=60, stream_output=stream)
        try:
            brain.run({"run": {"id": run_id, "profile": None}})
            return ("ok",)
        except BrainError as error:
            return (type(error).__name__, str(error).replace(run_id, "RUN"), error.cost)

    def test_l4_t1_quota_in_both_modes(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                kind, message, cost = self.outcome([{"lines": tool_turns(), "result": OBSERVED, "exit": 1}],
                                                   stream, "run-q%d" % stream)
                self.assertEqual(kind, "ProviderUnavailable")
                self.assertIn("You've hit your weekly limit", message)

    def test_l4_t6_unclassified_errors_identical_in_both_modes(self):
        cases = {
            "other_api_error": [{"result": dict(OBSERVED, api_error_status=400, api_error_type="invalid_request_error",
                                                api_error="bad_request"), "exit": 1}],
            "no_result_exit1": [{"exit": 1}],  # C1: claude.json absent/empty, nonzero exit
            "garbage_exit1": [{"lines": ["not json\n"], "exit": 1}],
            "budget_cap": [{"result": {"type": "result", "subtype": "error_max_budget_usd", "is_error": False,
                                       "total_cost_usd": 3.05}, "exit": 0}],  # n1
        }
        for name, steps in cases.items():
            with self.subTest(case=name):
                json_mode = self.outcome(steps, False, "run-j" + name)
                stream_mode = self.outcome(steps, True, "run-s" + name)
                self.assertEqual(json_mode[0], "BrainError")
                self.assertEqual(stream_mode[0], "BrainError")
                if name == "no_result_exit1":
                    # C1 byte-identical to today's message in both modes; stream with no result line lands here too.
                    self.assertEqual(json_mode[1], "Claude CLI exited with status 1. See %s." % (
                        self.home / "runs" / "RUN.stderr.txt"))
                    self.assertEqual(stream_mode[1], json_mode[1])
                if name == "budget_cap":
                    self.assertIn("budget cap", json_mode[1])
                    self.assertEqual(stream_mode[1], json_mode[1])


class CodexSide(unittest.TestCase):
    """Rowan L1 / Codex: AUTH before QUOTA; both codexbrain raise sites map quota to ProviderUnavailable."""

    def reduce(self, message):
        reducer = codexevents.CodexReducer.__new__(codexevents.CodexReducer)
        reducer.error = None
        reducer.error_kind = None
        reducer._write = lambda fact: None
        reducer._provider_failure({"type": "turn.failed", "error": {"message": message}})
        return reducer.error_kind

    def test_t8_auth_wins_over_quota_text(self):
        self.assertEqual(self.reduce("429 Too Many Requests: please log in again"), "authentication")
        self.assertEqual(self.reduce("usage limit reached"), "quota")
        self.assertEqual(self.reduce("something else broke"), "provider")

    def executable(self, directory, body):
        path = Path(directory) / "fake-codex"
        path.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                        'if sys.argv[-2:] == ["login", "status"]:\n'
                        ' print("Logged in using ChatGPT"); raise SystemExit(0)\n'
                        "message=sys.stdin.read()\n" + body)
        path.chmod(0o700)
        return str(path)

    def test_t8_codex_quota_is_provider_unavailable_at_both_raise_sites(self):
        failed = "print(json.dumps({'type':'turn.failed','error':{'message':'usage limit exceeded SECRET'}}))\n"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "employee"
            home.mkdir()
            for name, body in (("exit_zero", failed), ("exit_nonzero", failed + "raise SystemExit(1)\n")):
                with self.subTest(site=name):
                    with self.assertRaises(ProviderUnavailable) as caught:
                        CodexBrain(home, executable=self.executable(tmp, body)).run({})
                    self.assertEqual(caught.exception.kind, "quota")
                    self.assertNotIn("SECRET", str(caught.exception))
            auth = "print(json.dumps({'type':'turn.failed','error':{'message':'429 please log in'}}))\nraise SystemExit(1)\n"
            with self.assertRaises(BrainError) as caught:
                CodexBrain(home, executable=self.executable(tmp, auth)).run({})
            self.assertNotIsInstance(caught.exception, ProviderUnavailable)
            self.assertIn("authentication", str(caught.exception))


def baseline(**overrides):
    value = dict(claude=sys.executable, model="claude-opus-5-5", budget=100.0, timeout=600.0, poll=2.0,
                 retry=60.0, daily_limit=0, limit_notify="codex-manager", limit_allow=[])
    value.update(overrides)
    return value


class EndToEnd(Base):
    """Rowan L3: the whole 11-19 chain in stream mode. A probation run with tool turns ends in 429, three fast 429s
    follow, the worker restarts mid-pause, the reset passes, a good run confirms the probation."""

    def controller(self):
        self.pid = getattr(self, "pid", 5000) + 1
        controller = runtime.Controller(self.home, baseline(), label="com.synthetics.worker.i98test",
                                        restarter=lambda label: None, mode="kickstart",
                                        argv=["synthetics", "serve"], pid=self.pid, clock=lambda: 1000.0)
        with Store(self.home) as store:
            controller.start(store)
        return controller

    def request(self):
        with Store(self.home) as store:
            run = store.run_begin(event_ids=[])
        try:
            with Store(self.home, run_id=run["id"]) as store:
                return store.runtime_restart("i98 chain", "k-i98", settings={"timeout": 300})
        finally:
            with Store(self.home) as store:
                store.run_finish(run["id"], "requested")

    def status(self, request_id):
        with Store(self.home) as store:
            return {r["id"]: r for r in store.runtime_requests(20)}[request_id]["status"]

    def test_l3_t10_chain(self):
        first = self.controller()
        request = self.request()
        with Store(self.home) as store:
            first.idle(store, failure_paused=False)
        second = self.controller()
        self.assertEqual(second.probation, request["id"])
        event = self.feed("the 11-19 input")
        fake = FakeCli(Path(self.temp.name), [
            {"lines": tool_turns(), "result": OBSERVED, "exit": 1},
            {"result": dict(OBSERVED, result="You've hit your weekly limit"), "exit": 1},
            {"result": dict(OBSERVED, result="You've hit your weekly limit"), "exit": 1},
            {"result": dict(OBSERVED, result="You've hit your weekly limit"), "exit": 1},
            {"lines": tool_turns(), "result": SUCCESS, "exit": 0}])
        brain = ClaudeBrain(self.home, executable=str(fake.path), timeout=60, stream_output=True)
        engine = Engine(self.home, brain, runtime=second, limit_notify="codex-manager")
        stop = threading.Event()
        for _ in range(4):  # the probation run, then three fast 429s (each try due at once)
            self.assertIsInstance(engine._step(), ProviderStep)
            self.age_pause(engine)
        self.assertEqual(fake.calls(), 4)
        self.assertEqual(self.status(request["id"]), "applied_unconfirmed")  # no revert
        self.assertFalse((self.home / runtime.LAST_GOOD_FILE).exists()
                         and json.loads((self.home / runtime.LAST_GOOD_FILE).read_text()).get("timeout") == 300)
        # One more refusal leaves a real pause; the worker restarts in the middle of it.
        with Store(self.home) as store:
            row = engine._provider_pause_read(store)
            row["next_try_at"] = iso(datetime.now(timezone.utc) + timedelta(hours=1))
            engine._provider_pause_write(store, row, "test.paused", {})
        third = self.controller()
        self.assertEqual(third.probation, request["id"])
        self.assertEqual(self.status(request["id"]), "applied_unconfirmed")
        restarted = Engine(self.home, brain, runtime=third, limit_notify="codex-manager")
        self.assertIs(restarted._step(), PROVIDER_IDLE)
        self.assertEqual(fake.calls(), 4)
        self.assertIsNone(failure_streak(self.runs()))  # no stop, no failure pause at the next start
        # The reset passes; the good run confirms the probation and ends the pause.
        self.age_pause(restarted)
        finished = restarted._step()
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(self.status(request["id"]), "applied")
        self.assertEqual(len(self.outbox(PAUSE_WORDS)), 1)
        self.assertEqual(len(self.outbox(RESUME_WORDS)), 1)
        with Store(self.home) as store:
            self.assertEqual(store.event_pending(), [])
            processed = [r for r in store.run_list(50) if event["id"] in [e["id"] for e in r.get("events") or []]]
        self.assertEqual([r["status"] for r in processed if r["status"] == "finished"], ["finished"])
        stream = (self.home / "runs" / (self.runs()[1]["id"] + ".stream.jsonl"))
        self.assertTrue(stream.exists())
        stop.set()


class Bytes(unittest.TestCase):
    def test_n2_text_cut_in_bytes_at_a_utf8_boundary(self):
        raw = dict(OBSERVED, result="é" * 400)
        error = brainmod.classify_provider(raw)
        self.assertLessEqual(len(error.text.encode("utf-8")), 300)
        self.assertEqual(error.text, "é" * 150)
        self.assertEqual(brainmod.cut_utf8("aé", 2), "a")


if __name__ == "__main__":
    unittest.main()

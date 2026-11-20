"""#85 L1 (Vesper's review, Codex's ruling): the role block reaches a session only through the engine passing the
run record (with its `profile` key) to ClaudeBrain.run. These tests run the real Engine._step -> ClaudeBrain.run
path and stop at the CLI launch, so a change that drops the key fails here instead of silently removing the block.

The record always carries the key (a triple, or None = no profile.json at run start, which stays valid). A run
record WITHOUT the key fails closed: no CLI process, the run is recorded failed with the reason."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import authority, brain
from synthetics.engine import Engine
from synthetics.store import Store


class Launched(Exception):
    pass


class EngineSeam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = (Path(self.tmp.name) / "dev").resolve()
        with Store(self.home) as s:
            s.initialize("Dev", "developer")
            s.feed("david", "hello", external_id="seam-1")
        env = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def set_profile(self, name):
        (self.home / "profile.json").write_text(json.dumps({"v": 1, "profile": name}))

    def step(self, run_begin=None):
        """One real engine step; returns (system prompt the CLI would start with or None, the run record)."""
        seen = {}

        def capture(self_, command, *args, **kwargs):
            seen["system"] = command[command.index("--system-prompt") + 1]
            raise Launched()
        b = brain.ClaudeBrain(self.home, stream_output=True)
        patches = [mock.patch.object(brain.ClaudeBrain, "_run_stream", capture),
                   mock.patch.object(brain.ClaudeBrain, "_run_json", capture)]
        if run_begin is not None:
            patches.append(mock.patch.object(Store, "run_begin", run_begin))
        for p in patches:
            p.start()
        try:
            Engine(self.home, b).run(max_turns=1)
        except Exception:  # noqa: BLE001 - the engine records the failure, then may re-raise
            pass
        finally:
            for p in reversed(patches):
                p.stop()
        with Store(self.home) as s:
            runs = s.db.execute("SELECT data FROM runs").fetchall()
        records = [json.loads(r["data"]) for r in runs]
        self.assertEqual(len(records), 1, records)
        return seen.get("system"), records[0]

    def test_engine_session_prompt_carries_the_profile_block(self):
        self.set_profile("developer")
        system, run = self.step()
        self.assertIsNotNone(system, "the CLI was never launched")
        self.assertIn("profile developer v", system)
        self.assertIn(run["profile"]["sha256"][:12], system)

    def test_guard_engine_session_without_profile_json_has_no_block(self):
        system, run = self.step()
        self.assertIsNotNone(system)
        self.assertNotIn("Role defaults", system)
        self.assertIsNone(run["profile"])

    def test_run_record_without_the_key_fails_closed_before_launch(self):
        """The mutant shape: something between run_begin and brain.run drops `profile` from the run record."""
        self.set_profile("developer")
        real = Store.run_begin

        def dropping(self_, *args, **kwargs):
            run = real(self_, *args, **kwargs)
            run.pop("profile", None)
            return run
        system, run = self.step(dropping)
        self.assertIsNone(system, "a session started without its recorded profile")
        self.assertEqual(run["status"], "failed")
        self.assertIn("no profile key", run["error"])

    def test_brain_refuses_a_run_context_without_the_key(self):
        with self.assertRaises(brain.BrainError):
            brain.recorded_profile({"id": "run-x"})
        self.assertIsNone(brain.recorded_profile({"id": "run-x", "profile": None}))
        self.assertIs(brain.recorded_profile({}), brain.CURRENT)

    def test_meeting_run_row_without_the_key_fails_closed(self):
        from synthetics.floorsession import MeetingSession
        self.set_profile("developer")
        with Store(self.home) as s:
            s.meeting_begin("m1", "stand")
            hold = s.meeting_attach("m1")
            run = s.meeting_run_begin("m1", hold["token"])
        session = MeetingSession(self.home, run["id"], "m1", "brief", lambda *a: None, executable="claude", model=None)
        self.assertIn("profile developer v", session.command()[session.command().index("--system-prompt") + 1])
        stripped = {k: v for k, v in run.items() if k != "profile"}
        with mock.patch.object(Store, "_find", lambda self_, table, id_: stripped):
            with self.assertRaises(brain.BrainError):
                session.command()


if __name__ == "__main__":
    unittest.main()

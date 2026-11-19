"""Operating defaults lows (Vesper's #81 review, Codex's rulings): L2 a meeting run records the profile triple;
L3 profile.json is read once per session: the prompt is built from the triple run_begin recorded, so an owner
set/clear in between can't make prompt and run record disagree. L1 is the docs honest-limit line.

Guards (pass on the base too): a context without a run record still reads profile.json now; no profile at run
start and none now gives no block."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import authority, brain
from synthetics.store import Store

DOCS = Path(brain.__file__).resolve().parent.parent / "docs"


class Stop(Exception):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = (Path(self.tmp.name) / "dev").resolve()
        with Store(self.home) as s:
            s.initialize("Dev", "developer")
        env = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def set_profile(self, name):
        (self.home / "profile.json").write_text(json.dumps({"v": 1, "profile": name}))

    def clear_profile(self):
        (self.home / "profile.json").unlink()

    def prompt_of_run(self, context):
        """The --system-prompt ClaudeBrain.run would start the CLI with (stopped before any process)."""
        seen = {}

        def capture(self_, command, *args, **kwargs):
            seen["system"] = command[command.index("--system-prompt") + 1]
            raise Stop()
        b = brain.ClaudeBrain(self.home, stream_output=True)
        with mock.patch.object(brain.ClaudeBrain, "_run_stream", capture), \
                mock.patch.object(brain.ClaudeBrain, "_run_json", capture):
            with self.assertRaises(Stop):
                b.run(context)
        return seen["system"]


class L3ReadOnce(Base):
    def test_set_after_run_begin_does_not_reach_the_prompt(self):
        with Store(self.home) as s:
            run = s.run_begin()
        self.assertIsNone(run["profile"])
        self.set_profile("developer")  # owner sets it between run_begin and the prompt build
        self.assertNotIn("Role defaults", self.prompt_of_run({"run": run}))

    def test_clear_after_run_begin_keeps_the_recorded_block(self):
        from synthetics import profiles
        self.set_profile("developer")
        with Store(self.home) as s:
            run = s.run_begin()
        self.clear_profile()
        system = self.prompt_of_run({"run": run})
        self.assertIn("Role defaults (%s;" % profiles.label(profiles.shipped("developer")), system)
        self.assertIn(run["profile"]["sha256"][:12], system)

    def test_switch_after_run_begin_keeps_the_recorded_profile(self):
        self.set_profile("developer")
        with Store(self.home) as s:
            run = s.run_begin()
        self.set_profile("manager")
        system = self.prompt_of_run({"run": run})
        self.assertIn("profile developer v", system)
        self.assertNotIn("profile manager v", system)

    def test_invalid_at_run_start_fails_closed_even_if_fixed_later(self):
        (self.home / "profile.json").write_text("{broken")
        with Store(self.home) as s:
            run = s.run_begin()
        self.set_profile("developer")
        with self.assertRaises(brain.BrainError):
            self.prompt_of_run({"run": run})

    def test_shipped_file_changed_since_run_start_fails_closed(self):
        self.set_profile("developer")
        with Store(self.home) as s:
            run = s.run_begin()
        stale = dict(run, profile=dict(run["profile"], sha256="0" * 64))
        with self.assertRaises(brain.BrainError):
            self.prompt_of_run({"run": stale})

    def test_guard_context_without_a_run_reads_profile_json_now(self):
        self.set_profile("manager")
        self.assertIn("profile manager v", self.prompt_of_run({}))
        self.assertIn("profile manager v", brain.system_prompt(self.home))

    def test_guard_no_profile_then_or_now_gives_no_block(self):
        with Store(self.home) as s:
            run = s.run_begin()
        self.assertNotIn("Role defaults", self.prompt_of_run({"run": run}))


class L2MeetingRun(Base):
    def meeting_run(self):
        with Store(self.home) as s:
            s.meeting_begin("m1", "stand")
            hold = s.meeting_attach("m1")
            return s.meeting_run_begin("m1", hold["token"])

    def test_meeting_run_records_the_profile_triple(self):
        from synthetics import profiles
        self.set_profile("developer")
        prof = profiles.shipped("developer")
        run = self.meeting_run()
        self.assertEqual(run.get("profile"),
                         {"name": "developer", "version": prof["version"], "sha256": prof["sha256"]})

    def test_meeting_prompt_uses_the_recorded_profile(self):
        from synthetics.floorsession import MeetingSession
        self.set_profile("developer")
        run = self.meeting_run()
        self.clear_profile()
        s = MeetingSession(self.home, run["id"], "m1", "brief", lambda *a: None, executable="claude", model=None)
        system = s.command()[s.command().index("--system-prompt") + 1]
        self.assertIn("profile developer v", system)
        self.assertIn("live meeting", system)

    def test_meeting_run_without_profile_records_none(self):
        run = self.meeting_run()
        self.assertIn("profile", run)
        self.assertIsNone(run["profile"])


class L1HonestLimit(unittest.TestCase):
    def test_profiles_doc_states_the_cli_only_fence(self):
        text = (DOCS / "profiles.md").read_text()
        self.assertIn("Honest limit", text)
        self.assertIn("could write it with the shell", text)
        self.assertIn("not an enforced boundary", text)


if __name__ == "__main__":
    unittest.main()

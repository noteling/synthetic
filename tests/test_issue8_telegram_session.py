"""Issue #8 (noteling/synthetic#8): `telegram` subcommands inside an employee session.

`telegram install --with-worker` wrote and started a worker plist with caller-chosen --claude/--model/--budget,
bypassing M28 worker.manage; init took a token file. Every subcommand except `status` is now refused when
SYNTHETICS_SESSION_RUN is set, before any file or launchd effect. launchd is mocked throughout: no real
LaunchAgent is written, loaded or kicked.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from synthetics.cli import main
from synthetics.store import Store
from tests.humans_fixture import write_host_humans

SESSION = {"SYNTHETICS_SESSION_RUN": "run-issue8"}


class TelegramSessionRefusalTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "employee"
        with Store(self.home) as store:
            store.initialize("Aster", "Colleague")
        write_host_humans(self.home)
        self.token = self.root / "token"
        self.token.write_text("test-only-token")
        self.token.chmod(0o600)
        with patch.dict("os.environ", clear=False) as env:  # owner path: configure and pair outside a session
            env.pop("SYNTHETICS_SESSION_RUN", None)
            rc, out, err = self.command("init", "--token-file", str(self.token), "--thread", "human-one")
        self.assertEqual(rc, 0, err)
        self.config = Path(json.loads(out)["config"])
        Path(json.loads(self.config.read_text())["state_file"]).write_text(json.dumps({"paired": True}))

    def command(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = main(["--home", str(self.home), "telegram"] + list(args))
        return result, output.getvalue(), errors.getvalue()

    @contextlib.contextmanager
    def launchd(self):
        """Every launchd and adapter effect telegram_cli can reach, mocked; yields the mocks."""
        adapter = ModuleType("synthetics.telegram")
        served = []

        class Adapter:
            def __init__(self, path):
                pass

            def run(self):
                served.append(True)
        adapter.TelegramAdapter = Adapter
        with patch("synthetics.service.write_launch_agent", return_value=self.root / "x.plist") as write, \
                patch("synthetics.service.restart_launch_agent", return_value={}) as restart, \
                patch("synthetics.service.stop_launch_agent", return_value={}) as stop, \
                patch("synthetics.install.write_worker", return_value=self.root / "w.plist") as worker, \
                patch("synthetics.telegram_cli._start_if_needed", return_value={"loaded": True}) as start, \
                patch.dict("sys.modules", {"synthetics.telegram": adapter}):
            yield {"write_launch_agent": write, "restart_launch_agent": restart, "stop_launch_agent": stop,
                   "write_worker": worker, "start": start, "served": served}

    def test_every_writing_subcommand_is_refused_in_a_session(self):
        other_config = self.root / "other" / "telegram.json"
        cases = [
            ("install", "--with-worker", "--claude", sys.executable, "--model", "any-model", "--budget", "9999"),
            ("install",),
            ("restart", "--with-worker"),
            ("restart", "--worker-only"),
            ("stop", "--with-worker"),
            ("serve",),
            ("init", "--token-file", str(self.token), "--thread", "t2", "--config", str(other_config)),
        ]
        before = self.config.read_bytes()
        for argv in cases:
            with self.subTest(argv=argv), patch.dict("os.environ", SESSION), self.launchd() as mocks:
                rc, out, err = self.command(*argv)
                self.assertEqual(rc, 1, out)
                self.assertIn("refused inside an employee session", err)
                self.assertIn("telegram " + argv[0], err)
                for name in ("write_launch_agent", "restart_launch_agent", "stop_launch_agent", "write_worker",
                             "start"):
                    mocks[name].assert_not_called()
                self.assertEqual(mocks["served"], [])
        self.assertFalse(other_config.exists())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.home / "services").exists())

    def test_status_is_still_allowed_in_a_session(self):
        with patch.dict("os.environ", SESSION):
            rc, out, err = self.command("status")
        self.assertEqual(rc, 0, err)
        self.assertTrue(json.loads(out)["paired"])

    def test_owner_install_unchanged_outside_a_session(self):
        with patch.dict("os.environ", clear=False) as env, self.launchd() as mocks:
            env.pop("SYNTHETICS_SESSION_RUN", None)
            rc, out, err = self.command("install", "--with-worker", "--claude", sys.executable)
        self.assertEqual(rc, 0, err)
        self.assertEqual(mocks["write_launch_agent"].call_count, 1)  # the adapter plist
        self.assertEqual(mocks["write_worker"].call_count, 1)


if __name__ == "__main__":
    unittest.main()

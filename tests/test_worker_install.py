"""M25 test 10: worker and adapter install without any Telegram configuration.

Worker plists are only written (a bootstrapped worker would run a model on the
welcome event). The real-launchd leg uses a harmless adapter argv under a
temporary com.synthetics.adapter.m25test-* label and always removes it.
"""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from synthetics.install import WORKER_PREFIX, worker_argv, worker_label

ROOT = Path(__file__).resolve().parents[1]


class WorkerInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.agents = self.root / "agents"
        self.claude = self.root / "claude"
        self.claude.write_text("#!/bin/sh\nexit 1\n")
        self.claude.chmod(0o755)
        self.env = dict(os.environ, PYTHONPATH=str(ROOT))
        self.env.pop("SYNTHETICS_SESSION_RUN", None)
        out = self.cli("init", "--name", "W", "--role", "r", "--human", "ana")
        self.assertEqual(out.returncode, 0, out.stderr)

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home)] + list(args),
                              capture_output=True, text=True, env=env or self.env, cwd=str(ROOT))

    def test_worker_install_writes_plist_with_no_telegram_anywhere(self):
        out = self.cli("worker", "install", "--claude", str(self.claude), "--limit-notify", "ana",
                       "--runtime-config", "--allow-model", "claude-opus-5-5",
                       "--launch-agents-dir", str(self.agents), "--no-start")
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertTrue(result["label"].startswith(WORKER_PREFIX))
        self.assertNotIn("service", result)
        plist = plistlib.loads(Path(result["plist"]).read_bytes())
        argv = plist["ProgramArguments"]
        self.assertEqual(argv[argv.index("--claude") + 1], str(self.claude))
        self.assertEqual(argv[argv.index("--limit-notify") + 1], "ana")
        self.assertEqual(argv[argv.index("--runtime-config") + 1], str(self.home.resolve() / "runtime.json"))
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertNotIn("telegram", " ".join(argv))
        self.assertEqual(list(self.home.rglob("telegram*.json")), [])
        self.assertNotIn("ANTHROPIC_API_KEY", plist["EnvironmentVariables"])
        out = self.cli("worker", "remove", "--launch-agents-dir", str(self.agents), "--never-loaded")
        self.assertEqual(json.loads(out.stdout)["removed"], True, out.stderr)
        self.assertFalse(Path(result["plist"]).exists())

    def test_default_label_matches_telegram_worker_label(self):
        from synthetics.store import Store
        from synthetics.telegram_cli import _service_labels

        with Store(self.home) as store:
            identity = store.identity()
        self.assertEqual(worker_label(identity), _service_labels(self.home / "telegram.json", identity)[1])

    def test_argv_matches_pre_m25_telegram_worker_argv(self):
        argv = worker_argv(Path("/h"), "/c/claude", "m", 100.0, 600.0, poll=2.0, daily_limit=75.0,
                           limit_notify="ana", limit_allow=["ana"])
        self.assertEqual(argv[argv.index("serve"):],
                         ["serve", "--claude", "/c/claude", "--model", "m", "--budget", "100.0", "--timeout",
                          "600.0", "--poll", "2", "--daily-limit", "75.0", "--limit-notify", "ana",
                          "--limit-allow", "ana"])
        with self.assertRaises(ValueError):
            worker_argv(Path("/h"), "/c", "m", float("nan"), 600.0)

    def test_labels_outside_prefix_and_sessions_are_refused(self):
        out = self.cli("worker", "install", "--label", "com.synthetics.telegram.x", "--claude", str(self.claude),
                       "--launch-agents-dir", str(self.agents), "--no-start")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("com.synthetics.worker.", out.stderr)
        out = self.cli("adapter", "install", "--label", "com.synthetics.worker.x", "--no-start",
                       "--launch-agents-dir", str(self.agents), "--", "/bin/sleep", "1")
        self.assertNotEqual(out.returncode, 0)
        session = dict(self.env, SYNTHETICS_SESSION_RUN="run-x")
        out = self.cli("worker", "install", "--claude", str(self.claude), "--launch-agents-dir",
                       str(self.agents), "--no-start", env=session)
        self.assertNotEqual(out.returncode, 0)
        self.assertRegex(out.stderr, "refused inside an employee session|actor_unverified|run_inactive")  # M28
        self.assertFalse(self.agents.exists())

    def test_adapter_install_write_only(self):
        out = self.cli("adapter", "install", "--label", "com.synthetics.adapter.postern", "--keep-alive",
                       "on-failure", "--launch-agents-dir", str(self.agents), "--no-start",
                       "--", "/bin/sleep", "--not-an-option-for-us", "5")
        self.assertEqual(out.returncode, 0, out.stderr)
        plist = plistlib.loads((self.agents / "com.synthetics.adapter.postern.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], ["/bin/sleep", "--not-an-option-for-us", "5"])
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})

    @unittest.skipUnless(sys.platform == "darwin", "macOS launchd integration")
    def test_adapter_install_bootstraps_and_removes_real_job(self):
        label = "com.synthetics.adapter.m25test-" + uuid.uuid4().hex[:12]
        marker = self.root / "ran.txt"
        out = self.cli("adapter", "install", "--label", label, "--keep-alive", "never",
                       "--launch-agents-dir", str(self.agents),
                       "--", "/bin/sh", "-c", "echo ran > '%s'; sleep 30" % marker)
        try:
            self.assertEqual(out.returncode, 0, out.stderr)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "bootstrapped adapter did not run")
        finally:
            removed = self.cli("adapter", "remove", "--label", label, "--launch-agents-dir", str(self.agents))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((self.agents / (label + ".plist")).exists())
        check = subprocess.run(["/bin/launchctl", "print", "gui/%s/%s" % (os.getuid(), label)],
                               capture_output=True, text=True)
        self.assertNotEqual(check.returncode, 0)


if __name__ == "__main__":
    unittest.main()

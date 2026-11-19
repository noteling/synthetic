"""Executor diagnostics report actual guarantees without executing a model or writing the home."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import doctor
from synthetics.store import Store


def digest_tree(home):
    return {str(p.relative_to(home)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode)
            for p in home.rglob("*")
            if p.is_file() and not p.name.endswith(("-shm", "-wal"))}


class ExecutorDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "employee"
        with Store(self.home) as store:
            store.initialize("Example", "developer")

    def tearDown(self):
        self.tmp.cleanup()

    def runtime(self, **changes):
        value = dict(claude=sys.executable, model="claude-test", budget=100, timeout=60,
                     poll=2, retry=60, daily_limit=0, limit_notify=None, limit_allow=[])
        value.update(changes)
        (self.home / "runtime.json").write_text(json.dumps(value))

    def diagnose(self):
        # No health check may log in, query usage, launch a model or start a worker.
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("unexpected process")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")):
            return doctor.diagnose(self.home)

    def test_legacy_runtime_reports_claude_guarantees(self):
        self.runtime()
        report = self.diagnose()
        executor = report["executor"]
        self.assertEqual(executor["backend"], "claude")
        self.assertEqual(executor["executable"], sys.executable)
        self.assertTrue(executor["session_budget_cap"])
        self.assertTrue(executor["usage_cost"])
        self.assertTrue(executor["live_meetings"])
        self.assertNotIn("executor_budget_unsupported", {f["code"] for f in report["findings"]})

    def test_codex_does_not_require_an_inactive_claude_executable(self):
        self.runtime(brain="codex", codex=sys.executable, claude="", model="")
        before = digest_tree(self.home)
        report = self.diagnose()
        self.assertTrue(report["ok"], report["findings"])
        executor = report["executor"]
        self.assertEqual(executor["backend"], "codex")
        self.assertEqual(executor["executable"], sys.executable)
        self.assertFalse(executor["session_budget_cap"])
        self.assertFalse(executor["usage_cost"])
        self.assertFalse(executor["native_tool_allow_list"])
        self.assertEqual(executor["shell_sandbox"], "workspace-write")
        self.assertFalse(executor["live_meetings"])
        codes = {f["code"] for f in report["findings"]}
        self.assertTrue({"executor_budget_unsupported", "executor_cost_unavailable",
                         "executor_tool_allow_list_unavailable", "executor_live_meetings_unsupported"} <= codes)
        self.assertNotIn("claude_not_executable", codes)
        self.assertEqual(before, digest_tree(self.home))

    def test_missing_codex_is_configuration_error_without_running_it(self):
        self.runtime(brain="codex", codex=str(self.home / "missing-codex"), claude="", model="")
        report = self.diagnose()
        self.assertFalse(report["ok"])
        errors = [f for f in report["findings"] if f["level"] == "error"]
        self.assertTrue(any(f["code"] == "runtime_invalid" and "codex" in f["detail"] for f in errors), errors)
        self.assertNotIn("Unsupported runtime fields", " ".join(f["detail"] for f in errors))
        self.assertNotIn("executor", report)

    def test_absent_runtime_does_not_invent_a_selected_backend(self):
        report = self.diagnose()
        self.assertNotIn("executor", report)
        self.assertIn("runtime_absent", {f["code"] for f in report["findings"]})


if __name__ == "__main__":
    unittest.main()

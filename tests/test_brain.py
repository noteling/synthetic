import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest

from synthetics.brain import BrainError, ClaudeBrain


class ClaudeBrainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "employee"
        self.home.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def executable(self, body):
        path = Path(self.temporary.name) / "fake-claude"
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o700)
        return str(path)

    def test_success_uses_fresh_isolated_session_and_keeps_output(self):
        executable = self.executable(
            "import json, os, sys\n"
            "context = sys.stdin.read()\n"
            "assert 'Morgan' in context\n"
            "assert os.getcwd().endswith('/workspace')\n"
            "args = sys.argv[1:]\n"
            "assert args[args.index('--setting-sources') + 1] == ''\n"
            "assert '--strict-mcp-config' in args\n"
            "assert '--no-session-persistence' in args\n"
            "assert '--resume' not in args\n"
            "config = json.loads(args[args.index('--mcp-config') + 1])\n"
            "assert config['mcpServers']['synthetics']['args'][-2:] == ['--run-id', 'run-123']\n"
            "assert args[args.index('--model') + 1] == 'sonnet'\n"
            "print(json.dumps({'type': 'result', 'result': 'Saved the next step.', 'total_cost_usd': 0.02, 'is_error': False}))\n"
        )
        result = ClaudeBrain(self.home, executable=executable, model="sonnet").run({"identity": {"name": "Morgan"}, "run": {"id": "run-123"}})
        self.assertEqual(result.summary, "Saved the next step.")
        self.assertEqual(result.cost, 0.02)
        output = self.home / "runs" / "run-123.claude.json"
        self.assertTrue(output.exists())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_model_error_is_not_success(self):
        executable = self.executable("print('{\"result\": \"budget exceeded\", \"is_error\": true}')\n")
        with self.assertRaisesRegex(BrainError, "unsuccessful"):
            ClaudeBrain(self.home, executable=executable).run({})

    def test_budget_stop_subtype_is_explicit_even_when_cli_reports_no_error(self):
        for is_error in (False, True):
            with self.subTest(is_error=is_error):
                payload = {"type": "result", "subtype": "error_max_budget_usd", "is_error": is_error,
                           "total_cost_usd": 3.05, "errors": ["TEST_SECRET_MUST_NOT_APPEAR"]}
                executable = self.executable("import json,sys\nsys.stdin.read()\nprint(json.dumps(%r))\n" % payload)
                with self.assertRaisesRegex(BrainError, "budget") as raised:
                    ClaudeBrain(self.home, executable=executable, budget=3).run({})
                self.assertIn("3.00", str(raised.exception))
                self.assertIn("cap", str(raised.exception))
                self.assertNotIn("TEST_SECRET_MUST_NOT_APPEAR", str(raised.exception))

    def test_budget_stop_cannot_be_mistaken_for_successful_summary(self):
        payload = {"type": "result", "subtype": "error_max_budget_usd", "is_error": False,
                   "result": "Partial work only", "total_cost_usd": 1.05}
        executable = self.executable("import json,sys\nsys.stdin.read()\nprint(json.dumps(%r))\n" % payload)
        with self.assertRaisesRegex(BrainError, "budget"):
            ClaudeBrain(self.home, executable=executable).run({})

    def test_operator_grants_are_passed_to_cli_and_preserved_in_context(self):
        read_root = Path(self.temporary.name) / "reference"
        write_root = Path(self.temporary.name) / "internal projects"
        read_root.mkdir()
        write_root.mkdir()
        grants = [
            {"path": str(read_root), "mode": "read", "purpose": "Inspect assigned source", "granted_by": "david"},
            {"path": str(write_root), "mode": "write", "purpose": "Create internal projects", "granted_by": "david"},
        ]
        executable = self.executable(
            "import json, sys\n"
            "context = json.loads(sys.stdin.read().split('\\n\\n', 1)[1])\n"
            "assert context['access_grants'][0]['mode'] == 'read'\n"
            "assert context['access_grants'][1]['purpose'] == 'Create internal projects'\n"
            "args = sys.argv[1:]\n"
            "offset = args.index('--add-dir')\n"
            "assert args[offset + 1:offset + 3] == %r\n" % [str(read_root.resolve()), str(write_root.resolve())] +
            "print(json.dumps({'result': 'Used the provisioned roots.'}))\n"
        )
        result = ClaudeBrain(self.home, executable=executable).run({"access_grants": grants})
        self.assertEqual(result.summary, "Used the provisioned roots.")

    def test_absent_revoked_missing_and_duplicate_grants_do_not_expand_roots(self):
        root = Path(self.temporary.name) / "assigned"
        root.mkdir()
        brain = ClaudeBrain(self.home)
        self.assertNotIn("--add-dir", brain.command())
        grants = [
            {"path": str(root), "mode": "write", "revoked_at": "2026-11-15T00:00:00Z"},
            {"path": str(root), "mode": "unexpected"},
            {"path": str(root / "missing"), "mode": "write"},
            {"path": None, "mode": "write"},
        ]
        self.assertNotIn("--add-dir", brain.command(grants=grants))
        grants.extend([{"path": str(root), "mode": "read"}, {"path": str(root / "."), "mode": "write"}])
        command = brain.command(grants=grants)
        self.assertEqual(command.count(str(root.resolve())), 1)

    def test_nonzero_exit_invalid_json_and_empty_result_fail(self):
        cases = [
            ("raise SystemExit(7)\n", "status 7"),
            ("print('not JSON')\n", "invalid JSON"),
            ("print('{}')\n", "no final summary"),
            ("print('[]')\n", "unsuccessful"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                executable = self.executable(body)
                with self.assertRaisesRegex(BrainError, expected):
                    ClaudeBrain(self.home, executable=executable).run({})

    def test_missing_executable_is_actionable(self):
        with self.assertRaisesRegex(BrainError, "Could not start"):
            ClaudeBrain(self.home, executable=str(self.home / "absent")).run({})

    def test_timeout_terminates_descendants_before_they_can_have_late_effects(self):
        artifact = self.home / "late-effect"
        executable = self.executable(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            + repr("import pathlib,time; time.sleep(0.8); pathlib.Path(%r).write_text('too late')" % str(artifact))
            + "])\n"
            "time.sleep(10)\n"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(BrainError, "exceeded"):
            ClaudeBrain(self.home, executable=executable, timeout=0.15).run({})
        self.assertLess(time.monotonic() - started, 4)
        time.sleep(1)
        self.assertFalse(artifact.exists())

    def test_invalid_budget_or_timeout_rejected_before_execution(self):
        for arguments in [{"timeout": 0}, {"budget": 0}, {"timeout": -2}]:
            with self.assertRaises(ValueError):
                ClaudeBrain(self.home, **arguments)

    def test_untrusted_run_id_cannot_escape_log_folder(self):
        executable = self.executable("print('{\"result\": \"ok\"}')\n")
        ClaudeBrain(self.home, executable=executable).run({"run": {"id": "../../escaped"}})
        self.assertEqual(len(list((self.home / "runs").glob("*.claude.json"))), 1)
        self.assertFalse((self.home.parent / "escaped.claude.json").exists())


if __name__ == "__main__":
    unittest.main()

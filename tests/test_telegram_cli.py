import contextlib
import io
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from synthetics.cli import main
from synthetics.store import Store
from tests.humans_fixture import write_host_humans
from synthetics.telegram_cli import _start_if_needed, _service_labels


class TelegramCommandTests(unittest.TestCase):
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

    def command(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = main(["--home", str(self.home), "telegram"] + list(args))
        return result, output.getvalue(), errors.getvalue()

    def initialize(self):
        result, output, errors = self.command("init", "--token-file", str(self.token), "--thread", "human-one")
        self.assertEqual(result, 0, errors)
        return Path(json.loads(output)["config"])

    def test_connection_configuration_is_private_idempotent_and_not_silently_rebound(self):
        config = self.initialize()
        original = config.read_bytes()
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token.read_text(), config.read_text())
        self.initialize()
        result, _, errors = self.command("init", "--token-file", str(self.token), "--thread", "other-conversation")
        self.assertEqual(result, 1)
        self.assertIn("preserved", errors)
        self.assertEqual(config.read_bytes(), original)

    def test_status_requires_no_credentials_and_does_not_dump_receipt_error_bodies(self):
        config = self.initialize()
        state = Path(json.loads(config.read_text())["state_file"])
        state.write_text(json.dumps({"paired": True, "bot_username": "test_bot", "bound_user_id": 42,
                                    "bound_chat_id": 42, "deliveries": {"m1": {"status": "uncertain", "error": "private error"}}}))
        self.token.unlink()
        result, output, errors = self.command("status")
        self.assertEqual(result, 0, errors)
        value = json.loads(output)
        self.assertEqual(value["delivery_counts"], {"uncertain": 1})
        self.assertEqual(value["thread"], "human-one")
        self.assertNotIn("private error", output)

    def test_transport_exception_does_not_print_provider_url_or_secret(self):
        self.initialize()
        module = ModuleType("synthetics.telegram")
        class BrokenAdapter:
            def __init__(self, path):
                pass
            def run(self):
                raise RuntimeError("https://api.telegram.org/botTEST_SECRET/sendMessage")
        module.TelegramAdapter = BrokenAdapter
        with patch.dict("sys.modules", {"synthetics.telegram": module}):
            result, output, errors = self.command("serve")
        self.assertEqual(result, 1)
        self.assertIn("transport stopped", errors)
        self.assertNotIn("TEST_SECRET", errors + output)

    def test_worker_is_not_installed_before_human_pairing(self):
        self.initialize()
        with patch("synthetics.service.write_launch_agent") as write:
            result, _, errors = self.command("install", "--with-worker")
        self.assertEqual(result, 1)
        self.assertIn("Pair", errors)
        write.assert_not_called()

    def test_background_worker_preserves_login_identity_without_copying_ambient_credentials(self):
        config = self.initialize()
        state = Path(json.loads(config.read_text())["state_file"])
        state.write_text(json.dumps({"paired": True}))
        with patch("synthetics.service.write_launch_agent", return_value=self.root / "service.plist") as write, \
                patch("synthetics.telegram_cli._start_if_needed", return_value={"loaded": True}), \
                patch("synthetics.telegram_cli.shutil.which", return_value="/usr/local/bin/claude"), \
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-secret-do-not-copy"}):
            result, output, errors = self.command("install", "--with-worker", "--claude", sys.executable)
        self.assertEqual(result, 0, errors)
        self.assertEqual(write.call_count, 2)
        adapter, worker = write.call_args_list
        self.assertTrue(adapter.kwargs["keep_alive"])
        # M15: deliberate stops exit 0, so the worker restarts only after a crash.
        self.assertEqual(worker.kwargs["keep_alive"], "on-failure")
        argv = worker.args[1]
        self.assertIn("--daily-limit", argv)
        self.assertEqual(argv[argv.index("--limit-notify") + 1], "david")
        self.assertEqual(argv[argv.index("--limit-allow") + 1], "david")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv[argv.index("--claude") + 1], str(Path(sys.executable).absolute()))
        environment = worker.kwargs["environment"]
        self.assertTrue(environment["USER"])
        self.assertEqual(environment["USER"], environment["LOGNAME"])
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("test-secret-do-not-copy", output)

    def test_start_loaded_running_job_preserves_its_current_process(self):
        status = subprocess.CompletedProcess([], 0, stdout="service = {\n state = running\n pid = 42\n}\n")
        with patch("synthetics.telegram_cli.subprocess.run", return_value=status), \
                patch("synthetics.service.start_launch_agent") as start, \
                patch("synthetics.service.bootstrap_launch_agent") as bootstrap:
            result = _start_if_needed(self.root / "com.synthetics.test.plist")
        self.assertTrue(result["already_loaded"])
        start.assert_not_called()
        bootstrap.assert_not_called()

    def test_start_loaded_stopped_job_dispatches_start_without_bootstrapping_again(self):
        status = subprocess.CompletedProcess([], 0, stdout="service = {\n state = not running\n last exit code = 130\n}\n")
        with patch("synthetics.telegram_cli.subprocess.run", return_value=status), \
                patch("synthetics.service.start_launch_agent", return_value={"start_requested": True}) as start, \
                patch("synthetics.service.bootstrap_launch_agent") as bootstrap:
            result = _start_if_needed(self.root / "com.synthetics.test.plist")
        self.assertTrue(result["start_requested"])
        start.assert_called_once_with("com.synthetics.test")
        bootstrap.assert_not_called()

    def test_restart_worker_only_is_a_single_request_and_both_restart_worker_last(self):
        config = self.initialize()
        with Store(self.home) as store:
            identity = store.identity()
        adapter, worker = _service_labels(config, identity)
        with patch("synthetics.service.restart_launch_agent", side_effect=lambda label: {"label": label}) as restart:
            result, output, errors = self.command("restart", "--worker-only")
        self.assertEqual(result, 0, errors)
        self.assertEqual([call.args[0] for call in restart.call_args_list], [worker])
        self.assertEqual(json.loads(output)["restart_requests"], [{"label": worker}])
        with patch("synthetics.service.restart_launch_agent", side_effect=lambda label: {"label": label}) as restart:
            result, _, errors = self.command("restart", "--with-worker")
        self.assertEqual(result, 0, errors)
        self.assertEqual([call.args[0] for call in restart.call_args_list], [adapter, worker])

    def test_restart_errors_return_failure_without_claiming_success(self):
        self.initialize()
        with patch("synthetics.service.restart_launch_agent", side_effect=subprocess.CalledProcessError(3, ["launchctl"])):
            result, output, errors = self.command("restart", "--worker-only")
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertIn("restart request failed", errors)



class WorkerClaudeTests(unittest.TestCase):
    """Worker plists name an explicit Claude executable (human-channel.md, 2026-11-17)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def executable(self, name):
        path = self.root / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_explicit_path_wins(self):
        from synthetics.telegram_cli import _worker_claude
        explicit, native = self.executable("explicit"), self.executable("native")
        self.assertEqual(_worker_claude(str(explicit), native=native), str(explicit))

    def test_explicit_path_must_be_executable(self):
        from synthetics.telegram_cli import _worker_claude
        plain = self.root / "plain"
        plain.write_text("x")
        for bad in (plain, self.root / "missing"):
            with self.assertRaises(ValueError):
                _worker_claude(str(bad), native=self.root / "none")

    def test_native_install_preferred_over_path_lookup(self):
        from synthetics.telegram_cli import _worker_claude
        native = self.executable("native")
        with patch("synthetics.telegram_cli.shutil.which", return_value="/opt/homebrew/bin/claude"):
            self.assertEqual(_worker_claude(None, native=native), str(native))

    def test_path_lookup_when_no_native_install(self):
        from synthetics.telegram_cli import _worker_claude
        with patch("synthetics.telegram_cli.shutil.which", return_value="/usr/local/bin/claude"):
            self.assertEqual(_worker_claude(None, native=self.root / "none"), "/usr/local/bin/claude")
        with patch("synthetics.telegram_cli.shutil.which", return_value=None):
            with self.assertRaises(ValueError):
                _worker_claude(None, native=self.root / "none")


if __name__ == "__main__":
    unittest.main()

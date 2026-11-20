"""Configured startup, provisioning and diagnostics preserve the owner's connection selection.

Only temporary homes and inert executables are used; no model or launchd job is started.
"""

import hashlib
import io
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from synthetics import brainconfig, doctor, install, runtime
from synthetics.cli import main, parser
from synthetics.store import Store


def baseline(**changes):
    value = dict(claude=sys.executable, model="claude-opus-5-5", budget=100.0,
                 timeout=600.0, poll=2.0, retry=60.0, daily_limit=0.0,
                 limit_notify=None, limit_allow=[])
    value.update(changes)
    return value


class ConfiguredControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "employee"
        with Store(self.home) as store:
            store.initialize("Example", "developer")
        self.path = self.root / "connection.json"
        self.payload = dict(version=1, executor=dict(adapter="codex-cli", executable=sys.executable),
                            connection=dict(protocol="openai-responses", model="company-engineering",
                                            base_url="https://private-gateway.example/v1",
                                            auth={"type": "bearer-env", "variable": "PRIVATE_GATEWAY_TOKEN"},
                                            headers_from_env={"X-Private-Account": "PRIVATE_ACCOUNT"}))
        self.path.write_text(json.dumps(self.payload))
        self.configuration = brainconfig.load(self.path)
        self.config = baseline(brain="configured", claude="", model=self.configuration.model,
                               brain_config=str(self.configuration.path),
                               brain_config_sha256=self.configuration.fingerprint)
        self.pin = {"path": str(self.configuration.path), "sha256": self.configuration.fingerprint}

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--home", str(self.home)] + list(args))
        return status, out.getvalue(), err.getvalue()

    def test_runtime_accepts_only_the_pinned_connection_and_model(self):
        normalized = runtime.validate(self.config, allowed_config=self.pin)
        self.assertEqual(normalized["model"], "company-engineering")
        self.assertEqual(normalized["claude"], "")
        for changes in ({"model": "other-model"}, {"brain_config_sha256": "0" * 64},
                        {"brain_config": str(self.root / "other.json")}, {"claude": sys.executable},
                        {"codex": sys.executable}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runtime.validate(dict(self.config, **changes), allowed_config=self.pin)
        with self.assertRaisesRegex(ValueError, "pinned"):
            runtime.validate(baseline(), allowed_config=self.pin)
        with self.assertRaisesRegex(ValueError, "provisioned"):
            runtime.validate(self.config, allowed_config={})

    def test_changed_file_fails_closed_even_when_a_legacy_runtime_is_available(self):
        controller = runtime.Controller(self.home, self.config)
        controller.load()
        self.payload["connection"]["base_url"] = "https://different-gateway.example/v1"
        self.path.write_text(json.dumps(self.payload))
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, baseline())
        runtime.write_atomic(self.home / runtime.LAST_GOOD_FILE, baseline())
        with self.assertRaisesRegex(ValueError, "Configured runtime baseline is invalid"):
            controller.load()

    def test_saved_backend_switch_is_refused_and_safe_pinned_baseline_is_used(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, baseline())
        runtime.write_atomic(self.home / runtime.LAST_GOOD_FILE, baseline())
        controller = runtime.Controller(self.home, self.config)
        result = controller.load()
        self.assertEqual(result["source"], "baseline")
        self.assertEqual(result["effective"]["brain"], "configured")
        self.assertTrue(all("pinned" in problem for problem in result["problems"]))

    def test_operational_settings_survive_reload_without_changing_the_selection(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, dict(self.config, timeout=900, poll=4))
        controller = runtime.Controller(self.home, self.config)
        self.assertEqual(controller.load()["effective"]["timeout"], 900)
        with Store(self.home) as store:
            controller.start(store)
        state = runtime.read_state(self.home)
        self.assertEqual(state["effective"]["poll"], 4)
        self.assertEqual(state["policy"]["allowed_config"], self.pin)
        self.assertEqual(state["policy"]["allowed_models"], [self.configuration.model])
        self.assertEqual(state["policy"]["allowed_claude"], [])
        self.assertNotIn("private-gateway", json.dumps(state))

    def test_unprovisioned_controller_cannot_pick_a_saved_configured_runtime(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, self.config)
        controller = runtime.Controller(self.home, baseline(), allowed_models=[self.configuration.model,
                                                                             "claude-opus-5-5"])
        loaded = controller.load()
        self.assertEqual(loaded["effective"].get("brain", "claude"), "claude")
        self.assertIn("No brain config was provisioned", loaded["problems"][0])

    def test_config_selection_is_not_employee_self_service(self):
        for field in ("brain_config", "brain_config_sha256"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "manager or operator"):
                runtime.check_self_service({field: self.config[field]}, self.config, self.config)

    def test_runtime_requests_change_operational_settings_but_never_the_connection(self):
        controller = runtime.Controller(self.home, self.config, label="com.synthetics.worker.fixture", mode="exit")
        with Store(self.home) as store:
            controller.start(store)
            for settings in ({"model": "other-model"}, {"brain_config_sha256": "0" * 64},
                             {"brain": "claude", "claude": sys.executable, "model": "claude-opus-5-5",
                              "brain_config": None, "brain_config_sha256": None}):
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    store.runtime_request(settings, "fixture selection change", "operator")
            request = store.runtime_request({"timeout": 900}, "fixture timeout", "operator")
            with self.assertRaises(runtime.SelfRestart):
                controller.idle(store)
        saved = json.loads((self.home / runtime.CONFIG_FILE).read_text())
        self.assertEqual(saved["request_id"], request["id"])
        self.assertEqual(saved["timeout"], 900)
        self.assertEqual(saved["brain_config_sha256"], self.configuration.fingerprint)

    def test_cannot_publish_stale_claude_capabilities_for_configured_worker(self):
        controller = runtime.Controller(self.home, self.config)
        with Store(self.home) as store, patch("synthetics.runtime.write_json", side_effect=PermissionError("fixture")):
            with self.assertRaisesRegex(ValueError, "Cannot publish configured executor state"):
                controller.start(store)

    def test_cli_rejects_provider_model_and_allowlist_overrides_before_factory(self):
        for command in ("run", "serve", "worker"):
            prefix = [command] + (["install", "--no-start"] if command == "worker" else [])
            options = [("--model", "other"), ("--brain", "codex"), ("--claude", "/another/claude"),
                       ("--codex", "/another/codex")]
            if command != "run":
                options += [("--allow-model", "other"), ("--allow-claude", "/another/claude"),
                            ("--allow-codex", "/another/codex")]
            for option in options:
                with self.subTest(command=command, option=option), patch("synthetics.brain.make_brain") as factory:
                    code, _, error = self.call(*(prefix + ["--brain-config", str(self.path)] + list(option)))
                    self.assertEqual(code, 1, error)
                    self.assertIn("owns executor/model selection", error)
                    factory.assert_not_called()

    def test_run_passes_configuration_to_factory_without_an_invented_claude_model(self):
        fake = SimpleNamespace(configuration=self.configuration, backend="configured")
        with patch("synthetics.brain.make_brain", return_value=fake) as factory, \
                patch("synthetics.engine.Engine.run", return_value={"fixture": True}) as run:
            code, _, error = self.call("run", "--brain-config", str(self.path))
        self.assertEqual(code, 0, error)
        self.assertEqual(factory.call_args.kwargs["brain_config"], str(self.path))
        self.assertIsNone(factory.call_args.kwargs["model"])
        run.assert_called_once()

    def test_serve_publishes_configured_selection_without_runtime_config_flag(self):
        fake = SimpleNamespace(configuration=self.configuration, backend="configured")
        def serve(engine, **kwargs):
            with Store(self.home) as store:
                engine.runtime.start(store)
        with patch("synthetics.brain.make_brain", return_value=fake), \
                patch("synthetics.engine.Engine.serve", autospec=True, side_effect=serve), \
                patch("synthetics.install.resolve_claude", side_effect=AssertionError("legacy lookup")), \
                patch("synthetics.install.resolve_codex", side_effect=AssertionError("legacy lookup")):
            code, _, error = self.call("serve", "--brain-config", str(self.path))
        self.assertEqual(code, 0, error)
        state = runtime.read_state(self.home)
        self.assertEqual(state["effective"]["brain"], "configured")
        self.assertEqual(state["policy"]["allowed_config"], self.pin)

    def test_worker_install_argv_has_one_config_path_and_no_provider_coupling(self):
        with patch("synthetics.install.resolve_claude", side_effect=AssertionError("legacy lookup")), \
                patch("synthetics.install.resolve_codex", side_effect=AssertionError("legacy lookup")):
            code, output, error = self.call("worker", "install", "--brain-config", str(self.path),
                                            "--no-start", "--launch-agents-dir", str(self.root / "agents"))
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertNotIn("service", result)
        argv = plistlib.loads(Path(result["plist"]).read_bytes())["ProgramArguments"]
        self.assertEqual(argv.count("--brain-config"), 1)
        self.assertEqual(argv[argv.index("--brain-config") + 1], str(self.path.resolve()))
        for flag in ("--claude", "--codex", "--brain", "--model"):
            self.assertNotIn(flag, argv)
        args = parser().parse_args(argv[4:])
        self.assertEqual(args.brain_config, str(self.path.resolve()))

    def test_empty_or_invalid_config_never_falls_back_to_legacy_execution(self):
        corrupt = self.root / "corrupt.json"
        corrupt.write_text("{invalid")
        for path in ("", str(self.root / "missing.json"), str(corrupt)):
            for command in ("run", "serve", "worker", "doctor"):
                prefix = [command] + (["install", "--no-start", "--launch-agents-dir", str(self.root / "agents")]
                                      if command == "worker" else [])
                with self.subTest(path=path, command=command), \
                        patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")), \
                        patch.object(subprocess, "run", side_effect=AssertionError("unexpected process")):
                    code, _, error = self.call(*(prefix + ["--brain-config", path]))
                    self.assertEqual(code, 1, error)
        self.assertFalse((self.root / "agents").exists())

    def test_delegated_worker_install_cannot_change_the_connection(self):
        def refuse(code, message):
            raise ValueError(code + ": " + message)
        with self.assertRaisesRegex(ValueError, "executor_owner_only"):
            install.delegated_install_params("unused", SimpleNamespace(brain_config=str(self.path)), refuse)

    def tree(self):
        return {str(p.relative_to(self.home)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.home.rglob("*") if p.is_file() and not p.name.endswith(("-wal", "-shm"))}

    def diagnose(self, **kwargs):
        with patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")), \
                patch.object(subprocess, "run", side_effect=AssertionError("unexpected process")):
            return doctor.diagnose(self.home, **kwargs)

    def assert_private(self, report):
        text = json.dumps(report)
        for secret in ("private-gateway.example", "PRIVATE_GATEWAY_TOKEN", "PRIVATE_ACCOUNT", "x-private-account"):
            self.assertNotIn(secret, text)

    def test_doctor_inspects_explicit_config_without_runtime_or_credential_access(self):
        before = self.tree()
        report = self.diagnose(brain_config=str(self.path))
        self.assertEqual(report["executor"]["backend"], "configured")
        self.assertEqual(report["brain_config"], self.configuration.metadata())
        self.assertFalse(report["executor"]["live_meetings"])
        self.assert_private(report)
        self.assertEqual(before, self.tree())

    def test_doctor_reports_persisted_config_and_rejects_changed_config_without_fallback(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, self.config)
        report = self.diagnose()
        self.assertEqual(report["executor"]["model"], self.configuration.model)
        self.assert_private(report)
        self.payload["connection"]["model"] = "changed-model"
        self.path.write_text(json.dumps(self.payload))
        report = self.diagnose()
        self.assertFalse(report["ok"])
        self.assertNotIn("executor", report)
        self.assertIn("runtime_invalid", {finding["code"] for finding in report["findings"]})


if __name__ == "__main__":
    unittest.main()

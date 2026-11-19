"""Executor selection survives install, runtime loading and ordinary employee runs."""

import io
import json
import plistlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from synthetics import install, runtime
from synthetics.cli import main, parser
from synthetics.store import Store


def baseline(**changes):
    value = dict(claude=sys.executable, model="claude-opus-5-5", budget=100.0,
                 timeout=600.0, poll=2.0, retry=60.0, daily_limit=0.0,
                 limit_notify=None, limit_allow=[])
    value.update(changes)
    return value


class ExecutorRuntimeTests(unittest.TestCase):
    def test_old_config_has_no_new_default_fields_or_digest_change(self):
        old = baseline()
        result = runtime.validate(old)
        self.assertEqual(result, old)
        self.assertEqual(runtime.digest(result), runtime.digest(old))
        self.assertNotIn("brain", result)
        self.assertNotIn("codex", result)

    def test_codex_config_needs_no_claude_install_and_can_use_cli_model_default(self):
        config = baseline(brain="codex", codex=sys.executable, claude="", model="")
        self.assertEqual(runtime.validate(config)["brain"], "codex")
        runtime.validate(config, allowed_models=[""], allowed_claude=[], allowed_codex=[sys.executable])
        with self.assertRaisesRegex(ValueError, "allowed executable"):
            runtime.validate(config, allowed_models=[""], allowed_claude=[], allowed_codex=[])

    def test_codex_cannot_silently_take_the_claude_model(self):
        with self.assertRaisesRegex(ValueError, "Codex.*model|model.*Codex"):
            runtime.validate(baseline(brain="codex", codex=sys.executable))

    def test_provider_and_codex_path_are_not_employee_self_service(self):
        for field in ({"brain": "codex"}, {"codex": sys.executable}):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "manager or operator"):
                runtime.check_self_service(field, baseline(), baseline())

    def test_controller_keeps_selected_executor_and_optional_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            config = baseline(brain="codex", codex=sys.executable, claude="", model="", stream_output=False)
            controller = runtime.Controller(folder, config)
            loaded = controller.load()["effective"]
            self.assertEqual(loaded["brain"], "codex")
            self.assertEqual(loaded["codex"], sys.executable)
            self.assertFalse(loaded["stream_output"])
            self.assertEqual(controller.allowed_codex, [sys.executable])

    def test_persisted_switch_requires_provisioned_codex_path(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = baseline(brain="codex", codex=sys.executable, claude="", model="")
            runtime.write_atomic(home / runtime.CONFIG_FILE, config)
            blocked = runtime.Controller(home, baseline(), allowed_models=["", "claude-opus-5-5"])
            self.assertNotEqual(blocked.load()["source"], "file")
            allowed = runtime.Controller(home, baseline(), allowed_models=["", "claude-opus-5-5"],
                                         allowed_codex=[sys.executable])
            self.assertEqual(allowed.load()["effective"]["brain"], "codex")

    def test_executor_settings_parse_without_numeric_conversion(self):
        self.assertEqual(runtime.parse_assignments(["brain=codex", "codex=" + sys.executable]),
                         {"brain": "codex", "codex": sys.executable})

    def test_operator_switch_uses_explicit_codex_allowlist_but_self_switch_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            with Store(folder) as store:
                store.initialize("A", "developer")
                blocked = runtime.Controller(folder, baseline(), allowed_models=["", "claude-opus-5-5"])
                blocked.start(store)
                change = {"brain": "codex", "codex": sys.executable, "model": ""}
                with self.assertRaisesRegex(ValueError, "allowed executable"):
                    store.runtime_request(change, "switch backend", "operator", key="blocked")
                allowed = runtime.Controller(folder, baseline(), allowed_models=["", "claude-opus-5-5"],
                                             allowed_codex=[sys.executable])
                allowed.start(store)
                request = store.runtime_request(change, "switch backend", "operator", key="allowed")
                self.assertEqual(request["proposed"]["brain"], "codex")
                run = store.run_begin()
            with Store(folder, run_id=run["id"]) as store:
                with self.assertRaisesRegex(ValueError, "manager or operator"):
                    store.runtime_restart("switch myself", "self", settings=change)

    def test_delegated_install_cannot_add_codex_or_executor_allowlists(self):
        def refuse(code, message):
            raise ValueError(code + ": " + message)
        for changes in ({"brain": "codex"}, {"codex": sys.executable}, {"allow_codex": [sys.executable]}):
            args = dict(brain="claude", codex=None, allow_codex=[])
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "executor_owner_only"):
                install.delegated_install_params("unused", SimpleNamespace(**args), refuse)

    def test_codex_start_fails_if_active_executor_state_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as folder:
            config = baseline(brain="codex", codex=sys.executable, claude="", model="")
            controller = runtime.Controller(folder, config)
            with Store(folder) as store:
                store.initialize("A", "developer")
                with patch("synthetics.runtime.write_json", side_effect=PermissionError("fixture")):
                    with self.assertRaisesRegex(ValueError, "Cannot publish Codex executor state"):
                        controller.start(store)


class ExecutorCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "employee"
        with Store(self.home) as store:
            self.identity = store.initialize("A", "developer")
            store.feed("owner", "Inspect your current work", source="fixture")

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--home", str(self.home)] + list(args))
        return status, out.getvalue(), err.getvalue()

    def test_parser_defaults_are_provider_neutral_until_selection(self):
        args = parser().parse_args(["run", "--brain", "codex"])
        self.assertEqual(args.brain, "codex")
        self.assertIsNone(args.model)

    def test_run_chooses_codex_and_preserves_employee_context(self):
        calls = []
        class FakeBrain:
            def run(self, context):
                calls.append(context)
                return SimpleNamespace(summary="done", cost=None)
        with patch("synthetics.brain.make_brain", return_value=FakeBrain(), create=True) as factory:
            code, out, err = self.call("run", "--brain", "codex", "--codex", sys.executable)
        self.assertEqual(code, 0, err)
        self.assertEqual(factory.call_args.kwargs["brain"], "codex")
        self.assertEqual(factory.call_args.kwargs["model"], "")
        self.assertEqual(calls[0]["identity"]["id"], self.identity["id"])
        self.assertEqual(calls[0]["events"][0]["body"], "Inspect your current work")

    def test_codex_only_worker_install_writes_explicit_argv_without_launch(self):
        agents = Path(self.temp.name) / "agents"
        with patch("synthetics.install.resolve_claude", side_effect=AssertionError("inactive Claude resolved")):
            code, out, err = self.call("worker", "install", "--brain", "codex", "--codex", sys.executable,
                                       "--runtime-config", "--daily-limit", "0", "--no-start",
                                       "--launch-agents-dir", str(agents))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        argv = plistlib.loads(Path(result["plist"]).read_bytes())["ProgramArguments"]
        self.assertEqual(argv[argv.index("--brain") + 1], "codex")
        self.assertEqual(argv[argv.index("--codex") + 1], sys.executable)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--claude", argv)

    def test_empty_claude_model_keeps_existing_default_in_worker_argv(self):
        argv = install.worker_argv(self.home, sys.executable, "", 100, 600)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")

    def test_codex_serve_publishes_runtime_even_without_runtime_config_flag(self):
        captured = []
        def no_model_serve(engine, **kwargs):
            captured.append(engine.runtime)
            with Store(self.home) as store:
                engine.runtime.start(store)
        with patch("synthetics.engine.Engine.serve", autospec=True, side_effect=no_model_serve):
            code, out, err = self.call("serve", "--brain", "codex", "--codex", sys.executable)
        self.assertEqual(code, 0, err)
        self.assertIsInstance(captured[0], runtime.Controller)
        state = runtime.read_state(self.home)
        self.assertEqual(state["effective"]["brain"], "codex")
        self.assertEqual(state["policy"]["allowed_codex"], [sys.executable])

    def codex_serve_options(self):
        # Permit both models/executables, so a mismatched saved config is genuinely valid;
        # rejection must come from explicit executor selection, not baseline fallback.
        return ["serve", "--brain", "codex", "--codex", sys.executable,
                "--allow-model", "", "--allow-model", "claude-opus-5-5",
                "--allow-claude", sys.executable]

    def test_plain_codex_serve_refuses_valid_saved_claude_selection_before_execution(self):
        config = self.home / runtime.CONFIG_FILE
        runtime.write_atomic(config, baseline())
        before = config.read_bytes()
        with patch("synthetics.engine.Engine.serve") as serve:
            code, out, err = self.call(*self.codex_serve_options())
        self.assertEqual(code, 1)
        self.assertIn("explicitly selects Codex", err)
        self.assertIn("runtime.json selects claude", err)
        self.assertIn("--runtime-config", err)
        serve.assert_not_called()
        self.assertEqual(config.read_bytes(), before)
        with Store(self.home) as store:
            self.assertEqual(store.run_list(), [])

    def test_plain_codex_serve_also_refuses_claude_last_good_fallback(self):
        (self.home / runtime.CONFIG_FILE).write_text("{broken")
        runtime.write_atomic(self.home / runtime.LAST_GOOD_FILE, baseline())
        with patch("synthetics.engine.Engine.serve") as serve:
            code, out, err = self.call(*self.codex_serve_options())
        self.assertEqual(code, 1)
        self.assertIn("last-good settings select claude", err)
        serve.assert_not_called()

    def test_explicit_runtime_config_allows_saved_selection_to_override_launch_baseline(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, baseline())
        captured = []
        def stored_mode(engine, **kwargs):
            captured.append(engine.runtime.load()["effective"].get("brain", "claude"))
        with patch("synthetics.engine.Engine.serve", autospec=True, side_effect=stored_mode):
            code, out, err = self.call(*self.codex_serve_options(), "--runtime-config",
                                       str(self.home / runtime.CONFIG_FILE))
        self.assertEqual(code, 0, err)
        self.assertEqual(captured, ["claude"])

    def test_claude_launch_can_explicitly_follow_owner_provisioned_codex_runtime(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE,
                             baseline(brain="codex", codex=sys.executable, model=""))
        captured = []
        def stored_mode(engine, **kwargs):
            captured.append(engine.runtime.load()["effective"]["brain"])
        with patch("synthetics.engine.Engine.serve", autospec=True, side_effect=stored_mode):
            code, out, err = self.call("serve", "--claude", sys.executable,
                                       "--runtime-config", str(self.home / runtime.CONFIG_FILE),
                                       "--allow-model", "", "--allow-model", "claude-opus-5-5",
                                       "--allow-codex", sys.executable)
        self.assertEqual(code, 0, err)
        self.assertEqual(captured, ["codex"])


if __name__ == "__main__":
    unittest.main()

"""Independent configured-executor regressions; all runtime homes are fixtures."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from synthetics import floor, runtime
from synthetics.brain import make_brain
from synthetics.engine import Engine
from synthetics.store import SESSION_ENV, Store


class ConfiguredExecutorReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "employee"
        self.observation = self.root / "observation.json"
        with Store(self.home) as store:
            store.initialize("Review", "developer")
            store.feed("owner", "Continue the assigned work.")

    def configure(self, adapter="codex-cli", gateway=False):
        binary = self.root / "fixture-runtime"
        observe = repr(str(self.observation))
        fixture_root = repr(str(self.root.resolve()))
        code = ("#!" + sys.executable + "\n"
                "import json,os,sys\nfrom pathlib import Path\n"
                "if 'login' in sys.argv and 'status' in sys.argv:\n"
                " print('Logged in using ChatGPT');raise SystemExit(0)\n"
                "sys.stdin.read()\n"
                "connection_home=os.environ.get('CODEX_HOME')\n"
                "managed_home=Path(connection_home).resolve() if connection_home and Path(" + fixture_root + ") in Path(connection_home).resolve().parents else None\n"
                "auth=managed_home/'auth.json' if managed_home else None\n"
                "environment={k:os.environ[k] for k in ('CODEX_HOME','SYNTHETICS_HOME','SYNTHETICS_SESSION_RUN') if k in os.environ}\n"
                "observed={'environment':environment,'present_environment':list(os.environ),'args':sys.argv[1:],"
                "'auth_existed':bool(auth and auth.exists()),"
                "'home_mode':(managed_home.stat().st_mode & 0o777) if managed_home else None}\n"
                "if auth and auth.exists(): auth.unlink()\n"
                "Path(" + observe + ").write_text(json.dumps(observed))\n")
        if adapter == "codex-cli":
            code += ("print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Done'}}))\n"
                     "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n")
        else:
            code += "print(json.dumps({'type':'result','subtype':'success','result':'Done','is_error':False}))\n"
        binary.write_text(code)
        binary.chmod(0o700)
        connection = {"protocol": "claude-login" if adapter == "claude-cli" else "chatgpt-login"}
        if gateway:
            connection = dict(protocol="openai-responses", model="company-model",
                              base_url="https://gateway.example/v1",
                              auth={"type": "bearer-env", "variable": "COMPANY_MODEL_TOKEN"})
        config = dict(version=1, executor={"adapter": adapter, "executable": str(binary)},
                      connection=connection)
        path = self.root / "brain.json"
        path.write_text(json.dumps(config))
        return path

    def test_configured_claude_login_does_not_inherit_an_api_or_cloud_route(self):
        path = self.configure("claude-cli")
        routes = {"ANTHROPIC_API_KEY": "fixture-api-key",
                  "ANTHROPIC_AUTH_TOKEN": "fixture-auth-token",
                  "ANTHROPIC_BASE_URL": "https://unselected.example",
                  "CLAUDE_CODE_OAUTH_TOKEN": "fixture-oauth-token",
                  "CLAUDE_CODE_USE_BEDROCK": "1",
                  "CLAUDE_CODE_USE_VERTEX": "1",
                  "CLAUDE_CODE_USE_FOUNDRY": "1"}
        with patch.dict(os.environ, routes):
            brain = make_brain(self.home, brain_config=path, stream_output=False)
            Engine(self.home, brain).run()
        observed = json.loads(self.observation.read_text())
        for variable in routes:
            with self.subTest(variable=variable):
                self.assertNotIn(variable, observed["present_environment"])
        actual = observed["environment"]
        self.assertEqual(actual["SYNTHETICS_HOME"], str(self.home.resolve()))
        self.assertTrue(actual["SYNTHETICS_SESSION_RUN"].startswith("run-"))

    def test_gateway_uses_a_private_temporary_auth_home_and_preserves_host_login(self):
        path = self.configure(gateway=True)
        host_home = self.root / "host-codex-home"
        host_home.mkdir(mode=0o700)
        host_auth = host_home / "auth.json"
        host_auth.write_text('{"fixture":"saved-subscription-login"}')
        with patch.dict(os.environ, {"CODEX_HOME": str(host_home),
                                     "COMPANY_MODEL_TOKEN": "fixture-gateway-token"}):
            brain = make_brain(self.home, brain_config=path)
            Engine(self.home, brain).run()
        observed = json.loads(self.observation.read_text())
        child_home = Path(observed["environment"]["CODEX_HOME"])
        self.assertNotEqual(child_home, host_home)
        self.assertEqual(observed["home_mode"], 0o700)
        self.assertFalse(observed["auth_existed"], "A gateway must not copy the host's saved login")
        self.assertEqual(host_auth.read_text(), '{"fixture":"saved-subscription-login"}')
        self.assertFalse(child_home.exists(), "The gateway runtime home must be cleaned after the session")
        self.assertNotIn("fixture-gateway-token", " ".join(observed["args"]))

    def test_missing_config_fails_without_a_delegate_call_or_consuming_input(self):
        path = self.configure()
        brain = make_brain(self.home, brain_config=path)
        path.unlink()
        with patch.object(brain.delegate, "run") as delegate:
            with self.assertRaises(ValueError):
                Engine(self.home, brain).run()
        delegate.assert_not_called()
        with Store(self.home) as store:
            self.assertEqual(len(store.event_pending()), 1)
            self.assertEqual(store.run_list()[0]["status"], "failed")

    def test_recorded_model_metadata_accepts_the_configured_model_bound(self):
        # The validated config contract permits a 512-character model alias.
        # Run provenance must not reject a configuration that the adapter accepts.
        metadata = dict(adapter="codex-cli", protocol="openai-responses",
                        model="model-" + "x" * 506, config_sha256="a" * 64)
        with Store(self.home) as store:
            run = store.run_begin(executor="configured", executor_config=metadata)
            self.assertEqual(run["executor_config"], metadata)

    def prepare_meeting(self, active):
        key = b"q" * 32
        keyfile = self.root / "stand.key"
        keyfile.write_bytes(key)
        keyfile.chmod(0o600)
        with Store(self.home) as store:
            store.channel_register("stand", Store.credential_hash(key), ["stand:"], "review fixture")
            run = store.run_begin(executor="configured") if active else None
        return keyfile, run

    def assert_meeting_refused(self, keyfile):
        output = io.StringIO()
        with patch.dict(os.environ):
            os.environ.pop(SESSION_ENV, None)
            result = floor.attach(self.home, str(keyfile), stdout=output,
                                  stdin=io.StringIO(json.dumps({"v": 1, "type": "begin",
                                                              "meeting": "review",
                                                              "goal": "Introduce yourself"}) + "\n"))
        self.assertEqual(result, 2)
        self.assertIn("does not support persistent meeting", output.getvalue())
        with Store(self.home) as store:
            self.assertIsNone(store.meeting_pending())

    def test_active_configured_executor_refuses_meeting_before_yield(self):
        keyfile, run = self.prepare_meeting(active=True)
        runtime.write_json(self.home / runtime.STATE_FILE,
                           {"policy": {}, "effective": {"brain": "claude"}})
        self.assert_meeting_refused(keyfile)
        with Store(self.home) as store:
            self.assertEqual(store.run_get(run["id"])["status"], "running")
            with self.assertRaisesRegex(ValueError, "configured executor"):
                store.meeting_begin("review", "stand")

    def test_idle_configured_executor_refuses_meeting_from_published_selection(self):
        keyfile, _ = self.prepare_meeting(active=False)
        runtime.write_json(self.home / runtime.STATE_FILE,
                           {"policy": {}, "effective": {"brain": "configured"}})
        self.assert_meeting_refused(keyfile)


if __name__ == "__main__":
    unittest.main()

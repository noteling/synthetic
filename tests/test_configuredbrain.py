import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from synthetics.brain import BrainError, BrainResult, make_brain
from synthetics.engine import Engine
from synthetics.store import Store


SUCCESS = """
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Work recorded.'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':7}}))
"""


class ConfiguredBrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'employee'
        with Store(self.home) as store:
            self.identity = store.initialize('Morgan', 'maintainer')
            store.feed('owner', 'Investigate the project.')

    def config(self, check='', gateway=False):
        executable = self.root / 'runtime'
        executable.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                              'if "login" in sys.argv and "status" in sys.argv:\n'
                              ' print("Logged in using ChatGPT");raise SystemExit(0)\n'
                              'message=json.loads(sys.stdin.read().split("\\n\\n",1)[1])\n'
                              'args=sys.argv[1:]\n'
                              'settings={args[i+1].split("=",1)[0]:args[i+1].split("=",1)[1] '
                              'for i,a in enumerate(args) if a=="-c"}\n' + check + SUCCESS)
        executable.chmod(0o700)
        connection = {'protocol': 'chatgpt-login'}
        if gateway:
            connection = dict(protocol='openai-responses', model='company-maintainer',
                              base_url='https://gateway.example/v1',
                              auth={'type': 'bearer-env', 'variable': 'OPENAI_API_KEY'},
                              headers_from_env={'X-Account': 'COMPANY_ACCOUNT'})
        value = {'version': 1, 'executor': {'adapter': 'codex-cli', 'executable': './runtime'},
                 'connection': connection}
        path = self.root / 'brain.json'
        path.write_text(json.dumps(value))
        return path

    def test_config_path_drives_real_adapter_and_records_safe_provenance(self):
        path = self.config("""
assert settings['forced_login_method']=='"chatgpt"'
assert message['identity']['name']=='Morgan'
assert message['executor']['adapter']=='codex-cli'
assert message['run']['executor_config']['config_sha256']==message['executor']['config_sha256']
assert 'brain_config' not in message['executor']
""")
        brain = make_brain(self.home, brain_config=path)
        result = Engine(self.home, brain).run()
        with Store(self.home) as store:
            run = store.run_list()[0]
            self.assertEqual(store.identity()['id'], self.identity['id'])
        self.assertEqual(run['status'], 'finished')
        self.assertEqual(run['executor'], 'configured')
        self.assertEqual(run['executor_config'], brain.configuration.metadata())
        self.assertEqual(len(result), 1)
        self.assertFalse(brain.capabilities['live_meetings'])

    def test_gateway_routing_credentials_are_explicit_not_in_argv_or_snapshot(self):
        path = self.config("""
assert settings['forced_login_method']=='"api"'
assert settings['model_provider']=='"synthetics_gateway"'
assert 'https://gateway.example/v1' in settings['model_providers']
assert '"requires_openai_auth" = false' in settings['model_providers']
assert 'OPENAI_API_KEY' in settings['model_providers']
assert os.environ['OPENAI_API_KEY']=='fixture-private-token'
assert os.environ['COMPANY_ACCOUNT']=='fixture-private-account'
assert 'ANTHROPIC_API_KEY' not in os.environ
assert 'OPENAI_BASE_URL' not in os.environ
assert 'OPENAI_API_KEY' in settings['shell_environment_policy.exclude']
assert 'fixture-private-token' not in ' '.join(args)
assert 'fixture-private-account' not in json.dumps(message)
assert args[args.index('--model')+1]=='company-maintainer'
""", gateway=True)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-private-token',
                                     'COMPANY_ACCOUNT': 'fixture-private-account',
                                     'ANTHROPIC_API_KEY': 'unselected', 'OPENAI_BASE_URL': 'unselected'}):
            brain = make_brain(self.home, brain_config=path)
            Engine(self.home, brain).run()
        with Store(self.home) as store:
            self.assertEqual(store.run_list()[0]['status'], 'finished')
            provenance = json.dumps(store.run_list()[0]['executor_config'])
        self.assertNotIn('gateway.example', provenance)
        self.assertNotIn('OPENAI_API_KEY', provenance)
        self.assertNotIn('fixture-private', provenance)

    def test_missing_gateway_credentials_never_launch_or_acknowledge_work(self):
        brain = make_brain(self.home, brain_config=self.config(gateway=True))
        with patch.dict(os.environ, {}, clear=True), patch('synthetics.codexbrain.subprocess.Popen') as popen:
            with self.assertRaisesRegex(BrainError, 'credential environment'):
                Engine(self.home, brain).run()
        popen.assert_not_called()
        with Store(self.home) as store:
            self.assertEqual(store.run_list()[0]['status'], 'failed')
            self.assertEqual(len(store.event_pending()), 1)

    def test_changed_configuration_is_not_silently_reloaded(self):
        path = self.config()
        brain = make_brain(self.home, brain_config=path)
        changed = json.loads(path.read_text())
        changed['connection']['model'] = 'different-model'
        path.write_text(json.dumps(changed))
        with patch('synthetics.codexbrain.subprocess.Popen') as popen:
            with self.assertRaisesRegex(BrainError, 'configuration changed'):
                Engine(self.home, brain).run()
        popen.assert_not_called()
        with Store(self.home) as store:
            self.assertEqual(len(store.event_pending()), 1)

    def test_runtime_updates_preserve_host_connection(self):
        brain = make_brain(self.home, brain_config=self.config())
        settings = dict(brain='configured', brain_config=str(brain.configuration.path),
                        brain_config_sha256=brain.configuration.fingerprint, model=brain.model,
                        budget=80, timeout=120, stream_output=False, daily_limit=0,
                        limit_notify=None, limit_allow=[])
        engine = Engine(self.home, brain)
        engine.apply_runtime(settings)
        self.assertEqual(brain.delegate.timeout, 120)
        self.assertFalse(brain.delegate.stream_output)
        for changed in ({'model': 'different'}, {'brain_config_sha256': '0'*64},
                        {'brain_config': '/different/config.json'}):
            with self.subTest(changed=changed), self.assertRaises(BrainError):
                engine.apply_runtime(dict(settings, **changed))

    def test_run_provenance_rejects_unfiltered_connection_data(self):
        with Store(self.home) as store:
            with self.assertRaises(ValueError):
                store.run_begin(executor='configured', executor_config={'token': 'private'})

    def test_credential_reference_cannot_be_the_adapter_owned_runtime_home(self):
        path = self.config(gateway=True)
        value = json.loads(path.read_text())
        value['connection']['auth']['variable'] = 'CODEX_HOME'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'reference'):
            make_brain(self.home, brain_config=path)


if __name__ == '__main__':
    unittest.main()

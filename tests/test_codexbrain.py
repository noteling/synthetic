import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from synthetics.brain import BrainError, ClaudeBrain, executor_capabilities, make_brain
from synthetics.codexbrain import CodexBrain


SUCCESS = '''
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Saved the next step.'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':7}}))
'''


class CodexBrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'employee'
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def executable(self, body=SUCCESS):
        path = Path(self.tmp.name) / 'fake-codex'
        path.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                        'message=sys.stdin.read()\n' + body)
        path.chmod(0o700)
        return str(path)

    def test_provider_factory_is_explicit_and_defaults_stay_claude(self):
        self.assertIsInstance(make_brain(self.home), ClaudeBrain)
        codex = make_brain(self.home, brain='codex')
        self.assertIsInstance(codex, CodexBrain)
        self.assertEqual(codex.model, '')
        self.assertFalse(codex.capabilities['session_budget_cap'])
        self.assertFalse(codex.capabilities['live_meetings'])
        with self.assertRaisesRegex(ValueError, 'Unknown brain'):
            make_brain(self.home, brain='imaginary')
        caps = executor_capabilities('codex')
        caps['usage_cost'] = True
        self.assertFalse(codex.capabilities['usage_cost'])

    def test_fresh_session_context_mcp_and_login_isolation(self):
        fake = self.executable('''
args=sys.argv[1:]
config={args[i+1].split('=',1)[0]:args[i+1].split('=',1)[1] for i,a in enumerate(args) if a=='-c'}
instructions=json.loads(config['model_instructions_file'])
assert os.stat(instructions).st_mode & 0o777 == 0o600
assert 'continuing' in open(instructions).read()
assert 'Morgan' in message
assert '--run-id' in config['mcp_servers'] and 'run-123' in config['mcp_servers']
assert 'SYNTHETICS_SESSION_RUN' in os.environ
assert os.environ['SYNTHETICS_SESSION_RUN']=='run-123'
assert 'OPENAI_API_KEY' not in os.environ and 'CODEX_API_KEY' not in os.environ
assert 'OPENAI_BASE_URL' not in os.environ and 'ANTHROPIC_API_KEY' not in os.environ
assert '--ignore-user-config' in args and '--ignore-rules' in args
assert '--ephemeral' in args and '--skip-git-repo-check' in args
assert config['forced_login_method']=='"chatgpt"'
assert config['features.apps']=='false'
assert config['project_doc_max_bytes']=='0'
assert config['allow_login_shell']=='false'
assert args[args.index('--sandbox')+1]=='workspace-write'
assert '--model' not in args
''' + SUCCESS)
        with patch.dict(os.environ, {'OPENAI_API_KEY':'PRIVATE', 'CODEX_API_KEY':'PRIVATE',
                                     'OPENAI_BASE_URL':'PRIVATE','ANTHROPIC_API_KEY':'PRIVATE'}):
            result = CodexBrain(self.home, executable=fake).run(
                {'identity':{'name':'Morgan'},'run':{'id':'run-123','profile':None}})
        self.assertEqual(result.summary, 'Saved the next step.')
        self.assertIsNone(result.cost)
        self.assertEqual(result.raw['usage'], {'input_tokens':12,'output_tokens':7})
        result_file = self.home / 'runs/run-123.codex.json'
        self.assertEqual(result_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list((self.home / 'runs').glob('.codex-*')))

    def test_read_grants_do_not_become_writable_roots(self):
        read, write = Path(self.tmp.name)/'read', Path(self.tmp.name)/'write'
        read.mkdir(); write.mkdir()
        command = CodexBrain(self.home).command(grants=[{'mode':'read','path':str(read)},
                                                       {'mode':'write','path':str(write)},
                                                       {'mode':'write','path':str(read),'revoked_at':'yesterday'}],
                                               instructions_file='/private/instructions.md')
        self.assertEqual(command[command.index('--add-dir')+1], str(write.resolve()))
        self.assertNotIn(str(read), command)

    def test_error_or_missing_completion_never_becomes_success(self):
        for body in (
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Partial'}}))\n",
            SUCCESS + "print(json.dumps({'type':'turn.failed','error':{'message':'usage limit exceeded SECRET'}}))\n",
            "print('garbage SECRET')\n" + SUCCESS,
            "raise SystemExit(7)\n",
        ):
            with self.subTest(body=body):
                with self.assertRaises(BrainError) as raised:
                    CodexBrain(self.home, executable=self.executable(body)).run({})
                self.assertNotIn('SECRET', str(raised.exception))

    def test_timeout_preserves_external_tool_effects_and_cleans_instructions(self):
        fake = self.executable("import time\nopen('effect.txt','w').write('done')\ntime.sleep(10)\n")
        with self.assertRaisesRegex(BrainError, 'exceeded'):
            CodexBrain(self.home, executable=fake, timeout=0.2).run({})
        self.assertEqual((self.home/'workspace/effect.txt').read_text(), 'done')
        self.assertFalse(list((self.home/'runs').glob('.codex-*')))

    def test_unsafe_run_id_cannot_escape_artifact_directory(self):
        CodexBrain(self.home, executable=self.executable()).run({'run':{'id':'../../escape','profile':None}})
        self.assertEqual(len(list((self.home/'runs').glob('*.codex.json'))), 1)
        self.assertFalse((self.home.parent/'escape.codex.json').exists())

    def test_filtered_stream_never_saves_tool_or_reasoning_payload(self):
        fake = self.executable('''
print(json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'PRIVATE_REASONING'}}))
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'PRIVATE_COMMAND','aggregated_output':'PRIVATE_OUTPUT'}}))
''' + SUCCESS)
        CodexBrain(self.home, executable=fake).run({})
        stream = next((self.home/'runs').glob('*.stream.jsonl')).read_text()
        self.assertNotIn('PRIVATE_', stream)
        self.assertNotIn('Saved the next step.', stream)


if __name__ == '__main__':
    unittest.main()

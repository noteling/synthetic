"""A restarted normal Engine sends each captured source once to every runtime adapter."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from synthetics.brain import make_brain
from synthetics.engine import Engine
from synthetics.store import Store


BODY = "SOURCE-ONCE-51d9 " + "Keep the exact intention, café 🌱, and its provenance.\n" * 400


def expand(value, root):
    """Test-side reader of the documented JSON Pointer presentation, independent of its writer."""
    def target(pointer):
        found = root
        for part in pointer.split('/')[1:]:
            part = part.replace('~1', '/').replace('~0', '~')
            found = found[int(part)] if isinstance(found, list) else found[part]
        return expand(found, root)

    if isinstance(value, list):
        return [expand(v, root) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {'context_ref'}:
        return target(value['context_ref'])
    result = {k: expand(v, root) for k, v in value.items() if k != 'body_ref'}
    if 'body_ref' in value:
        result['body'] = target(value['body_ref'])
    return result


class PromptContextAcceptance(unittest.TestCase):
    def test_restart_repeated_inputs_reach_claude_codex_and_configured_once(self):
        for provider in ('claude', 'codex', 'configured-claude', 'configured-codex'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / 'employee'
                with Store(home) as store:
                    identity = store.initialize('Aster', 'developer')
                    key = b'acceptance-channel-key-not-a-secret'
                    store.channel_register('owner', Store.credential_hash(key), ['owner:'], 'test')
                    event = store.feed('david', BODY, source='owner:chat', credential=key,
                                       external_id='choice-1', thread='project/~discussion')
                    previous = store.run_begin()
                    store.run_fail(previous['id'], 'Simulated interruption; source remains pending')
                executable = root / 'runtime'
                executable.write_text('#!' + sys.executable + '\n' + '''
import json,sys
from pathlib import Path
if sys.argv[-2:] == ['login','status']:
 print('Logged in using ChatGPT');raise SystemExit(0)
Path('captured.txt').write_text(sys.stdin.read(),encoding='utf-8')
if 'exec' in sys.argv:
 print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Source read.'}}))
 print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
else:
 print(json.dumps({'type':'result','result':'Source read.','is_error':False}))
''')
                executable.chmod(0o700)
                runtime = 'codex' if 'codex' in provider else 'claude'
                if provider.startswith('configured'):
                    path = root / 'brain.json'
                    path.write_text(json.dumps({'version': 1, 'executor': {
                        'adapter': runtime + '-cli', 'executable': str(executable)},
                        'connection': {'protocol': 'chatgpt-login' if runtime == 'codex' else 'claude-login'}}))
                    brain = make_brain(home, brain_config=path, stream_output=False)
                else:
                    brain = make_brain(home, brain=runtime, claude=str(executable),
                                       codex=str(executable), stream_output=False)
                # Capture the actual unmodified Engine context at the adapter boundary.
                original_run = brain.run
                captured = []
                def observe(context):
                    captured.append(copy.deepcopy(context))
                    result = original_run(context)
                    self.assertEqual(context, captured[-1])
                    return result
                brain.run = observe
                runs = Engine(home, brain).run()
                self.assertEqual(len(runs), 1)
                prompt = (home / 'workspace/captured.txt').read_text()
                self.assertEqual(prompt.count('SOURCE-ONCE-51d9'), 1,
                                 'The same captured event must not be serialized repeatedly')
                presented = json.loads(prompt.split('\n\n', 1)[1])
                self.assertEqual(expand(presented, presented), captured[0])
                with Store(home) as store:
                    self.assertEqual(store.identity()['id'], identity['id'])
                    source = store.event_get(event['id'])
                    self.assertEqual(source['body'], event['body'])
                    self.assertEqual(source['auth']['channel'], 'owner')
                    self.assertEqual(store.run_list(1)[0]['status'], 'finished')


if __name__ == '__main__':
    unittest.main()

"""Regression coverage for the independent Codex executor/lifecycle review."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from synthetics import floor, ledgerline, runtime
from synthetics.brain import BrainError
from synthetics.codexbrain import CodexBrain
from synthetics.mcp import TOOLS
from synthetics.store import SESSION_ENV, Store


SUCCESS = (
    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Done'}}),flush=True)\n"
    "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}}),flush=True)\n"
)


class CodexReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'employee'
        self.home.mkdir()

    def executable(self, body):
        path = self.root / 'fake-codex'
        path.write_text('#!' + sys.executable + '\nimport json,sys,time\n'
                        'if sys.argv[-2:] == ["login", "status"]:\n'
                        ' print("Logged in using ChatGPT"); raise SystemExit(0)\n'
                        'sys.stdin.read()\n' + body)
        path.chmod(0o700)
        return str(path)

    def test_successful_stream_with_nonzero_exit_records_executor_failure(self):
        fake = self.executable(SUCCESS + 'raise SystemExit(7)\n')
        with self.assertRaisesRegex(BrainError, 'status 7'):
            CodexBrain(self.home, executable=fake).run({'run': {'id': 'run-exit', 'profile': None}})
        result = json.loads((self.home / 'runs/run-exit.codex.json').read_text())
        self.assertTrue(result['is_error'])
        self.assertEqual(result['subtype'], 'executor_exit')

    def test_successful_stream_followed_by_timeout_records_timeout(self):
        fake = self.executable(SUCCESS + 'time.sleep(10)\n')
        with self.assertRaisesRegex(BrainError, 'exceeded'):
            CodexBrain(self.home, executable=fake, timeout=0.2).run(
                {'run': {'id': 'run-timeout', 'profile': None}})
        result = json.loads((self.home / 'runs/run-timeout.codex.json').read_text())
        self.assertTrue(result['is_error'])
        self.assertEqual(result['subtype'], 'timeout')
        self.assertFalse(list((self.home / 'runs').glob('.codex-*')))

    def test_thread_start_failure_stops_the_spawned_child_and_cleans_pipes(self):
        fake = self.executable('time.sleep(10)\n')
        real_popen = subprocess.Popen
        processes = []

        def capture(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            if args and args[0] and args[0][0] == fake and 'exec' in args[0][1:]:
                processes.append(child)
            return child

        brain = CodexBrain(self.home, executable=fake)
        with patch('synthetics.codexbrain.subprocess.Popen', side_effect=capture), \
                patch('synthetics.codexbrain.threading.Thread.start', side_effect=RuntimeError('no reader thread')):
            with self.assertRaisesRegex(RuntimeError, 'no reader thread'):
                brain.run({'run': {'id': 'run-thread', 'profile': None}})
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stdin.closed)
        self.assertIsNone(brain._current)
        self.assertFalse(list((self.home / 'runs').glob('.codex-*')))
        result = json.loads((self.home / 'runs/run-thread.codex.json').read_text())
        self.assertTrue(result['is_error'])

    def test_thread_failure_after_valid_stream_is_not_a_successful_artifact(self):
        fake = self.root / 'fake-codex'
        fake.write_text('#!' + sys.executable + '\nimport json,sys\n'
                        'if sys.argv[-2:] == ["login", "status"]:\n'
                        ' print("Logged in using ChatGPT"); raise SystemExit(0)\n' + SUCCESS)
        fake.chmod(0o700)
        read_done = threading.Event()
        real_start = threading.Thread.start
        real_reader = CodexBrain._read_events
        starts = []

        def reader(stream, reducer):
            try:
                real_reader(stream, reducer)
            finally:
                read_done.set()

        def start(thread):
            starts.append(thread)
            if len(starts) == 1:
                return real_start(thread)
            self.assertTrue(read_done.wait(2), 'fake stream did not finish before feeder failure')
            raise RuntimeError('no feeder thread')

        brain = CodexBrain(self.home, executable=str(fake))
        try:
            with patch.object(CodexBrain, '_read_events', side_effect=reader), \
                    patch('synthetics.codexbrain.threading.Thread.start', new=start):
                with self.assertRaisesRegex(RuntimeError, 'no feeder thread'):
                    brain.run({'run': {'id': 'run-after-stream', 'profile': None}})
        finally:
            # Reap this fixture even if a regression prevents production cleanup.
            child = getattr(brain, '_current', None)
            if child is not None:
                child.wait(timeout=2)
                child.stdin.close()
                child.stdout.close()
        result = json.loads((self.home / 'runs/run-after-stream.codex.json').read_text())
        self.assertTrue(result['is_error'])
        self.assertEqual(result['subtype'], 'executor_error')

    def test_shell_and_every_mcp_tool_preserve_the_run_fence(self):
        run_id = 'run-fence'
        command = CodexBrain(self.home).command(run_id, instructions_file=self.root / 'instructions.md')
        settings = {command[i + 1].split('=', 1)[0]: command[i + 1].split('=', 1)[1]
                    for i, arg in enumerate(command) if arg == '-c'}
        # This checks the actual argv sent to the executable, not the parent's env.
        for key in ('mcp_servers', 'shell_environment_policy.set'):
            self.assertIn('"SYNTHETICS_HOME" = ' + json.dumps(str(self.home.resolve())), settings[key])
            self.assertIn('"SYNTHETICS_SESSION_RUN" = ' + json.dumps(run_id), settings[key])
        for tool in TOOLS:
            self.assertIn(json.dumps(tool['name']) + ' = {"approval_mode" = "approve"}',
                          settings['mcp_servers'])
        self.assertIn('"required" = true', settings['mcp_servers'])

    def prepare_active_codex(self):
        key = b'k' * 32
        keyfile = self.root / 'stand.key'
        keyfile.write_bytes(key)
        keyfile.chmod(0o600)
        with Store(self.home) as store:
            store.initialize('Review', 'developer')
            store.channel_register('stand', Store.credential_hash(key), ['stand:'], 'review fixture')
            store.feed('owner', 'Keep working')
            run = store.run_begin(executor='codex')
        return keyfile, run

    def assert_not_yielded(self, run):
        with Store(self.home) as store:
            self.assertIsNone(store.meeting_pending())
            self.assertEqual(store.run_get(run['id'])['status'], 'running')

    def test_attach_rejects_active_codex_with_no_runtime_state(self):
        key, run = self.prepare_active_codex()
        self.assertFalse((self.home / runtime.STATE_FILE).exists())
        self.assert_attach_refused(key, run)

    def test_attach_rejects_active_codex_despite_stale_claude_runtime_state(self):
        key, run = self.prepare_active_codex()
        runtime.write_json(self.home / runtime.STATE_FILE,
                           {'policy': {}, 'effective': {'brain': 'claude'}})
        self.assert_attach_refused(key, run)

    def assert_attach_refused(self, key, run):
        (self.home / 'floor.json').write_text('{"enabled":true}')
        output = io.StringIO()
        with patch.dict(os.environ):
            os.environ.pop(SESSION_ENV, None)
            result = floor.attach(self.home, str(key), stdout=output,
                                  stdin=io.StringIO(json.dumps({'v': 1, 'type': 'begin',
                                                              'meeting': 'review', 'goal': 'Introduce yourself'}) + '\n'))
        self.assertEqual(result, 2)
        self.assertIn('does not support persistent meeting', output.getvalue())
        self.assert_not_yielded(run)

    def test_store_atomic_boundary_refuses_active_codex_even_without_attach_guard(self):
        _, run = self.prepare_active_codex()
        with Store(self.home) as store:
            with self.assertRaisesRegex(ValueError, 'Codex executor'):
                store.meeting_begin('review', 'stand')
        self.assert_not_yielded(run)

    def test_export_filters_nested_usage_to_numeric_token_fields(self):
        usage = {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 2,
                 'reasoning_output_tokens': 1, 'secret': 'PRIVATE-CREDENTIAL',
                 'nested': {'password': 'PRIVATE-CREDENTIAL'}}
        fact = {'kind': 'result', 'usage': usage}
        result = ledgerline.export_stream('employee-test', 'run-test', 1, fact, json.dumps(fact).encode())
        self.assertNotIn('PRIVATE-CREDENTIAL', json.dumps(result))
        self.assertEqual(result['facts']['usage'],
                         {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 2,
                          'reasoning_output_tokens': 1})
        for bad in ({'input_tokens': True}, {'input_tokens': 'PRIVATE-CREDENTIAL'},
                    {'input_tokens': -1}, 'PRIVATE-CREDENTIAL'):
            fact['usage'] = bad
            result = ledgerline.export_stream('employee-test', 'run-test', 1, fact, json.dumps(fact).encode())
            self.assertEqual(result['facts']['usage'], {})
            self.assertNotIn('PRIVATE-CREDENTIAL', json.dumps(result))


if __name__ == '__main__':
    unittest.main()

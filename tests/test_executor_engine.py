import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from synthetics import floor, runtime
from synthetics.brain import BrainResult, ClaudeBrain, executor_capabilities
from synthetics.codexbrain import CodexBrain
from synthetics.engine import Engine
from synthetics.store import SESSION_ENV, Store


class ExecutorEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)/'employee'
        with Store(self.home) as s:
            self.identity=s.initialize('Morgan','developer')
            s.feed('owner','Investigate the assigned project.')

    def test_engine_passes_honest_executor_capabilities(self):
        contexts=[]

        class Capture:
            capabilities=executor_capabilities('codex')

            def run(self,context):
                contexts.append(context)
                return BrainResult('Recorded next steps.')

        Engine(self.home,Capture()).run()
        self.assertEqual(contexts[0]['identity']['id'],self.identity['id'])
        self.assertFalse(contexts[0]['executor']['session_budget_cap'])
        self.assertFalse(contexts[0]['executor']['usage_cost'])
        with Store(self.home) as s:
            self.assertEqual(s.run_list()[0]['status'],'finished')
            self.assertIsNone(s.run_list()[0]['cost'])

    def test_runtime_backend_change_replaces_executor_without_replacing_employee(self):
        engine=Engine(self.home,ClaudeBrain(self.home))
        settings=dict(brain='codex',codex=sys.executable,claude='',model='',budget=100,
                      timeout=300,daily_limit=0,limit_notify=None,limit_allow=[])
        engine.apply_runtime(settings)
        self.assertIsInstance(engine.brain,CodexBrain)
        self.assertEqual(engine.brain.executable,sys.executable)
        self.assertEqual(engine.brain.model,'')
        engine.apply_runtime(dict(settings,brain='claude',claude=sys.executable,model='claude-opus-5-5'))
        self.assertEqual(type(engine.brain),ClaudeBrain)
        with Store(self.home) as s:
            self.assertEqual(s.identity()['id'],self.identity['id'])
            self.assertEqual(len(s.event_pending()),1)

    def test_unsupported_live_meeting_fails_before_yielding_ordinary_run(self):
        key=b's'*32
        keyfile=Path(self.tmp.name)/'stand.key';keyfile.write_bytes(key);keyfile.chmod(0o600)
        with Store(self.home) as s:
            s.channel_register('stand',Store.credential_hash(key),['stand:'],'test')
            run=s.run_begin()
        runtime.write_json(self.home/runtime.STATE_FILE,{'policy':{},'effective':{'brain':'codex'}})
        output=io.StringIO()
        with patch.dict(os.environ):
            os.environ.pop(SESSION_ENV,None)
            result=floor.attach(self.home,str(keyfile),
                                stdin=io.StringIO(json.dumps({'v':1,'type':'begin','meeting':'demo','goal':'Introduce yourself'})+'\n'),
                                stdout=output)
        self.assertEqual(result,2)
        self.assertIn('does not support persistent meeting',output.getvalue())
        with Store(self.home) as s:
            self.assertIsNone(s.meeting_pending())
            self.assertEqual(s.run_get(run['id'])['status'],'running')


if __name__=='__main__':
    unittest.main()

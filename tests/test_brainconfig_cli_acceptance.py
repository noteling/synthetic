"""Opt-in actual-CLI acceptance against a local gateway; no inference or real key.

Set SYNTHETICS_TEST_CODEX to an installed Codex executable. The normal suite skips
this test so installing the package never requires a provider account or CLI.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from synthetics.store import Store
from tests.gateway_fixture import GatewayFixture


@unittest.skipUnless(os.environ.get('SYNTHETICS_TEST_CODEX'), 'explicit installed Codex CLI required')
class ConfiguredCLIIntegrationTests(unittest.TestCase):
    def test_configured_gateway_routes_tools_and_preserves_bystander_login(self):
        with tempfile.TemporaryDirectory(prefix='synthetics-gateway-cli-') as temporary:
            root = Path(temporary)
            home = root / 'employee'
            with Store(home) as store:
                identity = store.initialize('Aster', 'integration verifier')
                event = store.feed('owner', 'Retain the gateway verification procedure from this source.')
            bystander = root / 'host-runtime-home'
            bystander.mkdir(mode=0o700)
            auth = bystander / 'auth.json'
            auth.write_text('{"fixture":"bystander login must be untouched"}')
            before = auth.read_bytes()
            arguments = dict(subject=identity['id'], predicate='gateway_marker',
                             value='configured gateway tool loop', source=event['id'],
                             kind='procedure', key='gateway-acceptance-1')
            with GatewayFixture(arguments=arguments, final_text='Configured gateway completed.') as gateway:
                config = dict(version=1, executor=dict(adapter='codex-cli',
                              executable=os.environ['SYNTHETICS_TEST_CODEX']),
                              connection=dict(protocol='openai-responses', base_url=gateway.base_url,
                              model='company-maintainer', auth=dict(type='bearer-env',
                              variable='EMPLOYEE_GATEWAY_TEST_TOKEN')))
                path = root / 'brain.json'
                path.write_text(json.dumps(config))
                path.chmod(0o600)
                env = dict(os.environ, EMPLOYEE_GATEWAY_TEST_TOKEN=gateway.token,
                           CODEX_HOME=str(bystander))
                run = subprocess.run([sys.executable, '-m', 'synthetics', '--home', str(home),
                                      'run', '--brain-config', str(path), '--timeout', '120'],
                                     env=env, capture_output=True, text=True, timeout=150)
                facts = gateway.facts
                self.assertEqual(run.returncode, 0, run.stderr[-2000:])
            self.assertEqual(auth.read_bytes(), before)
            self.assertEqual(len(facts), 2, facts)
            self.assertTrue(all(f['auth_ok'] and f['model'] == 'company-maintainer' for f in facts))
            self.assertTrue(facts[1]['matched_tool_output'])
            with Store(home) as store:
                self.assertEqual(store.identity()['id'], identity['id'])
                self.assertEqual(store.run_list()[0]['status'], 'finished')
                self.assertEqual(store.run_list()[0]['executor'], 'configured')
                memories = store._all('memories')
                self.assertEqual(len(memories), 1)
                self.assertEqual(memories[0]['predicate'], 'gateway_marker')
                self.assertEqual(memories[0]['value'], 'configured gateway tool loop')
            self.assertFalse(list((home / 'runs').glob('.codex-*')))


if __name__ == '__main__':
    unittest.main()

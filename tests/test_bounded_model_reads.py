"""Functional byte ceilings at the MCP stdio boundary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from synthetics.store import Store


class BoundedModelReadsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'home'
        self.store = Store(self.home)
        self.store.initialize('Aster', 'maintainer')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def call(self, name, arguments):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': name, 'arguments': arguments}}
        process = subprocess.run([sys.executable, '-m', 'synthetics.mcp', '--home', str(self.home)],
                                 input=json.dumps(request) + '\n', text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)['result']
        self.assertFalse(result.get('isError', False), result)
        return result['content'][0]['text']

    def pages(self, name, args):
        chunks = {}
        while True:
            page = json.loads(self.call(name, args))
            if 'chunk' in page:
                chunks[page['key']] = chunks.get(page['key'], '') + page['chunk']
            if not page['next_cursor']:
                return {key: json.loads(value) for key, value in chunks.items()}
            args = {'cursor': page['next_cursor'], 'max_bytes': 1024}

    def test_unicode_reconstruction_and_insert_stability(self):
        self.store.message_send('reviewer', '雪🦊' * 3000, key='reconstruct')
        original = self.store.outbox_list()[0]
        first = json.loads(self.call('outbox_list', {'max_bytes': 1024}))
        self.store.message_send('reviewer', 'later', key='later')
        chunk = first['chunk']
        cursor = first['next_cursor']
        while cursor:
            page = json.loads(self.call('outbox_list', {'cursor': cursor, 'max_bytes': 1024}))
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 1024)
            chunk += page['chunk']
            cursor = page['next_cursor']
        self.assertEqual(json.loads(chunk), original)

    def test_section_reconstruction(self):
        self.store.feed('reviewer', '雪🦊' * 2000)
        expected = self.store.context()['events']
        result = self.pages('employee_context', {'section': 'events', 'max_bytes': 1024})
        self.assertEqual(result['events'], expected)

    def test_giant_outbox_item_is_bounded_in_utf8(self):
        self.store.message_send('reviewer', '🦊' * 30000, key='large')
        original = self.store.outbox_list()
        result = self.call('outbox_list', {})
        self.assertLessEqual(len(result.encode('utf-8')), 32000)
        self.assertEqual(self.store.outbox_list(), original)

    def test_giant_context_is_bounded_in_utf8(self):
        self.store.feed('reviewer', '雪' * 30000)
        original = self.store.context()
        result = self.call('employee_context', {})
        self.assertLessEqual(len(result.encode('utf-8')), 32000)
        self.assertEqual(self.store.context(), original)

    def test_explicit_small_budget_is_enforced_for_unicode_context(self):
        self.store.feed('reviewer', '雪' * 12000)
        result = self.call('employee_context', {'max_bytes': 1024})
        self.assertLessEqual(len(result.encode('utf-8')), 1024)
        self.assertTrue(json.loads(result))

    def test_explicit_small_budget_is_enforced_for_unicode_outbox(self):
        self.store.message_send('reviewer', '🦊' * 12000, key='small-budget')
        result = self.call('outbox_list', {'max_bytes': 1024})
        self.assertLessEqual(len(result.encode('utf-8')), 1024)
        self.assertTrue(json.loads(result))

    def test_batch_short_outbox_and_find_older_id(self):
        for i in range(55):
            self.store.message_send('reviewer', 'short', key='batch-%d' % i)
        items = self.store.outbox_list()
        page = json.loads(self.call('outbox_list', {}))
        self.assertEqual(len(page['entries']), 50)
        self.assertIsNone(page['next_cursor'])
        selected = json.loads(self.call('outbox_list', {'id': items[0]['id']}))
        self.assertEqual(selected['entries'][0]['value'], items[0])

    def test_context_discovers_and_selects_section(self):
        page = json.loads(self.call('employee_context', {}))
        self.assertIn('work', page['sections'])
        selected = json.loads(self.call('employee_context', {'section': 'work'}))
        self.assertEqual(selected['entries'][0]['value'], self.store.context()['work'])

    def test_invalid_budget_cursor_and_stale_context(self):
        def error(name, args):
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            p = subprocess.run([sys.executable, '-m', 'synthetics.mcp', '--home', str(self.home)], input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=10)
            return json.loads(p.stdout)['result'].get('isError', False)
        for budget in (0, 1023, 32001, True):
            self.assertTrue(error('outbox_list', {'max_bytes': budget}))
        self.assertTrue(error('outbox_list', {'cursor': 'not-a-cursor'}))
        self.store.feed('reviewer', 'x' * 5000)
        page = json.loads(self.call('employee_context', {'section': 'events', 'max_bytes': 1024}))
        self.store.feed('reviewer', 'changed')
        self.assertTrue(error('employee_context', {'cursor': page['next_cursor']}))

    def test_older_before_pages(self):
        for i in range(8):
            self.store.message_send('reviewer', str(i), key='old-%d' % i)
        all_items = self.store.outbox_list()
        newest = json.loads(self.call('outbox_list', {'limit': 3}))
        older = json.loads(self.call('outbox_list', {'limit': 3, 'before': newest['older_before']}))
        oldest = json.loads(self.call('outbox_list', {'limit': 3, 'before': older['older_before']}))
        got = [e['value'] for p in (oldest, older, newest) for e in p['entries']]
        self.assertEqual(got, all_items)
        self.assertIsNone(oldest['older_before'])

    def test_mixed_batch_and_chunk_pagination_is_complete(self):
        for i, body in enumerate(['small', '🦊' * 2000, 'last']):
            self.store.message_send('reviewer', body, key='mixed-%s' % i)
        expected = self.store.outbox_list()
        args = {'max_bytes': 1024}
        values, chunks = [], {}
        for _ in range(100):
            raw = self.call('outbox_list', args)
            self.assertLessEqual(len(raw.encode('utf-8')), 1024)
            page = json.loads(raw)
            values.extend(entry['value'] for entry in page.get('entries', []))
            if 'chunk' in page:
                key = page['key']
                chunks[key] = chunks.get(key, '') + page['chunk']
                if len(chunks[key]) == page['length']:
                    values.append(json.loads(chunks.pop(key)))
            if not page['next_cursor']:
                break
            args = {'cursor': page['next_cursor'], 'max_bytes': 1024}
        else:
            self.fail('pagination did not terminate')
        self.assertEqual(chunks, {})
        self.assertEqual(values, expected)

    def test_cross_tool_cursor_is_rejected(self):
        self.store.message_send('reviewer', 'large' * 1000, key='stale')
        first = json.loads(self.call('outbox_list', {'max_bytes': 1024}))
        cursor = first['next_cursor']
        # A cursor cannot be reused with a different tool.
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'employee_context', 'arguments': {'cursor': cursor}}}
        p = subprocess.run([sys.executable, '-m', 'synthetics.mcp', '--home', str(self.home)],
                           input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=10)
        self.assertTrue(json.loads(p.stdout)['result']['isError'])

    def test_delivery_mutation_and_cursor_tampering_rejected(self):
        from synthetics.boundedreads import token, untoken
        self.store.channel_register('tg', Store.credential_hash(b'a' * 32), ['tg:'], 'test', routes=['reviewer'])
        message = self.store.message_send('reviewer', 'x' * 5000, key='mutation')
        first = json.loads(self.call('outbox_list', {'max_bytes': 1024}))
        def error(name, args):
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            p = subprocess.run([sys.executable, '-m', 'synthetics.mcp', '--home', str(self.home)], input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=10)
            return json.loads(p.stdout)['result'].get('isError', False)
        state = untoken(first['next_cursor'])
        for field, value in [('index', -1), ('offset', -1), ('name', 'employee_context'), ('limit', True), ('hash', 'bad')]:
            altered = dict(state, **{field: value})
            self.assertTrue(error('outbox_list', {'cursor': token(altered)}), field)
        self.assertTrue(error('employee_context', {'section': 'no-such-section'}))
        self.assertTrue(error('outbox_list', {'id': 'no-such-message'}))
        self.store.channel_claim(b'a' * 32, message['id'])
        self.store.channel_ack(b'a' * 32, message['id'], 'delivered', receipt='tg:1')
        after = self.store.outbox_list()
        self.assertTrue(error('outbox_list', {'cursor': first['next_cursor']}))
        self.assertEqual(self.store.outbox_list(), after)

    def test_revoked_grant_invalidates_context_continuation(self):
        grant = self.store.access_grant('/tmp', 'read', 'x' * 1800, 'owner')
        first = json.loads(self.call('employee_context', {'section': 'access_grants', 'max_bytes': 1024}))
        self.store.access_revoke(grant['id'], granted_by='owner')
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'employee_context', 'arguments': {'cursor': first['next_cursor']}}}
        p = subprocess.run([sys.executable, '-m', 'synthetics.mcp', '--home', str(self.home)], input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=10)
        self.assertTrue(json.loads(p.stdout)['result']['isError'])

    def test_final_chunk_avoids_cursor_size_nonmonotonicity(self):
        self.store.message_send('reviewer', 'x' * 1100, key='end-chunk')
        page = json.loads(self.call('outbox_list', {'max_bytes': 1024}))
        self.assertIsNotNone(page['next_cursor'])
        from synthetics.boundedreads import token, untoken
        state = untoken(page['next_cursor'])
        state['offset'] = page['length'] - 700
        page = json.loads(self.call('outbox_list', {'cursor': token(state), 'max_bytes': 1024}))
        self.assertIsNone(page['next_cursor'])
        self.assertEqual(page['offset'] + len(page['chunk']), page['length'])

    def test_wall_clock_alone_does_not_invalidate_all_context_cursor(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from synthetics.boundedreads import page
        self.store.message_send('reviewer', 'waiting', key='clock')
        self.store.feed('reviewer', 'x' * 5000)
        run = self.store.run_begin()
        now = datetime.now(timezone.utc)
        class Clock(datetime):
            current = None
            @classmethod
            def now(cls, tz=None):
                return cls.current
        Clock.current = now + timedelta(minutes=11)
        with Store(self.home, run_id=run['id']) as bound, patch('synthetics.store.datetime', Clock):
            first = page(bound, 'employee_context', {'max_bytes': 1024})
            Clock.current += timedelta(minutes=1)
            second = page(bound, 'employee_context', {'cursor': first['next_cursor'], 'max_bytes': 1024})
            self.assertEqual(first['as_of'], second['as_of'])
            fresh = page(bound, 'employee_context', {'section': 'outbox_stuck'})
            self.assertNotEqual(first['as_of'], fresh['as_of'])
            self.assertEqual(fresh['entries'][0]['value'], bound.outbox_stuck(now=Clock.current))
            self.store.access_grant('/tmp', 'read', 'actual mutation', 'owner')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                page(bound, 'employee_context', {'cursor': first['next_cursor'], 'max_bytes': 1024})

    def test_bound_meeting_notice_counts_toward_budget(self):
        from synthetics.mcp import Server
        self.store.message_send('reviewer', '雪' * 5000, key='yield')
        before = self.store.outbox_list()
        run = self.store.run_begin()
        self.store.meeting_begin('m1', 'stand', grace=45)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'outbox_list', 'arguments': {'max_bytes': 1024}}}
        p = subprocess.run([sys.executable, '-m', 'synthetics.mcp', '--home', str(self.home), '--run-id', run['id']], input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=10)
        result = json.loads(p.stdout)['result']
        self.assertFalse(result.get('isError', False), result)
        raw = result['content'][0]['text']
        self.assertIn('a meeting is starting', json.loads(raw)['yield'])
        self.assertLessEqual(len(raw.encode('utf-8')), 1024)
        self.assertEqual(self.store.outbox_list(), before)

    def test_stale_run_can_read_but_cannot_mutate(self):
        self.store.message_send('reviewer', 'x' * 5000, key='stale-run')
        run = self.store.run_begin()
        def invoke(name, args):
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            p = subprocess.run([sys.executable, '-m', 'synthetics.mcp', '--home', str(self.home), '--run-id', run['id']], input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=10)
            return json.loads(p.stdout)['result']
        first = json.loads(invoke('outbox_list', {'max_bytes': 1024})['content'][0]['text'])
        self.store.run_fail(run['id'], 'test recovery')
        self.assertFalse(invoke('outbox_list', {'cursor': first['next_cursor'], 'max_bytes': 1024}).get('isError', False))
        before = self.store.outbox_list()
        self.assertTrue(invoke('message_send', {'to': 'reviewer', 'body': 'not permitted', 'key': 'stale-write'}).get('isError', False))
        self.assertEqual(self.store.outbox_list(), before)

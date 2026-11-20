"""The local Responses fixture exercises real HTTP/SSE without provider traffic."""

import gzip
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.gateway_fixture import GatewayFixture, offered_functions


class GatewayFixtureTests(unittest.TestCase):
    def request(self, gateway, value, token=None, compressed=False):
        body = json.dumps(value).encode()
        headers = {'Authorization': 'Bearer ' + (token if token is not None else gateway.token),
                   'Content-Type': 'application/json'}
        if compressed:
            body = gzip.compress(body)
            headers['Content-Encoding'] = 'gzip'
        try:
            response = urlopen(Request(gateway.base_url + '/responses', data=body, headers=headers), timeout=3)
        except HTTPError as error:
            error.close()
            raise
        with response:
            return response.status, response.headers.get_content_type(), response.read().decode()

    @staticmethod
    def events(body):
        return [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]

    def test_http_sse_function_call_requires_matching_output_before_final_message(self):
        with GatewayFixture(arguments={'value': 'fixture-value'}) as gateway:
            request = {'model': 'fixture-model', 'stream': True, 'input': [],
                       'tools': [{'type': 'function', 'name': 'mcp__synthetics__memory_put'}]}
            status, content_type, body = self.request(gateway, request)
            self.assertEqual((status, content_type), (200, 'text/event-stream'))
            events = self.events(body)
            self.assertEqual(events[0]['type'], 'response.created')
            self.assertEqual(events[-1]['type'], 'response.completed')
            call = events[-1]['response']['output'][0]
            self.assertEqual(call['name'], 'mcp__synthetics__memory_put')
            self.assertEqual(json.loads(call['arguments']), {'value': 'fixture-value'})
            self.assertEqual([event['sequence_number'] for event in events], list(range(len(events))))
            request['input'] = [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'ok'}]
            _, _, body = self.request(gateway, request)
            final = self.events(body)[-1]['response']
            self.assertEqual(final['output'][0]['content'][0]['text'], gateway.final_text)
            self.assertEqual(final['status'], 'completed')
            self.assertTrue(gateway.facts[-1]['matched_tool_output'])

    def test_wrong_call_id_is_refused(self):
        with GatewayFixture() as gateway:
            req = {'tools': [{'type': 'function', 'name': 'memory_put'}]}
            self.request(gateway, req)
            req['input'] = [{'type': 'function_call_output', 'call_id': 'wrong', 'output': 'private'}]
            with self.assertRaises(HTTPError) as raised:
                self.request(gateway, req)
            self.assertEqual(raised.exception.code, 409)
            self.assertFalse(gateway.facts[-1]['matched_tool_output'])

    def test_matching_call_id_without_a_tool_result_is_refused(self):
        with GatewayFixture() as gateway:
            req = {'tools': [{'type': 'function', 'name': 'memory_put'}]}
            self.request(gateway, req)
            req['input'] = [{'type': 'function_call_output', 'call_id': 'call_fixture_1'}]
            with self.assertRaises(HTTPError) as raised:
                self.request(gateway, req)
            self.assertEqual(raised.exception.code, 409)

    def test_authorization_is_checked_without_retaining_header_or_body(self):
        with GatewayFixture(token='FIXTURE-PRIVATE-TOKEN') as gateway:
            with self.assertRaises(HTTPError) as raised:
                self.request(gateway, {'input': 'PRIVATE-BODY'}, token='WRONG-TOKEN')
            self.assertEqual(raised.exception.code, 401)
            facts = json.dumps(gateway.facts)
            self.assertNotIn('TOKEN', facts)
            self.assertNotIn('PRIVATE-BODY', facts)
            self.assertFalse(gateway.facts[0]['auth_ok'])

    def test_safe_facts_do_not_retain_prompts_schemas_or_tool_results(self):
        with GatewayFixture(arguments={'value': 'PRIVATE-ARG'}) as gateway:
            req = {'tools': [{'type': 'function', 'name': 'memory_put',
                              'description': 'PRIVATE-DESCRIPTION', 'parameters': {'PRIVATE-SCHEMA': 'x'}}],
                   'input': [{'role': 'user', 'content': 'PRIVATE-PROMPT'}]}
            self.request(gateway, req)
            req['input'] = [{'type': 'function_call_output', 'call_id': 'call_fixture_1', 'output': 'PRIVATE-RESULT'}]
            self.request(gateway, req)
            self.assertNotIn('PRIVATE', json.dumps(gateway.facts))

    def test_gzip_input_and_non_streaming_response(self):
        with GatewayFixture(tool_pattern=None) as gateway:
            status, content_type, body = self.request(gateway, {'stream': False, 'input': []}, compressed=True)
            self.assertEqual((status, content_type), (200, 'application/json'))
            self.assertEqual(json.loads(body)['output'][0]['type'], 'message')

    def test_safe_facts_record_the_configured_model_route_verbatim(self):
        alias = 'organization/gateway:an-unusual model alias.v2'
        with GatewayFixture(tool_pattern=None) as gateway:
            self.request(gateway, {'model': alias})
            self.assertEqual(gateway.facts[0]['model'], alias)

    def test_model_fact_is_bounded_and_rejects_control_text_or_objects(self):
        for value in ('a' * 513, 'route\nPRIVATE-INJECTION', {'secret': 'PRIVATE-OBJECT'}, ''):
            with self.subTest(value=type(value).__name__):
                with GatewayFixture(tool_pattern=None) as gateway:
                    self.request(gateway, {'model': value})
                    self.assertIsNone(gateway.facts[0]['model'])
                    self.assertNotIn('PRIVATE', json.dumps(gateway.facts))

    def test_missing_tool_and_excess_requests_are_bounded_failures(self):
        with GatewayFixture(max_requests=1) as gateway:
            with self.assertRaises(HTTPError) as raised:
                self.request(gateway, {'tools': []})
            self.assertEqual(raised.exception.code, 400)
            with self.assertRaises(HTTPError) as raised:
                self.request(gateway, {'tools': []})
            self.assertEqual(raised.exception.code, 429)
            self.assertEqual(len(gateway.facts), 1)

    def test_failed_authentication_and_valid_requests_share_the_fact_bound(self):
        with GatewayFixture(tool_pattern=None, max_requests=1) as gateway:
            with self.assertRaises(HTTPError):
                self.request(gateway, {}, token='incorrect')
            self.request(gateway, {})
            self.assertEqual(len(gateway.facts), 1)

    def test_offered_function_shapes_and_namespace_are_recognized(self):
        tools = [{'type': 'function', 'function': {'name': 'flat'}},
                 {'type': 'namespace', 'name': 'mcp', 'tools': [{'type': 'function', 'name': 'memory_put'}]},
                 {'type': 'function', 'name': 'bad name with spaces'}, {'type': 'computer'}]
        self.assertEqual(offered_functions(tools), [{'name': 'flat'}, {'name': 'memory_put', 'namespace': 'mcp'}])


if __name__ == '__main__':
    unittest.main()

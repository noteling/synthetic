"""Local Responses HTTP fixture for exercising an actual executor CLI, without inference.

This is acceptance-test infrastructure, not an alternative product executor. The
server emits one configured function call and waits for its matching result before
returning a final message. It never forwards a request or persists request bodies,
authorization headers, prompts, tool arguments or tool results.

SSE shapes follow the official streaming/function-calling guides:
https://developers.openai.com/api/docs/guides/streaming-responses
https://developers.openai.com/api/docs/guides/function-calling
"""

import copy
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import re
import threading
import time


MAX_BODY = 8 * 1024 * 1024
MAX_TOOLS = 256
SAFE_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,256}\Z")


def offered_functions(tools, namespace=None, _depth=0):
    """The supported flat/namespace function names, never their schemas or descriptions."""
    found = []
    if _depth > 8:
        return found
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        kind = tool.get('type')
        if kind == 'namespace' and isinstance(tool.get('name'), str):
            if SAFE_NAME.fullmatch(tool['name']):
                found.extend(offered_functions(tool.get('tools'), tool['name'], _depth + 1))
        elif kind == 'function':
            detail = tool.get('function') if isinstance(tool.get('function'), dict) else tool
            name = detail.get('name')
            if isinstance(name, str) and SAFE_NAME.fullmatch(name):
                found.append(dict(name=name, **({'namespace': namespace} if namespace else {})))
        if len(found) >= MAX_TOOLS:
            break
    return found[:MAX_TOOLS]


def response_events(index, output):
    """A minimal streamed Responses lifecycle with a complete terminal response."""
    rid = 'resp_fixture_%d' % index
    base = dict(id=rid, object='response', created_at=int(time.time()), model='fixture-model',
                status='in_progress', output=[])
    events = [dict(type='response.created', response=dict(base))]
    item = output
    initial = dict(item, status='in_progress')
    if item['type'] == 'function_call':
        initial['arguments'] = ''
    else:
        initial['content'] = []
    events.append(dict(type='response.output_item.added', response_id=rid, output_index=0, item=initial))
    if item['type'] == 'function_call':
        events += [dict(type='response.function_call_arguments.delta', response_id=rid,
                        item_id=item['id'], output_index=0, delta=item['arguments']),
                   dict(type='response.function_call_arguments.done', response_id=rid,
                        item_id=item['id'], output_index=0, arguments=item['arguments'])]
    else:
        part = item['content'][0]
        common = dict(response_id=rid, item_id=item['id'], output_index=0, content_index=0)
        events += [dict(type='response.content_part.added', part=dict(part, text=''), **common),
                   dict(type='response.output_text.delta', delta=part['text'], **common),
                   dict(type='response.output_text.done', text=part['text'], **common),
                   dict(type='response.content_part.done', part=part, **common)]
    events.append(dict(type='response.output_item.done', response_id=rid, output_index=0, item=item))
    events.append(dict(type='response.completed', response=dict(base, status='completed', output=[item],
                       usage={'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2,
                              'input_tokens_details': {'cached_tokens': 0},
                              'output_tokens_details': {'reasoning_tokens': 0}})))
    for sequence, event in enumerate(events):
        event['sequence_number'] = sequence
    return events


class GatewayFixture:
    """Context-managed loopback server for a deterministic tool call and final answer.

    ``base_url`` ends in /v1. ``token`` is fixture-only, never a real provider key.
    ``facts`` exposes bounded structural observations and matching call IDs only.
    Namespace tools are supported when declared explicitly in the request catalog.
    """

    def __init__(self, tool_pattern='memory_put', arguments=None, final_text='Fixture tool loop completed.',
                 token='synthetics-local-fixture-only', max_requests=8):
        self.tool_pattern = re.compile(tool_pattern) if tool_pattern is not None else None
        self.arguments = copy.deepcopy(arguments or {})
        if not isinstance(self.arguments, dict) or len(json.dumps(self.arguments).encode()) > 65536:
            raise ValueError('fixture arguments must be a bounded object')
        if not isinstance(final_text, str) or len(final_text.encode()) > 65536:
            raise ValueError('fixture final text must be bounded text')
        if not isinstance(max_requests, int) or isinstance(max_requests, bool) or not 1 <= max_requests <= 32:
            raise ValueError('fixture max_requests must be between 1 and 32')
        self.final_text, self.token, self.max_requests = final_text, token, max_requests
        self._facts, self._lock = [], threading.Lock()
        self._request_count = 0
        self._call_id = None
        self._finished = False
        self._server = None
        self._thread = None

    @property
    def facts(self):
        with self._lock:
            return copy.deepcopy(self._facts)

    @property
    def base_url(self):
        if self._server is None:
            raise RuntimeError('fixture is not running')
        return 'http://127.0.0.1:%d/v1' % self._server.server_address[1]

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass  # Never log request paths/headers/bodies to the host's stderr.

            def do_GET(self):
                self.reply(405, {'error': {'code': 'fixture_post_only'}})

            def do_POST(self):
                path = self.path.split('?', 1)[0]
                authorized = self.headers.get('Authorization') == 'Bearer ' + fixture.token
                if path != '/v1/responses':
                    self.reply(404, {'error': {'code': 'fixture_route'}})
                    return
                if not authorized:
                    fixture.observe({'route': '/v1/responses', 'auth_ok': False, 'status': 401})
                    self.reply(401, {'error': {'code': 'fixture_auth'}})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '-1'))
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    self.reply(413, {'error': {'code': 'fixture_body_size'}})
                    return
                raw = self.rfile.read(length)
                encoding = self.headers.get('Content-Encoding', 'identity')
                try:
                    if encoding == 'gzip':
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_BODY + 1)
                    elif encoding != 'identity':
                        self.reply(415, {'error': {'code': 'fixture_encoding'}})
                        return
                    if len(raw) > MAX_BODY:
                        self.reply(413, {'error': {'code': 'fixture_body_size'}})
                        return
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError('not an object')
                except (ValueError, OSError, EOFError, RecursionError):
                    self.reply(400, {'error': {'code': 'fixture_json'}})
                    return
                status, payload = fixture.respond(request)
                if status != 200:
                    self.reply(status, payload)
                    return
                if request.get('stream') is False:
                    self.reply(200, payload[-1]['response'])
                    return
                body = ''.join('event: %s\ndata: %s\n\n' % (event['type'], json.dumps(event))
                               for event in payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def reply(self, status, value):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': 0.02}, daemon=True)
        self._thread.start()
        return self

    def observe(self, fact):
        with self._lock:
            self._record(fact)

    def _record(self, fact):
        if len(self._facts) < self.max_requests:
            self._facts.append(fact)

    def respond(self, request):
        """Select from offered tools and require this fixture's matching output call ID."""
        with self._lock:
            self._request_count += 1
            if self._request_count > self.max_requests:
                return 429, {'error': {'code': 'fixture_request_limit'}}
            offered = offered_functions(request.get('tools'))
            inputs = request.get('input')
            inputs = inputs if isinstance(inputs, list) else []
            outputs = [item for item in inputs if isinstance(item, dict)
                       and item.get('type') == 'function_call_output']
            matched = self._call_id is not None and any(item.get('call_id') == self._call_id
                                                       and isinstance(item.get('output'), (str, list))
                                                       for item in outputs)
            fact = {'route': '/v1/responses', 'auth_ok': True, 'request': self._request_count,
                    'tool_count': len(offered), 'input_count': len(inputs),
                    'function_output_count': len(outputs), 'matched_tool_output': matched}
            if self._finished:
                fact['status'] = 409
                self._record(fact)
                return 409, {'error': {'code': 'fixture_already_finished'}}
            if self._call_id is None and self.tool_pattern is not None:
                selected = next((tool for tool in offered if self.tool_pattern.search(tool['name'])), None)
                if selected is None:
                    fact.update(status=400, offered_names=[tool['name'] for tool in offered])
                    self._record(fact)
                    return 400, {'error': {'code': 'fixture_tool_not_offered'}}
                self._call_id = 'call_fixture_1'
                fact.update(status=200, tool=selected['name'], call_id=self._call_id)
                item = dict(type='function_call', id='fc_fixture_1', call_id=self._call_id,
                            status='completed', arguments=json.dumps(self.arguments), **selected)
            else:
                if self._call_id is not None and not matched:
                    fact['status'] = 409
                    self._record(fact)
                    return 409, {'error': {'code': 'fixture_missing_tool_output'}}
                self._finished = True
                fact['status'] = 200
                item = dict(type='message', id='msg_fixture_final', role='assistant', status='completed',
                            phase='final_answer', content=[dict(type='output_text', text=self.final_text, annotations=[])])
            self._record(fact)
            return 200, response_events(self._request_count, item)

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        self._server = None

"""Bounded, independently recoverable MCP read pages; no durable state is changed."""
import base64
import hashlib
import json
from datetime import datetime, timezone


NAMES = ('employee_context', 'outbox_list', 'reminder_list')
# reminder_list has no selectors and no window: its cursor carries only the position and the source hash.
STATE = {'employee_context': {'v', 'name', 'selector', 'status', 'limit', 'anchor', 'hash', 'index', 'offset', 'before', 'as_of'},
         'reminder_list': {'v', 'name', 'hash', 'index', 'offset'}}
STATE['outbox_list'] = STATE['employee_context']


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def token(value):
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).decode('ascii')


def untoken(value):
    try:
        data = json.loads(base64.b64decode(value.encode('ascii'), altchars=b'-_', validate=True))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except Exception:
        raise ValueError('Invalid continuation cursor.')


def page(store, name, args, notice=None):
    budget = args.get('max_bytes', 32000)
    if type(budget) is not int or not 1024 <= budget <= 32000:
        raise ValueError('max_bytes must be an integer between 1024 and 32000.')
    cursor = args.get('cursor')
    state = untoken(cursor) if cursor else None
    if state is not None:
        if set(args) - {'cursor', 'max_bytes'}:
            raise ValueError('A cursor cannot be combined with selectors.')
        if set(state) != STATE[name] or state['v'] != 1 or state['name'] != name:
            raise ValueError('Invalid continuation cursor.')
        if type(state['index']) is not int or type(state['offset']) is not int or state['index'] < 0 or state['offset'] < 0:
            raise ValueError('Invalid continuation cursor.')
    elif name == 'reminder_list':
        state = {'v': 1, 'name': name, 'hash': None, 'index': 0, 'offset': 0}
    else:
        state = {'v': 1, 'name': name, 'selector': args.get('section') if name == 'employee_context' else args.get('id'),
                 'status': args.get('status'), 'limit': args.get('limit', 50), 'anchor': None,
                 'hash': None, 'index': 0, 'offset': 0, 'before': args.get('before'), 'as_of': None}
    if name == 'employee_context':
        if cursor:
            try:
                moment = datetime.fromisoformat(state['as_of'])
                if moment.tzinfo is None or moment.isoformat() != state['as_of'] or moment.utcoffset().total_seconds() != 0:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('Invalid continuation cursor.')
        else:
            # Use the store clock, also used by raw context's time-derived views.
            from . import store as store_module
            moment = store_module.datetime.now(timezone.utc)
            state['as_of'] = moment.isoformat()
        data = store.context(as_of=moment)
        if state['selector'] is not None:
            if state['selector'] not in data:
                raise ValueError('Unknown context section.')
            data = {state['selector']: data[state['selector']]}
        sections = list(data)
        entries = list(data.items())
    elif name == 'reminder_list':
        entries = [(item['id'], item) for item in store.reminders_pending()]
    else:
        if state['status'] not in (None, 'pending', 'uncertain', 'failed', 'delivered') or type(state['limit']) is not int or not 1 <= state['limit'] <= 500:
            raise ValueError('Invalid continuation cursor.')
        items = store.outbox_list(status=state['status'])
        if state['before'] is not None:
            matches = [i for i, item in enumerate(items) if item['id'] == state['before']]
            if len(matches) != 1:
                raise ValueError('Invalid or stale before ID.')
            items = items[:matches[0]]
        if cursor and state['anchor'] is not None:
            indices = [i for i, item in enumerate(items) if item['id'] == state['anchor']]
            if len(indices) != 1:
                raise ValueError('Stale continuation cursor: anchor missing.')
            items = items[:indices[0] + 1]
        elif not cursor and items:
            state['anchor'] = items[-1]['id']
        elif cursor and state['anchor'] is None and items:
            items = []
        older = len(items) > state['limit'] and state['selector'] is None
        if state['selector'] is None:
            items = items[-state['limit']:]
        if state['selector'] is not None:
            items = [item for item in items if item['id'] == state['selector']]
            if not items:
                raise ValueError('Unknown outbox item within selection.')
        entries = [(item['id'], item) for item in items]
    digest = hashlib.sha256(dump(entries).encode('utf-8')).hexdigest()
    if cursor and state['hash'] != digest:
        raise ValueError('Stale continuation cursor: source changed.')
    state['hash'] = digest
    index, offset = state['index'], state['offset']
    if index > len(entries) or (index == len(entries) and offset):
        raise ValueError('Invalid continuation cursor.')
    result = {'kind': name, 'total': len(entries), 'index': index, 'next_cursor': None}
    if name == 'employee_context':
        result['sections'] = sections
        result['as_of'] = state['as_of']
    if name == 'outbox_list':
        result['older_before'] = items[0]['id'] if older and items else None
    if notice is not None:
        result['yield'] = notice
    if index == len(entries):
        if len(dump(result).encode('utf-8')) > budget:
            raise ValueError('Byte budget cannot fit continuation metadata.')
        return result
    # Pack complete records first; only split a record when it cannot fit alone.
    if offset == 0:
        packed = []
        for pos in range(index, len(entries)):
            key, value = entries[pos]
            following = dict(state, index=pos + 1, offset=0)
            trial = dict(result, entries=packed + [{'key': key, 'value': value}],
                         next_cursor=token(following) if pos + 1 < len(entries) else None)
            if len(dump(trial).encode('utf-8')) > budget:
                break
            packed = trial['entries']
            result = trial
        if packed:
            return result
    key, value = entries[index]
    encoded = dump(value)
    if offset > len(encoded):
        raise ValueError('Invalid continuation cursor.')
    result.update(key=key, encoding='json', offset=offset, length=len(encoded))
    def candidate(end):
        following = dict(state)
        following['index'], following['offset'] = (index + 1, 0) if end == len(encoded) else (index, end)
        result['chunk'] = encoded[offset:end]
        result['next_cursor'] = token(following) if following['index'] < len(entries) else None
        return len(dump(result).encode('utf-8')) <= budget
    # The final page drops its cursor, so its size is not monotonic.
    if candidate(len(encoded)):
        return result
    lo, hi = offset, len(encoded) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if candidate(mid):
            lo = mid
        else:
            hi = mid - 1
    if lo == offset and offset < len(encoded):
        raise ValueError('Byte budget cannot fit continuation metadata.')
    if not candidate(lo):
        raise ValueError('Byte budget cannot fit continuation metadata.')
    return result

import copy
import json
import unittest

from synthetics.promptcontext import project_context, render_context
from tests.test_prompt_context_acceptance import expand, read_prompt


class PromptContextTests(unittest.TestCase):
    def event(self, text='A long source with café 🌱 and exact whitespace.\n' * 200, **changes):
        return dict(id='event-' + 'a' * 32, body=text, sender='david', source='owner:chat',
                    auth={'channel': 'owner'}, thread='project/~meeting', reply_to='question-1', **changes)

    def test_identical_arrays_and_thread_overlap_resolve_without_losing_metadata(self):
        event = self.event()
        events = [event]
        context = {'identity': {'name': 'Aster'}, 'events': events,
                   'threads': {'project/~meeting': {'items': [dict(event, direction='incoming', sequence=8)]}},
                   'pending_events': events, 'run': {'id': 'run-1', 'event_ids': [event['id']], 'events': events},
                   'recent_runs': [{'id': 'run-0', 'events': events, 'summary': 'Previous attempt failed.'}]}
        before = copy.deepcopy(context)
        projected, references = project_context(context)
        self.assertEqual(expand(projected, references), before)
        self.assertEqual(context, before)
        encoded = json.dumps(projected, ensure_ascii=False)
        self.assertLess(len(encoded), len(json.dumps(before, ensure_ascii=False)) / 3)
        self.assertEqual(encoded.count('A long source'), 200)
        projected['identity']['name'] = 'changed projection only'
        self.assertEqual(context['identity']['name'], 'Aster')

    def test_source_and_auth_differences_are_preserved_with_shared_text(self):
        first = self.event()
        second = dict(first, auth=None, sender='unverified-label', source='manual')
        context = {'events': [first], 'recent_runs': [{'events': [second]}]}
        projected, references = project_context(context)
        self.assertIn('/recent_runs/0/events/0/body', references)
        self.assertIsNone(projected['recent_runs'][0]['events'][0]['auth'])
        self.assertEqual(expand(projected, references), context)

    def test_different_body_versions_never_point_to_the_wrong_words(self):
        first = self.event('Original instruction. ' * 100)
        second = dict(first, body='Corrected instruction. ' * 100)
        context = {'events': [first], 'recent_runs': [{'events': [second]}]}
        projected, references = project_context(context)
        self.assertEqual(expand(projected, references), context)
        self.assertEqual(projected['recent_runs'][0]['events'][0]['body'], second['body'])

    def test_json_pointer_escapes_thread_keys_and_has_no_reference_chain(self):
        event = self.event()
        context = {'threads': {'a/~b': {'items': [dict(event, direction='incoming')]}},
                   'events': [event], 'pending_events': [event]}
        projected, references = project_context(context)
        self.assertEqual(references['/events/0/body'], '/threads/a~1~0b/items/0/body')
        self.assertEqual(expand(projected, references), context)
        for target in references.values():
            self.assertFalse(any(p == target or p.startswith(target + '/') for p in references),
                             'Every target must be material, without nested placeholders')

    def test_small_payloads_and_literal_application_references_are_untouched(self):
        for context in ({}, {'events': [], 'pending_events': []},
                        {'events': [{'id': 'event-1', 'body': 'OK'}]},
                        {'memories': [{'value': {'context_ref': '/literal-user-data'},
                                       'id': 'event-user-example', 'body': 'a' * 500}]}):
            with self.subTest(context=context):
                projected, references = project_context(context)
                self.assertEqual(projected, context)
                self.assertEqual(references, {})
        event = self.event('OK')
        context = {'events': [event], 'threads': {'t': {'items': [dict(event)]}}}
        rendered = render_context(context, preamble='Continue.')
        self.assertLessEqual(len(rendered.encode()), len(('Continue.\n\n' + json.dumps(
            context, ensure_ascii=False, indent=2)).encode()))
        projected, references = read_prompt(rendered)
        self.assertEqual(expand(projected, references), context)

    def test_literal_reference_data_never_becomes_a_generated_pointer(self):
        literal = {'context_ref': '/events/0', 'body_ref': '/cycle',
                   'nested': {'context_ref': '/missing'}}
        event = dict(self.event(), body_ref='/literal-not-generated')
        context = {'memories': [literal], 'events': [event, {'context_ref': '/events/1'}],
                   'pending_events': [event, {'context_ref': '/events/1'}]}
        projected, references = project_context(context)
        self.assertEqual(expand(projected, references), context)
        rendered, refs = read_prompt(render_context(context, preamble='Continue.'))
        self.assertEqual(expand(rendered, refs), context)

    def test_reference_instructions_never_make_a_small_prompt_larger(self):
        for context in ({}, {'identity': {'name': 'Aster'}},
                        {'events': [self.event('short')], 'pending_events': [self.event('short')]}):
            raw = 'Continue.\n\n' + json.dumps(context, ensure_ascii=False, indent=2)
            rendered = render_context(context, preamble='Continue.')
            self.assertLessEqual(len(rendered.encode()), len(raw.encode()))
            view, refs = read_prompt(rendered)
            self.assertEqual(expand(view, refs), context)

    def test_rendering_is_deterministic_and_keeps_unique_historical_sources(self):
        current = self.event()
        previous = dict(current, id='event-' + 'b' * 32, body='Historical decision. ' * 80)
        context = {'events': [current], 'recent_runs': [{'events': [previous]}, {'events': [previous]}]}
        rendered = render_context(context, preamble='Continue.')
        self.assertEqual(rendered, render_context(context, preamble='Continue.'))
        parsed, references = read_prompt(rendered)
        self.assertEqual(expand(parsed, references), context)
        self.assertIn('Historical decision.', rendered)


if __name__ == '__main__':
    unittest.main()

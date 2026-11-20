"""The employee's MCP read preserves lesson qualifications and signals shortened evidence."""

import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import Server
from synthetics.projects import GET_BUDGET, ProjectStore, _canon
from synthetics.store import Store
from tests.humans_fixture import write_host_humans


class ProjectLessonRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = Store(self.root / 'employee')
        self.addCleanup(self.owner.close)
        write_host_humans(self.owner.home)
        self.owner.initialize('Reviewer', 'maintainer')
        self.run = self.owner.run_begin()['id']
        self.bound = Store(self.owner.home, run_id=self.run)
        self.addCleanup(self.bound.close)
        self.server = Server(self.bound, run_id=self.run)

    def append(self, evidence):
        self.body = dict(statement='Retain the conditions and evidence behind a project lesson.',
                         applies_when='When choosing an approach during review.',
                         uncertainty='A local observation, not a universal result.', evidence=evidence)
        return self.server.call('project_memory_append', {
            'project': 'example', 'entries': [{'kind': 'lesson', 'body': self.body}],
            'key': 'lesson-qualification-example'})['ids'][0]

    def get(self, record):
        return self.server.call('project_memory_get', {'project': 'example', 'id': record})

    def test_employee_can_retrieve_qualification_and_original_evidence(self):
        evidence = [{'ref': 'run-reviewed', 'level': 'observed', 'note': 'One controlled example.'}]
        record = self.append(evidence)
        got = self.get(record)
        self.assertEqual(got['lesson']['applies_when'], self.body['applies_when'])
        self.assertEqual(got['lesson']['uncertainty'], self.body['uncertainty'])
        self.assertEqual(got['lesson']['evidence'], evidence)
        self.assertEqual(got['lesson']['evidence_count'], 1)
        self.assertNotIn('evidence_truncated', got['lesson'])

    def test_budget_omitted_notes_are_marked_without_losing_stored_evidence(self):
        evidence = [{'ref': 'event-%03d' % index, 'level': 'observed',
                     'note': 'Relevant source qualification. ' * 15} for index in range(36)]
        record = self.append(evidence)
        got = self.get(record)
        lesson = got['lesson']
        self.assertLessEqual(len(_canon(got).encode('utf-8')), GET_BUDGET)
        self.assertEqual(lesson['evidence_count'], len(evidence))
        self.assertEqual([item['ref'] for item in lesson['evidence']],
                         [item['ref'] for item in evidence])
        self.assertFalse(any('note' in item for item in lesson['evidence']))
        self.assertTrue(lesson.get('evidence_truncated'),
                        'A bounded read must say when evidence annotations were omitted.')
        with ProjectStore(self.root / 'projects' / 'example' / 'memory.sqlite3', 'example') as project:
            stored = next(entry for entry in project.entries() if entry['id'] == record)
        self.assertEqual(stored['body']['evidence'], evidence)
        self.assertEqual(self.get(record)['lesson'], lesson)


if __name__ == '__main__':
    unittest.main()

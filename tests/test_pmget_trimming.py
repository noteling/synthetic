"""Evidence budget policy keeps reference identities and original counts honest."""

import unittest

from synthetics.projects import _shrink_lesson


class LessonEvidenceTrimmingTest(unittest.TestCase):
    def test_notes_are_omitted_before_references_and_reported(self):
        lesson = {'evidence': [{'ref': 'event-a', 'level': 'observed', 'note': 'One example.'},
                               {'ref': 'event-b', 'note': 'A second example.'}],
                  'evidence_count': 2, 'applies_when': 'Local checks', 'uncertainty': 'Limited sample'}
        self.assertTrue(_shrink_lesson(lesson))
        self.assertEqual(lesson['evidence'], [{'ref': 'event-a', 'level': 'observed'}, {'ref': 'event-b'}])
        self.assertTrue(lesson['evidence_truncated'])
        self.assertEqual(lesson['evidence_count'], 2)
        self.assertEqual(lesson['uncertainty'], 'Limited sample')
        self.assertEqual(lesson['applies_when'], 'Local checks')

    def test_omitted_references_do_not_change_the_stored_evidence_count(self):
        lesson = {'evidence': [{'ref': 'event-a'}, {'ref': 'event-b'}], 'evidence_count': 2}
        self.assertTrue(_shrink_lesson(lesson))
        self.assertEqual(lesson['evidence'], [{'ref': 'event-a'}])
        self.assertEqual(lesson['evidence_count'], 2)
        self.assertTrue(lesson['evidence_truncated'])
        self.assertTrue(_shrink_lesson(lesson))
        self.assertEqual(lesson['evidence'], [])
        self.assertFalse(_shrink_lesson(lesson))
        self.assertEqual(lesson['evidence_count'], 2)
        self.assertTrue(lesson['evidence_truncated'])

    def test_empty_or_free_text_evidence_is_not_marked_by_a_noop(self):
        for value, count in (([], 0), ('Observed in one controlled run.', 1)):
            with self.subTest(evidence=value):
                lesson = {'evidence': value, 'evidence_count': count}
                self.assertFalse(_shrink_lesson(lesson))
                self.assertEqual(lesson, {'evidence': value, 'evidence_count': count})


if __name__ == '__main__':
    unittest.main()

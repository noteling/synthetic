"""Operating defaults P1b: all-roles practices in employee.md (work-c093d56a, Codex ruling Q3).

Each practice has a team lesson behind it; they must sit in the Execution section and stay generic (no machine
paths, record IDs or ports), because employee.md ships to every home on every machine."""

import re
import unittest
from pathlib import Path

PROMPT = Path(__file__).resolve().parents[1] / "synthetics" / "prompts" / "employee.md"

PRACTICES = {
    "verify before reporting": "reread the actual memory, file or record",
    "own processes only": "Stop only processes you started, by the PID you recorded",
    "no pattern kill": "never kill by a command pattern",
    "never mutate a running checkout": "Never fetch, switch branches or check out in a checkout another process runs from",
    "detached long runs": "Start long runs (test suites, builds) detached and early in a session",
    "log per run, never overwritten": "never overwrite an earlier log",
    "intermittent cause unknown": "Record an intermittent failure's cause as unknown unless you have evidence",
    "scoped discovery": "ask its author for the exact path rather than searching the whole machine",
}


def execution_section(text):
    start = text.index("Execution:")
    return text[start:]


class EmployeePromptP1bTests(unittest.TestCase):
    def setUp(self):
        self.text = PROMPT.read_text()

    def test_each_practice_is_in_the_execution_section(self):
        section = execution_section(self.text)
        for name, phrase in PRACTICES.items():
            with self.subTest(name):
                self.assertIn(phrase, section)

    def test_practices_stay_generic(self):
        lines = [l for l in execution_section(self.text).splitlines()
                 if any(p in l for p in PRACTICES.values())]
        self.assertGreaterEqual(len(lines), 4)
        for line in lines:
            with self.subTest(line=line[:40]):
                self.assertNotRegex(line, r"/(Users|home|tmp|private)/")
                self.assertNotRegex(line, r"\b(event|run|question|message|memory|work|pm)-[0-9a-f]{8,}")
                self.assertNotRegex(line, r"127\.0\.0\.1|localhost:\d+|:\d{4,5}\b")


if __name__ == "__main__":
    unittest.main()

"""O1 (backlog 56): the employee prompt must not default to idling (team rule pm-8d1a3f37: work, ask the key
question, or say the blocker; when the goal is done, tell whoever assigned it and ask for the next one) and must
keep the rule against inventing tickets."""
import unittest
from pathlib import Path

PROMPT = Path(__file__).resolve().parents[1] / "synthetics" / "prompts" / "employee.md"


class NoIdlePrompt(unittest.TestCase):
    def setUp(self):
        self.text = " ".join(PROMPT.read_text(encoding="utf-8").split())

    def test_no_idle_default(self):
        self.assertNotIn("A quiet inbox can be the right state", self.text)
        self.assertIn("do not sit idle", self.text)
        self.assertIn("asking the person who can clarify your next step the key question", self.text)
        self.assertIn("telling them why you are blocked", self.text)

    def test_goal_done_asks_for_the_next_one(self):
        self.assertIn("When your goal is done, tell whoever assigned it and ask for the next one.", self.text)

    def test_still_no_invented_tickets(self):
        self.assertIn("Do not invent tickets or speculative cleanup work just to remain busy", self.text)
        self.assertIn("speculative churn is not", self.text)


if __name__ == "__main__":
    unittest.main()

"""Backlog 47: newer-source-exists counts only later uncovered sources from a human sender the intent cites."""
import unittest

from synthetics.projectlog import fold


class Log:
    def __init__(self):
        self.entries = []

    def add(self, kind, body, eid=None):
        eid = eid or "pm-%d" % (len(self.entries) + 1)
        self.entries.append({"id": eid, "seq": len(self.entries) + 1, "kind": kind, "author": "a", "body": body})
        return eid


def src(log, role="human", sender="david", thread="t1"):
    return log.add("source", {"origin": {"role": role, "sender": sender, "thread": thread},
                              "quote_source": "event", "capture": {"mode": "binding"}})


def flags(log):
    return fold(log.entries)["records"]["x"]["flags"]


class NewerSourceSenderTests(unittest.TestCase):
    def intent(self, log, *sources):
        log.add("intent", {"sources": list(sources), "claims": [{"cid": "c1", "text": "a"}]}, "x")

    def test_employee_reply_in_same_thread_does_not_flag(self):
        log = Log()
        self.intent(log, src(log))
        src(log, role="out", sender="employee-v")
        src(log, role="other", sender="employee-c")
        self.assertNotIn("newer-source-exists", flags(log))

    def test_relay_and_other_human_do_not_flag(self):
        log = Log()
        self.intent(log, src(log))
        src(log, role="secretary", sender="codex-desktop")
        src(log, role="human", sender="someone-else")
        self.assertNotIn("newer-source-exists", flags(log))

    def test_same_human_later_uncovered_flags_until_considered(self):
        log = Log()
        self.intent(log, src(log))
        later = src(log)
        self.assertIn("newer-source-exists", flags(log))
        log.add("considered", {"source": later, "outcome": "not-captured", "reason": "routine"})
        self.assertNotIn("newer-source-exists", flags(log))

    def test_no_cited_human_sender_keeps_old_rule(self):
        log = Log()
        self.intent(log, src(log, sender=None))
        src(log, role="out", sender="employee-v")
        self.assertIn("newer-source-exists", flags(log))

    def test_fold_records_source_sender(self):
        log = Log()
        s = src(log)
        self.assertEqual(fold(log.entries)["sources"][s]["sender"], "david")


if __name__ == "__main__":
    unittest.main()

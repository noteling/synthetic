import unittest

from synthetics.projectlog import fold


class Log:
    def __init__(self):
        self.entries = []

    def add(self, kind, body, eid=None, author="vesper"):
        seq = len(self.entries) + 1
        eid = eid or f"pm-{seq:04d}"
        self.entries.append({"seq": seq, "id": eid, "kind": kind, "author": author, "run": None,
                             "at": "2026-11-17T00:00:00Z", "body": body})
        return eid


def src(log, role="human", thread="t1", auth=None, qs="event"):
    return log.add("source", {"origin": {"role": role, "thread": thread, "auth": auth}, "quote_source": qs,
                              "capture": {"mode": "binding"}})


class FoldTests(unittest.TestCase):
    def test_a1_chain_only_a1_1_c2_superseded(self):
        log = Log()
        s = [src(log) for _ in range(4)]
        log.add("intent", {"act": "request", "sources": [s[0]], "claims": [{"cid": "c1", "text": "a"}, {"cid": "c2", "text": "b"}]}, "a11")
        log.add("intent", {"act": "correction", "sources": [s[1]], "claims": [{"cid": "c1", "text": "c"}, {"cid": "c2", "text": "d"}]}, "a12")
        log.add("relate", {"rel": "revises", "from": "a12#c2", "target": "a11#c2"})
        log.add("relate", {"rel": "carries", "from": "a12#c1", "target": "a11#c1"})
        log.add("intent", {"act": "correction", "sources": [s[2]], "claims": [{"cid": "c1", "text": "e"}, {"cid": "c2", "text": "f"}]}, "a13")
        log.add("relate", {"rel": "refines", "from": "a13#c1", "target": "a12#c1"})
        st = fold(log.entries)
        superseded = sorted(r for r, c in st["claims"].items() if c["status"] == "superseded")
        self.assertEqual(superseded, ["a11#c2"])
        self.assertEqual(st["claims"]["a11#c2"]["head"], "a12#c2")
        self.assertEqual(st["records"]["a11"]["status"], "current")

    def test_retract_removes_effect_and_retract_of_retract_restores(self):
        log = Log()
        s = src(log)
        log.add("intent", {"sources": [s], "claims": [{"cid": "c1", "text": "a"}]}, "x")
        log.add("intent", {"sources": [s], "claims": [{"cid": "c1", "text": "b"}]}, "y")
        rel = log.add("relate", {"rel": "revises", "from": "y#c1", "target": "x#c1"})
        r1 = log.add("retract", {"target": rel, "reason": "wrong"})
        self.assertEqual(fold(log.entries)["claims"]["x#c1"]["status"], "current")
        log.add("retract", {"target": r1, "reason": "undo"})
        self.assertEqual(fold(log.entries)["claims"]["x#c1"]["status"], "superseded")

    def test_secretary_is_secondhand_and_auth_copied(self):
        log = Log()
        s = src(log, role="secretary", auth={"channel": "k"})
        h = src(log, auth={"channel": "k"})
        log.add("intent", {"sources": [s], "claims": [{"cid": "c1", "text": "a"}]}, "x")
        log.add("intent", {"sources": [h], "claims": [{"cid": "c1", "text": "a"}]}, "y")
        st = fold(log.entries)
        self.assertEqual(st["sources"][s]["quote_source"], "secondhand")
        self.assertIn("secondhand", st["records"]["x"]["flags"])
        self.assertEqual(st["records"]["y"]["ceiling"], "verified")
        self.assertEqual(st["sources"][h]["auth"], {"channel": "k"})

    def test_redact_withdraws_intent(self):
        log = Log()
        s = src(log)
        log.add("intent", {"sources": [s], "claims": [{"cid": "c1", "text": "a"}]}, "x")
        log.add("redact", {"target": s, "sha256": "0", "reason": "misrouted"})
        st = fold(log.entries)
        self.assertTrue(st["sources"][s]["redacted"])
        self.assertEqual(st["records"]["x"]["status"], "withdrawn")

    def test_lesson_dispute_and_status(self):
        log = Log()
        a = log.add("lesson", {"statement": "a"}, "la")
        b = log.add("lesson", {"statement": "b"}, "lb")
        log.add("relate", {"rel": "disputes", "from": b, "target": a})
        st = fold(log.entries)
        self.assertEqual(st["records"][a]["status"], "disputed")
        log.add("status", {"target": a, "to": "accepted"})
        self.assertEqual(fold(log.entries)["records"][a]["status"], "accepted")

    def test_newer_source_flag_cleared_by_considered(self):
        log = Log()
        s = src(log)
        log.add("intent", {"sources": [s], "claims": [{"cid": "c1", "text": "a"}]}, "x")
        later = src(log)
        self.assertIn("newer-source-exists", fold(log.entries)["records"]["x"]["flags"])
        log.add("considered", {"source": later, "outcome": "not-captured", "reason": "routine"})
        self.assertNotIn("newer-source-exists", fold(log.entries)["records"]["x"]["flags"])

    def test_fold_is_deterministic_and_order_independent_of_input_list(self):
        log = Log()
        s = src(log)
        log.add("intent", {"sources": [s], "claims": [{"cid": "c1", "text": "a"}]}, "x")
        self.assertEqual(fold(log.entries), fold(list(reversed(log.entries))))


if __name__ == "__main__":
    unittest.main()

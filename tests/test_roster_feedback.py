"""M26 consumer feedback (Rowan, via Codex): via/matched_as, operator-seed label, memory-vs-view rule."""

import unittest
from pathlib import Path

from synthetics import roster

ROOT = Path(__file__).resolve().parent.parent
ROWAN = "employee-28ed6d90e0fe4b42b64bae0e41e82cba"
DAVID = "david"
CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"


def entry(seq, kind, body, author="operator:operator"):
    return {"id": "co-%04d" % seq, "seq": seq, "kind": kind, "author": author,
            "at": "2026-11-18T09:00:%02dZ" % seq, "body": body}


def state():
    seed = "/x/rowan/state.sqlite3 identity row (%s)" % ROWAN
    return roster.company_fold([
        entry(1, "person", {"id": ROWAN, "kind": "employee", "name": "Rowan", "role": "Noteling maintainer",
                            "source": seed, "certainty": "stated"}),
        entry(2, "alias", {"alias": "rowan", "id": ROWAN, "source": seed, "certainty": "stated"}),
        entry(3, "person", {"id": DAVID, "kind": "human", "name": "David", "role": "Product sponsor",
                            "source": "desktop note", "certainty": "stated"}),
        entry(4, "alias", {"alias": "Dana Example", "id": DAVID, "source": "event-83304469", "certainty": "stated"},
              author="employee-a22b6a2a8f524cd6b6800b0a620dbe06"),
        entry(5, "person", {"id": CODEX, "kind": "employee", "name": "Codex", "role": "Synthetics manager",
                            "source": "operator statement", "certainty": "stated"}),
    ])


class ViaTest(unittest.TestCase):
    def test_alias_reported_when_text_is_also_the_name(self):
        st = state()
        self.assertEqual(roster.resolve(st, "Rowan"), {"id": ROWAN, "via": "alias"})
        self.assertEqual(roster.matched_as(st, ROWAN, "Rowan"), ["name", "alias"])

    def test_plain_name_and_alias_and_id(self):
        st = state()
        self.assertEqual(roster.resolve(st, "codex"), {"id": CODEX, "via": "name"})
        self.assertEqual(roster.matched_as(st, CODEX, "codex"), ["name"])
        self.assertEqual(roster.resolve(st, "David"), {"id": DAVID, "via": "id"})  # ID "david" folds equal
        self.assertEqual(roster.matched_as(st, DAVID, "David"), ["id", "name"])
        self.assertEqual(roster.resolve(st, "dana example"), {"id": DAVID, "via": "alias"})
        self.assertEqual(roster.resolve(st, ROWAN), {"id": ROWAN, "via": "id"})
        self.assertEqual(roster.matched_as(st, ROWAN, ROWAN), ["id"])
        self.assertEqual(roster.matched_as(st, ROWAN, "nobody"), [])


class OperatorSeedLabelTest(unittest.TestCase):
    def test_identity_row_seed_renders_as_operator_seed_and_auth_stays_null(self):
        st = state()
        p = st["people"][ROWAN]
        text = roster.render_person(p)
        self.assertIn("operator seed (identity row)", text)
        self.assertNotIn("unauthenticated source", text)
        self.assertIsNone(p["roles"][-1]["auth"])          # nothing upgraded
        self.assertNotIn("authenticated", text.replace("unauthenticated", ""))

    def test_other_unauthenticated_sources_keep_the_old_label(self):
        text = roster.render_person(state()["people"][DAVID])  # operator, but not an identity row
        self.assertIn("unauthenticated source", text)
        self.assertNotIn("operator seed", text)
        not_operator = {"by": "employee-x", "source": "y identity row", "auth": None}
        self.assertEqual(roster.provenance_label(not_operator), "unauthenticated source")
        self.assertEqual(roster.provenance_label({"by": "operator:op", "source": "identity row",
                                                  "auth": {"channel": "fieldpost"}}), "authenticated")


class MemoryVersusViewRuleTest(unittest.TestCase):
    def test_rule_in_prompt_and_docs(self):
        prompt = (ROOT / "synthetics" / "prompts" / "employee.md").read_text()
        docs = (ROOT / "docs" / "roster.md").read_text()
        self.assertIn("When a memory and a view disagree, the memory is the evidence", prompt)
        self.assertIn("When a memory and a view disagree, the memory wins", docs)


if __name__ == "__main__":
    unittest.main()

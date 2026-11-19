"""Docket links (low batch after VIS G3): the Docket table on Lookout's Team page links its questions.

The question cell links to that question's open-question anchor in the same block (#q-<id>); the "now" cell shows
the hop with the onward question it went via (hop_via, new additive docket field) and the questions it carries, all
linked like the G3 open-question list. Relayed bodies never appear."""
import json
import unittest

from synthetics import capture, docket, lookout, sharedstatus
from tests import test_vis_g3_relay_links as g3


class DocketLinkTests(unittest.TestCase):
    """Reuses the G3 fixture: Rowan asked Codex (rq); Codex asked David (dq) carrying rq."""

    setUp_g3 = g3.RelayLinkTests.setUp
    status = g3.RelayLinkTests.status

    def setUp(self):
        self.setUp_g3()
        capture.write_humans(self.root / "manager", {"default_human": "david", "senders": ["david"]})
        # A question to David whose recipient reported a hop via an onward question (with an unsafe id).
        self.hq = self.mgr.question_ask("david", "Budget for M30?", key="hq")
        self.via = "question-<b>x'y"
        self.mgr.question_hop(self.hq["id"], "with-person", "2026-11-18T11:00:00Z", self.via, sender="david",
                              person="secretary")
        self.assertTrue(sharedstatus.write(self.mgr, paused=None))

    def docket_rows(self, page):
        start = page.index("<b>Waiting on David</b>")
        return page[start:page.index("</table>", start)]

    def test_build_has_hop_via(self):
        items = {i["question_id"]: i for i in docket.build(self.mgr)["items"]}
        self.assertEqual(items[self.hq["id"]]["hop_via"], self.via)
        self.assertIsNone(items[self.dq["id"]]["hop_via"])
        status = {i["question_id"]: i for i in self.status("manager")["docket"]["items"]}
        self.assertEqual(status[self.hq["id"]]["hop_via"], self.via)

    def test_docket_rows_link_question_carries_and_via(self):
        rows = self.docket_rows(lookout.team_page(self.root, agents={}))
        # Question cell links to the open-question anchor in the same block.
        self.assertIn("<a href='#q-%s'><code>%s</code></a> Rowan needs" % (self.dq["id"], self.dq["id"]), rows)
        # "now" cell: carried question linked, asker named.
        self.assertIn("carries <a href='#q-%s'><code>%s</code></a> for Rowan" % (self.rq["id"], self.rq["id"]), rows)
        # "now" cell: hop with the onward question, anchor sanitized and text escaped.
        self.assertIn("with-secretary via <a href='#q-question-bxy'><code>question-&lt;b&gt;x&#x27;y</code></a>", rows)
        self.assertNotIn("<b>x", rows)
        self.assertNotIn("RELAYED-BODY-SENTINEL", rows)


if __name__ == "__main__":
    unittest.main()

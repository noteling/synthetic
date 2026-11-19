"""VIS G3: relay hops are linked on both sides of Lookout's Team page.

The asker's open question shows where it is now (hop) and links the onward question; the carrier's open question
shows which askers' questions it carries and links them back. status.json carries only IDs, never a relayed body."""
import json
import tempfile
import unittest
from pathlib import Path

from synthetics import lookout, sharedstatus
from synthetics.roster import CompanyStore
from synthetics.store import Store


def person(pid, name, kind="employee"):
    return {"kind": "person", "body": {"id": pid, "kind": kind, "name": name, "role": "role of " + name,
                                       "source": "operator statement", "certainty": "stated"}}


def relayed_body(sender, msg_id, question_id, to):
    """Shape of a Fieldpost-relayed question (fieldpost/relay.py _format_body)."""
    return ("from: %s\nmessage-id: %s\nquestion-id: %s\nto: %s\nwork-id: -\nsent-at: 2026-11-18T10:00:00Z\n\n---\n\n"
            "RELAYED-BODY-SENTINEL" % (sender, msg_id, question_id, to))


class RelayLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mgr = Store(self.root / "manager")
        self.addCleanup(self.mgr.close)
        self.mgr.initialize("Codex", "manager")
        self.rowan = Store(self.root / "rowan")
        self.addCleanup(self.rowan.close)
        self.rowan.initialize("Rowan", "maintainer")
        self.mgr_id, self.rowan_id = self.mgr.identity()["id"], self.rowan.identity()["id"]
        with CompanyStore.at_root(self.root) as co:
            co.append([person(self.mgr_id, "Codex"), person(self.rowan_id, "Rowan"),
                       person("david", "David", kind="human")], author="operator", operator=True)
        key = "k" * 64
        self.mgr.channel_register("fieldpost", Store.credential_hash(key), ["fieldpost"], "test",
                                  routes=[self.rowan_id])
        self.rq = self.rowan.question_ask("codex-manager", "Writable checkout?", key="rq")
        self.rowan.question_routed(self.rq["id"], self.mgr_id, "fieldpost")
        self.mgr.feed(self.rowan_id, relayed_body(self.rowan_id, self.rq["message_id"], self.rq["id"], "codex-manager"),
                      source="fieldpost", external_id="m1", credential=key)
        self.dq = self.mgr.question_ask("david", "Rowan needs a writable checkout; OK?", key="dq",
                                        relays=[self.rq["id"]])
        hop = [m for m in self.mgr.outbox_list() if m.get("kind") == "hop"][0]
        self.rowan.question_hop(hop["hop"]["question_id"], hop["hop"]["hop"], hop["hop"]["since"], hop["hop"]["via"],
                                sender=hop["sender"], person=hop["hop"]["person"])
        for store in (self.mgr, self.rowan):
            self.assertTrue(sharedstatus.write(store, paused=None))

    def status(self, home):
        return json.loads(sharedstatus.path(self.root / home).read_text())

    def test_status_json_carries_ids_only(self):
        mgr_q = {q["id"]: q for q in self.status("manager")["questions_open"]}[self.dq["id"]]
        self.assertEqual(mgr_q["carries"], [{"question_id": self.rq["id"], "asker": self.rowan_id}])
        self.assertIsNone(mgr_q["hop_via"])
        rowan_q = {q["id"]: q for q in self.status("rowan")["questions_open"]}[self.rq["id"]]
        self.assertEqual(rowan_q["hop"], "with-david")
        self.assertEqual(rowan_q["hop_via"], self.dq["id"])
        self.assertEqual(rowan_q["carries"], [])
        for home in ("manager", "rowan"):
            self.assertNotIn("RELAYED-BODY-SENTINEL", sharedstatus.path(self.root / home).read_text())

    def test_team_page_links_both_sides(self):
        page = lookout.team_page(self.root, agents={})
        # Both open questions are anchors on the page.
        self.assertIn("<li id='q-%s'>" % self.dq["id"], page)
        self.assertIn("<li id='q-%s'>" % self.rq["id"], page)
        # Carrier side: carries the asker's question, links it, names the asker.
        self.assertIn("carries <a href='#q-%s'><code>%s</code></a> for Rowan" % (self.rq["id"], self.rq["id"]), page)
        # Asker side: where it is now, and the onward question it went via.
        self.assertIn("with-david via <a href='#q-%s'><code>%s</code></a>" % (self.dq["id"], self.dq["id"]), page)
        self.assertNotIn("RELAYED-BODY-SENTINEL", page)

    def test_plain_open_question_unchanged(self):
        other = self.rowan.question_ask("david", "Unrelated <b>q</b>?", key="plain")
        sharedstatus.write(self.rowan, paused=None)
        page = lookout.team_page(self.root, agents={})
        line = [l for l in page.split("<li ") if other["id"] in l][0]
        self.assertNotIn(" — ", line)
        self.assertIn("Unrelated &lt;b&gt;q&lt;/b&gt;?", line)

    def test_old_status_without_g3_fields_still_renders(self):
        path = sharedstatus.path(self.root / "rowan")
        value = json.loads(path.read_text())
        for q in value["questions_open"]:
            q.pop("hop_via", None)
            q.pop("carries", None)
        path.write_text(json.dumps(value))
        page = lookout.team_page(self.root, agents={})
        self.assertIn("— with-david</li>", page)


if __name__ == "__main__":
    unittest.main()

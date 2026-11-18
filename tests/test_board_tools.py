"""M27 (iv) Board: MCP tools, mention resolution, prompt lines, doctor board-prefix warning (design tests 2, 3; §6; C7; Q4)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.humans_fixture import HOST_HUMANS
from synthetics import doctor
from synthetics.mcp import METHODS, MUTATIONS, Server
from synthetics.projects import ProjectError, ProjectStore
from synthetics.roster import CompanyStore
from synthetics.store import Store

ROWAN = "employee-28ed6d90e0fe4b42b64bae0e41e82cba"
PROMPT = Path(__file__).resolve().parents[1] / "synthetics" / "prompts" / "employee.md"


def person(pid, name):
    return {"kind": "person", "body": {"id": pid, "kind": "employee", "name": name, "role": "maintainer",
                                       "source": "operator statement", "certainty": "stated"}}


class BoardToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "vesper"
        self.store = Store(self.home)
        self.me = self.store.initialize("Vesper", "maintainer")["id"]
        with CompanyStore.at_root(self.root) as co:
            co.append([person(ROWAN, "Rowan"), person(self.me, "Vesper"),
                       {"kind": "alias", "body": {"alias": "rowan-bot", "id": ROWAN, "source": "operator statement",
                                                  "certainty": "stated"}}], author="operator", operator=True)
        with self.ps() as ps:
            ps.grant(self.me, "post", by="operator")
            ps.grant(ROWAN, "post", by="operator")
        self.run = self.store.run_begin()["id"]
        self.bound = Store(self.home, run_id=self.run)
        self.server = Server(self.bound, run_id=self.run)

    def tearDown(self):
        self.bound.close()
        self.store.close()
        self.tmp.cleanup()

    def ps(self):
        return ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")

    def test_tools_registered(self):
        self.assertIn("board_post", METHODS)
        self.assertIn("board_read", METHODS)
        self.assertIn("board_post", MUTATIONS)
        self.assertNotIn("board_read", MUTATIONS)

    def test_03_mentions_resolve_alias_and_unknown_rejected(self):
        out = self.server.call("board_post", {"project": "synthetics", "topic": "t", "body": "b",
                                              "mentions": ["rowan-bot"], "key": "k1"})
        with self.ps() as ps:
            post = ps.state()["posts"][out["ids"][0]]
            self.assertEqual(post["mentions"], [ROWAN])
            self.assertEqual(ps.entries()[-1]["run"], self.run)  # fenced (Q4)
        with self.assertRaises(ProjectError) as cm:
            self.server.call("board_post", {"project": "synthetics", "topic": "t", "body": "b",
                                            "mentions": ["nobody-here"], "key": "k2"})
        self.assertEqual(cm.exception.code, "unknown_person")

    def test_append_accepts_post_but_never_backlog(self):
        self.server.call("project_memory_append", {"project": "synthetics", "key": "a1", "entries": [
            {"kind": "post", "body": {"topic": "t", "body": "b", "mentions": ["Rowan"]}}]})
        with self.ps() as ps:
            self.assertEqual([p["mentions"] for p in ps.state()["posts"].values()], [[ROWAN]])
        with self.assertRaises(ProjectError) as cm:
            self.server.call("project_memory_append", {"project": "synthetics", "key": "a2", "entries": [
                {"kind": "backlog", "body": {"work_id": "work-x", "status": "ready"}}]})
        self.assertEqual(cm.exception.code, "bad_kind")

    def test_02_post_grant_cannot_write_lessons_and_read_cannot_post(self):
        with self.assertRaises(ProjectError) as cm:
            self.server.call("project_memory_append", {"project": "synthetics", "key": "l1", "entries": [
                {"kind": "lesson", "body": {"text": "x"}}]})
        self.assertEqual(cm.exception.code, "no_grant")
        with self.ps() as ps:
            ps.grant(self.me, "read", by="operator")
        with self.assertRaises(ProjectError) as cm:
            self.server.call("board_post", {"project": "synthetics", "topic": "t", "body": "b", "key": "k3"})
        self.assertEqual(cm.exception.code, "no_grant")
        self.assertEqual(self.server.call("board_read", {"project": "synthetics"})["posts"], [])

    def test_board_read_thread_since_and_backlog(self):
        root = self.server.call("board_post", {"project": "synthetics", "topic": "root", "body": "r",
                                               "key": "r"})["ids"][0]
        with self.ps() as ps:
            reply = ps.append([{"kind": "post", "body": {"topic": "re", "body": "x", "reply_to": root}}],
                              author=ROWAN)["ids"][0]
            other = ps.append([{"kind": "post", "body": {"topic": "other", "body": "y"}}], author=ROWAN)["ids"][0]
            seq_root = ps.state()["posts"][root]["seq"]
        thread = self.server.call("board_read", {"project": "synthetics", "thread": reply})
        self.assertEqual([p["id"] for p in thread["posts"]], [root, reply])
        self.assertEqual(thread["posts"][0]["replies"], [reply])
        self.assertNotIn("backlog", thread)
        since = self.server.call("board_read", {"project": "synthetics", "since": seq_root})
        self.assertEqual([p["id"] for p in since["posts"]], [reply, other])
        self.assertIn("backlog", self.server.call("board_read", {"project": "synthetics"}))
        with self.assertRaises(ProjectError):
            self.server.call("board_read", {"project": "synthetics", "thread": root, "since": 0})

    def test_c7_prompt_lines(self):
        text = PROMPT.read_text(encoding="utf-8")
        self.assertIn("board_post", text)
        self.assertIn("title, status, waiting_for", text)
        self.assertIn("visible to everyone who can read that project", text)
        self.assertIn("question_ask for anything you need answered", text)

    def test_q4_doctor_warns_on_board_prefix(self):
        cfg = copy.deepcopy(HOST_HUMANS)
        (self.home / "humans.json").write_text(json.dumps(cfg), encoding="utf-8")
        codes = lambda r: {p["code"] for p in r["findings"]}
        self.assertNotIn("humans_board_prefix", codes(doctor.diagnose(self.home, launchd=lambda l: (False, None))))
        cfg["limit_bypass"]["channel_prefixes"].append("board:")
        (self.home / "humans.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertIn("humans_board_prefix", codes(doctor.diagnose(self.home, launchd=lambda l: (False, None))))


if __name__ == "__main__":
    unittest.main()

"""VIS G1: Lookout's per-project "open work" page, built from published works (the project log's backlog rows).

Live data at 02:15Z 11-19: the synthetics board backlog held 35 rows, 29 of them done or cancelled, so the 6 open
works were mixed into a mostly finished list. /p/<project>/work shows only ready and waiting works, grouped by owner
(by name), with the age of each row's latest projection."""
import tempfile
import unittest
from pathlib import Path

from synthetics import lookout
from synthetics.projects import ProjectStore
from synthetics.roster import CompanyStore

VESPER, ROWAN = "employee-v", "employee-r"
BLOCKER = "question-" + "a" * 32 + " with-david"


def person(pid, name):
    return {"kind": "person", "body": {"id": pid, "kind": "employee", "name": name, "role": "role of " + name,
                                       "source": "operator statement", "certainty": "stated"}}


def backlog(work_id, status, title, priority=3, waiting_for="", blocked_by=None):
    body = {"work_id": work_id, "status": status, "title": title, "origin": "self", "priority": priority,
            "waiting_for": waiting_for, "evidence": []}
    if blocked_by:
        body["blocked_by"] = blocked_by
    return {"kind": "backlog", "body": body}


class OpenWorkPageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        with CompanyStore.at_root(self.root) as co:
            co.append([person(VESPER, "Vesper"), person(ROWAN, "Rowan")], author="operator", operator=True)
        store = ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")
        self.addCleanup(store.close)
        store.append([backlog("work-v1", "ready", "Open slice <b>one</b>", priority=2),
                      backlog("work-v2", "done", "FINISHED-TITLE"),
                      backlog("work-v3", "waiting", "Waiting slice", waiting_for="Codex review",
                              blocked_by=[BLOCKER])], author=VESPER)
        store.append([backlog("work-r1", "waiting", "Rowan glance"),
                      backlog("work-r2", "cancelled", "CANCELLED-TITLE"),
                      backlog("work-r3", "ready", "UNPUBLISHED-TITLE")], author=ROWAN)
        store.append([backlog("work-r3", "unpublished", "UNPUBLISHED-TITLE")], author=ROWAN)
        (self.root / "projects" / "noteling").mkdir()
        ProjectStore(self.root / "projects" / "noteling" / "memory.sqlite3", "noteling").close()

    def get(self, path):
        return lookout.route(str(self.root), path, agents={})

    def test_open_work_page_lists_only_ready_and_waiting(self):
        code, body = self.get("/p/synthetics/work")
        self.assertEqual(code, 200)
        for text in ("Open slice &lt;b&gt;one&lt;/b&gt;", "Waiting slice", "Rowan glance", "Codex review"):
            self.assertIn(text, body)
        qid, rest = BLOCKER.split(" ", 1)  # VIS-O: the blocker's question ID moves into the tooltip
        self.assertIn("<span title='%s'>an answer to a question</span> %s" % (qid, rest), body)
        for text in ("FINISHED-TITLE", "CANCELLED-TITLE", "UNPUBLISHED-TITLE", "<b>one</b>"):
            self.assertNotIn(text, body)
        self.assertIn("2 closed", body)  # done + cancelled are counted and left to the board page
        self.assertIn('href="/p/synthetics/board"', body)

    def test_grouped_by_owner_name_and_priority_order(self):
        _, body = self.get("/p/synthetics/work")
        self.assertIn("<h2>Rowan (1)</h2>", body)
        self.assertIn("<h2>Vesper (2)</h2>", body)
        self.assertLess(body.index("<h2>Rowan"), body.index("<h2>Vesper"))
        self.assertLess(body.index("Waiting slice"), body.index("Open slice"))  # higher priority first: 3 before 2

    def test_updated_age_from_latest_projection(self):
        entries, _ = lookout.project_entries(self.root, "synthetics")
        now = max(lookout._parse(e["at"]) for e in entries if e["kind"] == "backlog")
        body = lookout.work_page(str(self.root), "synthetics", now=now)
        self.assertIn("<td>0m</td>", body)

    def test_empty_project_and_nav_link(self):
        code, body = self.get("/p/noteling/work")
        self.assertEqual(code, 200)
        self.assertIn("No open published work", body)
        _, listing = self.get("/projects")  # VIS-O: nav is Overview · Projects · Diagnostics
        self.assertIn("href='/p/synthetics'", listing)
        self.assertIn('href="/projects"', self.get("/")[1])


if __name__ == "__main__":
    unittest.main()

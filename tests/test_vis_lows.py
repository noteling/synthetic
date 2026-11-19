"""VIS LOW items (Codex event-3b0572e9, from Rowan's P3 consumer review): G-a1 work titles in the threads list,
G6 names instead of raw IDs, G7 a "last updated" age on work rows, G8 a drill-down for stuck outbox items,
G9 relative timestamps, G10 paging for big thread pages (a live thread page was 203 KB)."""
import json
import re
from datetime import datetime, timedelta, timezone

from synthetics import lookout, sharedstatus
from synthetics.projects import ProjectStore
from tests import test_lookout as tl

WORK = "work-" + "1" * 32
PAGE = 50  # messages per thread page (lookout.THREAD_PAGE)


def backlog(work_id, title, status="waiting"):
    return {"kind": "backlog", "body": {"work_id": work_id, "status": status, "title": title, "origin": "self",
                                        "priority": 2, "waiting_for": "", "evidence": []}}


class _Project(tl._Root):
    def project(self, entries, author=None):
        store = ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")
        self.addCleanup(store.close)
        for entry in entries:
            store.append([entry], author=author or self.ident["id"])
        return store

    def get(self, path):
        return lookout.route(str(self.root), path, agents={})


class ThreadTitlesAndNamesTests(_Project):
    def setUp(self):
        super().setUp()
        me = self.ident["id"]
        self.project([backlog(WORK, "Build <the> slice"),
                      tl.fp_source(tl.CODEX, me, "message-0001", "please build", for_work=WORK)])

    def test_ga1_threads_list_shows_work_title(self):
        code, body = self.get("/p/synthetics/threads")
        self.assertEqual(code, 200)
        self.assertIn(">Build &lt;the&gt; slice</a>", body)
        self.assertNotIn(">%s</a>" % WORK, body)

    def test_g6_names_not_ids_in_threads_and_board(self):
        _, body = self.get("/p/synthetics/threads")
        row = body.split("<table>", 1)[1]
        self.assertIn(">Codex</span>", row)
        self.assertIn(">Vesper</span>", row)
        self.assertNotIn(">%s<" % tl.CODEX, row)  # the raw ID survives only as a tooltip
        key = lookout.thread_key(dict(sender=tl.CODEX, to=self.ident["id"], works=[WORK]))
        _, page = self.get("/p/synthetics/thread/" + lookout.quote(key, safe=""))
        self.assertIn("<b><span title='%s'>Codex</span></b>" % tl.CODEX, page)
        self.assertIn("About: Build &lt;the&gt; slice", page)
        _, board = self.get("/p/synthetics/board")
        self.assertIn("<td><span title='%s'>Vesper</span></td>" % self.ident["id"], board)

    def test_g9_relative_times_with_absolute_tooltip(self):
        _, body = self.get("/p/synthetics/threads")
        self.assertRegex(body, r"<time datetime='2026-11-18T10:00:00Z' title='2026-11-18T10:00:00Z'>(\d+[dhm] )*\d+[dhm] ago</time>")
        now = datetime(2026, 11, 18, 10, 5, tzinfo=timezone.utc)
        self.assertIn(">5m ago<", lookout._rel("2026-11-18T10:00:00Z", now))
        self.assertIn(">in 2h 0m<", lookout._rel("2026-11-18T12:05:00Z", now))
        self.assertIn(">just now<", lookout._rel("2026-11-18T10:05:30Z", now))
        self.assertEqual(lookout._rel("not a time", now), "not a time")


class ThreadPagingTests(_Project):
    def setUp(self):
        super().setUp()
        me = self.ident["id"]
        self.n = PAGE * 2 + 7  # 107 messages: pages of 50, 50, 7
        self.project([tl.fp_source(tl.CODEX, me, "message-%04d" % i, "MSG-%04d-END" % i, for_work=WORK)
                      for i in range(self.n)])
        self.key = lookout.thread_key(dict(sender=tl.CODEX, to=me, works=[WORK]))

    def page(self, query=""):
        code, body = self.get("/p/synthetics/thread/%s%s" % (lookout.quote(self.key, safe=""), query))
        self.assertEqual(code, 200)
        return set(re.findall(r"MSG-(\d{4})-END", body)), body

    def test_g10_default_is_newest_page(self):
        shown, body = self.page()
        self.assertIn("Page 3 of 3", body)
        self.assertEqual(shown, {"%04d" % i for i in range(PAGE * 2, self.n)})
        self.assertIn("?page=2'>older<", body)
        self.assertNotIn(">newer<", body)

    def test_g10_explicit_and_bad_pages(self):
        shown, body = self.page("?page=1")
        self.assertEqual(shown, {"%04d" % i for i in range(PAGE)})
        self.assertIn("Page 1 of 3", body)
        self.assertIn("?page=2'>newer<", body)
        for bad in ("?page=99", "?page=x", "?page=-4"):
            shown, _ = self.page(bad)
            self.assertTrue(shown)  # clamped, never empty or an error
        self.assertEqual(self.page("?page=99")[0], self.page()[0])


class StatusDrillDownTests(_Project):
    def test_g7_g8_status_export_carries_updated_and_stuck_items_without_body(self):
        w = self.store.work_put("Work row", description="d")
        self.store.message_send("nobody-routes-this", "STUCK-BODY-SENTINEL", key="k1")
        later = datetime.now(timezone.utc) + timedelta(minutes=30)
        value = sharedstatus.build(self.store, now=later)
        self.assertEqual(value["works"][0]["id"], w["id"])
        self.assertTrue(value["works"][0]["updated_at"])
        stuck = value["outbox_stuck"]
        self.assertEqual(stuck["count"], 1)
        self.assertEqual(set(stuck["items"][0]), {"id", "to", "kind", "question_id", "state", "age_minutes",
                                                  "routed_by"})
        self.assertEqual((stuck["items"][0]["to"], stuck["items"][0]["routed_by"]), ("nobody-routes-this", []))
        self.assertNotIn("STUCK-BODY-SENTINEL", json.dumps(value))

    def test_g7_g8_team_page_updated_column_and_stuck_table(self):
        self.store.work_put("Work row", description="d")
        self.store.message_send(tl.CODEX, "STUCK-BODY-SENTINEL", key="k1")
        later = datetime.now(timezone.utc) + timedelta(minutes=30)
        value = sharedstatus.build(self.store, now=later)
        path = sharedstatus.path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        body = lookout.team_page(str(self.root), agents={}, now=later)
        self.assertIn("<th>updated</th>", body)
        self.assertRegex(body, r"<td><time datetime='[^']+' title='[^']+'>30m ago</time></td>")
        self.assertIn("Stuck messages (newest 1 of 1)", body)
        self.assertIn("<td><span title='%s'>Codex</span></td>" % tl.CODEX, body)
        self.assertIn("no channel routes this address", body)
        self.assertIn("<td>30m</td>", body)
        self.assertNotIn("STUCK-BODY-SENTINEL", body)

"""#64 S1e: a plain board_read stays under 32,000 serialized bytes; open rows first, the rest by count and cursor."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import projecttools, projectview
from synthetics.mcp import METHODS, Server
from synthetics.projects import ProjectError, ProjectStore
from synthetics.store import Store

LIMIT = 32000
OTHER = "employee-" + "9" * 32
KEYS = {"owner", "work_id", "title", "origin", "status", "waiting_for", "priority"}
CLOSED = ("done", "cancelled")


def read_7a35b2c(ps, thread=None, since=None):
    """board_read's thread= and since= results as main 7a35b2c built them, copied."""
    st = ps.state()
    posts = st.get("posts") or {}
    if thread is not None:
        root = posts[thread]["root"]
        want = {pid for pid, p in posts.items() if p["root"] == root}
    else:
        want = {pid for pid, p in posts.items() if p["seq"] > int(since or 0)}
    out, used, more = [], 0, 0
    for e in ps.entries():
        if e["kind"] != "post" or e["id"] not in want:
            continue
        item = {"id": e["id"], "seq": e["seq"], "at": e["at"], "author": e["author"],
                "topic": e["body"].get("topic", ""), "body": e["body"].get("body", ""),
                "mentions": e["body"].get("mentions", []), "reply_to": e["body"].get("reply_to"),
                "refs": e["body"].get("refs", []), "replies": posts[e["id"]].get("replies", [])}
        size = len(json.dumps(item))
        if used + size > 8192:
            more += 1
            continue
        used += size
        out.append(item)
    return {"project": ps.project, "posts": out, "more": more, "as_of_seq": ps.head_seq()}


def row(n, status="ready", title=None, waiting="", priority=1, **more):
    return dict({"work_id": "work-%05d" % n, "title": "row %d" % n if title is None else title, "origin": "self",
                 "status": status, "waiting_for": waiting, "evidence": [], "priority": priority}, **more)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = Store(self.root / "vesper")
        self.me = self.s.initialize("Vesper", "maintainer")["id"]
        with self.ps() as ps:
            ps.grant(self.me, "post", by="operator")
            ps.grant(OTHER, "post", by="operator")
        self.server = Server(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def ps(self, project="synthetics"):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def put(self, *bodies, author=OTHER, project="synthetics"):
        with self.ps(project) as ps:
            for at in range(0, len(bodies), 50):
                ps.append([{"kind": "backlog", "body": b} for b in bodies[at:at + 50]], author=author)

    def posts(self, n, body="p" * 900):
        with self.ps() as ps:
            return [ps.append([{"kind": "post", "body": {"topic": "post %d" % i, "body": body}}], author=OTHER)["ids"][0]
                    for i in range(n)]

    def text(self, **arguments):
        """The exact text tools/call sends."""
        result = self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "board_read", "arguments": dict({"project": "synthetics"}, **arguments)}})["result"]
        self.assertFalse(result.get("isError", False), result)
        return result["content"][0]["text"]

    def read(self, **arguments):
        text = self.text(**arguments)
        self.assertLessEqual(len(text.encode("utf-8")), LIMIT)
        return json.loads(text)

    def refused(self, code, **arguments):
        with self.assertRaises(ProjectError) as caught:
            self.server.call("board_read", dict({"project": "synthetics"}, **arguments))
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def walk(self):
        """Every page from the plain call on, checking what each says is left."""
        pages = [self.read()]
        while pages[-1]["backlog_next"] is not None:
            self.assertLess(len(pages), 100)
            pages.append(self.read(backlog_cursor=pages[-1]["backlog_next"]))
        return pages


class Bounded(Case):
    def test_a_large_backlog_stays_under_the_limit_and_fills_the_page(self):
        self.put(*[row(n, title="t" * 190, waiting="w" * 190, priority=n % 4) for n in range(300)])
        self.posts(12)
        page = self.read()
        order = sorted(range(300), key=lambda n: (-(n % 4), n))
        got = [r["work_id"] for r in page["backlog"]]
        self.assertEqual(got, ["work-%05d" % n for n in order[:len(got)]])
        self.assertGreater(len(page["posts"]), 5)
        following = self.read(backlog_cursor=page["backlog_next"])["backlog"][0]
        grown = dict(page, backlog=page["backlog"] + [following])
        self.assertGreater(len(json.dumps(grown, ensure_ascii=False).encode("utf-8")), LIMIT)

    def test_the_limit_counts_bytes_exactly_with_posts_multibyte_and_notice(self):
        for n in range(8):
            self.posts(1, body=("é日\U0001f600\"\\\n" * 60) + "x" * n)
        self.put(*[row(n, title="日" * 66, waiting="\U0001f600" * 50) for n in range(150)],
                 *[row(n, status="done", title="é" * 100) for n in range(900, 1100)])
        for notice in (None, "a meeting is starting 日: " + "n" * 300):
            for first in (True, False):
                with self.subTest(notice=bool(notice), first=first), \
                        mock.patch.object(Server, "_yield_notice", return_value=notice):
                    args = {} if first else {"backlog_cursor": self.read()["backlog_next"]}
                    full = self.text(**args)
                    size, page = len(full.encode("utf-8")), json.loads(full)
                    self.assertLessEqual(size, LIMIT)
                    self.assertGreater(size, LIMIT - 900)
                    self.assertEqual(page.get("yield"), notice)
                    with mock.patch.object(projecttools, "BOARD_RESULT_BYTES", size):
                        self.assertEqual(self.text(**args), full)
                    with mock.patch.object(projecttools, "BOARD_RESULT_BYTES", size - 1):
                        self.assertEqual(json.loads(self.text(**args))["backlog"], page["backlog"][:-1])

    def test_the_notice_counted_is_the_notice_sent(self):
        self.put(*[row(n, title="t" * 200) for n in range(300)])
        for first, later in ((None, "n" * 2000), ("n" * 2000, None)):
            with mock.patch.object(Server, "_yield_notice", side_effect=[first] + [later] * 5):
                page = self.read()
            self.assertEqual(page.get("yield"), first)

    def test_a_notice_still_reaches_thread_and_since_reads(self):
        post = self.posts(1)[0]
        for arguments in ({"thread": post}, {"since": 0}):
            with mock.patch.object(Server, "_yield_notice", return_value="stop now"):
                self.assertEqual(json.loads(self.text(**arguments))["yield"], "stop now")

    def test_the_largest_row_the_projection_writes_is_never_cut(self):
        biggest = row(1, title="\x01" * 200, waiting="\x02" * 200, priority=10 ** 18,
                      blocked_by=["question-%s %s" % ("a" * 32, "b" * 80)] * 8)
        biggest["work_id"] = "work-" + "a" * 32
        self.put(biggest)
        got = self.read()["backlog"][0]
        self.assertEqual(set(got), KEYS | {"blocked_by"})
        self.assertEqual({k: got[k] for k in KEYS - {"owner"}}, {k: biggest[k] for k in KEYS - {"owner"}})
        self.assertEqual(got["blocked_by"], biggest["blocked_by"])

    def test_an_oversized_row_is_cut_and_marked_and_the_walk_goes_on(self):
        self.posts(12)
        wide, odd = row(0, priority=9, title="日" * 66), row(5, status="done", priority=int("7" * 4000))
        wide["work_id"] = "work-" + "é" * 20000
        odd["work_id"] = "work-" + "\x01" * 20000
        self.put(wide, row(1), odd, row(6, status="cancelled"))
        pages = self.walk()
        first = pages[0]["backlog"]
        self.assertEqual(first[0]["cut"], ["work_id"])
        self.assertEqual(first[0]["title"], wide["title"])
        self.assertEqual(first[0]["work_id"], "work-" + "é" * 97)  # 199 bytes: no split character
        self.assertEqual(first[1]["work_id"], "work-00001")
        self.assertNotIn("cut", first[1])
        closed = [r for p in pages[1:] for r in p["backlog"]]
        self.assertEqual([r["status"] for r in closed], ["cancelled", "done"])
        self.assertEqual((closed[1]["cut"], closed[1]["priority"]), (["work_id", "priority"], None))

    def test_oversized_rows_still_make_progress_one_page_at_a_time(self):
        self.posts(12, body="p" * 1060)
        # Under and over the row bound. Uncut, a row of the longer length couldn't share a page with the posts; the
        # bound cuts it to 200 bytes first, so here it can (without the bound the plain page would be empty).
        for length in (16000, 23700):
            bodies = []
            for n in range(6):
                body = row(n)
                body["work_id"] = "work-%d" % n + "a" * length
                bodies.append(body)
            self.put(*bodies)
            with self.subTest(length=length), mock.patch.object(Server, "_yield_notice", return_value="n" * 400):
                pages = self.walk()
                got = [r["work_id"] for p in pages for r in p["backlog"]]
                self.assertEqual(got, [b["work_id"][:200 if length == 23700 else None] for b in bodies])
                self.assertTrue(all(p["backlog"] for p in pages))
            self.put(*[dict(b, status="unpublished") for b in bodies])

    def test_a_list_the_log_would_refuse_is_cut_and_marked_too(self):
        bad = dict(row(1), owner=OTHER, seq=1, blocked_by=["q" * 300] * 500)
        fake = mock.Mock(project="p")
        fake.state.return_value = {"posts": {}, "backlog": {"x": bad}}
        fake.entries.return_value, fake.head_seq.return_value = [], 1
        got = projecttools._board_read(fake)["backlog"][0]
        self.assertEqual((got["blocked_by"], got["cut"]), (["q" * 200] * 8, ["blocked_by"]))


class OpenThenClosed(Case):
    def fill(self, opened, closed):
        self.put(*[row(n, status=("ready", "waiting")[n % 2], title="o" * 150, waiting="w" * (n % 180), priority=n % 5)
                   for n in range(opened)],
                 *[row(5000 + n, status=CLOSED[n % 2], title="c" * 150, priority=n % 5) for n in range(closed)])

    def test_a_small_board_returns_open_rows_and_a_cursor_to_the_closed_ones(self):
        post = self.posts(1)[0]
        self.put(row(1), row(2, status="done"), row(3, status="waiting", priority=4), row(4, status="cancelled"))
        page = self.read()
        self.assertEqual([p["id"] for p in page["posts"]], [post])
        self.assertEqual([r["work_id"] for r in page["backlog"]], ["work-00003", "work-00001"])
        self.assertEqual(set(page["backlog"][0]), KEYS)
        self.assertEqual(page["backlog_total"], {"open": 2, "closed": 2})
        self.assertEqual(page["backlog_left_out"], {"open": 0, "closed": 2})
        following = self.read(backlog_cursor=page["backlog_next"])
        self.assertEqual(set(following), {"project", "as_of_seq", "backlog", "backlog_total", "backlog_left_out",
                                          "backlog_next"})
        self.assertEqual([r["work_id"] for r in following["backlog"]], ["work-00004", "work-00002"])
        self.assertEqual((following["backlog_left_out"], following["backlog_next"]), ({"open": 0, "closed": 0}, None))

    def test_the_walk_returns_every_row_once_in_order_and_the_counts_end_at_zero(self):
        self.fill(230, 170)
        pages = self.walk()
        self.assertGreater(len(pages), 4)
        seen = {"open": 0, "closed": 0}
        for page in pages:
            kinds = {"closed" if r["status"] in CLOSED else "open" for r in page["backlog"]}
            self.assertEqual(len(kinds), 1)  # progress, and never both
            seen[kinds.pop()] += len(page["backlog"])
            self.assertEqual(page["backlog_total"], {"open": 230, "closed": 170})
            self.assertEqual(page["backlog_left_out"], {"open": 230 - seen["open"], "closed": 170 - seen["closed"]})
            self.assertEqual(page["backlog_next"] is None, page["backlog_left_out"] == {"open": 0, "closed": 0})
        rows = [r for p in pages for r in p["backlog"]]
        with self.ps() as ps:
            stored = list(ps.state()["backlog"].values())
        opened = sorted((r for r in stored if r["status"] not in CLOSED),
                        key=lambda r: (projectview.priority_rank(r["priority"]), r["owner"], r["work_id"]))
        closed = sorted((r for r in stored if r["status"] in CLOSED), key=lambda r: -r["seq"])
        self.assertEqual([r["work_id"] for r in rows], [r["work_id"] for r in opened + closed])

    def test_closed_rows_come_newest_change_first_by_seq(self):
        self.put(row(1, status="done"), row(2, status="done"), row(3, status="cancelled", priority=9))
        self.put(row(1, status="done", title="changed"))
        page = self.read(backlog_cursor=self.read()["backlog_next"])
        self.assertEqual([r["work_id"] for r in page["backlog"]], ["work-00001", "work-00003", "work-00002"])

    def test_edges_no_rows_only_open_only_closed(self):
        zero = {"open": 0, "closed": 0}
        page = self.read()
        self.assertEqual((page["backlog"], page["backlog_total"], page["backlog_left_out"], page["backlog_next"]),
                         ([], zero, zero, None))
        self.put(row(1))
        page = self.read()
        self.assertEqual((len(page["backlog"]), page["backlog_left_out"], page["backlog_next"]), (1, zero, None))
        self.put(row(1, status="done"))
        page = self.read()
        self.assertEqual((page["backlog"], page["backlog_left_out"]), ([], {"open": 0, "closed": 1}))
        self.assertIsNotNone(page["backlog_next"])
        self.assertEqual(len(self.read(backlog_cursor=page["backlog_next"])["backlog"]), 1)

    def test_the_last_open_page_still_points_at_the_closed_rows(self):
        for opened in (230, 3):  # several open pages, and all open rows in the plain call
            with self.subTest(opened=opened):
                name = "p%d" % opened
                with self.ps(name) as ps:
                    ps.grant(self.me, "read", by="operator")
                    ps.grant(OTHER, "post", by="operator")
                self.put(*[row(n, title="o" * 190, waiting="w" * 190) for n in range(opened)],
                         *[row(900 + n, status=CLOSED[n % 2]) for n in range(4)], project=name)
                pages = [self.read(project=name)]
                while pages[-1]["backlog_left_out"]["open"]:
                    self.assertLess(len(pages), 20)
                    self.assertTrue(pages[-1]["backlog"])
                    pages.append(self.read(project=name, backlog_cursor=pages[-1]["backlog_next"]))
                self.assertEqual(len(pages) > 1, opened == 230)
                last = pages[-1]
                self.assertEqual({r["status"] for p in pages for r in p["backlog"]}, {"ready"})
                self.assertEqual(sum(len(p["backlog"]) for p in pages), opened)
                self.assertEqual(last["backlog_left_out"], {"open": 0, "closed": 4})
                self.assertIsNotNone(last["backlog_next"])
                after = self.read(project=name, backlog_cursor=last["backlog_next"])
                self.assertEqual([r["work_id"] for r in after["backlog"]],
                                 ["work-%05d" % n for n in (903, 902, 901, 900)])
                self.assertEqual((after["backlog_left_out"], after["backlog_next"]), ({"open": 0, "closed": 0}, None))

    def test_an_empty_backlog_with_a_backlog_next_means_closed_rows_only(self):
        self.put(*[row(n, status=CLOSED[n % 2]) for n in range(3)])
        page = self.read()
        self.assertEqual((page["backlog"], page["backlog_total"], page["backlog_left_out"]),
                         ([], {"open": 0, "closed": 3}, {"open": 0, "closed": 3}))
        self.assertIsNotNone(page["backlog_next"])
        after = self.read(backlog_cursor=page["backlog_next"])
        self.assertEqual(([r["work_id"] for r in after["backlog"]], after["backlog_next"]),
                         (["work-00002", "work-00001", "work-00000"], None))
        # With an open row, after a full share of posts, the plain page is not empty.
        self.posts(12)
        self.put(row(1000, title="日" * 66, waiting="w" * 200))
        self.assertEqual([r["work_id"] for r in self.read()["backlog"]], ["work-01000"])
        self.assertIn("An empty backlog with a backlog_next that is not null means closed rows only.",
                      METHODS["board_read"]["description"])

    def test_thread_and_since_reads_are_byte_identical_to_7a35b2c(self):
        self.fill(60, 40)
        first = self.posts(14, body="日 body " * 60)
        with self.ps() as ps:
            reply = ps.append([{"kind": "post", "body": {"topic": "re", "body": "r", "reply_to": first[3]}}],
                              author=self.me)["ids"][0]
            seq = ps.state()["posts"][first[9]]["seq"]
            cases = [{"thread": first[3]}, {"thread": reply}, {"thread": first[0]}, {"since": 0}, {"since": seq},
                     {"since": 10 ** 6}]
            expected = [json.dumps(read_7a35b2c(ps, **c), ensure_ascii=False) for c in cases]
        for case, want in zip(cases, expected):
            with self.subTest(**case):
                self.assertEqual(self.text(**case), want)

    def test_the_description_says_open_only_and_how_to_get_the_rest(self):
        text = METHODS["board_read"]["description"]
        for part in ("open rows (ready, waiting) only", "backlog_next", "backlog_cursor", "closed ones (done, cancelled)"):
            self.assertIn(part, text)


class Cursor(Case):
    def cursor(self):
        self.put(*[row(n, title="SECRETWORD" * 15) for n in range(200)], row(900, status="done"))
        return self.read()["backlog_next"]

    def test_it_is_a_position_and_a_hash_only(self):
        self.assertRegex(self.cursor(), r"\A[1-9][0-9]{0,6}-[0-9a-f]{64}\Z")

    def test_malformed_and_out_of_range_cursors_are_refused_without_an_echo(self):
        good = self.cursor()
        position, digest = good.split("-")
        bad = ["", " ", "x", good + "\n", " " + good, good[:-1], good + "0", good.upper(), "0" + good, "-1-" + digest,
               "+1-" + digest, "١-" + digest, "1" * 8 + "-" + digest, "1" * 5000 + "-" + digest, position + "_" + digest, digest, "1e3-" + digest,
               "201-" + digest, "202-" + digest, "9999999-" + digest, good + good]
        for cursor in bad:
            with self.subTest(cursor=cursor):
                message = self.refused("bad_cursor", backlog_cursor=cursor)
                self.assertNotIn(digest, message)
        self.assertEqual(self.read(backlog_cursor="200-" + digest)["backlog"][0]["work_id"], "work-00900")

    def test_any_row_change_refuses_it_and_a_post_does_not(self):
        good = self.cursor()
        self.posts(2)
        self.read(backlog_cursor=good)
        for change in (row(150, title="other"), row(150, status="waiting"), row(150, priority=3),
                       row(150, evidence=["message-1"]), row(150, status="done"), row(150, status="unpublished"),
                       row(901), row(900, status="done")):
            with self.subTest(change=change):
                self.put(change)
                message = self.refused("stale_cursor", backlog_cursor=good)
                self.assertIn("start again", message)
                fresh = self.read()["backlog_next"]
                self.assertNotEqual(fresh, good)
                self.read(backlog_cursor=fresh)
                good = fresh

    def test_a_cursor_of_another_project_is_refused_even_with_the_same_rows(self):
        bodies = [row(n, title="t" * 190) for n in range(200)]
        with self.ps("other") as ps:
            ps.grant(self.me, "read", by="operator")
            ps.grant(OTHER, "post", by="operator")
        self.put(*bodies, project="other")
        self.put(*bodies)
        states = []
        for project in ("other", "synthetics"):
            with self.ps(project) as ps:
                states.append([(k, r["seq"]) for k, r in ps.state()["backlog"].items()])
        self.assertEqual(*states)
        foreign = json.loads(self.text(project="other"))["backlog_next"]
        self.refused("stale_cursor", backlog_cursor=foreign)

    def test_the_project_is_in_the_hash_itself(self):
        rows = [dict(row(n), owner=OTHER, seq=n, entry="pm-x", at="t") for n in range(3)]
        pages = []
        for name in ("a", "b"):
            fake = mock.Mock(project=name)
            fake.state.return_value = {"posts": {}, "backlog": {str(r["seq"]): dict(r, status="done") for r in rows}}
            fake.entries.return_value, fake.head_seq.return_value = [], 3
            pages.append(projecttools._board_read(fake)["backlog_next"])
        self.assertNotEqual(*pages)

    def test_it_is_not_combined_with_thread_or_since(self):
        good, post = self.cursor(), self.posts(1)[0]
        for extra in ({"thread": post}, {"since": 0}, {"since": 5}, {"thread": ""}):
            with self.subTest(**extra):
                self.refused("bad_args", backlog_cursor=good, **extra)

    def test_the_read_grant_is_checked_before_the_cursor(self):
        good = self.cursor()
        with self.ps("closedoff") as ps:
            ps.grant(OTHER, "post", by="operator")
        for cursor in (good, "garbage", ""):
            with self.subTest(cursor=cursor):
                self.refused("no_grant", project="closedoff", backlog_cursor=cursor)


if __name__ == "__main__":
    unittest.main()

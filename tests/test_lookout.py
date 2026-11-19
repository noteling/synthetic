"""VIS P1 "Lookout": shared/status.json export, read-only boundary, liveness, threads, server (design reviews/VIS/P0)."""
import builtins
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from synthetics import lookout, sharedstatus
from synthetics.engine import Engine
from synthetics.mcp import Server
from synthetics.projects import ProjectStore
from synthetics.roster import CompanyStore
from synthetics.store import Store

SENTINELS = ("SENTINEL-DESC", "SENTINEL-NOTE", "SENTINEL-MEMORY", "SENTINEL-SELF", "SENTINEL-SUMMARY",
             "SENTINEL-ERROR", "SENTINEL-EVENT-BODY", "SENTINEL-QUESTION-TAIL", "SENTINEL-TRANSCRIPT")
VESPER = "employee-v"
CODEX = "employee-c"


def person(pid, name, kind="employee"):
    return {"kind": "person", "body": {"id": pid, "kind": kind, "name": name, "role": "role of " + name,
                                       "source": "operator statement", "certainty": "stated"}}


def fp_source(sender, to, mid, text, work=None, for_work=None, home=VESPER, direction="in"):
    if direction == "in":
        heads = ["from: %s" % sender, "message-id: %s" % mid, "to: %s" % to]
        heads += ["work-id: %s" % work] if work else []
        heads += ["for-work: %s" % for_work] if for_work else []
        data = "\n".join(heads + ["sent-at: 2026-11-18T10:00:00Z", "", "---", "", text])
        origin = {"role": "other", "direction": "in", "sender": sender, "source": "fieldpost", "thread": None,
                  "auth": {"channel": "fieldpost"}, "external_id": mid, "event_id": "event-x" + mid[-4:], "home": home,
                  "occurred_at": "2026-11-18T10:00:00Z"}
    else:
        data = text
        origin = {"role": "out", "direction": "out", "sender": sender, "source": "outbox", "thread": None,
                  "auth": None, "external_id": None, "event_id": mid, "home": home,
                  "occurred_at": "2026-11-18T09:00:00Z"}
    return {"kind": "source", "bytes": data,
            "body": {"origin": origin, "capture": {"mode": "tag"}, "quote_source": "event"}}


class _Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "vesper"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.ident = self.store.initialize("Vesper", "maintainer")
        with CompanyStore.at_root(self.root) as co:
            co.append([person(self.ident["id"], "Vesper"), person(CODEX, "Codex"),
                       person("david", "David", kind="human"),
                       {"kind": "alias", "body": {"alias": "codex-manager", "id": CODEX,
                                                  "source": "operator statement", "certainty": "stated"}}],
                      author="operator", operator=True)

    def private_fixture(self):
        """Sentinels in every private field of the home (design §7 test 1)."""
        s = self.store
        w = s.work_put("Build Lookout", description="SENTINEL-DESC")
        s.work_put(id=w["id"], note="SENTINEL-NOTE", status="ready")
        s.memory_put(self.ident["id"], "fact", "SENTINEL-MEMORY", source="test")
        s.self_note("how-i-work", "SENTINEL-SELF", source="test")
        s.feed("david", "SENTINEL-EVENT-BODY", external_id="e1")
        s.question_ask("david", "Should Lookout show summaries?\nSENTINEL-QUESTION-TAIL", work_id=w["id"], key="q1")
        run = s.run_begin()
        s.run_finish(run["id"], "SENTINEL-SUMMARY", cost=1.5)
        run2 = s.run_begin()
        s.run_fail(run2["id"], "SENTINEL-ERROR")
        (self.home / "runs").mkdir(exist_ok=True)
        (self.home / "runs" / (run["id"] + ".jsonl")).write_text("SENTINEL-TRANSCRIPT")
        return w


class StatusExportTests(_Root):
    def test_fields_mode_and_no_private_text(self):
        w = self.private_fixture()
        self.assertTrue(sharedstatus.write(self.store, paused=None))
        path = sharedstatus.path(self.home)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text()
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)
        value = json.loads(text)
        self.assertEqual(set(value), {"v", "employee", "name", "role", "written_at", "pid", "session", "works",
                                      "docket", "docket_error", "questions_open", "runs", "outbox_stuck", "glances"})
        self.assertEqual(value["session"], {"state": "idle", "run": None, "started_at": None, "paused": None})
        self.assertEqual([x["id"] for x in value["works"]], [w["id"]])
        self.assertEqual(set(value["works"][0]), {"id", "title", "status", "priority", "waiting_for", "wake_at",
                                                  "waiting_question_ids", "published"})
        self.assertEqual(value["questions_open"][0]["first_line"], "Should Lookout show summaries?")
        self.assertEqual([r["status"] for r in value["runs"]], ["failed", "finished"])
        self.assertEqual(set(value["runs"][0]), {"id", "status", "started_at", "finished_at", "cost", "events",
                                                 "failed_tool_count"})
        self.assertEqual(list(path.parent.iterdir()), [path])  # atomic: no temp file left

    def test_running_session_and_pause_kept_by_mcp_writes(self):
        run = self.store.run_begin()
        sharedstatus.write(self.store, paused={"reason": "daily_limit", "day": "2026-11-18", "limit_usd": 9.0,
                                               "cost_usd": 10.0, "unknown_cost_runs": 1})
        session = sharedstatus.read(self.home)["session"]
        self.assertEqual((session["state"], session["run"]), ("running", run["id"]))
        self.assertEqual(session["paused"]["reason"], "daily_limit")
        self.assertNotIn("unknown_cost_runs", session["paused"])
        sharedstatus.write(self.store)  # KEEP: the MCP server doesn't know the pause
        self.assertEqual(sharedstatus.read(self.home)["session"]["paused"]["reason"], "daily_limit")

    def test_write_failure_never_raises(self):
        with mock.patch.object(sharedstatus, "build", side_effect=RuntimeError("boom")):
            self.assertFalse(sharedstatus.write(self.store))

    def test_mcp_work_and_question_changes_rewrite_status(self):
        """Codex C1: work_put, question_ask/close/resolve/relays rewrite status.json; other tools don't."""
        server = Server(self.store)
        server.call("memory_put", {"subject": self.ident["id"], "predicate": "p", "value": "v", "source": "s"})
        self.assertIsNone(sharedstatus.read(self.home))
        server.call("work_put", {"title": "Visible work"})
        self.assertEqual([w["title"] for w in sharedstatus.read(self.home)["works"]], ["Visible work"])
        server.call("question_ask", {"to": "david", "body": "One?", "key": "k1"})
        self.assertEqual(len(sharedstatus.read(self.home)["questions_open"]), 1)
        qid = sharedstatus.read(self.home)["questions_open"][0]["id"]
        server.call("question_close", {"question_id": qid, "reason": "obsolete"})
        self.assertEqual(sharedstatus.read(self.home)["questions_open"], [])

    def test_engine_writes_at_start_session_and_end(self):
        seen = {}

        class Brain:
            def run(inner, context):
                seen["during"] = sharedstatus.read(self.home)["session"]
                return SimpleNamespace(summary="SENTINEL-SUMMARY", cost=0.5)
        self.store.feed("david", "hello", external_id="e1")
        Engine(self.home, Brain()).run(max_turns=1)
        self.assertEqual(seen["during"]["state"], "running")
        after = sharedstatus.read(self.home)
        self.assertEqual(after["session"]["state"], "idle")
        self.assertEqual(after["runs"][0]["status"], "finished")
        self.assertNotIn("SENTINEL-SUMMARY", sharedstatus.path(self.home).read_text())

    def test_engine_writes_after_failed_session(self):
        class Brain:
            def run(inner, context):
                raise RuntimeError("SENTINEL-ERROR")
        self.store.feed("david", "hello", external_id="e1")
        with self.assertRaises(RuntimeError):
            Engine(self.home, Brain()).run(max_turns=1)
        value = sharedstatus.read(self.home)
        self.assertEqual((value["session"]["state"], value["runs"][0]["status"]), ("idle", "failed"))
        self.assertNotIn("SENTINEL-ERROR", sharedstatus.path(self.home).read_text())


class BoundaryTests(_Root):
    def setUp(self):
        super().setUp()
        self.work = self.private_fixture()
        sharedstatus.write(self.store, paused=None)
        (self.home / "runtime.state.json").write_text(json.dumps(
            {"label": "com.synthetics.worker.x", "pid": os.getpid(), "started_at": "2026-01-01T00:00:00Z",
             "effective": {"timeout": 600}}))
        (self.home / "worker.lock").write_text(str(os.getpid()))
        self.project = ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")
        self.addCleanup(self.project.close)
        self.project.append([fp_source(CODEX, self.ident["id"], "message-aaaa", "Please build <script>x()</script>",
                                       work="work-c1", for_work="work-v1")], author=VESPER, run="run-x")
        self.project.append([fp_source(self.ident["id"], "codex-manager", "message-bbbb", "Built it.",
                                       work="work-v1", for_work="work-c1", home=CODEX)], author=CODEX, run="run-y")
        (self.root / "projects" / "noteling").mkdir()
        ProjectStore(self.root / "projects" / "noteling" / "memory.sqlite3", "noteling").close()  # C3: empty store
        self.paths = ["/", "/p/synthetics/threads", "/p/synthetics/board", "/p/synthetics/memory",
                      "/p/noteling/threads", "/p/noteling/board", "/p/noteling/memory",
                      "/p/synthetics/work", "/p/noteling/work"]  # VIS G1

    def all_pages(self):
        out = {}
        for path in self.paths:
            code, body = lookout.route(str(self.root), path, agents={"com.synthetics.worker.x": os.getpid()})
            self.assertEqual(code, 200, path)
            out[path] = body
        key = next(iter(lookout.threads(lookout.messages(*lookout.project_entries(self.root, "synthetics")))))
        code, out["thread"] = lookout.route(str(self.root), "/p/synthetics/thread/" + urllib.request.quote(key, safe=""),
                                            agents={})
        self.assertEqual(code, 200)
        return out

    def test_no_database_or_private_path_opened_and_no_sentinel_served(self):
        opened = []
        real_open, real_io_open = builtins.open, io.open

        def spy(opener):
            def wrapped(file, *a, **k):
                opened.append(str(file))
                return opener(file, *a, **k)
            return wrapped

        def no_db(*a, **k):
            raise AssertionError("Lookout opened a database: %r" % (a,))
        with mock.patch("builtins.open", spy(real_open)), mock.patch("io.open", spy(real_io_open)), \
                mock.patch("sqlite3.connect", no_db):
            pages = self.all_pages()
        allowed = ("runtime.state.json", "worker.lock", str(Path("shared") / "status.json"))
        self.assertTrue(any(p.endswith("status.json") for p in opened))  # the spy saw the reads
        self.assertTrue(any(p.endswith("log.jsonl") for p in opened))
        for path in opened:
            p = Path(path).resolve()
            if self.home.resolve() in p.parents:
                self.assertTrue(str(p).endswith(allowed), path)
            self.assertNotIn("sqlite3", p.name)
        for path, body in pages.items():
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, body, path)
        self.assertIn("Build Lookout", pages["/"])  # the work title is shared
        self.assertIn("Should Lookout show summaries?", pages["/"])

    def test_pages_change_nothing_on_disk(self):
        def snapshot():
            out = {}
            for p in sorted(self.root.rglob("*")):
                if p.is_file():
                    out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            return out
        self.store.close()
        self.project.close()
        before = snapshot()
        self.all_pages()
        self.assertEqual(snapshot(), before)

    def test_threads_group_both_directions_resolve_alias_and_escape(self):
        msgs = lookout.messages(*lookout.project_entries(self.root, "synthetics"),
                                canon=lookout.canonical(lookout.company(self.root)))
        groups = lookout.threads(msgs)
        self.assertEqual(len(groups), 1)
        (key, convo), = groups.items()
        self.assertEqual([m["id"] for m in convo], ["message-aaaa", "message-bbbb"])
        self.assertEqual(convo[1]["to"], CODEX)  # codex-manager resolved to the canonical ID
        page = self.all_pages()["thread"]
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_dedup_by_message_id_keeps_headers(self):
        self.project.append([fp_source(self.ident["id"], None, "message-bbbb", "Built it.", direction="out")],
                            author=VESPER, run="run-z")
        msgs = lookout.messages(*lookout.project_entries(self.root, "synthetics"))
        self.assertEqual([m["id"] for m in msgs], ["message-aaaa", "message-bbbb"])
        self.assertEqual(msgs[1]["to"], "codex-manager")
        self.assertIn(VESPER, msgs[1]["captured_by"])

    def test_redacted_source_text_not_served(self):
        sid = self.project.entries()[0]["id"]
        self.project.redact(sid, reason="private", by="op")
        pages = self.all_pages()
        self.assertFalse(any("Please build" in body for body in pages.values()))


class LivenessTests(_Root):
    def write_runtime(self, pid, started="2026-11-18T10:00:00Z", timeout=600):
        (self.home / "runtime.state.json").write_text(json.dumps(
            {"label": "L", "pid": pid, "started_at": started, "effective": {"timeout": timeout}}))
        (self.home / "worker.lock").write_text(str(pid))

    def status(self, state, started_at=None, written="2026-11-18T11:00:00Z"):
        sharedstatus.path(self.home).parent.mkdir(exist_ok=True)
        sharedstatus.path(self.home).write_text(json.dumps(
            {"employee": "x", "written_at": written,
             "session": {"state": state, "run": "run-1" if state == "running" else None, "started_at": started_at,
                         "paused": None}}))

    NOW = datetime(2026, 11, 18, 11, 5, tzinfo=timezone.utc)

    def live(self, agents=None):
        return lookout.liveness(self.home, {"L": os.getpid()} if agents is None else agents, now=self.NOW)

    def test_running_idle_suspect_stale(self):
        self.write_runtime(os.getpid())
        self.status("running", started_at="2026-11-18T11:00:00Z")
        self.assertEqual(self.live()["session"]["state"], "running")
        self.status("running", started_at="2026-11-18T10:40:00Z")
        self.assertEqual(self.live()["session"]["state"], "suspect")  # C2: 1500 s > 600 + 300
        self.status("idle")
        self.assertEqual(self.live()["session"]["state"], "idle")
        self.status("running", started_at="2026-11-18T11:00:00Z", written="2026-11-18T09:00:00Z")
        self.assertEqual(self.live()["session"]["state"], "stale (worker not running)")  # written before start

    def test_same_second_write_after_start_is_not_stale(self):
        # F1: worker started at .911455 s; its first export in that second must not read as stale.
        self.write_runtime(os.getpid(), started="2026-11-18T10:59:44.911455Z")
        self.status("idle", written="2026-11-18T10:59:44Z")  # pre-r2 whole-second written_at
        self.assertEqual(self.live()["session"]["state"], "idle")
        self.status("idle", written="2026-11-18T10:59:44.950000Z")  # r2 microsecond written_at
        self.assertEqual(self.live()["session"]["state"], "idle")
        self.status("idle", written="2026-11-18T10:59:44.900000Z")  # written just before the start
        self.assertEqual(self.live()["session"]["state"], "stale (worker not running)")
        self.status("idle", written="2026-11-18T10:59:43Z")
        self.assertEqual(self.live()["session"]["state"], "stale (worker not running)")

    def test_written_at_has_microseconds_and_follows_worker_start(self):
        sharedstatus.path(self.home).parent.mkdir(exist_ok=True)
        before = datetime.now(timezone.utc)
        self.assertTrue(sharedstatus.write(self.store, paused=None))
        text = json.loads(sharedstatus.path(self.home).read_text())["written_at"]
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
        self.write_runtime(os.getpid(), started=before.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        self.assertFalse(lookout.liveness(self.home, {"L": os.getpid()})["session"]["state"].startswith("stale"))

    def test_dead_worker_is_stale_and_mismatch_shown(self):
        dead = 2 ** 22 + 12345
        self.write_runtime(dead)
        self.status("running", started_at="2026-11-18T11:00:00Z")
        live = self.live({"L": 4242})
        self.assertEqual(live["session"]["state"], "stale (worker not running)")
        self.assertFalse(live["service"]["worker_alive"])
        self.assertIn("mismatch", live["service"])
        self.assertIs(self.live({})["service"]["loaded"], False)

    def test_no_status_file(self):
        self.write_runtime(os.getpid())
        self.assertEqual(self.live()["session"]["state"], "no status.json")


class ServerTests(_Root):
    def test_bind_refused_post_405_get_200(self):
        with self.assertRaises(ValueError):
            lookout.serve(self.root, port=0, bind="0.0.0.0")
        holder = {}
        ready = threading.Event()

        def on_ready(server):
            holder["server"] = server
            ready.set()
        thread = threading.Thread(target=lookout.serve, args=(self.root,), kwargs={"port": 0, "ready": on_ready},
                                  daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        server = holder["server"]
        try:
            host, port = server.server_address[:2]
            self.assertEqual(host, "127.0.0.1")
            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("no-store", resp.headers["Cache-Control"])
            request = urllib.request.Request("http://127.0.0.1:%d/" % port, data=b"x", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 405)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen("http://127.0.0.1:%d/p/../etc" % port, timeout=5)
            self.assertEqual(caught.exception.code, 404)
            head = urllib.request.Request("http://127.0.0.1:%d/" % port, method="HEAD")
            with urllib.request.urlopen(head, timeout=5) as resp:
                self.assertEqual((resp.status, resp.read()), (200, b""))
            with mock.patch.object(lookout, "team_page", side_effect=RuntimeError("SENTINEL-ERROR")):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5)
                self.assertEqual(caught.exception.code, 500)
                self.assertNotIn(b"SENTINEL-ERROR", caught.exception.read())
        finally:
            server.shutdown()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()

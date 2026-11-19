"""#25 P1: the answer desk (docs/answer-desk.md). End to end on a temporary home: pairing, refusals, an answer
that wakes the linked work through `channel inbound`, record-derived status (C2) and a leak grep (C3)."""
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from synthetics import answer_desk, lookout, sharedstatus
from synthetics.capture import write_humans
from synthetics.store import Store

ROOT = Path(__file__).resolve().parents[1]
SECOND_LINE = "SENTINEL-FULL-BODY only the full question has this line."
BODY = "Which element did you put the sticker on?\n" + SECOND_LINE
ANSWER = "SENTINEL-ANSWER the product title."


class DeskBase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SYNTHETICS_SESSION_RUN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "homes"
        self.home = self.root / "rowan"
        self.key = Path(self.tmp.name) / "lookout-desk.key"
        self.key.write_text("d" * 64 + "\n")
        self.key.chmod(0o600)
        with Store(self.home) as store:
            store.initialize("Rowan", "maintainer")
        write_humans(self.home, {"default_human": "david", "senders": ["david"], "channel_prefixes": ["lookout:"]})
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "channel", "register",
                              "lookout-desk", "--source-prefix", "lookout:", "--key-file", str(self.key)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        with Store(self.home) as store:
            self.work = store.work_put(title="needs David", key="w1")
            self.question = store.question_ask("david", BODY, work_id=self.work["id"], key="q1")
            sharedstatus.write(store)
        self.server = ThreadingHTTPServer((answer_desk.BIND, 0), answer_desk.make_handler(self.root, self.key))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port
        self.pages = []

    def request(self, method, path, fields=None, cookie=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        head = {"Host": "127.0.0.1:%d" % self.port}
        body = None
        if fields is not None or raw is not None:
            body = raw if raw is not None else urlencode(fields).encode()
            head.update({"Origin": self.origin, "Content-Type": "application/x-www-form-urlencoded",
                         "Sec-Fetch-Site": "same-origin"})
        if cookie:
            head["Cookie"] = "desk=%s" % cookie
        head.update(headers or {})
        head = {k: v for k, v in head.items() if v is not None}
        conn.request(method, path, body=body, headers=head)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        conn.close()
        self.pages.append(text)
        return resp.status, dict(resp.getheaders()), text

    def paired(self):
        code = answer_desk.pair(self.root)
        status, headers, _ = self.request("POST", "/pair", {"code": code})
        self.assertEqual(status, 303)
        cookie = re.match(r"desk=([0-9a-f]{64}); HttpOnly; SameSite=Strict; Path=/; Max-Age=43200",
                          headers["Set-Cookie"]).group(1)
        return code, cookie

    def form(self, cookie):
        status, _, text = self.request("GET", "/q/rowan/%s" % self.question["id"], cookie=cookie)
        self.assertEqual(status, 200, text)
        nonce = re.search(r"name='nonce' value='([0-9a-f]{32})'", text).group(1)
        token = re.search(r"name='csrf' value='([0-9a-f]{64})'", text).group(1)
        return text, nonce, token

    def stored(self):
        with Store(self.home) as store:
            return (store._get("questions", self.question["id"])["status"],
                    store._get("works", self.work["id"])["status"],
                    store.db.execute("SELECT COUNT(*) FROM events WHERE data LIKE '%lookout:desk%'").fetchone()[0])


class PairingTests(DeskBase):
    def test_pair_sets_hash_only_session(self):
        code, cookie = self.paired()
        state = self.root / "answer-desk"
        self.assertEqual(oct(state.stat().st_mode & 0o777), "0o700")
        for path in state.iterdir():
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600", path)
            text = path.read_text()
            self.assertNotIn(cookie, text)
            self.assertNotIn(code, text)
        self.assertFalse((state / "pairing.json").exists())  # used up
        self.assertTrue(answer_desk.valid_session(self.root, cookie))
        status, _, _ = self.request("POST", "/pair", {"code": code})
        self.assertEqual(status, 401)  # one-time

    def test_five_wrong_codes_kill_the_code(self):
        code = answer_desk.pair(self.root)
        for _ in range(answer_desk.PAIR_TRIES):
            status, _, _ = self.request("POST", "/pair", {"code": "wrong-wrong-wrong-wrong"})
            self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/pair", {"code": code})
        self.assertEqual(status, 401)
        self.assertFalse((self.root / "answer-desk" / "pairing.json").exists())

    def test_four_wrong_then_right_pairs(self):
        code = answer_desk.pair(self.root)
        for _ in range(answer_desk.PAIR_TRIES - 1):
            self.assertIsNone(answer_desk.redeem(self.root, "nope"))
        self.assertIsNotNone(answer_desk.redeem(self.root, code.upper().replace("-", " ")))

    def test_expired_code(self):
        code = answer_desk.pair(self.root, now=1000.0)
        self.assertIsNone(answer_desk.redeem(self.root, code, now=1000.0 + answer_desk.PAIR_SECONDS + 1))
        self.assertIsNone(answer_desk.redeem(self.root, code, now=1001.0))

    def test_index_lists_only_asks_of_the_human(self):
        _, cookie = self.paired()
        with Store(self.home) as store:
            store.question_ask("employee-vesper", "PEER-ONLY question", key="q9")
            sharedstatus.write(store)
        status, _, text = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("/q/rowan/%s" % self.question["id"], text)
        self.assertIn("Which element did you put the sticker on?", text)
        self.assertNotIn("PEER-ONLY", text)

    def test_logout_all(self):
        _, cookie = self.paired()
        answer_desk.logout_all(self.root)
        status, headers, _ = self.request("GET", "/", cookie=cookie)
        self.assertEqual((status, headers.get("Location")), (303, "/pair"))

    def test_cli_refused_in_session(self):
        for command in ("pair", "logout-all", "serve"):
            extra = ["--key-file", str(self.key)] if command == "serve" else []
            out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "answer-desk",
                                  command] + extra, capture_output=True, text=True, timeout=60,
                                 env=dict(os.environ, SYNTHETICS_SESSION_RUN="run-x"))
            self.assertNotEqual(out.returncode, 0, command)
            self.assertIn("refused inside an employee session", out.stderr + out.stdout)
        self.assertFalse((self.root / "answer-desk").exists())

    def test_cli_pair_prints_a_code_that_redeems(self):
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "answer-desk", "pair"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        code = out.stdout.strip().rsplit(" ", 1)[-1]
        self.assertIsNotNone(answer_desk.redeem(self.root, code))



class PairingConcurrencyTests(DeskBase):
    """PR #67 review F1: the 5-try cap holds under concurrent redeems (threaded server, other processes)."""

    def count_live_reads(self):
        seen = []
        original = answer_desk._read_private

        def counting(path):
            value = original(path)
            if path.name == "pairing.json" and value:
                seen.append(1)
            return value
        patch = mock.patch.object(answer_desk, "_read_private", counting)
        patch.start()
        self.addCleanup(patch.stop)
        return seen

    def test_concurrent_wrong_codes_respect_the_cap(self):
        answer_desk.pair(self.root)
        seen = self.count_live_reads()
        n = 40
        barrier = threading.Barrier(n)
        errors, results = [], []

        def go():
            barrier.wait()
            try:
                results.append(answer_desk.redeem(self.root, "wrong-wrong-wrong-wrong"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=go) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(results, [None] * n)
        self.assertLessEqual(len(seen), answer_desk.PAIR_TRIES)
        state = self.root / "answer-desk"
        self.assertFalse((state / "pairing.json").exists())
        self.assertEqual([p.name for p in state.iterdir() if p.name.endswith(".tmp")], [])

    def test_concurrent_wrong_posts_are_all_401(self):
        answer_desk.pair(self.root)
        n = 20
        barrier = threading.Barrier(n)
        statuses = []

        def go():
            barrier.wait()
            statuses.append(self.request("POST", "/pair", {"code": "wrong-wrong-wrong-wrong"})[0])
        threads = [threading.Thread(target=go) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(statuses, [401] * n)
        self.assertFalse((self.root / "answer-desk" / "pairing.json").exists())

    def test_redeem_waits_for_the_file_lock_of_another_process(self):
        import fcntl
        code = answer_desk.pair(self.root)
        fd = os.open(self.root / "answer-desk" / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)  # a separate open file description, as `answer-desk pair` would hold
        results = []
        worker = threading.Thread(target=lambda: results.append(answer_desk.redeem(self.root, code)))
        worker.start()
        worker.join(0.5)
        self.assertTrue(worker.is_alive())
        self.assertEqual(results, [])
        fcntl.flock(fd, fcntl.LOCK_UN)
        worker.join(60)
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0])

    def test_unreadable_status_is_not_an_outcome(self):
        """Review F2: no status.json is no evidence that the question left the open list."""
        sharedstatus.path(self.home).unlink()
        self.assertEqual(answer_desk.outcome(self.root, "rowan", self.question["id"], {"state": "processed"}),
                         (True, False))
        sharedstatus.path(self.home).write_text("{not json")
        self.assertEqual(answer_desk.outcome(self.root, "rowan", self.question["id"], {"state": "processed"}),
                         (True, False))

class AnswerTests(DeskBase):
    def test_answer_wakes_work_through_inbound(self):
        _, cookie = self.paired()
        text, nonce, token = self.form(cookie)
        self.assertIn(SECOND_LINE, text)  # D1: the full question
        status, headers, _ = self.request("POST", "/a/rowan/%s" % self.question["id"],
                                          {"nonce": nonce, "csrf": token, "answer": ANSWER, "sender": "mallory"},
                                          cookie=cookie)
        self.assertEqual(status, 303)
        location = headers["Location"]
        event_id = re.match(r"^/s/rowan/(event-[0-9a-f]{32})$", location).group(1)
        self.assertEqual(self.stored(), ("answered", "ready", 1))
        with Store(self.home) as store:
            event = store._get("events", event_id)
        self.assertEqual((event["sender"], event["source"], event["auth"]["channel"]),
                         ("david", "lookout:desk", "lookout-desk"))  # form sender ignored (C1)
        with Store(self.home) as store:
            question = store._get("questions", self.question["id"])
        self.assertIsNone(question.get("routed_by"))
        self.assertIsNone(question.get("to_id"))
        # Resubmitting the same form is idempotent: same event, no second answer.
        status, headers2, _ = self.request("POST", "/a/rowan/%s" % self.question["id"],
                                           {"nonce": nonce, "csrf": token, "answer": ANSWER}, cookie=cookie)
        # P2: the same form again reaches the idempotent inbound (it was 409 in P1, before inbound).
        self.assertEqual((status, headers2["Location"]), (303, location))
        self.assertEqual(self.stored()[2], 1)

    def test_status_is_from_records_not_http(self):
        """C2: a 303/200 alone never reads as read or answered."""
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        status, headers, _ = self.request("POST", "/a/rowan/%s" % self.question["id"],
                                          {"nonce": nonce, "csrf": token, "answer": ANSWER}, cookie=cookie)
        location = headers["Location"]
        event_id = location.split("/")[3]
        status, _, text = self.request("GET", location, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("✓ Submitted", text)
        self.assertIn("… Employee read it", text)
        self.assertIn("… Outcome", text)
        self.assertNotIn(ANSWER, text)
        with Store(self.home) as store:
            run = store.run_begin([event_id])
        status, _, text = self.request("GET", location, cookie=cookie)
        self.assertIn("… Employee read it", text)  # captured but unfinished is not "read"
        with Store(self.home) as store:
            store.run_finish(run["id"], "done")
        status, _, text = self.request("GET", location, cookie=cookie)
        self.assertIn("✓ Employee read it", text)
        self.assertIn("… Outcome", text)  # status.json still lists the question
        with Store(self.home) as store:
            sharedstatus.write(store)
        status, _, text = self.request("GET", location, cookie=cookie)
        self.assertIn("✓ Outcome", text)
        # Another event ID (not delivered by the desk) never shows as submitted.
        with Store(self.home) as store:
            other = store.feed("david", "plain", source="manual", external_id="p1")
        status, _, text = self.request("GET", "/s/rowan/%s?q=%s" % (other["id"], self.question["id"]),
                                       cookie=cookie)
        self.assertIn("… Submitted", text)
        self.assertNotIn("✓", text)


class RefusalTests(DeskBase):
    def post(self, cookie, form_nonce, form_token, **overrides):
        fields = {"nonce": form_nonce, "csrf": form_token, "answer": ANSWER}
        headers = overrides.pop("headers", None)
        fields.update(overrides)
        return self.request("POST", "/a/rowan/%s" % self.question["id"], fields, cookie=cookie, headers=headers)[0]

    def test_unpaired(self):
        status, headers, _ = self.request("GET", "/q/rowan/%s" % self.question["id"])
        self.assertEqual((status, headers.get("Location")), (303, "/pair"))
        self.assertEqual(self.post(None, "0" * 32, "0" * 64), 401)
        self.assertEqual(self.post("f" * 64, "0" * 32, "0" * 64), 401)
        self.assertEqual(self.stored(), ("open", "waiting", 0))

    def test_post_header_and_token_checks(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        _, nonce2, token2 = self.form(cookie)
        cases = [
            dict(headers={"Host": "evil.example:%d" % self.port}),
            dict(headers={"Origin": None}),
            dict(headers={"Origin": "http://evil.example"}),
            dict(headers={"Origin": "http://localhost:%d" % self.port}),
            dict(headers={"Sec-Fetch-Site": "cross-site"}),
            dict(headers={"Content-Type": "text/plain"}),
            dict(csrf="0" * 64),
            dict(csrf=token2),  # a token for another nonce
            dict(nonce="zz"),
        ]
        for case in cases:
            self.assertIn(self.post(cookie, nonce, token, **case), (400, 403), case)
        self.assertEqual(self.post(cookie, nonce, token, answer="   "), 400)
        self.assertEqual(self.post(cookie, nonce, token, answer="x" * (answer_desk.MAX_BODY + 1)), 400)
        self.assertEqual(self.stored(), ("open", "waiting", 0))
        self.assertEqual(self.post(cookie, nonce2, token2), 303)

    def test_get_host_checked_and_methods(self):
        _, cookie = self.paired()
        status, _, text = self.request("GET", "/q/rowan/%s" % self.question["id"], cookie=cookie,
                                       headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertNotIn(SECOND_LINE, text)
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request(method, "/", cookie=cookie)[0], 405)

    def test_not_answerable_questions(self):
        _, cookie = self.paired()
        with Store(self.home) as store:
            peer = store.question_ask("employee-vesper", "peer only", key="q2")
        for path in ("/q/rowan/%s" % peer["id"], "/q/rowan/question-%s" % ("0" * 32),
                     "/q/nobody/%s" % self.question["id"], "/q/..%%2Frowan/%s" % self.question["id"]):
            status, _, text = self.request("GET", path, cookie=cookie)
            self.assertEqual(status, 404, path)
            self.assertNotIn("peer only", text)
        _, nonce, token = self.form(cookie)
        status, _, _ = self.request("POST", "/a/rowan/%s" % peer["id"],
                                    {"nonce": nonce, "csrf": answer_desk.csrf(cookie, peer["id"], nonce),
                                     "answer": ANSWER}, cookie=cookie)
        self.assertEqual(status, 409)
        with Store(self.home) as store:
            self.assertEqual(store._get("questions", peer["id"])["status"], "open")


class LeakTests(DeskBase):
    def test_no_secret_or_body_outside_the_store(self):
        code, cookie = self.paired()
        text, nonce, token = self.form(cookie)
        _, headers, _ = self.request("POST", "/a/rowan/%s" % self.question["id"],
                                     {"nonce": nonce, "csrf": token, "answer": ANSWER}, cookie=cookie)
        self.request("GET", headers["Location"], cookie=cookie)
        self.request("GET", "/", cookie=cookie)
        with Store(self.home) as store:
            sharedstatus.write(store)
        key = self.key.read_text().strip()
        question_page = self.pages.index(text)
        for i, page in enumerate(self.pages):
            for secret in (key, cookie, code, ANSWER):
                self.assertNotIn(secret, page)
            if i != question_page:
                self.assertNotIn(SECOND_LINE, page)
        files = [p for p in self.root.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".log", ".txt")]
        self.assertTrue(any(p.name == "status.json" for p in files))
        for path in files:
            data = path.read_text(errors="replace")
            for secret in (key, cookie, code, ANSWER, SECOND_LINE):
                self.assertNotIn(secret, data, path)
        lookout_page = lookout.route(str(self.root), "/", agents={})[1]
        for secret in (key, cookie, code, ANSWER, SECOND_LINE):
            self.assertNotIn(secret, lookout_page)


class LookoutLinkTests(DeskBase):
    """D3: Lookout renders the link only when a desk is configured; it stays GET/HEAD-only and keyless."""

    def test_link_only_when_configured(self):
        plain = lookout.route(str(self.root), "/", agents={})[1]
        self.assertIn("Needs you (1)", plain)
        self.assertNotIn("Answer here", plain)
        linked = lookout.route(str(self.root), "/", agents={}, desk=8766)[1]
        self.assertIn("<a href='http://127.0.0.1:8766/q/rowan/%s'>Answer here</a>" % self.question["id"], linked)
        handler = lookout.make_handler(str(self.root), desk=8766)
        self.assertIs(handler.do_POST, handler.do_PUT)  # still refused, unchanged


if __name__ == "__main__":
    unittest.main()

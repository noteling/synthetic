"""#25 P2 (i25/p2/P2.md sha 114e1ac1, approved by Codex with D1-D3): the answer desk's context button, the desk
journal and safe retries, the status page reading its question from the event record, and the additive
`channel event-status` fields and own-events lookup. T1-T12 as numbered in P2.md §4."""
import json
import re
import subprocess
import sys
import threading
import unittest
from unittest import mock

from synthetics import answer_desk, sharedstatus
from synthetics.store import Store

from tests.test_i25_answer_desk import ANSWER, ROOT, SECOND_LINE, DeskBase

CONTEXT = "SENTINEL-CONTEXT it was on the title, more later."


class P2Base(DeskBase):
    def post(self, cookie, nonce, token, answer=ANSWER, kind=None, qid=None):
        fields = {"nonce": nonce, "csrf": token, "answer": answer}
        if kind is not None:
            fields["kind"] = kind
        return self.request("POST", "/a/rowan/%s" % (qid or self.question["id"]), fields, cookie=cookie)

    def desk_events(self):
        with Store(self.home) as store:
            return [json.loads(r["data"]) for r in
                    store.db.execute("SELECT data FROM events WHERE data LIKE '%lookout:desk%' ORDER BY seq")]

    def contract_with(self, before_inbound=None, after_inbound=None):
        """Patch the desk's contract calls: run hooks around the real `inbound`."""
        real = answer_desk.contract

        def wrapper(home, key_file, command, *args, lines=None):
            if command == "inbound" and before_inbound:
                before_inbound()
            row = real(home, key_file, command, *args, lines=lines)
            if command == "inbound" and after_inbound:
                return after_inbound(row)
            return row
        patch = mock.patch.object(answer_desk, "contract", wrapper)
        patch.start()
        self.addCleanup(patch.stop)
        return patch


class RetryTests(P2Base):
    def test_t1_lost_response_then_same_form_is_the_same_event(self):
        """T1: the first send is accepted but its response is lost; sending the same form again must reach the
        idempotent inbound (P1 refused it 409 at the open check, since the question had already closed)."""
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        dropped = []

        def lose(row):
            if not dropped:
                dropped.append(row)
                return dict(result="rejected", error=dict(code="no_response", message="lost"))
            return row
        patch = self.contract_with(after_inbound=lose)
        status, _, text = self.post(cookie, nonce, token)
        self.assertEqual(status, 503)
        self.assertIn("Send again", text)
        self.assertIn("value='%s'" % nonce, text)  # same nonce: the retry can't count twice
        self.assertIn(answer_desk.E(ANSWER), text)  # David's own text is kept
        self.assertEqual(self.stored()[:2], ("answered", "ready"))  # it did go through
        status, headers, _ = self.post(cookie, nonce, token)
        patch.stop()
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/s/rowan/%s" % dropped[0]["event_id"])
        self.assertEqual(len(self.desk_events()), 1)

    def test_t1b_no_response_on_the_open_check_keeps_the_form(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        real = answer_desk.contract

        def silent(home, key_file, command, *args, lines=None):
            if command == "question":
                return dict(result="rejected", error=dict(code="no_response", message="timeout"))
            return real(home, key_file, command, *args, lines=lines)
        with mock.patch.object(answer_desk, "contract", silent):
            status, _, text = self.post(cookie, nonce, token)
        self.assertEqual(status, 503)
        self.assertIn("Nothing was sent", text)
        self.assertIn(answer_desk.E(ANSWER), text)
        self.assertIsNone(answer_desk.journal_find(self.root, "rowan", self.question["id"], nonce))
        self.assertEqual(self.desk_events(), [])
        self.assertEqual(self.post(cookie, nonce, token)[0], 303)

    def test_t2_same_form_with_edited_text_is_refused(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        self.assertEqual(self.post(cookie, nonce, token)[0], 303)
        status, _, text = self.post(cookie, nonce, token, answer=ANSWER + " edited")
        self.assertEqual(status, 409)
        self.assertIn("already sent with different text", text)
        self.assertEqual(len(self.desk_events()), 1)

    def test_t3_concurrent_double_click_is_one_event(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        n = 4
        barrier = threading.Barrier(n)
        statuses = []

        def go():
            barrier.wait()
            statuses.append(self.post(cookie, nonce, token)[0])
        threads = [threading.Thread(target=go) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(statuses, [303] * n)
        self.assertEqual(len(self.desk_events()), 1)

    def test_t4_fresh_form_after_close_is_refused(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        _, nonce2, token2 = self.form(cookie)
        self.assertEqual(self.post(cookie, nonce, token)[0], 303)
        status, _, _ = self.post(cookie, nonce2, token2, answer="second answer")
        self.assertEqual(status, 409)
        self.assertEqual(len(self.desk_events()), 1)

    def test_t5_answered_elsewhere_between_check_and_send(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)

        def elsewhere():
            with Store(self.home) as store:
                store.feed("david", "answered on telegram", source="manual", external_id="tg1",
                           reply_to=self.question["id"])
        self.contract_with(before_inbound=elsewhere)
        status, _, text = self.post(cookie, nonce, token)
        self.assertEqual(status, 409)
        self.assertIn("answered elsewhere first", text)
        self.assertIn(answer_desk.E(ANSWER), text)  # his text, for his own copy
        self.assertEqual(self.desk_events(), [])
        entry = answer_desk.journal_find(self.root, "rowan", self.question["id"], nonce)
        self.assertEqual((entry["state"], entry["error_code"]), ("refused", "invalid_state"))


class ReviewF1Tests(P2Base):
    """Vesper's P2 review F1: a journal entry alone never skips the open check; only a found event does."""

    def test_pending_context_without_event_after_close_is_refused(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        answer_desk.journal_claim(self.root, "rowan", self.question["id"], nonce, "context", "david", None)
        with Store(self.home) as store:
            store.feed("david", "answered on telegram", source="manual", external_id="tg1",
                       reply_to=self.question["id"])
        status, _, _ = self.post(cookie, nonce, token, answer=CONTEXT, kind="context")
        self.assertEqual(status, 409)
        self.assertEqual(self.desk_events(), [])

    def test_pending_answer_without_event_is_a_first_send(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        answer_desk.journal_claim(self.root, "rowan", self.question["id"], nonce, "answer", "david", None)
        status, headers, _ = self.post(cookie, nonce, token)
        self.assertEqual(status, 303)
        self.assertEqual(self.stored(), ("answered", "ready", 1))
        entry = answer_desk.journal_find(self.root, "rowan", self.question["id"], nonce)
        self.assertEqual(headers["Location"], "/s/rowan/%s" % entry["event_id"])


class ContextTests(P2Base):
    def test_t6_context_keeps_the_question_open(self):
        _, cookie = self.paired()
        text, nonce, token = self.form(cookie)
        self.assertIn("Send as context, keep the question open", text)
        self.assertIn("The question stays open", text)
        status, headers, _ = self.post(cookie, nonce, token, answer=CONTEXT, kind="context")
        self.assertEqual(status, 303)
        self.assertEqual(self.stored(), ("open", "waiting", 1))
        event = self.desk_events()[0]
        self.assertIsNone(event.get("reply_to"))
        self.assertEqual(event["in_reply_to"], self.question["id"])
        self.assertEqual((event["sender"], event["auth"]["channel"]), ("david", "lookout-desk"))
        status, _, page = self.request("GET", headers["Location"], cookie=cookie)
        self.assertIn("accepted as context for %s; the question stays open" % self.question["id"], page)
        self.assertNotIn(CONTEXT, page)

    def test_t7_context_for_a_closed_question_is_refused(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        _, nonce2, token2 = self.form(cookie)
        self.assertEqual(self.post(cookie, nonce, token)[0], 303)
        self.assertEqual(self.post(cookie, nonce2, token2, answer=CONTEXT, kind="context")[0], 409)
        self.assertEqual(len(self.desk_events()), 1)

    def test_t8_kind_switch_on_one_form_is_refused(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        self.assertEqual(self.post(cookie, nonce, token, answer=CONTEXT, kind="context")[0], 303)
        status, _, text = self.post(cookie, nonce, token, answer=CONTEXT, kind="answer")
        self.assertEqual(status, 409)
        self.assertIn("already sent as context", text)
        self.assertEqual(self.stored(), ("open", "waiting", 1))
        self.assertEqual(self.post(cookie, nonce, token, kind="bogus")[0], 400)


class StatusTests(P2Base):
    def test_t9_status_page_reads_the_question_from_the_record(self):
        _, cookie = self.paired()
        with Store(self.home) as store:
            other = store.question_ask("david", "another question", key="q-other")
        _, nonce, token = self.form(cookie)
        _, headers, _ = self.post(cookie, nonce, token)
        location = headers["Location"]
        status, _, plain = self.request("GET", location, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("accepted as the answer to %s" % self.question["id"], plain)
        with Store(self.home) as store:
            sharedstatus.write(store)
        status, _, wrong = self.request("GET", location + "?q=" + other["id"], cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("accepted as the answer to %s" % self.question["id"], wrong)
        self.assertNotIn(other["id"], wrong)


    def test_review_l2_a_finished_work_reads_no_longer_open(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        _, headers, _ = self.post(cookie, nonce, token)
        event_id = headers["Location"].rsplit("/", 1)[1]
        with Store(self.home) as store:
            run = store.run_begin([event_id])
            store.run_finish(run["id"], "done")
            store.work_put(id=self.work["id"], status="done", evidence=[dict(ref=event_id, level="observed")])
            sharedstatus.write(store)
        _, _, text = self.request("GET", headers["Location"], cookie=cookie)
        self.assertIn("✓ Outcome", text)
        self.assertIn("%s is no longer open" % self.work["id"], text)

    def test_review_l1_delivery_names_the_routes(self):
        path = sharedstatus.path(self.home)
        status = json.loads(path.read_text())
        status["docket"] = dict(items=[dict(question_id=self.question["id"], ack="delivered",
                                            delivered_at="2026-11-19T17:00:00Z", routes=["telegram"])])
        path.write_text(json.dumps(status))
        rows = answer_desk.context_rows(self.root, "rowan", dict(question_id=self.question["id"], work_ids=[]))
        self.assertIn("Delivery: delivered elsewhere too (routes: telegram); only the first answer counts", rows)


class EventStatusContractTests(P2Base):
    def cli(self, *args, key=None):
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "channel",
                              "event-status", "--key-file", str(key or self.key)] + list(args),
                             capture_output=True, text=True, timeout=60)
        return out.returncode, json.loads(out.stdout.splitlines()[0])

    def test_t10_lookup_finds_only_this_channels_own_events(self):
        other_key = self.key.with_name("other.key")
        other_key.write_text("o" * 64 + "\n")
        other_key.chmod(0o600)
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "channel", "register",
                              "other", "--source-prefix", "other:", "--key-file", str(other_key)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        mine = answer_desk.contract(self.home, self.key, "inbound", lines=[dict(
            v=1, kind="message", external_id="desk:x:1", sender="david", body=ANSWER, source="lookout:desk",
            question_id=self.question["id"])])
        theirs = answer_desk.contract(self.home, other_key, "inbound", lines=[dict(
            v=1, kind="message", external_id="desk:x:2", sender="david", body="SENTINEL-OTHER", source="other:t",
            in_reply_to=self.question["id"])])
        self.assertEqual((mine["result"], theirs["result"]), ("accepted", "accepted"))
        code, row = self.cli("--source", "lookout:desk", "--external-id", "desk:x:1")
        self.assertEqual(code, 0)
        self.assertEqual(row, dict(v=1, event_id=mine["event_id"], state="accepted", run_id=None,
                                   question_id=self.question["id"], in_reply_to=None))
        code, row = self.cli("--event", theirs["event_id"], key=other_key)
        self.assertEqual((row["question_id"], row["in_reply_to"]), (None, self.question["id"]))
        refusals = [self.cli("--source", "other:t", "--external-id", "desk:x:2")[1],
                    self.cli("--source", "lookout:desk", "--external-id", "desk:nope")[1]]
        self.assertEqual([r["error"]["code"] for r in refusals], ["not_routed", "not_routed"])
        self.assertEqual(refusals[0]["error"]["message"], refusals[1]["error"]["message"])
        for bad in ([], ["--source", "lookout:desk"], ["--event", mine["event_id"], "--source", "lookout:desk",
                                                       "--external-id", "desk:x:1"]):
            self.assertEqual(self.cli(*bad)[1]["error"]["code"], "bad_line", bad)
        with Store(self.home) as store:
            audit = [dict(r) for r in store.db.execute(
                "SELECT record_id, data FROM audit WHERE action='channel.event_lookup' ORDER BY seq")]
        self.assertEqual([a["record_id"] for a in audit], [mine["event_id"], "(not found)", "(not found)"])
        self.assertEqual([json.loads(a["data"])["result"] for a in audit], ["returned", "refused", "refused"])
        for a in audit:
            self.assertNotIn("SENTINEL", a["data"])
        self.assertNotIn("sender", json.dumps(row))

    def test_t11_desk_restart_with_a_pending_submit(self):
        _, cookie = self.paired()
        _, nonce, _ = self.form(cookie)
        answer_desk.journal_claim(self.root, "rowan", self.question["id"], nonce, "answer", "david", None,
                                  [self.work["id"]])
        status, _, text = self.request("GET", "/", cookie=cookie)
        self.assertIn("Unconfirmed submits", text)
        self.assertIn(self.question["id"], text)
        # The killed desk's inbound did land before it died: the lookup settles the entry from that record.
        row = answer_desk.contract(self.home, self.key, "inbound", lines=[dict(
            v=1, kind="message", external_id=answer_desk.external_id(self.question["id"], nonce), sender="david",
            body=ANSWER, source=answer_desk.SOURCE, question_id=self.question["id"])])
        status, _, text = self.request("GET", "/", cookie=cookie)
        self.assertNotIn("Unconfirmed submits", text)
        entry = answer_desk.journal_find(self.root, "rowan", self.question["id"], nonce)
        self.assertEqual((entry["state"], entry["event_id"]), ("accepted", row["event_id"]))


class ContextLeakTests(P2Base):
    def test_t12_context_and_journal_leak_nothing(self):
        thread_marker = "SENTINEL-THREAD another message in this thread"
        other_marker = "SENTINEL-OTHERQ second line of another question"
        note_marker = "SENTINEL-NOTE a private work note"
        with Store(self.home) as store:
            store.work_put(id=self.work["id"], note=note_marker, description="SENTINEL-DESC work description")
            question = store.question_ask("david", "Threaded ask?\n" + SECOND_LINE, work_id=self.work["id"],
                                          key="q-thread", thread="t-sticker")
            store.feed("david", thread_marker, source="manual", external_id="m1", thread="t-sticker")
            store.question_ask("david", "Other ask\n" + other_marker, key="q-other")
            sharedstatus.write(store)
        code, cookie = self.paired()
        status, _, page = self.request("GET", "/q/rowan/%s" % question["id"], cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Asked by Rowan (maintainer)", page)
        self.assertIn("Blocks %s: needs David (waiting" % self.work["id"], page)
        nonce = re.search(r"name='nonce' value='([0-9a-f]{32})'", page).group(1)
        token = re.search(r"name='csrf' value='([0-9a-f]{64})'", page).group(1)
        _, headers, _ = self.post(cookie, nonce, token, answer=CONTEXT, kind="context", qid=question["id"])
        self.request("GET", headers["Location"], cookie=cookie)
        status, _, again = self.request("GET", "/q/rowan/%s" % question["id"], cookie=cookie)
        self.assertIn("Sent here earlier", again)
        self.request("GET", "/", cookie=cookie)
        key = self.key.read_text().strip()
        for text in self.pages:
            for secret in (key, cookie, code, CONTEXT, thread_marker, other_marker, note_marker,
                           "SENTINEL-DESC"):
                self.assertNotIn(secret, text)
        journal = (self.root / "answer-desk" / "submissions.json").read_text()
        self.assertEqual(oct((self.root / "answer-desk" / "submissions.json").stat().st_mode & 0o777), "0o600")
        for secret in (key, cookie, code, token, CONTEXT, SECOND_LINE):
            self.assertNotIn(secret, journal)
        for entry in json.loads(journal)["entries"]:
            self.assertLessEqual(set(entry), set(answer_desk.JOURNAL_FIELDS))
        with Store(self.home) as store:
            sharedstatus.write(store)
        status_json = sharedstatus.path(self.home).read_text()
        for secret in (CONTEXT, SECOND_LINE, cookie, token):
            self.assertNotIn(secret, status_json)


class JournalBoundTests(P2Base):
    def test_journal_is_bounded(self):
        for i in range(answer_desk.JOURNAL_MAX + 5):
            answer_desk.journal_claim(self.root, "rowan", self.question["id"], "%032x" % i, "answer", "david", None,
                                      now=1000.0 + i)
        self.assertEqual(len(answer_desk.journal(self.root, now=1000.0 + 300)), answer_desk.JOURNAL_MAX)
        self.assertEqual(answer_desk.journal(self.root, now=1000.0 + 300 + answer_desk.JOURNAL_DAYS * 86400), [])



class LowsTests(P2Base):
    """Vesper's #71 review L1 (a pending entry that never reached the store cost one lookup + one audit row per
    view of / for 7 days) and n1 (lookup arguments unbounded before the audit row)."""
    cli = EventStatusContractTests.cli

    def lookups(self):
        with Store(self.home) as store:
            return store.db.execute("SELECT COUNT(*) FROM audit WHERE action='channel.event_lookup'").fetchone()[0]

    def old_pending(self, nonce, kind="answer"):
        old = answer_desk.time.time() - getattr(answer_desk, "LOST_AFTER", 120) - 1  # getattr: fail-first on fff5085
        answer_desk.journal_claim(self.root, "rowan", self.question["id"], nonce, kind, "david", None, now=old)

    def test_l1_old_missing_pending_settles_lost_and_stops_looking(self):
        _, cookie = self.paired()
        _, nonce, _ = self.form(cookie)
        self.old_pending(nonce)
        for _ in range(3):
            status, _, text = self.request("GET", "/", cookie=cookie)
            self.assertEqual(status, 200)
        self.assertEqual(self.lookups(), 1)
        entry = answer_desk.journal_find(self.root, "rowan", self.question["id"], nonce)
        self.assertEqual((entry["state"], entry["event_id"]), ("lost", None))
        self.assertNotIn("Unconfirmed submits", text)
        self.assertIn("Not received", text)

    def test_l1_young_missing_pending_stays_pending(self):
        _, cookie = self.paired()
        _, nonce, _ = self.form(cookie)
        answer_desk.journal_claim(self.root, "rowan", self.question["id"], nonce, "answer", "david", None)
        _, _, text = self.request("GET", "/", cookie=cookie)
        self.assertIn("Unconfirmed submits", text)
        self.assertEqual(answer_desk.journal_find(self.root, "rowan", self.question["id"], nonce)["state"],
                         "pending")

    def test_l1_lost_entry_sent_again_is_a_first_send(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        self.old_pending(nonce)
        self.request("GET", "/", cookie=cookie)
        status, headers, _ = self.post(cookie, nonce, token)
        self.assertEqual(status, 303)
        self.assertEqual(self.stored(), ("answered", "ready", 1))
        entry = answer_desk.journal_find(self.root, "rowan", self.question["id"], nonce)
        self.assertEqual(entry["state"], "accepted")
        self.assertEqual(headers["Location"], "/s/rowan/%s" % entry["event_id"])

    def test_l1_lost_context_after_close_is_refused(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        self.old_pending(nonce, kind="context")
        self.request("GET", "/", cookie=cookie)
        with Store(self.home) as store:
            store.feed("david", "answered on telegram", source="manual", external_id="tg1",
                       reply_to=self.question["id"])
        status, _, _ = self.post(cookie, nonce, token, answer=CONTEXT, kind="context")
        self.assertEqual(status, 409)
        self.assertEqual(self.desk_events(), [])

    def test_l1_lost_then_found_on_resend_is_the_same_event(self):
        """An orphaned inbound that landed after the entry settled lost: the resend's lookup finds it."""
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        self.old_pending(nonce)
        self.request("GET", "/", cookie=cookie)
        row = answer_desk.contract(self.home, self.key, "inbound", lines=[dict(
            v=1, kind="message", external_id=answer_desk.external_id(self.question["id"], nonce), sender="david",
            body=ANSWER, source=answer_desk.SOURCE, question_id=self.question["id"])])
        status, headers, _ = self.post(cookie, nonce, token)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/s/rowan/%s" % row["event_id"])
        self.assertEqual(len(self.desk_events()), 1)

    def test_n1_lookup_arguments_are_bounded_before_the_audit(self):
        cap = getattr(Store, "EVENT_LOOKUP_MAX", 256)  # getattr: fail-first on fff5085
        for args in (["--source", "lookout:desk", "--external-id", "x" * (cap + 1)],
                     ["--source", "s" * (cap + 1), "--external-id", "desk:x:1"]):
            self.assertEqual(self.cli(*args)[1]["error"]["code"], "bad_line")
        self.assertEqual(self.lookups(), 0)
        self.assertEqual(self.cli("--source", "lookout:desk", "--external-id", "x" * cap)[1]["error"]["code"],
                         "not_routed")
        self.assertEqual(self.lookups(), 1)


if __name__ == "__main__":
    unittest.main()

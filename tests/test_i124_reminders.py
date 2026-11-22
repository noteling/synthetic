"""#124: list and cancel own pending reminders. Temp homes only."""

import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from synthetics import boundedreads, cli, engine, floorpersona, floortools, mcp
from synthetics.store import Store, timestamp
from tests import test_satchel_p2 as p2

PAST, LATER, FAR = "2020-01-01T10:00:00.000000Z", "2098-01-01T10:00:00.000000Z", "2099-01-01T10:00:00.000000Z"
NOW = "2021-01-01T00:00:00.000000Z"
FIELDS = {"at", "reason", "work_id", "id", "created_at", "fired_at"}
CANCEL_FIELDS = {"cancelled_at", "cancel_reason", "cancelled_by_run"}


def old_reminders_list(self, include_fired=False):
    """f195e80's Store.reminders_list, copied."""
    return [r for r in self._all("reminders") if include_fired or r["fired_at"] is None]


def old_due_events(self, now=None):
    """The reminder half of f195e80's Store.due_events, copied."""
    now = timestamp(now)
    count = 0
    with self._tx():
        for reminder in self._all("reminders"):
            if reminder["fired_at"] is not None or reminder["at"] > now:
                continue
            event = self._event(dict(sender="clock", body=reminder["reason"], kind="reminder", source="clock",
                                     external_id=reminder["id"], work_id=reminder["work_id"], thread=None, reply_to=None))
            reminder.update(fired_at=now, event_id=event["id"])
            self._save("reminders", reminder, "reminder.fired")
            if reminder["work_id"]:
                # Not work_get: its shared_with view field must never be saved into the record.
                work = self._work_view(self._get("works", reminder["work_id"]))
                # P2-fix (2): a reminder linked before the refusal existed never wakes a routine.
                if work["status"] == "waiting" and not self._is_routine(work):
                    work.update(status="ready", waiting_for="", waiting_question_id=None, waiting_question_ids=[], wake_at=None, updated_at=now)
                    self._save("works", work, "work.awakened")
            count += 1
    return count


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "vesper"
        self.store = self.open()
        self.store.initialize("Vesper", "maintainer")
        self.n = 0

    def open(self, run_id=None):
        store = Store(self.home, run_id=run_id)
        self.addCleanup(store.close)
        return store

    def set(self, at=FAR, reason="look again", work=None):
        self.n += 1
        return self.store.reminder_set(at, reason, work and work["id"], key="k%d" % self.n)

    def waiting(self, **fields):
        return self.store.work_put("Waits", **dict(dict(status="waiting", waiting_for="someone"), **fields))

    def stored(self, record):
        return self.store._get("reminders", record["id"])

    def dump(self):
        """Every record table and the audit length: equal dumps mean nothing was written."""
        rows = {t: [tuple(r) for r in self.store.db.execute("SELECT id,data FROM %s ORDER BY id" % t)]
                for t in ("reminders", "works", "questions", "events", "messages")}
        return rows, self.store.db.execute("SELECT count(*) FROM audit").fetchone()[0]

    def server(self, run_id=None):
        return mcp.Server(self.open(run_id), run_id=run_id)

    def rpc(self, server, name, arguments):
        result = server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}})["result"]
        return bool(result.get("isError")), result["content"][0]["text"]

    def walk(self, budget=32000, name="reminder_list", limit=2000):
        """Every record of a full walk in order, with duplicates kept; checks each page's bytes and total."""
        server, args, values, chunk = self.server(), {"max_bytes": budget}, [], ""
        for _ in range(limit):
            bad, text = self.rpc(server, name, args)
            self.assertFalse(bad, text)
            self.assertLessEqual(len(text.encode("utf-8")), budget)
            page = json.loads(text)
            self.assertEqual(page["total"], len(self.store.reminders_list()))
            values += [e["value"] for e in page.get("entries", [])]
            if "chunk" in page:
                chunk += page["chunk"]
                if len(chunk) == page["length"]:
                    values.append(json.loads(chunk))
                    chunk = ""
            if page["next_cursor"] is None:
                self.assertEqual(chunk, "")
                return values
            args = {"cursor": page["next_cursor"], "max_bytes": budget}
        self.fail("the walk did not end")


class CancelTests(Base):
    def test_record_stays_with_time_reason_and_run(self):
        work = self.waiting(wake_at=FAR)
        record = self.set(work=work)
        run = self.store.run_begin([])
        out = self.open(run["id"]).reminder_cancel(record["id"], "  replaced  ")
        self.assertEqual(set(out), FIELDS | CANCEL_FIELDS)
        self.assertEqual(out, self.stored(record))
        self.assertEqual({k: out[k] for k in FIELDS - {"fired_at"}}, {k: record[k] for k in FIELDS - {"fired_at"}})
        self.assertEqual((out["fired_at"], out["cancel_reason"], out["cancelled_by_run"]),
                         ("cancelled", "replaced", run["id"]))
        self.assertEqual(timestamp(out["cancelled_at"]), out["cancelled_at"])
        self.assertGreaterEqual(out["cancelled_at"], record["created_at"])
        row = self.store.db.execute("SELECT data FROM audit WHERE action='reminder.cancelled'").fetchall()
        self.assertEqual([json.loads(r[0]) for r in row], [out])

    def test_outside_a_run_the_run_is_null(self):
        self.assertIsNone(self.store.reminder_cancel(self.set()["id"], "x")["cancelled_by_run"])

    def test_repeat_returns_the_stored_record_and_writes_nothing(self):
        record = self.set(at=PAST)
        first = self.store.reminder_cancel(record["id"], "first reason")
        before = self.dump()
        for reason in ("first reason", "another reason"):
            self.assertEqual(self.store.reminder_cancel(record["id"], reason), dict(first, already=True))
        self.assertEqual(self.dump(), before)

    def test_fired_is_refused_with_time_and_event(self):
        record = self.set(at=PAST)
        self.store.due_events(NOW)
        event = self.stored(record)["event_id"]
        before = self.dump()
        with self.assertRaisesRegex(ValueError, "%s already fired at %s .%s" % (record["id"], NOW, event)):
            self.store.reminder_cancel(record["id"], "too late")
        self.assertEqual(self.dump(), before)

    def test_due_but_unfired_is_pending_and_can_be_cancelled(self):
        work = self.waiting()
        record = self.set(at=PAST, work=work)
        self.assertEqual([r["id"] for r in self.walk()], [record["id"]])
        self.store.reminder_cancel(record["id"], "obsolete")
        self.assertEqual(self.store.due_events(NOW), 0)
        self.assertEqual(self.store.work_get(work["id"])["status"], "waiting")

    def test_unknown_other_home_and_empty_values(self):
        with Store(Path(self.tmp.name) / "other") as other:
            other.initialize("Rowan", "maintainer")
            foreign = other.reminder_set(FAR, "theirs", key="k")
            for bad in (foreign["id"], "reminder-" + "0" * 32):
                with self.assertRaisesRegex(ValueError, "No reminders record %s" % bad):
                    self.store.reminder_cancel(bad, "x")
            self.assertIsNone(other._get("reminders", foreign["id"])["fired_at"])
        record = self.set()
        for args in ((record["id"], " "), (record["id"], None), ("", "x"), (None, "x")):
            with self.assertRaises(ValueError):
                self.store.reminder_cancel(*args)
        self.assertEqual(self.stored(record), record)

    def test_prefix_resolves_and_an_ambiguous_one_is_refused(self):
        record = self.set()
        server = self.server()
        bad, text = self.rpc(server, "reminder_cancel", {"reminder_id": record["id"][:17], "reason": "x"})
        self.assertFalse(bad, text)
        self.assertEqual(json.loads(text)["resolved"], {record["id"][:17]: record["id"]})
        with self.store._tx():
            for tail in ("a" * 24, "b" * 24):
                self.store._save("reminders", dict(record, id="reminder-12345678" + tail), "reminder.set")
        with self.assertRaisesRegex(ValueError, "ambiguous_id"):
            self.store.reminder_cancel("reminder-12345678", "x")

    def test_cancel_before_the_time_passes_adds_no_event_and_keeps_question_links(self):
        """The issue's case: an obsolete reminder fires and clears the waiting work's question link."""
        work = self.store.work_put("Waits on an answer")
        question = self.store.question_ask("david", "Which?", work_id=work["id"], key="q")
        waiting = self.store.work_get(work["id"])
        control = self.store.work_put("Control")
        self.store.question_ask("rowan", "Which?", work_id=control["id"], key="q2")
        obsolete, kept = self.set(at=PAST, reason="obsolete", work=work), self.set(at=PAST, work=control)
        out = self.store.reminder_cancel(obsolete["id"], "replaced")
        self.assertNotIn("work_without_wake", out)  # the open question still wakes it
        self.assertEqual(self.store.due_events(NOW), 1)
        self.assertEqual([e["external_id"] for e in self.store.event_pending()], [kept["id"]])
        self.assertEqual(self.store.work_get(work["id"]), waiting)
        self.assertEqual(waiting["waiting_question_ids"], [question["id"]])
        self.assertEqual(self.store.work_get(control["id"])["waiting_question_ids"], [])  # what a fire does

    def test_the_others_on_the_same_work_still_fire_and_wake_it(self):
        work = self.waiting()
        first, middle, last = (self.set(at=PAST, work=work) for _ in range(3))
        self.assertNotIn("work_without_wake", self.store.reminder_cancel(middle["id"], "replaced"))
        self.assertEqual(self.store.due_events(NOW), 2)
        self.assertEqual([e["external_id"] for e in self.store.event_pending()], [first["id"], last["id"]])
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")
        self.assertEqual(self.stored(middle)["fired_at"], "cancelled")

    def test_linked_work_is_never_changed(self):
        for fields in (dict(), dict(wake_at=FAR), dict(status="ready", waiting_for="")):
            work = self.waiting(**fields)
            record = self.set(work=work)
            before = self.store._get("works", work["id"])
            self.store.reminder_cancel(record["id"], "x")
            self.assertEqual(self.store._get("works", work["id"]), before)


class HintTests(Base):
    def hint(self, work, again=False):
        """The hint of a first cancel and of its repeat; neither it nor `already` is ever saved."""
        record = self.set(work=work)
        out = self.store.reminder_cancel(record["id"], "x")
        repeat = self.store.reminder_cancel(record["id"], "x")
        self.assertEqual(repeat, dict(out, already=True))
        self.assertEqual(set(self.stored(record)), FIELDS | CANCEL_FIELDS)
        self.assertFalse({"already", "work_without_wake"} & set(self.store._get("works", work["id"])))
        audit = self.store.db.execute("SELECT data FROM audit WHERE record_id=?", (record["id"],)).fetchall()
        self.assertFalse(any("work_without_wake" in r[0] or '"already"' in r[0] for r in audit))
        return out.get("work_without_wake")

    def test_waiting_with_nothing_left(self):
        work = self.waiting()
        self.assertEqual(self.hint(work), work["id"])

    def test_wake_at_even_past_is_a_wake(self):
        self.assertIsNone(self.hint(self.waiting(wake_at=FAR)))
        work = self.waiting(wake_at=FAR)
        with self.store._tx():
            self.store._save("works", dict(self.store._get("works", work["id"]), wake_at=PAST), "work.updated")
        self.assertIsNone(self.hint(work))  # due, not fired yet

    def test_linked_open_question_is_a_wake_and_a_closed_one_is_not(self):
        work = self.store.work_put("Asks")
        question = self.store.question_ask("david", "Which?", work_id=work["id"], key="q")
        self.assertIsNone(self.hint(work))
        self.store.question_close(question["id"], "obsolete")
        self.assertEqual(self.store.work_get(work["id"])["status"], "waiting")
        self.assertEqual(self.hint(work), work["id"])
        # A stale link to a question that is no longer open doesn't count either.
        with self.store._tx():
            self.store._save("works", dict(self.store._get("works", work["id"]), waiting_question_id=question["id"],
                                           waiting_question_ids=[question["id"]]), "work.updated")
        self.assertEqual(self.hint(work), work["id"])

    def test_another_pending_reminder_due_or_not_is_a_wake(self):
        for at in (FAR, PAST):
            work = self.waiting()
            self.set(at=at, work=work)
            self.assertIsNone(self.hint(work))

    def test_a_cancelled_or_fired_reminder_or_another_works_is_not(self):
        work, other = self.waiting(), self.waiting()
        self.set(work=other)
        self.set()
        self.store.reminder_cancel(self.set(work=work)["id"], "x")
        self.set(at=PAST, work=other)
        self.assertEqual(self.hint(work), work["id"])
        self.set(at=PAST, work=work)
        self.store.due_events(NOW)
        self.store.work_put(id=work["id"], status="waiting", waiting_for="someone")
        self.assertEqual(self.hint(work), work["id"])

    def test_never_for_ready_terminal_routine_or_unlinked(self):
        self.assertIsNone(self.hint(self.store.work_put("Ready")))
        for status, extra in (("cancelled", dict(cancel_reason="x")),
                              ("done", dict(evidence=[dict(level="observed", ref=str(Path(__file__).resolve()))]))):
            work = self.waiting()
            record = self.set(work=work)
            self.store.work_put(id=work["id"], status=status, **extra)
            self.assertIsNone(self.hint(work))
            self.assertNotIn("work_without_wake", self.store.reminder_cancel(record["id"], "x"))
        routine = self.waiting()
        record = self.set(work=routine)
        with self.store._tx():  # a reminder linked before the refusal existed
            self.store._save("works", dict(self.store._get("works", routine["id"]), routine=dict(kind="hourly-check")),
                             "work.updated")
        before = self.store._get("works", routine["id"])
        for _ in range(2):
            self.assertNotIn("work_without_wake", self.store.reminder_cancel(record["id"], "x"))
        self.assertEqual(self.store._get("works", routine["id"]), before)
        self.assertNotIn("work_without_wake", self.store.reminder_cancel(self.set()["id"], "x"))

    def test_repeat_shows_the_hint_as_it_is_now(self):
        work = self.waiting(wake_at=FAR)
        record = self.set(work=work)
        self.assertNotIn("work_without_wake", self.store.reminder_cancel(record["id"], "x"))
        self.store.work_put(id=work["id"], status="ready")
        self.store.work_put(id=work["id"], status="waiting", waiting_for="someone")
        self.assertEqual(self.store.reminder_cancel(record["id"], "x")["work_without_wake"], work["id"])
        self.store.reminder_set(FAR, "new", work["id"], key="replacement")
        self.assertNotIn("work_without_wake", self.store.reminder_cancel(record["id"], "x"))

    def test_legacy_waiting_work_without_link_fields(self):
        work = self.store.work_put("Legacy")
        question = self.store.question_ask("david", "Which?", work_id=work["id"], key="q")
        with self.store._tx():
            legacy = self.store._get("works", work["id"])
            legacy.pop("waiting_question_id"), legacy.pop("waiting_question_ids")
            self.store._save("works", legacy, "work.updated")
        record = self.set(work=work)
        self.assertNotIn("work_without_wake", self.store.reminder_cancel(record["id"], "x"))
        self.assertEqual(self.store._get("works", work["id"]), legacy)
        self.assertEqual(self.store.question_list("open")[0]["id"], question["id"])


class KeyTests(Base):
    def test_a_cancelled_reminders_key_is_refused(self):
        record = self.store.reminder_set(LATER, "Check", key="followup")
        out = self.store.reminder_cancel(record["id"], "answered already")
        before = self.dump()
        with self.assertRaises(ValueError) as caught:
            self.store.reminder_set(LATER, "Check", key="followup")
        for part in (record["id"], out["cancelled_at"], "answered already", "use a new key"):
            self.assertIn(part, str(caught.exception))
        with self.assertRaisesRegex(ValueError, "different content"):
            self.store.reminder_set(FAR, "Check", key="followup")
        self.assertEqual(self.dump(), before)
        again = self.store.reminder_set(LATER, "Check", key="followup-2")
        self.assertEqual((again["fired_at"], again["id"] != record["id"]), (None, True))

    def test_pending_and_fired_replays_are_unchanged(self):
        record = self.store.reminder_set(PAST, "Check", key="followup")
        self.assertEqual(self.store.reminder_set(PAST, "Check", key="followup"), record)
        self.store.due_events(NOW)
        replay = self.store.reminder_set(PAST, "Check", key="followup")
        self.assertEqual((replay["id"], replay["fired_at"]), (record["id"], NOW))

    def test_cli_gets_the_same_refusal_and_status_drops_the_cancelled_one(self):
        record, kept = self.store.reminder_set(LATER, "Check", key="followup"), self.set()
        self.store.reminder_cancel(record["id"], "answered already")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", str(self.home), "remind", LATER, "Check", "--key", "followup"])
        self.assertNotEqual(code, 0)
        self.assertIn(record["id"], err.getvalue())
        self.assertIn("use a new key", err.getvalue())
        self.assertEqual([r["id"] for r in engine.status(self.home)["reminders"]], [kept["id"]])


class OldCodeTests(Base):
    """What f195e80 does with a record this code cancelled: after a rollback, or a worker not restarted yet."""

    def test_old_code_neither_fires_nor_lists_it(self):
        work = self.waiting()
        cancelled, control = self.set(at=PAST, work=work), self.set(at=PAST)
        self.store.reminder_cancel(cancelled["id"], "x")
        record = self.stored(cancelled)
        self.assertEqual([r["id"] for r in old_reminders_list(self.store)], [control["id"]])
        self.assertEqual(old_due_events(self.store, NOW), 1)
        self.assertEqual([e["external_id"] for e in self.store.event_pending()], [control["id"]])
        self.assertEqual(self.store.work_get(work["id"])["status"], "waiting")
        self.assertEqual(self.stored(cancelled), record)

    def test_a_cancelled_at_alone_would_not_stop_it(self):
        """Why fired_at carries a marker (shape r0 probe)."""
        record = self.set(at=PAST)
        with self.store._tx():
            self.store._save("reminders", dict(record, cancelled_at=NOW), "reminder.cancelled")
        self.assertEqual(old_due_events(self.store, NOW), 1)


class RaceTests(Base):
    def test_either_order_on_two_connections(self):
        first, second = self.set(at=PAST), self.set(at=PAST)
        other = self.open()
        other.reminder_cancel(first["id"], "x")
        self.assertEqual(self.store.due_events(NOW), 1)
        with self.assertRaisesRegex(ValueError, "already fired"):
            other.reminder_cancel(second["id"], "x")
        self.assertEqual([e["external_id"] for e in self.store.event_pending()], [second["id"]])

    def overlap(self, hold_method, holder, waiter):
        """Run holder(store) paused inside its transaction (in hold_method) while waiter(store) starts."""
        inside, go, calling, results = threading.Event(), threading.Event(), threading.Event(), {}

        def run(name, action, hold):
            with Store(self.home) as store:  # both are open before the transaction starts: opening a store writes
                ready.wait(10)
                if hold:
                    real = getattr(store, hold_method)

                    def paused(*a, **kw):
                        inside.set()
                        go.wait(10)
                        return real(*a, **kw)
                    setattr(store, hold_method, paused)
                else:
                    inside.wait(10)
                    calling.set()
                try:
                    results[name] = action(store)
                except ValueError as exc:
                    results[name] = exc

        ready = threading.Barrier(2)
        a = threading.Thread(target=run, args=("holder", holder, True))
        b = threading.Thread(target=run, args=("waiter", waiter, False))
        a.start(), b.start()
        self.assertTrue(calling.wait(10))
        b.join(0.4)
        self.assertTrue(b.is_alive(), "the second writer did not wait for the first one's transaction")
        go.set()
        a.join(10), b.join(10)
        return results

    def test_a_cancel_during_a_fire_waits_and_is_refused(self):
        work = self.waiting()
        record = self.set(at=PAST, work=work)
        results = self.overlap("_event", lambda s: s.due_events(NOW), lambda s: s.reminder_cancel(record["id"], "x"))
        self.assertEqual(results["holder"], 1)
        self.assertIsInstance(results["waiter"], ValueError)
        self.assertIn("already fired", str(results["waiter"]))
        self.assertEqual(self.stored(record)["fired_at"], NOW)
        self.assertNotIn("cancelled_at", self.stored(record))

    def test_a_fire_during_a_cancel_waits_and_skips_it(self):
        work = self.waiting()
        record = self.set(at=PAST, work=work)
        results = self.overlap("_save", lambda s: s.reminder_cancel(record["id"], "x"), lambda s: s.due_events(NOW))
        self.assertEqual((results["holder"]["fired_at"], results["waiter"]), ("cancelled", 0))
        self.assertEqual(self.store.event_pending(), [])
        self.assertEqual(self.store.work_get(work["id"])["status"], "waiting")


class FencingTests(Base):
    def test_a_run_that_is_not_active_is_refused_on_first_and_repeat_call(self):
        record, done = self.set(), self.set()
        run = self.store.run_begin([])
        bound = self.open(run["id"])
        bound.reminder_cancel(done["id"], "x")
        self.store.run_finish(run["id"], "over")
        before = self.dump()
        for target in (record, done):
            with self.assertRaisesRegex(ValueError, "no longer active"):
                bound.reminder_cancel(target["id"], "x")
        self.assertEqual(self.dump(), before)

    def test_it_is_a_mutation_and_the_list_is_not(self):
        self.assertIn("reminder_cancel", mcp.MUTATIONS)
        self.assertNotIn("reminder_list", mcp.MUTATIONS)
        self.assertEqual(mcp.METHODS["reminder_cancel"]["inputSchema"]["required"], ["reminder_id", "reason"])
        self.assertEqual(set(mcp.METHODS["reminder_list"]["inputSchema"]["properties"]), {"cursor", "max_bytes"})

    def test_a_meeting_run_gets_neither_tool_and_its_ready_is_unchanged(self):
        work = self.waiting()
        goal = "Talk about %s" % work["id"]
        before = json.dumps(floorpersona.build(self.store, goal))
        record, cancelled = self.set(work=work), self.set(work=work)
        self.store.reminder_cancel(cancelled["id"], "x")
        self.assertEqual(json.dumps(floorpersona.build(self.store, goal)), before)
        self.store.meeting_begin("m1", "stand")
        run = self.store.meeting_run_begin("m1", self.store.meeting_attach("m1")["token"])
        server = self.server(run["id"])
        listed = {t["name"] for t in server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(listed, floortools.MEETING_TOOLS)
        for name, args in (("reminder_list", {}), ("reminder_cancel", {"reminder_id": record["id"], "reason": "x"})):
            with self.assertRaisesRegex(ValueError, "Unknown tool"):
                server.call(name, args)
        self.assertEqual(self.stored(record), record)


class ListTests(Base):
    def test_order_is_time_then_creation_then_id(self):
        late = self.set(at=FAR)
        due = self.set(at=PAST)
        tie = [("f", PAST), ("a", NOW), ("d", NOW), ("c", NOW), ("b", FAR)]  # (ID digit, created_at), stored in this order
        with self.store._tx():
            for digit, created in tie:
                self.store._save("reminders", dict(late, id="reminder-" + digit * 32, at=LATER, created_at=created),
                                 "reminder.set")
        self.assertEqual([r["id"] for r in self.walk()],
                         [due["id"]] + ["reminder-" + d * 32 for d in "facdb"] + [late["id"]])

    def test_records_are_whole_and_only_pending_ones(self):
        work = self.waiting()
        kept = self.set(reason="r" * 5000, work=work)
        self.store.reminder_cancel(self.set()["id"], "x")
        self.set(at=PAST)
        self.store.due_events(NOW)
        self.assertEqual(self.walk(), [kept])

    def test_empty(self):
        bad, text = self.rpc(self.server(), "reminder_list", {})
        self.assertEqual(json.loads(text), {"kind": "reminder_list", "total": 0, "index": 0, "next_cursor": None})

    def check_all(self, count, budget):
        with self.store._tx():
            for i in range(count):
                at = "2099-01-%02dT%02d:%02d:00.000000Z" % (28 - i // 1440, 23 - i // 60 % 24, 59 - i % 60)
                self.store._save("reminders", dict(at=at, reason="r%d" % i, work_id=None, id="reminder-%032x" % (i * 7919),
                                                   created_at=NOW, fired_at=None), "reminder.set")
        ids = [r["id"] for r in self.walk(budget)]
        self.assertEqual(len(ids), count)
        self.assertEqual(ids, ["reminder-%032x" % (i * 7919) for i in reversed(range(count))])  # each once, soonest first
        self.assertEqual(self.store.context()["reminders"]["pending"], count)

    def test_51_are_all_listed(self):
        self.check_all(51, 32000)

    def test_51_in_small_pages(self):
        self.check_all(51, 1024)

    def test_more_than_500_are_all_listed(self):
        self.check_all(523, 32000)

    def test_a_record_too_large_alone_comes_in_chunks(self):
        small, big, last = self.set(at=PAST), self.set(at=LATER, reason="雪🦊" * 3000), self.set(at=FAR)
        self.assertEqual(self.walk(1024), [small, big, last])

    def test_bounds_count_utf8_bytes_and_the_meeting_notice(self):
        self.set(reason="🦊" * 12000)
        run = self.store.run_begin([])
        server = self.server(run["id"])
        with mock.patch.object(server, "_yield_notice", lambda: "a meeting is starting"):
            for budget in (1024, 32000):
                bad, text = self.rpc(server, "reminder_list", {"max_bytes": budget})
                self.assertFalse(bad, text)
                self.assertLessEqual(len(text.encode("utf-8")), budget)
                self.assertEqual(json.loads(text)["yield"], "a meeting is starting")
        with mock.patch.object(server, "_yield_notice", mock.Mock(side_effect=[None, "late"])) as notice:
            self.assertNotIn("yield", json.loads(self.rpc(server, "reminder_list", {})[1]))
        self.assertEqual(notice.call_count, 1)  # one read, counted; a later one would not be
        for budget in (0, 1023, 32001, True):
            self.assertTrue(self.rpc(server, "reminder_list", {"max_bytes": budget})[0], budget)

    def test_any_change_of_the_pending_set_refuses_the_cursor(self):
        for change in ("set", "cancel", "fire"):
            with self.subTest(change):
                records = [self.set(at=PAST, reason="r" * 600) for _ in range(4)]
                page = json.loads(self.rpc(self.server(), "reminder_list", {"max_bytes": 1024})[1])
                args = {"cursor": page["next_cursor"], "max_bytes": 1024}
                self.assertFalse(self.rpc(self.server(), "reminder_list", args)[0])
                if change == "set":
                    self.set()
                elif change == "cancel":
                    self.store.reminder_cancel(records[-1]["id"], "x")
                else:
                    self.store.due_events(NOW)
                bad, text = self.rpc(self.server(), "reminder_list", args)
                self.assertTrue(bad)
                self.assertIn("Stale", text)

    def test_cursors_are_not_interchangeable(self):
        for i in range(4):
            self.set(reason="r" * 600)
            self.store.message_send("codex", "m" * 600, key="m%d" % i)
            self.store.feed("david", "e" * 600)
        server = self.server()
        names = ("reminder_list", "outbox_list", "employee_context")
        cursors = {n: json.loads(self.rpc(server, n, {"max_bytes": 1024})[1])["next_cursor"] for n in names}
        for name in names:
            for source, cursor in cursors.items():
                bad, text = self.rpc(server, name, {"cursor": cursor, "max_bytes": 1024})
                self.assertEqual(bad, source != name, (name, source, text))
        # A cursor of the right name in another read's shape is refused too, both ways.
        for source, name, called in (("outbox_list", "reminder_list", "reminder_list"),
                                     ("reminder_list", "outbox_list", "outbox_list"),
                                     ("reminder_list", "outbox_list", "reminder_list")):
            state = dict(json.loads(base64.urlsafe_b64decode(cursors[source])), name=name)
            bad, text = self.rpc(server, called, {"cursor": boundedreads.token(state)})
            self.assertEqual((bad, text), (True, "Invalid continuation cursor."))

    def test_no_selectors(self):
        self.set(reason="r" * 2000)
        server = self.server()
        cursor = json.loads(self.rpc(server, "reminder_list", {"max_bytes": 1024})[1])["next_cursor"]
        self.assertEqual(set(json.loads(base64.urlsafe_b64decode(cursor))), {"v", "name", "hash", "index", "offset"})
        for args in ({"limit": 1}, {"before": "x"}, {"status": "pending"}, {"id": "x"}):
            self.assertTrue(self.rpc(server, "reminder_list", args)[0], args)
        for bad_state in ({"index": 5}, {"index": True}, {"index": -1}, {"offset": 10 ** 6}, {"index": 1, "offset": 1}, {"v": 2}):
            state = dict(json.loads(base64.urlsafe_b64decode(cursor)), **bad_state)
            self.assertTrue(self.rpc(server, "reminder_list", {"cursor": boundedreads.token(state), "max_bytes": 1024})[0], bad_state)

    def test_reading_writes_nothing(self):
        self.set(at=PAST), self.set()
        before = self.dump()
        self.walk()
        self.assertEqual(self.dump(), before)


class SnapshotTests(Base):
    def test_line_counts_what_the_list_shows_and_is_left_out_at_zero(self):
        self.assertNotIn("reminders", self.store.context())
        due, later = self.set(at=PAST), self.set(at=LATER)
        self.assertEqual(self.store.context()["reminders"], {"pending": 2, "next_at": PAST})
        self.store.reminder_cancel(due["id"], "x")
        self.assertEqual(self.store.context()["reminders"], {"pending": 1, "next_at": LATER})
        self.assertEqual(len(self.walk()), 1)
        self.store.due_events(FAR)
        self.assertEqual(self.stored(later)["fired_at"], FAR)
        self.assertNotIn("reminders", self.store.context())

    def test_prompt_and_descriptions(self):
        prompt = (Path(mcp.__file__).parent / "prompts" / "employee.md").read_text(encoding="utf-8")
        self.assertEqual(prompt.count("When you replace a reminder, cancel the old one; look with reminder_list first."), 1)
        for name, limit in (("reminder_list", 200), ("reminder_cancel", 350)):
            self.assertLessEqual(len(mcp.METHODS[name]["description"]), limit)


class RoutineTests(Base):
    def test_a_real_routine_is_untouched_by_the_tools(self):
        self.store.reminder_cancel(self.set()["id"], "x")
        seeded = self.store.routine_seed(["hourly-check"])
        before = [w for w in self.store.work_list() if w.get("routine")]
        self.assertTrue(before, seeded)
        with self.assertRaisesRegex(ValueError, "routine"):
            self.store.reminder_set(FAR, "x", before[0]["id"], key="r")
        self.store.reminder_cancel(self.set()["id"], "x")
        self.assertEqual([w for w in self.store.work_list() if w.get("routine")], before)


class SatchelTests(p2.Fixture):
    def test_a_cancelled_reminder_travels_and_stays_cancelled(self):
        with Store(self.home) as store:
            work = store.work_put("Waits", status="waiting", waiting_for="someone", wake_at=FAR)
            cancelled = store.reminder_set(PAST, "obsolete", work["id"], key="a")
            kept = store.reminder_set(PAST, "still owed", key="b")
            record = store.reminder_cancel(cancelled["id"], "answered; password=%s" % "hunter22secret")
        archive = self.extract()["archive"]
        self.recreate(archive, source_lost=True, attest="disk died")
        with Store(self.root / "new") as store:
            restored = store._get("reminders", cancelled["id"])
            self.assertEqual({k: v for k, v in restored.items() if k != "cancel_reason"},
                             {k: v for k, v in record.items() if k != "cancel_reason"})
            self.assertNotIn("hunter22secret", restored["cancel_reason"])
            self.assertTrue(restored["cancel_reason"].startswith("answered; "))
            self.assertEqual([r["id"] for r in store.reminders_pending()], [kept["id"]])
            self.assertTrue(store.reminder_cancel(cancelled["id"], "again")["already"])
            self.assertEqual(store.due_events(NOW), 1)
            self.assertEqual(store.work_get(work["id"])["status"], "waiting")


if __name__ == "__main__":
    unittest.main()

"""Stage 2 click P1: one approved, named click through a fake Noteling reader (temp homes only).

T1-T16 follow the P0 acceptance kit (rowan workspace realworld/click-P0/P0.md §5) and Codex's review conditions
(management/realworld/click-P0/review.md C1/C2, owner-decisions.md)."""

import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from synthetics import click, glance, lookout, sharedstatus
from synthetics.capture import write_humans
from synthetics.mcp import Server
from synthetics.store import Store

TELEGRAM_KEY = b"t" * 32
DESKTOP_KEY = b"d" * 32
LABEL = "Add to cart - Velvet Lamp Zebra42"   # a hit label carrying page words
ADDRESS = "https://shopper@www.example-shop.test:8443/ip/Velvet-Lamp/42?q=Zebra42#reviews"
PAGE_WORDS = ("Zebra42", "Velvet", ":8443", "/ip/", "reviews", "shopper", "911.5", "dom-id-cart7")
HIT = {"role": "AXButton", "kind": "button", "label": LABEL, "visible": True, "enabled": True,
       "frame": [911.5, 422.25, 120, 40], "domID": "dom-id-cart7"}
PAGE_TOKEN, HIT_TOKEN = "a" * 64, "b" * 64


def prepared(**changes):
    report = {"ok": True, "exit": 0, "find": {"match": "exact", "count": 1, "unique": True, "hits": [dict(HIT)]},
              "guard": "safe", "page_token": PAGE_TOKEN, "hit_token": HIT_TOKEN, "host": ADDRESS,
              "page": {"site": ADDRESS, "sheetOpen": False, "elements": 52}}
    report.update(changes)
    return report


FIRED = {"ok": True, "exit": 0, "pressed": True, "changed": True}


class Crash(BaseException):
    """Stands in for the session dying mid-fire (not an Exception, so nothing catches it)."""


class Fake:
    def __init__(self, prepare=None, fire=None, delay=0.0):
        self.prepare, self.fire, self.delay = prepare or prepared(), fire or dict(FIRED), delay
        self.calls, self.lock = [], threading.Lock()

    def __call__(self, args):
        with self.lock:
            self.calls.append(list(args))
        result = self.prepare if "--prepare" in args else self.fire
        if self.delay and "--fire" in args:
            time.sleep(self.delay)
        if isinstance(result, BaseException):
            raise result
        return result

    def fires(self):
        return [c for c in self.calls if "--fire" in c]


class ClickBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "rowan"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Rowan", "maintainer")
        write_humans(self.home, {"default_human": "david", "senders": ["david", "erin"],
                                 "channel_prefixes": ["telegram:", "lookout:"],
                                 "operators": {"senders": ["codex-desktop"], "source_prefixes": ["codex-desktop:"]}})
        self.store.channel_register("telegram", hashlib.sha256(TELEGRAM_KEY).hexdigest(), ["telegram:"], "test",
                                    routes=["david"])
        self.store.channel_register("desktop", hashlib.sha256(DESKTOP_KEY).hexdigest(), ["codex-desktop:"], "test")
        self.run = self.store.run_begin()["id"]
        self.bound = Store(self.home, run_id=self.run)
        self.addCleanup(self.bound.close)
        self.server = Server(self.bound, run_id=self.run)
        self.fake = Fake()
        click.reader = self.fake
        self.addCleanup(setattr, click, "reader", None)
        self.opt_in(True)
        self.n = 0

    def opt_in(self, on):
        path = self.home / click.OPT_IN
        if on:
            path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        elif path.exists():
            path.unlink()

    def ask(self, name="Add to cart", **extra):
        return self.server.call("screen_click_request", dict({"name": name, "why": "David asked me to add the lamp."},
                                                             **extra))

    def answer(self, qid, body="yes Add to cart", sender="david", source="telegram:", credential=TELEGRAM_KEY,
               reply=True):
        self.n += 1
        return self.store.feed(sender, body, source=source + str(self.n), external_id="m%d" % self.n,
                               reply_to=qid if reply else None, credential=credential)

    def fire(self, request):
        return self.server.call("screen_click", {"request": request})

    def questions(self):
        return [json.loads(r["data"]) for r in self.store.db.execute("SELECT data FROM questions").fetchall()]

    def record(self, request):
        return self.store._get("clicks", request)

    def shift(self, request, field, minutes):
        record = self.record(request)
        record[field] = (click._parse(record[field]) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.store.db.execute("UPDATE clicks SET data=? WHERE id=?", (json.dumps(record), request))


class ApprovalTests(ClickBase):
    def test_happy_path_one_press(self):
        asked = self.ask()
        self.assertEqual(asked["state"], "asked")
        self.assertEqual(asked["host"], "www.example-shop.test")
        question = self.store._get("questions", asked["question_id"])
        self.assertEqual(question["to"], "david")
        self.assertEqual(question["click_id"], asked["request"])
        self.assertIn('"Add to cart" on www.example-shop.test', question["body"])
        self.assertIn("reply: yes Add to cart", question["body"])
        self.answer(asked["question_id"])
        done = self.fire(asked["request"])
        self.assertEqual((done["state"], done["pressed"], done["changed"]), ("done", True, True))
        fire = self.fake.fires()[0]
        self.assertIn("--require-front", fire)
        self.assertEqual(fire[fire.index("--expect") + 1], "%s,%s" % (PAGE_TOKEN, HIT_TOKEN))
        self.assertEqual(fire[fire.index("--named-approval") + 1], "Add to cart")
        self.assertEqual(fire[fire.index("--token") + 1], self.fake.calls[0][self.fake.calls[0].index("--token") + 1])

    def test_answer_normalized_but_must_name_control(self):
        asked = self.ask()
        self.answer(asked["question_id"], body="Yes, add to CART!")
        self.assertEqual(self.fire(asked["request"])["state"], "done")

    def test_t1_no_answer_never_launches(self):
        asked = self.ask()
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("asked", "waiting"))
        self.assertEqual(self.fake.fires(), [])

    def test_t2_proxy_close_refused_and_forged_proxy_never_approves(self):
        asked = self.ask()
        other = self.store.feed("vesper", "yes Add to cart", source="fieldpost:1", external_id="v1")
        with self.assertRaisesRegex(ValueError, "click approval question"):
            self.server.call("question_close", {"question_id": asked["question_id"], "reason": "x",
                                                "event_id": other["id"]})
        # Even a proxy record put there some other way never approves.
        question = self.store._get("questions", asked["question_id"])
        question.update(status="answered_by_proxy", answer_event_id=other["id"], secondhand=True)
        self.store.db.execute("UPDATE questions SET data=? WHERE id=?", (json.dumps(question), question["id"]))
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("declined", "not_approved"))
        self.assertEqual(self.fake.fires(), [])

    def test_t3_other_human_cannot_answer(self):
        asked = self.ask()
        with self.assertRaises(ValueError):
            self.answer(asked["question_id"], sender="erin")
        self.assertEqual(self.fire(asked["request"])["reason"], "waiting")
        self.assertEqual(self.fake.fires(), [])

    def test_t4_unauthenticated_answer_never_approves(self):
        asked = self.ask()
        self.store.feed("david", "yes Add to cart", source="telegram:x", external_id="u1",
                        reply_to=asked["question_id"])   # no credential: auth null
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("declined", "not_approved"))
        self.assertEqual(self.fake.fires(), [])

    def test_t4_c2_operator_relay_never_approves_even_as_david(self):
        asked = self.ask()
        event = self.answer(asked["question_id"], source="codex-desktop:", credential=DESKTOP_KEY)
        self.assertIsNotNone(event["auth"])            # authenticated, sender david, but an operator channel
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("declined", "not_approved"))
        self.assertEqual(self.fake.fires(), [])

    def test_t5_no_or_plain_yes_declines_finally(self):
        for body in ("no", "yes", "yes Add to basket", "ok go"):
            with self.subTest(body=body):
                asked = self.ask()
                self.answer(asked["question_id"], body=body)
                out = self.fire(asked["request"])
                self.assertEqual((out["state"], out["reason"]), ("declined", "declined"))
                self.assertEqual(self.fire(asked["request"])["state"], "declined")
        self.assertEqual(self.fake.fires(), [])

    def test_t6_expiry(self):
        asked = self.ask()
        self.shift(asked["request"], "asked_at", 31)       # nobody answered for 31 min
        self.assertEqual(self.fire(asked["request"])["state"], "expired")
        asked = self.ask()                                  # the expired one no longer blocks
        self.shift(asked["request"], "asked_at", 31)       # answered 31 min after the ask
        self.answer(asked["question_id"])
        self.assertEqual(self.fire(asked["request"])["state"], "expired")
        asked = self.ask()
        self.answer(asked["question_id"])
        later = click._now() + timedelta(minutes=11)        # fire 11 min after the answer
        with mock.patch.object(click, "_now", return_value=later):
            self.assertEqual(self.fire(asked["request"])["state"], "expired")
        self.assertEqual(self.fake.fires(), [])

    def test_t7_second_fire_already_used(self):
        asked = self.ask()
        self.answer(asked["question_id"])
        self.assertEqual(self.fire(asked["request"])["state"], "done")
        again = self.fire(asked["request"])
        self.assertEqual((again["state"], again["reason"]), ("done", "already_used"))
        self.assertEqual(len(self.fake.fires()), 1)

    def test_t8_concurrent_fires_launch_once(self):
        asked = self.ask()
        self.answer(asked["question_id"])
        self.fake.delay = 0.3
        results, start = [], threading.Barrier(4)

        def go():
            with Store(self.home, run_id=self.run) as s:   # one connection per thread, like separate sessions
                start.wait(10)
                results.append(click.screen_click(s, asked["request"]))
        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(self.fake.fires()), 1)
        self.assertEqual(sorted(r.get("reason") or "pressed" for r in results),
                         ["already_used"] * 3 + ["pressed"])

    def test_t9_crash_after_cas_becomes_unknown_never_retried(self):
        asked = self.ask()
        self.answer(asked["question_id"])
        self.fake.fire = Crash()
        with self.assertRaises(Crash):
            click.screen_click(self.bound, asked["request"])
        self.assertEqual(self.record(asked["request"])["state"], "firing")
        self.fake.fire = dict(FIRED)
        self.assertEqual(self.fire(asked["request"])["reason"], "already_used")   # still fresh: maybe in flight
        later = click._now() + timedelta(minutes=3)
        with mock.patch.object(click, "_now", return_value=later):
            out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("unknown", "unknown"))
        self.assertEqual(self.fire(asked["request"])["reason"], "already_used")
        self.assertEqual(len(self.fake.fires()), 1)

    def test_t9_timeout_is_unknown_not_launched_is_failed(self):
        for error, state in ((glance.GlanceError("timeout", "t"), "unknown"),
                             (glance.GlanceError("no_answer", "n"), "unknown"),
                             (glance.GlanceError("busy", "b"), "failed")):
            with self.subTest(error=error.reason):
                asked = self.ask()
                self.answer(asked["question_id"])
                self.fake.fire = error
                self.assertEqual(self.fire(asked["request"])["state"], state)

    def test_t10_page_changed_and_not_front_press_nothing(self):
        for code, reason in ((6, "page_changed"), (9, "not_front")):
            with self.subTest(reason=reason):
                asked = self.ask()
                self.answer(asked["question_id"])
                self.fake.fire = {"ok": False, "exit": code, "pressed": False}
                out = self.fire(asked["request"])
                self.assertEqual((out["state"], out["reason"], out["pressed"]), ("failed", reason, False))
                self.assertIn("explanation", out)


class RefusalTests(ClickBase):
    def assertRefused(self, out, reason):
        self.assertEqual((out["state"], out["reason"]), ("refused", reason))
        self.assertEqual(self.questions(), [])
        self.assertEqual(self.fake.fires(), [])

    def test_t11_prepare_refusals_ask_nothing(self):
        alike = prepared(find={"match": "alike", "count": 1, "unique": True, "hits": [dict(HIT)]})
        many = prepared(find={"match": "exact", "count": 2, "unique": False, "hits": [dict(HIT), dict(HIT)]})
        hidden = prepared(find={"match": "exact", "count": 1, "unique": True, "hits": [dict(HIT, visible=False)]})
        off = prepared(find={"match": "exact", "count": 1, "unique": True, "hits": [dict(HIT, enabled=False)]})
        sheet = prepared(page={"site": ADDRESS, "sheetOpen": True})
        absent = {"ok": False, "exit": 1, "find": {"match": "none", "count": 0, "unique": False, "hits": []}}
        nobrowser = {"ok": False, "exit": 3}
        notoken = prepared(page_token="short")
        for report, reason in ((alike, "not_exact"), (many, "not_unique"), (hidden, "not_visible"),
                               (off, "disabled_control"), (sheet, "sheet_open"), (absent, "not_exact"),
                               (nobrowser, "no_browser"), (notoken, "bad_report")):
            with self.subTest(reason=reason):
                self.fake.prepare = report
                self.assertRefused(self.ask(), reason)

    def test_t12_irreversible_refused_outright_without_launch(self):
        for name in ("Place your order", "Buy now", "Send", "Delete", "Pay", "Donate", "Subscribe", "Book a table",
                     "Proceed to checkout", "Reserve", "I agree", "Accept all", "Transfer", "Post", "Reply  all"):
            with self.subTest(name=name):
                self.assertRefused(self.ask(name), "denied_word")
        self.assertEqual(self.fake.calls, [])

    def test_t12_noteling_guard_confirm_or_forbidden_refused(self):
        for guard in ("confirm", "forbidden", None):
            with self.subTest(guard=guard):
                self.fake.prepare = prepared(guard=guard)
                self.assertRefused(self.ask(), "guard" if guard else "bad_report")

    def test_deny_words_are_whole_words(self):
        for name in ("Add to cart", "Sender details", "Postcode lookup", "Bookmarks", "Placeholder"):
            self.assertFalse(click.denied(name), name)

    def test_one_open_request_per_home(self):
        self.ask()
        out = self.ask("Save for later")
        self.assertEqual((out["state"], out["reason"]), ("refused", "request_open"))
        self.assertEqual(len(self.questions()), 1)

    def test_no_default_human(self):
        write_humans(self.home, {"default_human": None, "senders": ["david"]})
        self.assertRefused(self.ask(), "no_human")
        self.assertEqual(self.fake.calls, [])

    def test_flag_shaped_values_refused(self):
        for args in ({"name": "--ask"}, {"name": "Add\nto cart"}, {"name": "Add", "role": "button x"},
                     {"name": "Add", "app": "not a bundle"}, {"name": "Add", "why": "x" * 201},
                     {"name": "Add", "why": "line\nbreak"}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.server.call("screen_click_request", dict({"why": "because"}, **args))
        self.assertEqual(self.fake.calls, [])

    def test_t13_question_ask_yes_grants_nothing(self):
        asked = self.ask()
        mine = self.server.call("question_ask", {"to": "david", "body": "May I press Add to cart?", "key": "k1"})
        self.answer(mine["id"])                       # David says yes to the employee's own question
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("asked", "waiting"))
        self.assertEqual(self.fake.fires(), [])
        self.assertNotIn("click_id", self.store._get("questions", mine["id"]))
        with self.assertRaises(ValueError):
            self.server.call("screen_click", {"request": mine["id"]})

    def test_t15_off_by_default(self):
        self.opt_in(False)
        listed = {t["name"] for t in self.server.respond({"jsonrpc": "2.0", "id": 1,
                                                          "method": "tools/list"})["result"]["tools"]}
        self.assertFalse(listed & click.NAMES)
        for name, args in (("screen_click_request", {"name": "Add to cart", "why": "x"}),
                           ("screen_click", {"request": "click-" + "0" * 32})):
            with self.assertRaisesRegex(ValueError, "Unknown tool"):
                self.server.call(name, args)
        (self.home / click.OPT_IN).write_text('{"enabled": "yes"}', encoding="utf-8")
        self.assertFalse(click.enabled(self.home))
        self.assertEqual(self.fake.calls, [])
        self.opt_in(True)
        listed = {t["name"] for t in self.server.respond({"jsonrpc": "2.0", "id": 2,
                                                          "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(listed & click.NAMES, click.NAMES)

    def test_t16_c1_uncorrelated_yes_cannot_bind(self):
        asked = self.ask()
        loose = self.answer(asked["question_id"], reply=False)    # David's authenticated "yes Add to cart", uncorrelated
        with self.assertRaisesRegex(ValueError, "click approval question"):
            self.server.call("question_resolve", {"question_id": asked["question_id"], "event_id": loose["id"]})
        with self.assertRaisesRegex(ValueError, "click approval question"):
            self.server.call("question_close", {"question_id": asked["question_id"], "reason": "x",
                                                "event_id": loose["id"]})
        self.assertEqual(self.store._get("questions", asked["question_id"])["status"], "open")
        self.assertEqual(self.fire(asked["request"])["reason"], "waiting")
        self.assertEqual(self.fake.fires(), [])

    def test_t16_engine_rechecks_correlation_even_if_a_record_says_answered(self):
        # Defence in depth: a record answered by an uncorrelated message (an older engine's question_resolve, or a
        # direct write) still never approves; screen_click reads reply_to from the answer event itself.
        for resolved in (False, True):
            with self.subTest(resolved_by_run=resolved):
                asked = self.ask()
                loose = self.answer(asked["question_id"], reply=False)
                question = self.store._get("questions", asked["question_id"])
                question.update(status="answered", answer_event_id=loose["id"], answered_at=loose["created_at"])
                if resolved:
                    question["resolved_by_run"] = self.run
                self.store.db.execute("UPDATE questions SET data=? WHERE id=?", (json.dumps(question), question["id"]))
                out = self.fire(asked["request"])
                self.assertEqual((out["state"], out["reason"]), ("declined", "not_approved"))
        self.assertEqual(self.fake.fires(), [])

    def test_t16_ordinary_questions_still_resolve(self):
        mine = self.server.call("question_ask", {"to": "david", "body": "Lunch?", "key": "k2"})
        loose = self.answer(mine["id"], body="sure", reply=False)
        self.assertEqual(self.server.call("question_resolve", {"question_id": mine["id"],
                                                               "event_id": loose["id"]})["status"], "answered")

    def test_withdrawn_click_question_declines(self):
        asked = self.ask()
        self.server.call("question_close", {"question_id": asked["question_id"], "reason": "not needed"})
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("declined", "not_approved"))


class LeakTests(ClickBase):
    def test_t14_no_page_words_in_home_or_status_or_lookout(self):
        refused = prepared(find={"match": "alike", "count": 1, "unique": True, "hits": [dict(HIT)]})
        self.fake.prepare = refused
        self.ask()
        self.fake.prepare = prepared()
        asked = self.ask()
        self.answer(asked["question_id"])
        self.fire(asked["request"])
        sharedstatus.write(self.bound)
        self.store.close()
        self.bound.close()
        blobs = {}
        for path in self.home.rglob("*"):
            if path.is_file():
                blobs[str(path)] = path.read_bytes()
        html = lookout.team_page(self.home.parent, agents={})
        blobs["lookout"] = html.encode("utf-8")
        for name, data in blobs.items():
            for word in PAGE_WORDS:
                self.assertNotIn(word.encode("utf-8"), data, "%s leaked into %s" % (word, name))
        self.assertIn(b"www.example-shop.test", blobs["lookout"])
        self.assertIn(b"Recent clicks", blobs["lookout"])
        status = json.loads((self.home / "shared" / "status.json").read_text())
        self.assertEqual(status["clicks"][0]["state"], "done")
        for row in status["clicks"]:
            self.assertFalse({"token", "page_token", "hit_token", "why"} & set(row))


if __name__ == "__main__":
    unittest.main()

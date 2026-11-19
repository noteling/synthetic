"""Issue #17 Ledgerline E2 (`export runs`) and E3 (`routine verify RUN`), with Codex's C1-C5 (event-5ced3aac)."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from synthetics.cli import main
from synthetics.mcp import Server
from synthetics.store import Store
from tests import test_authority as _warrant  # module import: a bare WarrantTest name would be collected here

try:
    from synthetics import ledgerline
except ImportError:  # pragma: no cover - fail-first on main
    ledgerline = None

EPOCH = "1970-01-01T00:00:00Z"


def ts(minute, second=0):
    return "2026-11-19T12:%02d:%02d.000000Z" % (minute, second)


def owner_env():
    return {k: v for k, v in os.environ.items() if k not in ("SYNTHETICS_SESSION_RUN", "SYNTHETICS_HOME")}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, owner_env(), clear=True), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def home_bytes(home):
    """Every file's sha256, except the SQLite -shm shared-memory index: any WAL reader may rewrite it, and the
    CLT Python 3.9 sqlro fallback (sqlite 3.51, backlog 40) does. It holds no persistent state."""
    found = {}
    for path in sorted(Path(home).rglob("*")):
        if path.is_file() and not path.name.endswith("-shm"):
            found[str(path.relative_to(home))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "employee"
        self.store = Store(self.home)
        self.identity = self.store.initialize("Morgan", "maintainer")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()


class Export(HomeCase):
    def session_calls(self):
        run = self.store.run_begin()
        event = self.store.feed("priya", "PRIVATE BODY words")
        with Store(self.home, run_id=run["id"]) as bound:
            server = Server(bound, run_id=run["id"])
            server.call("event_search", {"query": "PRIVATE"})
            server.call("work_put", {"title": "SECRET TITLE", "note": "SECRET NOTE", "description": "SECRET DESC",
                                     "key": "w1"})
            server.call("self_note", {"section": "how-i-work", "text": "SELF TEXT", "source": "s", "key": "n1"})
            server.call("event_get", {"id": event["id"]})
            with self.assertRaises(ValueError):
                server.call("event_get", {"id": "event-" + "0" * 32})
        self.store.run_finish(run["id"], "ok")
        return run, event

    def exported(self, *extra):
        out = Path(self.temporary.name) / "export.jsonl"
        code, text, err = run_cli("--home", str(self.home), "export", "runs", "--out", str(out), *extra)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        return out.read_text(encoding="utf-8"), [json.loads(line) for line in out.read_text().splitlines()]

    def test_allow_list_hashes_free_text_and_keeps_ids(self):
        run, event = self.session_calls()
        raw, lines = self.exported("--audit")
        for secret in ("PRIVATE BODY", "SECRET TITLE", "SECRET NOTE", "SECRET DESC", "SELF TEXT"):
            self.assertNotIn(secret, raw)
        calls = [l for l in lines if l["kind"] == "mcp.call"]
        self.assertEqual([c["tool"] for c in calls], ["event_search", "work_put", "self_note", "event_get", "event_get"])
        self.assertEqual(calls[0]["args"]["query"], {"bytes": 7, "sha256": hashlib.sha256(b"PRIVATE").hexdigest()})
        self.assertEqual(calls[1]["args"]["title"]["bytes"], len("SECRET TITLE"))
        self.assertEqual(calls[1]["args"]["key"], {"bytes": 2, "sha256": hashlib.sha256(b"w1").hexdigest()})  # r2 n2
        self.assertEqual(calls[3]["args"]["id"], event["id"])
        self.assertEqual(calls[3]["result"]["id"], event["id"])
        self.assertFalse(calls[4]["success"])
        self.assertTrue("error_code" in calls[4] or "error" in calls[4])
        # self_note: hashed only in its already-redacted form.
        receipt = json.loads((self.home / "runs" / (run["id"] + ".mcp.jsonl")).read_text().splitlines()[2])
        self.assertEqual(calls[2]["args_sha256"], ledgerline.sha256_text(ledgerline.canonical(receipt["arguments"])))
        self.assertNotEqual(calls[2]["args_sha256"], ledgerline.sha256_text(ledgerline.canonical(
            {"section": "how-i-work", "text": "SELF TEXT", "source": "s", "key": "n1"})))
        runs = [l for l in lines if l["kind"] == "run"]
        self.assertEqual(runs[0]["receipt_chain"]["lines"], 5)
        audits = [l for l in lines if l["kind"] == "audit"]
        works = [a for a in audits if a["action"].startswith("work.")]
        self.assertTrue(works)
        for row in works:
            self.assertTrue(set(row["fields"]) <= {"id", "status", "priority", "wake_at", "waiting_question_ids"})
        received = [a for a in audits if a["action"] == "event.received"]
        self.assertEqual(received[0]["fields"]["id"], event["id"])
        self.assertNotIn("body", received[0]["fields"])
        finished = [a for a in audits if a["action"] == "run.finished"]
        self.assertEqual(finished[0]["fields"]["receipt_chain"]["lines"], 5)

    def test_unknown_argument_and_action_export_only_hashes(self):
        projected = ledgerline.project_args({"mystery": "free words", "nested": {"a": 1}, "id": "work-1"})
        self.assertEqual(set(projected["mystery"]), {"bytes", "sha256"})
        self.assertEqual(set(projected["nested"]), {"bytes", "sha256"})
        self.assertEqual(projected["id"], "work-1")
        line = ledgerline.export_audit("h", {"seq": 1, "at": "t", "action": "brand.new", "record_id": "r",
                                            "data": json.dumps({"body": "x"})})
        self.assertEqual(set(line), {"kind", "home_id", "seq", "at", "action", "record_id", "data_sha256"})

    def test_static_no_credential_field_is_allow_listed(self):
        pattern = re.compile(ledgerline.CREDENTIAL_NAMES)
        names = set(ledgerline.ARG_KEEP)
        for _, fields in ledgerline.AUDIT_FIELDS:
            names.update(fields)
        self.assertEqual([n for n in names if pattern.search(n)], [])
        self.assertEqual(ledgerline.ARG_KEEP & ledgerline.ARG_TEXT, frozenset())

    def test_export_and_verify_leave_home_bytes_unchanged(self):
        run, _ = self.session_calls()
        self.store.close()
        before = home_bytes(self.home)
        out = Path(self.temporary.name) / "x.jsonl"
        self.assertEqual(run_cli("--home", str(self.home), "export", "runs", "--audit", "--out", str(out))[0], 0)
        self.assertEqual(run_cli("--home", str(self.home), "export", "runs", "--run", run["id"])[0], 0)
        self.assertEqual(run_cli("--home", str(self.home), "routine", "verify", run["id"])[0], 0)
        after = home_bytes(self.home)
        # C5 (Codex event-425a18b4): the main db, its -wal and every other file are byte-compared; only -shm is
        # skipped (home_bytes). A -wal absent before must be absent or empty after.
        self.assertIn("state.sqlite3", before)
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        empty = hashlib.sha256(b"").hexdigest()
        for name in [k for k in after if k not in before]:
            self.assertTrue(name.endswith("-wal") and after[name] == empty, "new non-empty file %s" % name)
        self.store = Store(self.home)


class Verify(HomeCase):
    """E3 fixtures: consistent, since jump, spilled page (run-30546289 shape), truncated body, missing evidence."""

    def dream(self, receipts, through, omissions=(), previous="unset", cutoff=ts(50), rearm=True, finish=True):
        run = self.store.run_begin()
        work = "work-" + "d" * 32
        with self.store._tx():
            if previous != "unset":
                self.store._audit("routine.rearmed", work, {"id": work, "routine": {
                    "kind": "dream", "covered_through": previous, "rearmed_by_run": "run-earlier"}})
            if rearm:
                self.store._audit("routine.rearmed", work, {"id": work, "routine": {
                    "kind": "dream", "covered_through": through, "rearmed_by_run": run["id"],
                    "coverage": {"run": run["id"], "cutoff": cutoff, "through": through, "advanced": True,
                                 "omissions": [{"reason": "r", "from": f} for f in omissions]}}})
        if receipts is not None:
            folder = self.home / "runs"
            folder.mkdir(exist_ok=True)
            with open(str(folder / (run["id"] + ".mcp.jsonl")), "w") as handle:
                for tool, arguments, result in receipts:
                    handle.write(json.dumps({"tool": tool, "arguments": arguments, "success": True,
                                             "result": result}) + "\n")
        verdict = ledgerline.verify_dream(self.home, run["id"])
        if finish:
            self.store.run_finish(run["id"], "dream")
        return verdict, run

    @staticmethod
    def page(since, first, last, capped, size=1000, truncated=None, cursor=None, next_cursor="c1"):
        arguments = {"since": since} if cursor is None else {"cursor": cursor}
        result = {"bytes": size, "sha256": "0" * 64, "ids": ["x"], "count": 1, "created_first": first,
                  "created_last": last, "capped": capped, "next_cursor": next_cursor if capped else None}
        if truncated:
            result["truncated"] = truncated
        return arguments, result

    def both(self, events, memories):
        return [("event_search",) + e for e in events] + [("memory_search",) + m for m in memories]

    def test_consistent_full_read(self):
        events = [self.page(EPOCH, ts(1), ts(5), True), self.page(None, ts(6), ts(9), False, cursor="c1")]
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        verdict, _ = self.dream(self.both(events, memories), through=ts(50))
        self.assertEqual(verdict["verdict"], "consistent", verdict)
        self.assertEqual(verdict["previous"], "1970-01-01T00:00:00.000000Z")  # C3: first dream starts at 1970
        self.assertIn("predicted, not observed", verdict["spill_check"])  # C2

    def test_gap_via_since_jump_is_overclaimed(self):
        events = [self.page(EPOCH, ts(1), ts(5), True), self.page(ts(20), ts(20), ts(30), False)]
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        verdict, _ = self.dream(self.both(events, memories), through=ts(30))
        self.assertEqual(verdict["verdict"], "overclaimed", verdict)
        self.assertEqual(verdict["first_unread"], ts(5))

    def run_30546289_shape(self, omissions):
        first_event = "2026-11-15T20:57:36.063976Z"
        events = [self.page(EPOCH, first_event, "2026-11-16T01:00:00Z", True, size=96054)]
        memories = [self.page(EPOCH, "2026-11-15T20:57:36Z", "2026-11-16T18:07:21Z", True, size=25000)]
        return self.dream(self.both(events, memories), through=first_event, omissions=omissions,
                          cutoff="2026-11-19T12:44:47.677863Z")[0]

    def test_run_30546289_shape_is_possibly_spilled_without_its_omission(self):
        verdict = self.run_30546289_shape(omissions=())
        self.assertEqual(verdict["verdict"], "possibly_spilled", verdict)
        self.assertNotEqual(verdict["verdict"], "consistent")
        self.assertEqual(verdict["unrecorded"][0]["bytes"], 96054)

    def test_run_30546289_shape_with_recorded_omission_is_consistent(self):
        verdict = self.run_30546289_shape(omissions=("2026-11-15T20:57:36.063976Z", "2026-11-16T18:09:02.030321Z"))
        self.assertEqual(verdict["verdict"], "consistent", verdict)

    def test_b1_spill_then_reread_same_since_is_consistent(self):
        # Rowan's probe P1: the guidance re-reads a spilled range with a smaller max_bytes.
        events = [self.page(EPOCH, ts(1), ts(9), False, size=40000),
                  self.page(EPOCH, ts(1), ts(9), False, size=20000)]
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        verdict, _ = self.dream(self.both(events, memories), through=ts(50))
        self.assertEqual(verdict["verdict"], "consistent", verdict)
        self.assertEqual(verdict["streams"]["events"]["healed"][0]["line"], 1)

    def test_b1_spill_then_reread_same_cursor_is_consistent(self):
        # Rowan's probe P2: the spilled page was a cursor page; the re-read names the same cursor.
        events = [self.page(EPOCH, ts(1), ts(5), True, next_cursor="c1"),
                  self.page(None, ts(6), ts(9), False, cursor="c1", size=40000),
                  self.page(None, ts(6), ts(9), False, cursor="c1", size=20000)]
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        verdict, _ = self.dream(self.both(events, memories), through=ts(50))
        self.assertEqual(verdict["verdict"], "consistent", verdict)

    def test_b1_unhealed_spill_still_bounds_the_chain(self):
        # A re-read that is itself spilled heals nothing; the break stays and through past it is overclaimed.
        events = [self.page(EPOCH, ts(1), ts(9), False, size=40000),
                  self.page(EPOCH, ts(1), ts(9), False, size=39000)]
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        verdict, _ = self.dream(self.both(events, memories), through=ts(50))
        self.assertEqual(verdict["verdict"], "overclaimed", verdict)
        self.assertEqual(verdict["first_unread"], ts(1))
        self.assertNotIn("healed", verdict["streams"]["events"])

    def test_spill_threshold_is_configurable(self):
        events = [self.page(EPOCH, ts(1), ts(9), False, size=40000)]
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        receipts = self.both(events, memories)
        verdict, run = self.dream(receipts, through=ts(50))
        self.assertEqual(verdict["verdict"], "overclaimed")
        relaxed = ledgerline.verify_dream(self.home, run["id"], spill_bytes=50000)
        self.assertEqual(relaxed["verdict"], "consistent", relaxed)
        code, text, err = run_cli("--home", str(self.home), "routine", "verify", run["id"], "--spill-bytes", "50000")
        self.assertEqual((code, json.loads(text)["verdict"]), (0, "consistent"), err)

    def truncated_receipts(self, chunks, get_bytes=10):
        item = {"id": "event-" + "a" * 32, "created_at": ts(3), "body_len": 100}
        events = [self.page(EPOCH, ts(1), ts(9), False, truncated=[item])]
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        receipts = self.both(events, memories)
        for begin, size in chunks:
            receipts.append(("event_get", {"id": item["id"], "body_offset": begin},
                             {"bytes": get_bytes, "sha256": "0" * 64, "id": item["id"], "body_len": 100,
                              "body_offset": begin, "returned_chars": size}))
        return receipts

    def test_truncated_body_unread_bounds_the_chain(self):
        verdict, _ = self.dream(self.truncated_receipts([(0, 60)]), through=ts(50))
        self.assertEqual(verdict["verdict"], "overclaimed", verdict)
        self.assertEqual(verdict["first_unread"], ts(3))
        recorded, _ = self.dream(self.truncated_receipts([]), through=ts(3), omissions=(ts(3),))
        self.assertEqual(recorded["verdict"], "consistent", recorded)

    def test_truncated_body_read_in_chunks_is_consistent(self):
        verdict, _ = self.dream(self.truncated_receipts([(0, 60), (60, 40)]), through=ts(50))
        self.assertEqual(verdict["verdict"], "consistent", verdict)

    def test_g2_spilled_event_get_covers_nothing(self):
        verdict, _ = self.dream(self.truncated_receipts([(0, 100)], get_bytes=90000), through=ts(50))
        self.assertEqual(verdict["verdict"], "overclaimed", verdict)
        self.assertEqual(verdict["first_unread"], ts(3))
        self.assertEqual(verdict["spilled_event_get"], [{"id": "event-" + "a" * 32, "bytes": 90000}])

    def test_g4_unread_from_printed_on_breaks_and_streams(self):
        arguments, result = self.page(EPOCH, ts(1), ts(5), True, size=40000)
        result["unread_from"] = ts(6)
        memories = [self.page(EPOCH, ts(1), ts(8), False)]
        verdict, _ = self.dream(self.both([(arguments, result)], memories), through=ts(1), omissions=(ts(1),))
        self.assertEqual(verdict["verdict"], "consistent", verdict)
        arguments2, result2 = self.page(EPOCH, ts(1), ts(5), True)
        result2["unread_from"] = ts(6)
        verdict2, _ = self.dream(self.both([(arguments2, result2)], memories), through=ts(5), omissions=(ts(6),))
        self.assertEqual(verdict2["streams"]["events"]["unread_from"], ts(6))
        spilled, _ = self.dream(self.both([(arguments, result)], memories), through=ts(50))
        self.assertEqual(spilled["breaking"]["unread_from"], ts(6), spilled)

    def test_g1_scope_is_this_runs_own_reads(self):
        verdict, _ = self.dream(self.both([], [self.page(EPOCH, ts(1), ts(8), False)]), through=ts(50))
        self.assertEqual(verdict["verdict"], "overclaimed", verdict)  # no event reads in this run
        self.assertIn("own receipts only", verdict["scope"])

    def test_previous_checkpoint_is_the_start(self):
        events = [self.page(ts(10), ts(10), ts(20), False)]
        memories = [self.page(ts(10), ts(10), ts(20), False)]
        verdict, _ = self.dream(self.both(events, memories), through=ts(50), previous=ts(10))
        self.assertEqual(verdict["verdict"], "consistent", verdict)
        self.assertEqual(verdict["previous"], ts(10))
        behind, _ = self.dream(self.both(events, memories), through=ts(50), previous=ts(5))
        self.assertEqual(behind["verdict"], "overclaimed", behind)

    def test_missing_evidence(self):
        verdict, _ = self.dream(None, through=ts(50))
        self.assertEqual(verdict["verdict"], "no_evidence")
        no_rearm, _ = self.dream([], through=ts(50), rearm=False)  # C3: no routine.rearmed row
        self.assertEqual(no_rearm["verdict"], "no_evidence")
        old = self.dream([("event_search", {"since": EPOCH}, None), ("memory_search", {"since": EPOCH}, None)],
                         through=ts(50))[0]
        self.assertEqual(old["verdict"], "no_evidence")

    def test_chain_status_reported(self):
        verdict, run = self.dream([], through=None, finish=False)
        self.assertEqual(verdict["chain"]["chain"], "unanchored")  # still running, nothing anchored
        self.assertIn("receipt_chain", verdict["chain_note"])


class Fence(_warrant.WarrantTest):
    """C5: export runs and routine verify are fenced exactly like doctor after G4; refusals audited."""

    def fence_rows(self, name, what):
        return [r for r in self.audit(name, "authority.refused") if what in json.dumps(r)]

    def test_session_other_home_refused_and_audited(self):
        out = str(self.root / "o.jsonl")
        for argv, what in ((("export", "runs", "--out", out), "export runs"),
                           (("routine", "verify", "run-x"), "routine verify")):
            self.refused("own_home_only", "rowan", *argv, session="vesper")
            self.assertTrue(self.fence_rows("vesper", what), what)
        self.assertFalse(os.path.exists(out))

    def test_session_own_home_and_owner_allowed(self):
        out = str(self.root / "own.jsonl")
        self.ok("vesper", "export", "runs", "--out", out, session="vesper")
        self.ok("rowan", "export", "runs", "--out", str(self.root / "owner.jsonl"))
        code, verdict, err = self.cli("vesper", "routine", "verify", self.runs["vesper"], session="vesper")
        self.assertEqual(code, 0, err)
        self.assertEqual(verdict["verdict"], "no_evidence")

    def test_covering_delegation_allows_like_doctor(self):
        self.root_delegation(actions=["worker.manage"], scope={"worker.manage": {"homes": ["employees"], "ops": ["restart"]}})
        self.ok("rowan", "export", "runs", "--out", str(self.root / "d.jsonl"), session="codex")


# Run only Fence's own tests here, not every inherited WarrantTest test again (as in test_g4_doctor_fence).
for _name in dir(_warrant.WarrantTest):
    if _name.startswith("test_") and _name not in Fence.__dict__:
        setattr(Fence, _name, None)


if __name__ == "__main__":
    unittest.main()

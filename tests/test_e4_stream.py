"""E4 Ledgerline executor stream (Codex event-8b40b227, conditions C1-C6): receipt join key, stream reducer,
json fallback switch, timeout, observed spills in routine verify, export allow-list."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from synthetics import ledgerline, runtime
from synthetics.brain import BrainError, ClaudeBrain
from synthetics.mcp import Server
from synthetics.store import Store
from tests.test_i17_export_verify import EPOCH, HomeCase, home_bytes, run_cli, ts

MARKERS = ("MARK-CWD", "MARK-SESSION", "MARK-SOCKET", "MARK-MEMPATH", "MARK-BODY", "MARK-SAVED", "MARK-PROSE",
           "MARK-THINK", "MARK-INPUT", "MARK-CRED")
SPILL = ("Error: result (96,210 characters across 1 line) exceeds maximum allowed tokens. Output has been saved to "
         "/Users/x/.claude/projects/MARK-SAVED/tool-results/blob.txt\nUse offset and limit to read it.")
PREVIEW = "<persisted-output>\nOutput too large (58.6KB). Full output saved to: /tmp/MARK-SAVED.txt\nPreview: MARK-BODY"


def stream_lines(body="MARK-BODY full page"):
    """Probe-shaped stream-json lines (reviews/E4/probe run1/run2), with marker strings in every private field."""
    init = {"type": "system", "subtype": "init", "cwd": "/tmp/MARK-CWD", "original_cwd": "/tmp/MARK-CWD",
            "session_id": "MARK-SESSION", "messaging_socket_path": "/tmp/MARK-SOCKET",
            "memory_paths": ["/Users/x/MARK-MEMPATH"], "additional_directories": ["/MARK-CWD"],
            "credential_expires_at": "MARK-CRED", "uuid": "MARK-SESSION",
            "tools": ["Bash", "mcp__synthetics__event_search"], "model": "claude-opus-5-5",
            "claude_code_version": "2.1.326", "mcp_servers": [{"name": "synthetics", "status": "connected",
                                                                 "source": "MARK-CWD"}]}
    use = lambda i, name: {"type": "assistant", "session_id": "MARK-SESSION", "message": {"content": [
        {"type": "tool_use", "id": "toolu_%d" % i, "name": name, "input": {"q": "MARK-INPUT"}}]}}
    shown = lambda i, content, **kw: {"type": "user", "session_id": "MARK-SESSION", "message": {"content": [
        dict({"tool_use_id": "toolu_%d" % i, "type": "tool_result", "content": content}, **kw)]},
        "tool_use_result": {"stdout": "MARK-BODY", "persistedOutputPath": "/MARK-SAVED"}}
    lines = [init,
             use(1, "mcp__synthetics__event_search"), shown(1, [{"type": "text", "text": body}]),
             use(2, "mcp__synthetics__event_search"), shown(2, SPILL),
             use(3, "Bash"), shown(3, PREVIEW, is_error=False),
             use(4, "Bash"), shown(4, "MARK-BODY exit 1", is_error=True),
             use(5, "Read"), shown(5, [{"type": "image", "source": "MARK-BODY"}]),
             {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "MARK-THINK"},
                                                           {"type": "text", "text": "MARK-PROSE done"}]}},
             {"type": "rate_limit_event", "session_id": "MARK-SESSION"},
             {"type": "result", "subtype": "success", "is_error": False, "result": "Saved the next step.",
              "total_cost_usd": 0.02, "num_turns": 6, "duration_ms": 1234, "session_id": "MARK-SESSION"}]
    return [json.dumps(line) + "\n" for line in lines]


class ReceiptJoinKey(HomeCase):
    """Test 1 (E4a): the receipt records the client's tool_use_id; the response bytes don't change."""

    def call(self, server, meta):
        params = {"name": "work_list", "arguments": {}}
        if meta is not None:
            params["_meta"] = meta
        return json.dumps(server.respond({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}))

    def test_tool_use_id_recorded_and_response_identical(self):
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as bound:
            server = Server(bound, run_id=run["id"])
            plain = self.call(server, None)
            joined = self.call(server, {"claudecode/toolUseId": "toolu_015kBt5EinsCzHWxDUsaqvvS"})
            bad = self.call(server, {"claudecode/toolUseId": "../../etc MARK"})
        self.assertEqual(plain, joined)
        self.assertEqual(plain, bad)
        receipts = [json.loads(l) for l in (self.home / "runs" / (run["id"] + ".mcp.jsonl")).read_text().splitlines()]
        self.assertNotIn("tool_use_id", receipts[0])
        self.assertEqual(receipts[1]["tool_use_id"], "toolu_015kBt5EinsCzHWxDUsaqvvS")
        self.assertNotIn("tool_use_id", receipts[2])  # an id that is not a plain token is dropped
        self.store.run_finish(run["id"], "ok")


class Reducer(unittest.TestCase):
    """Test 2 (E4b, C2): shapes, reported_chars, nothing private in stream.jsonl, static allowed keys."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "run-1.stream.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def reduce(self, lines):
        reducer = ledgerline.StreamReducer(self.path)
        for line in lines:
            reducer.feed(line)
        reducer.close()
        return reducer, [json.loads(l) for l in self.path.read_text().splitlines()]

    def test_shapes_and_facts(self):
        reducer, facts = self.reduce(stream_lines())
        shown = {f["tool_use_id"]: f for f in facts if f["kind"] == "displayed"}
        self.assertEqual({k: v["shape"] for k, v in shown.items()},
                         {"toolu_1": "full", "toolu_2": "spilled", "toolu_3": "preview", "toolu_4": "error",
                          "toolu_5": "unknown"})
        self.assertEqual(shown["toolu_1"]["sha256"], hashlib.sha256(b"MARK-BODY full page").hexdigest())
        self.assertEqual(shown["toolu_2"]["reported_chars"], 96210)
        self.assertTrue(shown["toolu_2"]["saved_outside_home"])
        self.assertNotIn("sha256", shown["toolu_5"])
        init = facts[0]
        self.assertEqual(init["kind"], "init")
        self.assertEqual(init["mcp_servers"], [{"name": "synthetics", "status": "connected"}])
        turn = [f for f in facts if f["kind"] == "turn"]
        self.assertEqual(turn, [dict(turn[0], text_blocks=1, text_bytes=len("MARK-PROSE done"))])
        result = facts[-1]
        self.assertEqual({k: result[k] for k in ("subtype", "is_error", "num_turns", "total_cost_usd")},
                         {"subtype": "success", "is_error": False, "num_turns": 6, "total_cost_usd": 0.02})
        self.assertEqual(json.loads(reducer.output_text())["result"], "Saved the next step.")

    def test_nothing_private_reaches_the_file(self):
        self.reduce(stream_lines())
        text = self.path.read_text()
        for marker in MARKERS:
            self.assertNotIn(marker, text)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_written_keys_are_the_allowed_keys(self):
        _, facts = self.reduce(stream_lines())
        for fact in facts:
            self.assertLessEqual(set(fact), ledgerline.STREAM_ALLOWED[fact["kind"]] | ledgerline.STREAM_COMMON, fact)

    def test_static_forbidden_names_are_never_allowed(self):
        # C2: session_id, cwd, the socket path, credential_expires_at and memory_paths (and text) never reach it.
        allowed = set().union(*ledgerline.STREAM_ALLOWED.values()) | ledgerline.STREAM_COMMON
        for name in ledgerline.STREAM_FORBIDDEN:
            self.assertNotIn(name, allowed)
        for name in ("session_id", "cwd", "messaging_socket_path", "credential_expires_at", "memory_paths"):
            self.assertIn(name, ledgerline.STREAM_FORBIDDEN)

    def test_chain_links_every_line(self):
        self.reduce(stream_lines())
        raws = self.path.read_text().splitlines()
        previous = None
        for raw in raws:
            self.assertEqual(json.loads(raw)["prev_sha256"], previous)
            previous = hashlib.sha256(raw.encode()).hexdigest()

    def test_reducer_failure_is_recorded_and_result_kept(self):
        with mock.patch.object(ledgerline.StreamReducer, "facts", side_effect=RuntimeError("MARK-BODY")):
            reducer, facts = self.reduce(stream_lines())
        self.assertTrue(facts and all(f["kind"] == "stream_error" for f in facts))
        self.assertEqual(json.loads(reducer.output_text())["total_cost_usd"], 0.02)
        self.assertNotIn("MARK-BODY", self.path.read_text())


FAKE = r'''
import json, sys, time
args = sys.argv[1:]
sys.stdin.read()
mode = args[args.index("--output-format") + 1]
result = json.loads(%(result)r)
if mode == "stream-json":
    assert "--verbose" in args
    for line in json.loads(%(lines)r):
        sys.stdout.write(line)
        sys.stdout.flush()
    time.sleep(%(sleep)r)
    print(json.dumps(result))
else:
    assert "--verbose" not in args
    print(json.dumps(result))
'''


class Brain(unittest.TestCase):
    """Test 3 (C1): json and stream-json give identical results; stream_output false is the old path."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "employee"
        self.home.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def fake(self, result, sleep=0.0, lines=None):
        path = Path(self.temporary.name) / "fake-claude"
        body = FAKE % {"result": json.dumps(result), "sleep": sleep,
                       "lines": json.dumps(stream_lines()[:-1] if lines is None else lines)}
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o700)
        return str(path)

    def outcome(self, executable, stream, run_id, timeout=60):
        brain = ClaudeBrain(self.home, executable=executable, timeout=timeout, stream_output=stream)
        try:
            result = brain.run({"run": {"id": run_id}})
            return ("ok", result.summary, result.cost, result.raw)
        except BrainError as error:
            return ("error", str(error).replace(run_id, "RUN"), error.cost)

    CASES = {
        "success": {"type": "result", "subtype": "success", "is_error": False, "result": "Saved.", "total_cost_usd": 0.5},
        "budget": {"type": "result", "subtype": "error_max_budget_usd", "is_error": False, "total_cost_usd": 3.05},
        "is_error": {"type": "result", "subtype": "success", "is_error": True, "result": "x", "total_cost_usd": 0.1},
        "no_summary": {"type": "result", "subtype": "success", "is_error": False, "result": " ", "total_cost_usd": 0.2},
    }

    def test_identical_results_json_and_stream(self):
        for name, result in self.CASES.items():
            with self.subTest(case=name):
                executable = self.fake(result)
                self.assertEqual(self.outcome(executable, False, "run-j" + name),
                                 self.outcome(executable, True, "run-s" + name))
                self.assertFalse((self.home / "runs" / ("run-j%s.stream.jsonl" % name)).exists())
                self.assertTrue((self.home / "runs" / ("run-s%s.stream.jsonl" % name)).exists())

    def test_reducer_forced_to_raise_changes_nothing(self):
        executable = self.fake(self.CASES["success"])
        expected = self.outcome(executable, False, "run-a")
        with mock.patch.object(ledgerline.StreamReducer, "facts", side_effect=RuntimeError("boom")):
            self.assertEqual(self.outcome(executable, True, "run-b"), expected)

    def test_unopenable_stream_file_changes_nothing(self):
        executable = self.fake(self.CASES["success"])
        expected = self.outcome(executable, False, "run-c")
        (self.home / "runs" / "run-d.stream.jsonl").mkdir(parents=True)  # a directory: open() fails
        self.assertEqual(self.outcome(executable, True, "run-d"), expected)

    def test_stream_output_false_is_the_pre_e4_command_and_files(self):
        brain = ClaudeBrain(self.home, executable="claude", stream_output=False)
        streaming = ClaudeBrain(self.home, executable="claude")
        old = brain.command("run-1")
        new = streaming.command("run-1")
        self.assertEqual(old[old.index("--output-format") + 1], "json")
        self.assertNotIn("--verbose", old)
        at = new.index("--output-format")
        self.assertEqual(new[:at + 1] + ["json"] + new[at + 3:], old)
        # The json path writes stdout byte for byte to claude.json, as b09f5a5 did.
        executable = self.fake(self.CASES["success"])
        brain = ClaudeBrain(self.home, executable=executable, stream_output=False)
        brain.run({"run": {"id": "run-old"}})
        self.assertEqual((self.home / "runs" / "run-old.claude.json").read_text(),
                         json.dumps(self.CASES["success"]) + "\n")
        self.assertEqual(sorted(p.name for p in (self.home / "runs").iterdir()),
                         ["run-old.claude.json", "run-old.stderr.txt"])

    def test_stream_claude_json_is_the_result_line_only(self):
        executable = self.fake(self.CASES["success"])
        ClaudeBrain(self.home, executable=executable).run({"run": {"id": "run-new"}})
        self.assertEqual((self.home / "runs" / "run-new.claude.json").read_text(),
                         json.dumps(self.CASES["success"]) + "\n")
        for path in (self.home / "runs").iterdir():
            for marker in MARKERS:
                self.assertNotIn(marker, path.read_text(), path.name)

    def test_timeout_mid_stream_keeps_partial_facts_and_no_raw_file(self):
        # Test 4: a session cut by the timeout leaves the facts it produced, an intact chain, and no raw stream.
        executable = self.fake(self.CASES["success"], sleep=30, lines=stream_lines()[:5])
        with self.assertRaisesRegex(BrainError, "exceeded"):
            ClaudeBrain(self.home, executable=executable, timeout=3).run({"run": {"id": "run-t"}})
        folder = self.home / "runs"
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["run-t.claude.json", "run-t.stderr.txt", "run-t.stream.jsonl"])
        facts = [json.loads(l) for l in (folder / "run-t.stream.jsonl").read_text().splitlines()]
        self.assertEqual([f["kind"] for f in facts], ["init", "tool_use", "displayed", "tool_use", "displayed"])
        self.assertEqual(ledgerline.check_chain(folder / "run-t.stream.jsonl", None)["chain"], "unanchored")
        for path in folder.iterdir():
            for marker in MARKERS:
                self.assertNotIn(marker, path.read_text(), path.name)


class RuntimeSwitch(unittest.TestCase):
    """C1: stream_output is optional in runtime.json; a config without it keeps its digest and is valid."""

    def config(self, **extra):
        claude = os.path.realpath("/bin/sh")
        return dict({"claude": claude, "model": "claude-opus-5-5", "budget": 10.0, "timeout": 600.0, "poll": 2.0,
                     "retry": 60.0, "daily_limit": 0.0, "limit_notify": None, "limit_allow": []}, **extra)

    def test_absent_is_default_true_and_digest_unchanged(self):
        config = runtime.validate(self.config())
        self.assertNotIn("stream_output", config)
        self.assertTrue(runtime.setting(config, "stream_output"))
        expected = hashlib.sha256(json.dumps({k: config[k] for k in runtime.FIELDS}, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(runtime.digest(config), expected)

    def test_false_round_trips(self):
        config = runtime.validate(self.config(stream_output=False))
        self.assertIs(runtime.setting(config, "stream_output"), False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.json"
            runtime.write_atomic(path, config)
            self.assertIs(json.loads(path.read_text())["stream_output"], False)
        self.assertEqual(runtime.parse_assignments(["stream_output=false"]), {"stream_output": False})
        with self.assertRaises(ValueError):
            runtime.validate(self.config(stream_output="no"))

    def test_engine_applies_it_to_the_brain(self):
        from synthetics.engine import Engine
        with tempfile.TemporaryDirectory() as folder:
            brain = ClaudeBrain(folder, executable="claude")
            engine = Engine(folder, brain)
            engine.apply_runtime(runtime.validate(self.config(stream_output=False, runs_keep_days=7)))
            self.assertFalse(brain.stream_output)
            self.assertEqual(engine.retention, {"keep_days": 7.0, "max_mb": 200.0})


class VerifyObserved(HomeCase):
    """Test 5 (E4c): observed spills on the run-30546289 shape; a 40 KB page displayed whole is consistent."""

    FIRST = "2026-11-15T20:57:36.063976Z"

    def dream(self, pages, displayed, through, omissions=()):
        run = self.store.run_begin()
        work = "work-" + "d" * 32
        with self.store._tx():
            self.store._audit("routine.rearmed", work, {"id": work, "routine": {
                "kind": "dream", "covered_through": through, "rearmed_by_run": run["id"],
                "coverage": {"run": run["id"], "cutoff": "2026-11-19T12:44:47.677863Z", "through": through,
                             "omissions": [{"reason": "r", "from": f} for f in omissions]}}})
        folder = self.home / "runs"
        folder.mkdir(exist_ok=True)
        with open(str(folder / (run["id"] + ".mcp.jsonl")), "w") as handle:
            for tool, use_id, arguments, result in pages:
                record = {"tool": tool, "arguments": arguments, "success": True, "result": result}
                if use_id:
                    record["tool_use_id"] = use_id
                handle.write(json.dumps(record) + "\n")
        if displayed is not None:
            with open(str(folder / (run["id"] + ".stream.jsonl")), "w") as handle:
                for use_id, shape, sha in displayed:
                    handle.write(json.dumps({"kind": "displayed", "tool_use_id": use_id, "shape": shape,
                                             "sha256": sha, "bytes": 1435}) + "\n")
        verdict = ledgerline.verify_dream(self.home, run["id"])
        self.store.run_finish(run["id"], "dream")
        return verdict

    def shape(self, displayed_sha="B" * 64, shape="spilled", through=None, omissions=(), stream=True, size=96054):
        events = ("event_search", "T1", {"since": EPOCH},
                  {"bytes": size, "sha256": "A" * 64, "created_first": self.FIRST, "created_last": "2026-11-16T01:00:00Z",
                   "capped": True, "next_cursor": "c1"})
        memories = ("memory_search", "T2", {"since": EPOCH},
                    {"bytes": 25000, "sha256": "M" * 64, "created_first": "2026-11-15T20:57:36Z",
                     "created_last": "2026-11-19T12:00:00Z", "capped": False})
        shown = [("T1", shape, displayed_sha), ("T2", "full", "M" * 64)] if stream else None
        return self.dream([events, memories], shown, through or self.FIRST, omissions)

    def test_run_30546289_shape_is_observed_spilled(self):
        verdict = self.shape()
        self.assertEqual(verdict["verdict"], "spilled", verdict)
        self.assertEqual(verdict["unrecorded"][0]["spill"], "observed")
        self.assertIn("observed", verdict["spill_check"])
        self.assertNotIn("predicted", verdict["spill_check"])

    def test_with_recorded_omission_is_consistent(self):
        self.assertEqual(self.shape(omissions=(self.FIRST,))["verdict"], "consistent")

    def test_through_past_the_spill_is_overclaimed(self):
        verdict = self.shape(through="2026-11-16T01:00:00Z")
        self.assertEqual(verdict["verdict"], "overclaimed", verdict)
        self.assertEqual(verdict["first_unread"], "2026-11-15T20:57:36.063976Z")

    def test_40kb_page_displayed_whole_is_consistent(self):
        # i17 alone calls this possibly_spilled (40 105 > 32 000); the stream saw it whole.
        verdict = self.shape(displayed_sha="A" * 64, shape="full", size=40105, through="2026-11-16T01:00:00Z")
        self.assertEqual(verdict["verdict"], "consistent", verdict)

    def test_sha_mismatch_with_full_shape_counts_as_spilled(self):
        self.assertEqual(self.shape(shape="full", size=20000)["verdict"], "spilled")

    def test_run_without_stream_keeps_the_predicted_output(self):
        verdict = self.shape(stream=False)
        self.assertEqual(verdict["verdict"], "possibly_spilled")
        self.assertIn("predicted, not observed", verdict["spill_check"])
        self.assertNotIn("spill", verdict["unrecorded"][0])

    def test_r1_g4_missing_join_never_upgrades_a_page(self):
        # E4-r1 G4 (Rowan): a receipt with tool_use_id but no matching displayed line (a timeout mid-stream, or the
        # reducer failing on that line) is judged by the prediction, never as displayed in full.
        memories = ("memory_search", "T2", {"since": EPOCH},
                    {"bytes": 25000, "sha256": "M" * 64, "created_first": "2026-11-15T20:57:36Z",
                     "created_last": "2026-11-19T12:00:00Z", "capped": False})

        def events(use_id, size):
            return ("event_search", use_id, {"since": EPOCH},
                    {"bytes": size, "sha256": "A" * 64, "created_first": self.FIRST,
                     "created_last": "2026-11-16T01:00:00Z", "capped": True, "next_cursor": "c1"})
        cases = (("no displayed line, 96 KB", events("T-missing", 96054), ("T-other", "full", "A" * 64)),
                 ("no displayed line, 40 KB", events("T-missing", 40105), ("T-other", "full", "A" * 64)),
                 ("shape unknown (reducer failure)", events("T1", 40105), ("T1", "unknown", "A" * 64)))
        for name, page, shown in cases:
            with self.subTest(name):
                verdict = self.dream([page, memories], [shown, ("T2", "full", "M" * 64)], self.FIRST)
                self.assertEqual(verdict["verdict"], "possibly_spilled", verdict)
                self.assertNotIn("spill", verdict["unrecorded"][0])  # predicted, not observed
                self.assertEqual(verdict["spill_join"]["observed"], 1)  # only the memory page joined
                self.assertEqual(verdict["spill_join"]["predicted"], 1)


class ExportStream(HomeCase):
    """Test 6: `kind: stream` export lines are allow-listed only; the home's bytes are unchanged."""

    def test_stream_lines_allow_listed(self):
        run = self.store.run_begin()
        folder = self.home / "runs"
        folder.mkdir(exist_ok=True)
        reducer = ledgerline.StreamReducer(folder / (run["id"] + ".stream.jsonl"))
        for line in stream_lines():
            reducer.feed(line)
        reducer.close()
        with open(str(folder / (run["id"] + ".stream.jsonl")), "a") as handle:  # a hand-edited extra field
            handle.write(json.dumps({"kind": "displayed", "tool_use_id": "toolu_9", "shape": "full",
                                     "note": "MARK-BODY"}) + "\n")
        self.store.run_finish(run["id"], "ok")
        self.assertIn("stream_chain", self.store.run_get(run["id"]))
        before = home_bytes(self.home)
        code, out, _ = run_cli("--home", str(self.home), "export", "runs", "--run", run["id"])
        self.assertEqual(code, 0)
        self.assertEqual(home_bytes(self.home), before)
        lines = [json.loads(l) for l in out.splitlines() if json.loads(l)["kind"] == "stream"]
        self.assertEqual(len(lines), 14)  # 13 reduced facts (init, 5 tool_use, 5 displayed, turn, result) + the edit
        for line in lines:
            allowed = ledgerline.STREAM_ALLOWED[line["stream_kind"]]
            for key, value in line["facts"].items():
                if key not in allowed:
                    self.assertEqual(set(value), {"bytes", "sha256"})
        for marker in MARKERS:
            self.assertNotIn(marker, out)


if __name__ == "__main__":
    unittest.main()

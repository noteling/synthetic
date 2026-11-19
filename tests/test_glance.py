"""RW-1 Glance: screen_glance through a fake reader and a stub `open`; page words never reach stored state."""

import json
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from synthetics import glance, lookout, sharedstatus
from synthetics.mcp import METHODS, MUTATIONS, Server
from synthetics.store import Store

LABEL = "Add to cart - Velvet Lamp Zebra42"   # stands in for page words an "alike" hit carries
SITE = "www.example-shop.test"
ADDRESS = "https://shopper@www.example-shop.test:8443/ip/Velvet-Lamp/42?q=Zebra42#reviews"   # host + page words
PAGE = {"app": "Google Chrome", "bundle": "com.google.Chrome", "site": ADDRESS, "documents": 1, "tabs": 3,
        "sheetOpen": False, "kinds": {"button": 12, "link": 40}, "withPageID": 7, "elements": 52, "onScreen": 30,
        "loading": False, "truncated": False, "ms": 140}
# Needles for the leak tests. None can occur in a hex ID or an ISO timestamp: a bare "8443" matched a random
# run ID (Codex, PR #32 checks), but ":8443" can't, since no clock field reaches 84 and hex has no colon.
PAGE_WORDS = ("Zebra42", "Velvet", ":8443", "/ip/", "reviews", "shopper", "911.5")
HIT = {"role": "AXButton", "kind": "button", "label": LABEL, "visible": True, "enabled": True,
       "frame": [911.5, 422.25, 120, 40]}


def report(exit_code=0, ok=True, page=True, find=None, error=None, text=None):
    value = {"ok": ok, "exit": exit_code}
    if page:
        value["page"] = dict(PAGE)
    if find is not None:
        value["find"] = find
    if error:
        value["error"] = error
    if text:
        value["text"] = text
    return value


class Fake:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, args):
        self.calls.append(list(args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class GlanceToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.store = Store(self.home)
        self.store.initialize("Rowan", "maintainer")
        self.run = self.store.run_begin()["id"]
        self.bound = Store(self.home, run_id=self.run)
        self.server = Server(self.bound, run_id=self.run)
        self.addCleanup(setattr, glance, "reader", None)
        self.opt_in({"enabled": True})

    def opt_in(self, value):
        path = self.home / glance.OPT_IN
        if value is None:
            path.unlink()
        else:
            path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def listed(self):
        reply = self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {entry["name"] for entry in reply["result"]["tools"]}

    def tearDown(self):
        self.bound.close()
        self.store.close()
        self.tmp.cleanup()

    def glance(self, result, **arguments):
        glance.reader = Fake(result)
        return self.server.call("screen_glance", arguments), glance.reader

    def stored_bytes(self):
        """Everything this home keeps on disk that Lookout or a reviewer could see: DB rows, audit, status, logs."""
        chunks = []
        for table in ("glances", "audit"):
            chunks += [row[0] for row in self.store.db.execute("SELECT data FROM %s" % table)]
        for path in self.home.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl"):
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(chunks)

    def test_registered_as_a_recorded_tool(self):
        self.assertIn("screen_glance", METHODS)
        self.assertIn("screen_glance", MUTATIONS)

    def test_a_page_read_gives_counts_and_records_them(self):
        result, fake = self.glance(report())
        self.assertEqual(fake.calls, [["--read-page", "--json"]])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["page"]["elements"], 52)
        rows = glance.recent(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], result["glance_id"])
        self.assertEqual(rows[0]["run_id"], self.run)
        self.assertEqual(rows[0]["counts"]["kinds"], {"button": 12, "link": 40})
        self.assertEqual(rows[0]["counts"]["site"], SITE)   # D1: host only

    def test_find_passes_name_and_role_but_never_full_or_contains(self):
        find = {"name": "Add to cart", "role": "button", "match": "alike", "count": 1, "unique": True, "hits": [HIT]}
        result, fake = self.glance(report(find=find), find="Add to cart", role="button", app="com.google.Chrome")
        args = fake.calls[0]
        self.assertEqual(args[:5], ["--find", "Add to cart", "--role", "button", "--json"])
        self.assertEqual(args[-2:], ["--app", "com.google.Chrome"])
        self.assertNotIn("--full", args)
        self.assertNotIn("--contains", args)
        self.assertEqual(result["find"]["hits"][0]["label"], LABEL)   # the employee sees the hit
        row = glance.recent(self.store)[0]
        self.assertEqual(row["find"], {"name": "Add to cart", "role": "button", "match": "alike", "count": 1,
                                       "unique": True, "hits": [{"visible": True, "enabled": True}]})

    def test_page_words_never_reach_stored_state(self):
        find = {"name": "Add to cart", "match": "alike", "count": 1, "unique": True, "hits": [HIT]}
        self.glance(report(find=find, text="Zebra42 secret page text"), find="Add to cart")
        stored = self.stored_bytes()
        self.assertTrue(stored)
        for needle in PAGE_WORDS + ("secret page text",):
            self.assertNotIn(needle, stored)

    def test_status_json_and_lookout_show_the_glance_without_page_words(self):
        find = {"name": "Add to cart", "match": "alike", "count": 1, "unique": True, "hits": [HIT]}
        result, _ = self.glance(report(find=find, text="Zebra42 page text"), find="Add to cart")
        status = sharedstatus.read(self.home)   # the MCP call rewrote it
        self.assertEqual([g["id"] for g in status["glances"]], [result["glance_id"]])
        html = lookout.team_page(self.home.parent, agents={})
        self.assertIn("Recent glances", html)
        self.assertIn(self.run, html)
        self.assertIn("alike", html)
        self.assertIn(SITE, html)
        for needle in PAGE_WORDS:
            self.assertNotIn(needle, html)
            self.assertNotIn(needle, json.dumps(status))

    def test_site_is_cut_to_its_host(self):
        cases = {ADDRESS: SITE, "www.example-shop.test": SITE, "Example.TEST:80/a": "example.test",
                 "http://[::1]:8765/x": "[::1]", "": None, None: None, 42: None}
        for site, expected in cases.items():
            with self.subTest(site=site):
                self.assertEqual(glance.host(site), expected)

    def test_off_unless_the_home_opts_in(self):
        self.assertIn("screen_glance", self.listed())
        for value in (None, {"enabled": False}, {"enabled": "yes"}, "not json", "[true]"):
            with self.subTest(value=value):
                self.opt_in(value)
                self.assertNotIn("screen_glance", self.listed())
                self.assertIn("work_put", self.listed())
                glance.reader = Fake(report())
                with self.assertRaises(ValueError):
                    self.server.call("screen_glance", {})
                self.assertEqual(glance.reader.calls, [])
                self.opt_in({"enabled": True})
        self.assertEqual(glance.recent(self.store), [])

    def test_each_exit_is_explained(self):
        cases = [(1, "not_found"), (2, "usage"), (3, "no_browser"), (4, "no_permission"), (5, "partial")]
        for code, status in cases:
            with self.subTest(code=code):
                result, _ = self.glance(report(exit_code=code, ok=False, page=code in (1, 5)), find="Buy")
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertTrue(result["explanation"])
                self.assertEqual(glance.recent(self.store)[0]["status"], status)

    def test_partial_read_without_find_is_not_ok(self):
        result, _ = self.glance(report(exit_code=5, ok=True))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")

    def test_reader_failures_are_results_not_errors(self):
        for reason in ("noteling_missing", "noteling_too_old", "timeout", "launch_failed"):
            with self.subTest(reason=reason):
                result, _ = self.glance(glance.GlanceError(reason, "sentence for " + reason))
                self.assertEqual(result, dict(ok=False, exit=None, status=reason, explanation="sentence for " + reason,
                                              glance_id=result["glance_id"]))
                self.assertEqual(glance.recent(self.store)[0]["status"], reason)

    def test_bad_arguments_are_refused_before_reading(self):
        glance.reader = Fake(report())
        for arguments in ({"find": "  "}, {"role": "button"}, {"full": True}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.server.call("screen_glance", arguments)
        self.assertEqual(glance.reader.calls, [])

    def test_values_that_could_be_noteling_flags_are_refused_before_reading(self):
        # Review F1: Noteling picks its mode with CommandLine.arguments.contains(...), and --ask is tested before
        # --find, so a value like "--ask" or "--full" would change what Noteling does.
        glance.reader = Fake(report())
        for arguments in ({"find": "--read-page", "app": "--full"}, {"find": "--full"}, {"find": "--ask"},
                          {"find": " -x"}, {"find": "Add\n--full"}, {"find": 5},
                          {"find": "Add to cart", "role": "--full"}, {"find": "Add", "role": "push button"},
                          {"find": "Add", "role": "-button"}, {"app": "--full"}, {"app": "-com.google.Chrome"},
                          {"app": "Chrome"}, {"app": "com.google.Chrome --full"}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.server.call("screen_glance", arguments)
        self.assertEqual(glance.reader.calls, [])
        self.assertEqual(glance.recent(self.store), [])
        for arguments in ({"find": "--full"}, {"app": "--full"}, {"find": "a", "role": "--full"}):
            with self.subTest(direct=arguments):
                with self.assertRaises(ValueError):
                    glance.arguments(**arguments)

    def test_unprintable_characters_in_find_are_refused_before_reading(self):
        # Review L3: control (incl. DEL), format, line- and paragraph-separator characters never name a control.
        glance.reader = Fake(report())
        for find in ("Add\x7f", "Add\u2028cart", "Add\u2029cart", "Add\u200bcart", "\u202eAdd", "Add\x85cart"):
            with self.subTest(find=ascii(find)):
                with self.assertRaises(ValueError):
                    self.server.call("screen_glance", {"find": find})
        self.assertEqual(glance.reader.calls, [])
        self.assertEqual(glance.recent(self.store), [])
        for find in ("Añadir al carrito", "Add to cart \u2013 Bananas", "カートに入れる", "Save\u00a0draft"):
            with self.subTest(passes=find):
                glance.check(find=find)

    def test_a_lone_surrogate_in_find_is_refused_by_check_before_reading(self):
        # Review N1 on #32: MCP JSON "Add\\ud800" decodes to a lone surrogate (category Cs).
        # check() must refuse it with its own sentence, before any reader runs or anything is recorded.
        glance.reader = Fake(report())
        for find in ("Add\ud800", "\udfffcart"):
            with self.subTest(find=ascii(find)):
                with self.assertRaisesRegex(ValueError, "invisible format characters"):
                    self.server.call("screen_glance", {"find": find})
        self.assertEqual(glance.reader.calls, [])
        self.assertEqual(glance.recent(self.store), [])

    def test_ordinary_names_roles_and_bundles_pass(self):
        result, fake = self.glance(report(find={"name": "Save - draft", "match": "none", "count": 0,
                                                "unique": False, "hits": []}, exit_code=1, ok=False),
                                   find="Save - draft", role="button", app="com.google.Chrome")
        self.assertEqual(fake.calls, [["--find", "Save - draft", "--role", "button", "--json",
                                       "--app", "com.google.Chrome"]])
        self.assertEqual(result["status"], "not_found")

    def test_the_result_keeps_only_known_page_fields(self):
        page = dict(PAGE, contains=2, words="Quokka77 page text")
        value = report(text="Quokka77 page text")
        value["page"] = page
        result, _ = self.glance(value)
        self.assertNotIn("Quokka77", json.dumps(result))
        self.assertEqual(set(result["page"]), set(glance.PAGE_FIELDS))

    def test_recent_is_newest_first_and_bounded(self):
        for _ in range(7):
            self.glance(report())
        rows = glance.recent(self.store)
        self.assertEqual(len(rows), glance.RECENT)
        self.assertGreaterEqual(rows[0]["at"], rows[-1]["at"])

    def test_a_finished_run_cannot_glance(self):
        self.store.run_finish(self.run, "done") if hasattr(self.store, "run_finish") else self.skipTest("no run_finish")
        glance.reader = Fake(report())
        with self.assertRaises(ValueError):
            self.server.call("screen_glance", {})


class ParseTest(unittest.TestCase):
    def test_the_last_json_report_line_wins(self):
        text = "noise\n{\"not\": \"a report\"}\n" + json.dumps(report()) + "\n"
        self.assertEqual(glance.parse(text)["exit"], 0)

    def test_old_noteling_prose_means_too_old(self):
        # C4: 0.10.2 ignores --json and prints a human line; that shape is "too old", the line is never parsed.
        for text in ("No browser window found, or Accessibility permission is missing.\n", "{\"exit\": true}"):
            with self.subTest(text=text):
                with self.assertRaises(glance.GlanceError) as caught:
                    glance.parse(text)
                self.assertEqual(caught.exception.reason, "noteling_too_old")
                self.assertEqual(str(caught.exception), "Noteling too old (need ≥ 0.10.5).")


    def test_no_output_means_no_answer_not_too_old(self):
        # Review F3: a crashed or killed instance writes nothing; that isn't an old Noteling.
        for text in ("", "  \n", None):
            with self.subTest(text=text):
                with self.assertRaises(glance.GlanceError) as caught:
                    glance.parse(text)
                self.assertEqual(caught.exception.reason, "no_answer")


class LaunchReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.default_lock = glance.LOCK
        self.addCleanup(setattr, glance, "LOCK", glance.LOCK)
        glance.LOCK = str(self.root / "glance.lock")
        self.app = self.root / "Noteling.app"
        (self.app / "Contents").mkdir(parents=True)
        self.version("0.10.5")
        self.log = self.root / "open-args.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def version(self, text):
        with open(self.app / "Contents" / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": text}, handle)

    def opener(self, body):
        """A stub `open`: records its argv, then runs body with $OUT set to the -o file."""
        path = self.root / "open"
        path.write_text("#!/bin/sh\nprintf '%%s\\n' \"$@\" > '%s'\n"
                        "while [ $# -gt 0 ]; do [ \"$1\" = -o ] && OUT=\"$2\"; shift; done\n%s\n"
                        % (self.log, body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_runs_a_background_instance_and_reads_its_json(self):
        line = json.dumps(report())
        read = glance.LaunchReader(str(self.app), opener=self.opener("echo '%s' > \"$OUT\"" % line))
        self.assertEqual(read(["--read-page", "--json"])["page"]["elements"], 52)
        argv = self.log.read_text().split("\n")
        self.assertEqual(argv[:5], ["-n", "-g", "-W", "-a", str(self.app)])
        self.assertIn("--args", argv)
        self.assertEqual(argv[argv.index("--args") + 1:argv.index("--args") + 3], ["--read-page", "--json"])
        out = argv[argv.index("-o") + 1]
        self.assertFalse(os.path.exists(os.path.dirname(out)))   # temp folder removed

    def test_missing_and_old_noteling_are_refused_without_launching(self):
        opener = self.opener("exit 0")
        with self.assertRaises(glance.GlanceError) as caught:
            glance.LaunchReader(str(self.root / "Nope.app"), opener=opener)(["--read-page", "--json"])
        self.assertEqual(caught.exception.reason, "noteling_missing")
        self.version("0.10.2")
        with self.assertRaises(glance.GlanceError) as caught:
            glance.LaunchReader(str(self.app), opener=opener)(["--read-page", "--json"])
        self.assertEqual(caught.exception.reason, "noteling_too_old")
        self.assertFalse(self.log.exists())

    def test_a_new_enough_app_that_prints_prose_is_too_old(self):
        opener = self.opener("echo 'No browser window found, or Accessibility permission is missing.' > \"$OUT\"")
        with self.assertRaises(glance.GlanceError) as caught:
            glance.LaunchReader(str(self.app), opener=opener)(["--read-page", "--json"])
        self.assertEqual(caught.exception.reason, "noteling_too_old")

    def test_an_instance_that_writes_nothing_is_no_answer(self):
        with self.assertRaises(glance.GlanceError) as caught:
            glance.LaunchReader(str(self.app), opener=self.opener("exit 0"))(["--read-page", "--json"])
        self.assertEqual(caught.exception.reason, "no_answer")

    def test_glances_take_a_host_wide_lock_and_wait_for_it(self):
        # Review F2: a timeout kills instances that appeared since the glance began, so two glances never overlap.
        import fcntl
        line = json.dumps(report())
        read = glance.LaunchReader(str(self.app), timeout=0.3, opener=self.opener("echo '%s' > \"$OUT\"" % line))
        with open(glance.LOCK, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(glance.GlanceError) as caught:
                read(["--read-page", "--json"])
            self.assertEqual(caught.exception.reason, "busy")
            self.assertFalse(self.log.exists())   # never launched while another glance held the lock
        self.assertEqual(read(["--read-page", "--json"])["exit"], 0)   # released: the next glance runs
        self.assertEqual(read(["--read-page", "--json"])["exit"], 0)   # and its own lock was released too

    def test_the_lock_lives_in_the_users_own_temp_folder_whatever_tmpdir_says(self):
        # Review L1: per-user temp folder, not world-writable /tmp. Every home must agree on it, so it can't
        # depend on $TMPDIR (a worker without it would otherwise fall back to /tmp and take a different lock).
        self.assertIsNone(self.default_lock)
        glance.LOCK = None
        first = glance.LaunchReader(str(self.app)).lock
        saved = os.environ.pop("TMPDIR", None)
        try:
            tempfile.tempdir = None
            second = glance.LaunchReader(str(self.app)).lock
        finally:
            if saved is not None:
                os.environ["TMPDIR"] = saved
            tempfile.tempdir = None
        self.assertEqual(first, second)
        self.assertEqual(os.path.basename(first), "synthetics-glance.lock")
        if os.path.exists("/usr/bin/getconf") and sys.platform == "darwin":
            self.assertNotEqual(os.path.realpath(os.path.dirname(first)), os.path.realpath("/tmp"))
            self.assertFalse(os.stat(os.path.dirname(first)).st_mode & stat.S_IWOTH)

    def test_a_symlink_at_the_lock_path_is_refused_not_followed(self):
        # Review L1: O_NOFOLLOW, so a planted link can't make a glance create or lock someone else's file.
        target = self.root / "elsewhere"
        os.symlink(str(target), glance.LOCK)
        line = json.dumps(report())
        read = glance.LaunchReader(str(self.app), opener=self.opener("echo '%s' > \"$OUT\"" % line))
        with self.assertRaises(glance.GlanceError) as caught:
            read(["--read-page", "--json"])
        self.assertEqual(caught.exception.reason, "lock_failed")
        self.assertFalse(target.exists())
        self.assertFalse(self.log.exists())   # never launched

    def test_the_lock_file_is_private_to_the_user(self):
        self.addCleanup(os.umask, os.umask(0o022))   # a permissive umask must not widen it
        line = json.dumps(report())
        glance.LaunchReader(str(self.app), opener=self.opener("echo '%s' > \"$OUT\"" % line))(["--read-page", "--json"])
        self.assertEqual(os.stat(glance.LOCK).st_mode & 0o777, 0o600)

    def test_a_hang_times_out_kills_only_the_new_instance_and_removes_temp_files(self):
        binary = str(self.app / "Contents" / "MacOS" / "Noteling")
        snapshots = [[(100, binary), (7, "/usr/bin/other --json")],                         # before launch
                     [(100, binary), (7, "/usr/bin/other --json"), (4242, binary + " --read-page --json"),
                      (4243, "/Applications/Other.app/Contents/MacOS/Other --read-page --json")]]
        killed = []
        folders = []
        real_mkdtemp = tempfile.mkdtemp

        def recording_mkdtemp(*args, **kwargs):
            folders.append(real_mkdtemp(*args, **kwargs))
            return folders[-1]
        # The temp folder is taken from mkdtemp, not from the stub's argv log: under a loaded full suite the stub
        # can be killed by the 0.5 s timeout before it writes that log (FileNotFoundError flake, py3.14).
        patch = mock.patch.object(glance.tempfile, "mkdtemp", recording_mkdtemp)
        patch.start()
        self.addCleanup(patch.stop)
        read = glance.LaunchReader(str(self.app), timeout=0.5, opener=self.opener("sleep 5"),
                                   processes=lambda: snapshots.pop(0) if len(snapshots) > 1 else snapshots[0],
                                   kill=lambda pid, sig: killed.append((pid, sig)))
        with self.assertRaises(glance.GlanceError) as caught:
            read(["--read-page", "--json"])
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertEqual(killed, [(4242, 9)])
        self.assertEqual(len(folders), 1)
        self.assertFalse(os.path.exists(folders[0]))


if __name__ == "__main__":
    unittest.main()

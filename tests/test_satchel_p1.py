"""Satchel P1 (docs/satchel.md): extract with secrets masked, fail-closed, verify. Synthetic fixtures only."""

import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

from synthetics.mcp import Server
from synthetics.store import Store

try:  # fail-first on main: the module doesn't exist there, so each test fails on its own
    from synthetics import satchel
except ImportError:  # pragma: no cover
    satchel = None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tag(n=6):
    return secrets.token_hex(n)


def seeds():
    """One fake secret per M2 kind: (kind, full text to plant, the secret part that must not survive)."""
    out = []
    pem_body = "MIIEfake" + tag(20)
    out.append(("pem", "-----BEGIN RSA PRIVATE KEY-----\n%s\n-----END RSA PRIVATE KEY-----" % pem_body, pem_body))
    v = "sk-ant-api03-" + tag(16)
    out.append(("anthropic", "key %s here" % v, v))
    v = "sk-proj-" + tag(14)
    out.append(("openai", "OPENAI %s." % v, v))
    v = "ghp_" + secrets.token_hex(18)
    out.append(("github", "push with %s" % v, v))
    v = "github_pat_" + tag(14)
    out.append(("github", "pat %s" % v, v))
    v = "AKIA" + "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16))
    out.append(("aws", "aws %s ok" % v, v))
    v = "xoxb-" + tag(8)
    out.append(("slack", "slack %s" % v, v))
    v = "%d:%s" % (secrets.randbelow(10 ** 9) + 10 ** 8, tag(18)[:35])
    out.append(("telegram", "bot %s now" % v, v))
    v = "%d:%s-" % (secrets.randbelow(10 ** 9) + 10 ** 8, tag(18)[:34])  # C2: ends in '-'
    out.append(("telegram", "bot2 %s" % v, v))
    v = "abc" + tag(10)
    out.append(("bearer", "Authorization: Bearer %s" % v, v))
    v = "eyJ%s.eyJ%s.%s" % (tag(6), tag(6), tag(8))
    out.append(("jwt", "jwt %s" % v, v))
    v = "hunter" + tag(4)
    out.append(("assign", "password=%s" % v, v))
    v = "tok" + tag(4)
    out.append(("assign", "token: %s" % v, v))
    v = "key" + tag(4)
    out.append(("assign", "the api_key is %s" % v, v))
    v = "env" + tag(4)
    out.append(("assign", "DB_PASSWORD=%s" % v, v))
    v = "pw" + tag(4)
    out.append(("userinfo", "https://admin:%s@example.invalid/x" % v, v))
    return out


class Fixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(satchel, "synthetics.satchel missing")
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.home = root / "employee"
        self.out = root / "out"
        self.out.mkdir()
        self.tmpbase = root / "local-tmp"
        self.tmpbase.mkdir()
        self.keys = root / "keys"
        self.keys.mkdir()
        self.store = Store(self.home)
        self.store.initialize("Morgan", "Noteling maintainer")
        self.me = self.store.identity()["id"]
        self.old_key = secrets.token_hex(32)  # rotated away below; its hash stays only in an audit row
        self.store.channel_register("desk", Store.credential_hash(self.old_key), ["desk:"], "operator")
        self.channel_keys = []
        for name in ("desk", "postern"):
            key = secrets.token_hex(32)
            path = self.keys / (name + ".key")
            path.write_text(key)
            path.chmod(0o600)
            self.channel_keys.append(key)
            self.store.channel_register(name, Store.credential_hash(key), [name + ":"], "operator")
        token_file = root / "bot.token"
        token_file.write_text("%d:%s" % (987654321, "Z" * 35))
        (self.home / "telegram.json").write_text(json.dumps({"token_file": str(token_file)}))

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def extract(self, **kw):
        return satchel.extract(self.home, self.out, temp_base=self.tmpbase, **kw)

    def members(self, archive):
        with tarfile.open(archive) as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}

    def assert_nothing_written(self):
        self.assertEqual(sorted(os.listdir(str(self.out))), [])
        self.assertEqual([p for p in os.listdir(str(self.tmpbase)) if p.startswith("satchel-tmp-")], [])


class LeakT3(Fixture):
    def plant(self):
        planted = seeds()
        pasted_key = self.channel_keys[0]
        planted.append(("channel-key", "my key is pasted: %s" % pasted_key, pasted_key))
        stored_hash = Store.credential_hash(self.channel_keys[1])
        planted.append(("channel-key-hash", "hash %s" % stored_hash, stored_hash))
        texts = [text for _, text, _ in planted]
        memory_ids = []
        for kind, text, _ in planted:
            memory_ids.append(self.store.memory_put(self.me, "seed %s" % kind, text, source="test",
                                                    key="seed-" + tag(3))["id"])
        joined = "\n".join(texts)
        self.store.feed("priya", "pasted telegram message:\n" + joined, source="telegram:1")
        self.store.message_send("priya", "reply with " + joined, key="m1")
        self.store.work_put(title="w", note="note " + joined)
        self.store.self_note("how-i-work", "self " + texts[11] + " " + texts[13], source="test")
        (self.home / "prompt.local.md").write_text("Local prompt\n" + joined + "\n")
        journal = self.home / "meetings" / "mtg-1" / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        first = json.dumps({"op": "begin", "prev_sha256": None, "at": "2026-11-20T00:00:00Z"}, sort_keys=True)
        second = json.dumps({"op": "heard", "text": joined, "prev_sha256": sha(first.encode())}, sort_keys=True)
        journal.write_text(first + "\n" + second + "\n")
        # capture tables: a coverage reason and a capture_outbox error quoting a secret (synthetic rows)
        self.store.db.execute("INSERT INTO coverage_marks(event_id, outcome, reason, at) VALUES (?,?,?,?)",
                              ("event-x", "skipped", "reason " + joined, "2026-11-20T00:00:00Z"))
        self.store.db.execute("INSERT INTO capture_outbox(event_id, project, mode, direction, status, attempts, error, "
                              "created_at) VALUES (?,?,?,?,?,?,?,?)",
                              ("event-x", "p", "capture", "in", "failing", 1, "error " + joined, "2026-11-20T00:00:00Z"))
        run = self.store.run_begin()
        bound = Store(self.home, run_id=run["id"])
        server = Server(bound, run_id=run["id"])
        server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "memory_put", "arguments": {
                            "subject": self.me, "predicate": "via mcp", "value": joined, "source": "x",
                            "key": "mcp-seed"}}})
        stream = self.home / "runs" / (run["id"] + ".stream.jsonl")
        line = json.dumps({"kind": "turn", "seq": 1, "prev_sha256": None, "text_blocks": 1, "text": joined})
        stream.write_text(line + "\n")
        bound.close()
        self.store.run_finish(run["id"], "summary " + joined)
        return planted, memory_ids, run["id"]

    def test_no_seed_survives_in_raw_archive_bytes(self):
        planted, memory_ids, run_id = self.plant()
        result = self.extract()
        raw = Path(result["archive"]).read_bytes()
        for kind, _, secret in planted:
            self.assertFalse(secret.encode("utf-8") in raw, "%s seed %s survived" % (kind, secret))
            self.assertFalse(sha(secret.encode("utf-8")).encode() in raw, "%s seed hash survived" % kind)
        for key in self.channel_keys + [self.old_key]:  # the stored hashes of both channel keys are gone too (freed pages included)
            self.assertFalse(Store.credential_hash(key).encode() in raw, "channel key hash survived")
        self.assertFalse(b"ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ" in raw)  # telegram token file never opened
        members = self.members(result["archive"])
        self.assertNotIn("config/telegram.json", members)
        manifest = json.loads(members["manifest.json"])
        # Every seeded memory row has a mask of the planted kind, located at that row.
        conn = sqlite3.connect(str(self.home / "state.sqlite3"))
        rowids = {r[1]: r[0] for r in conn.execute("SELECT rowid, id FROM memories")}
        conn.close()
        for (kind, _, _), memory_id in zip(planted, memory_ids):
            here = [m for m in manifest["masks"] if m.get("table") == "memories" and m["row"] == rowids[memory_id]]
            self.assertIn(kind, {m["kind"] for m in here}, kind)
        for member in ("config/prompt.local.md", "meetings/mtg-1/journal.jsonl"):  # runs.jsonl: hashes dropped, see run line
            self.assertTrue(any(m["member"] == member for m in manifest["masks"]), member)
        self.assertEqual(manifest["row_counts"]["channels"], 0)
        db = Path(self.temporary.name) / "t3.sqlite3"
        db.write_bytes(members["state.sqlite3"])
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 0)
        conn.close()
        for table in ("events", "messages", "works", "roster_log", "audit", "idempotency", "runs", "coverage_marks",
                      "capture_outbox"):
            self.assertTrue(any(m.get("table") == table for m in manifest["masks"]), table)
        text = json.dumps(manifest["masks"])
        for _, _, secret in planted:
            self.assertNotIn(secret, text)
        self.assertEqual(manifest["reprovision"]["channels"][0]["name"], "desk")
        self.assertNotIn("key_sha256", json.dumps(manifest["reprovision"]))
        runs = [json.loads(l) for l in members["runs.jsonl"].decode().splitlines()]
        run_line = [l for l in runs if l["kind"] == "run" and l["run_id"] == run_id][0]
        self.assertTrue(run_line["masked"])
        call = [l for l in runs if l["kind"] == "mcp.call" and l["run_id"] == run_id][0]
        self.assertIsNone(call["args"]["value"]["sha256"])  # F8: no hash of a masked raw value
        self.assertIn("masked", call["args"]["value"])
        self.assertIsNone(call["args_sha256"])
        self.assertTrue(satchel.verify(result["archive"], temp_base=self.tmpbase)["ok"])

    def test_source_home_is_untouched(self):
        self.plant()
        before = {p.name: sha(p.read_bytes()) for p in self.home.iterdir() if p.is_file()
                  and not p.name.startswith("state.sqlite3")}
        conn = sqlite3.connect(str(self.home / "state.sqlite3"))
        dump_before = list(conn.iterdump())
        conn.close()
        self.extract()
        after = {p.name: sha(p.read_bytes()) for p in self.home.iterdir() if p.is_file()
                 and not p.name.startswith("state.sqlite3")}
        conn = sqlite3.connect(str(self.home / "state.sqlite3"))
        self.assertEqual(list(conn.iterdump()), dump_before)
        conn.close()
        self.assertEqual(before, after)


class FalsePositivesT9(Fixture):
    def test_ordinary_values_stay_byte_identical(self):
        desk_code = "-".join("".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(5))
                             for _ in range(4))
        text = ("sum %s id memory-%s event-%s git %s short %s code %s at 2026-11-20T21:44:02.423149Z "
                "path /Users/x/projects/a.py; the password field is empty; token budget 5; "
                "{\"token_budget\": 5, \"key_sha256\": \"%s\"} line_sha256 %s"
                % (sha(b"x"), tag(16), tag(16), secrets.token_hex(20), secrets.token_hex(4)[:7], desk_code,
                   sha(b"other"), sha(b"y")))
        memory = self.store.memory_put(self.me, "plain", text, source="test")
        (self.home / "glance.json").write_text(json.dumps({"token_budget": 5, "key_sha256": sha(b"z")}, indent=1))
        conn = sqlite3.connect(str(self.home / "state.sqlite3"))
        row = conn.execute("SELECT rowid, data FROM memories WHERE id=?", (memory["id"],)).fetchone()
        conn.close()
        result = self.extract()
        members = self.members(result["archive"])
        manifest = json.loads(members["manifest.json"])
        self.assertEqual([m for m in manifest["masks"] if m.get("table") == "memories"], [])
        self.assertEqual(members["config/glance.json"], (self.home / "glance.json").read_bytes())
        db = Path(self.temporary.name) / "copy.sqlite3"
        db.write_bytes(members["state.sqlite3"])
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("SELECT data FROM memories WHERE rowid=?", (row[0],)).fetchone()[0], row[1])
        conn.close()

    def test_detector_is_not_all_hex(self):
        self.assertEqual(satchel.detect(sha(b"a") + " " + secrets.token_hex(20)), [])
        self.assertEqual(satchel.detect("«masked:assign:3» token is «masked:assign:4»"), [])

    def test_unmatched_candidate_token_is_kept(self):
        other = secrets.token_hex(32)
        masker = satchel.Masker({Store.credential_hash(secrets.token_hex(32))})
        self.assertEqual(masker.text("k " + other, {})[0], "k " + other)


class FailClosedT10(Fixture):
    def test_detector_exception(self):
        with mock.patch.object(satchel, "detect", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.extract()
        self.assert_nothing_written()

    def test_non_utf8_text(self):
        self.store.close()
        conn = sqlite3.connect(str(self.home / "state.sqlite3"))
        conn.execute("INSERT INTO memories(id, data) VALUES ('memory-bad', CAST(X'FFFE00' AS TEXT))")
        conn.commit()
        conn.close()
        self.store = Store(self.home)
        with self.assertRaises(satchel.SatchelError) as caught:
            self.extract()
        self.assertEqual(caught.exception.code, "undecodable")
        self.assert_nothing_written()

    def test_unknown_table(self):
        self.store.db.execute("CREATE TABLE mystery (x TEXT)")
        with self.assertRaises(satchel.SatchelError) as caught:
            self.extract()
        self.assertEqual(caught.exception.code, "unknown_table")
        self.assert_nothing_written()

    def test_unknown_config_file(self):
        (self.home / "mystery.json").write_text("{}")
        with self.assertRaises(satchel.SatchelError) as caught:
            self.extract()
        self.assertEqual(caught.exception.code, "unknown_home_file")
        self.assert_nothing_written()

    def test_unreadable_known_config(self):
        (self.home / "floor.json").write_text("{not json")
        with self.assertRaises(satchel.SatchelError):
            self.extract()
        self.assert_nothing_written()


class TempOutsideOutC1(Fixture):
    def test_out_holds_only_the_final_tar_during_and_after(self):
        seen = []
        real = satchel._export_runs

        def spy(home, masker):
            seen.append(sorted(os.listdir(str(self.out))))
            seen.append([p for p in os.listdir(str(self.tmpbase)) if p.startswith("satchel-tmp-")])
            return real(home, masker)

        with mock.patch.object(satchel, "_export_runs", spy):
            result = self.extract()
        self.assertEqual(seen[0], [])
        self.assertEqual(len(seen[1]), 1)
        self.assertEqual(os.listdir(str(self.out)), [Path(result["archive"]).name])
        self.assertEqual([p for p in os.listdir(str(self.tmpbase)) if p.startswith("satchel-tmp-")], [])
        self.assertEqual(Path(result["archive"]).stat().st_mode & 0o777, 0o600)

    def test_temp_dir_is_private(self):
        modes = []
        real = satchel._export_runs

        def spy(home, masker):
            entry = [p for p in self.tmpbase.iterdir() if p.name.startswith("satchel-tmp-")][0]
            modes.append(entry.stat().st_mode & 0o777)
            return real(home, masker)

        with mock.patch.object(satchel, "_export_runs", spy):
            self.extract()
        self.assertEqual(modes, [0o700])

    def test_stale_own_temp_removed_live_one_kept(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = self.tmpbase / "satchel-tmp-stale"
        stale.mkdir()
        (stale / "owner.pid").write_text(str(dead.pid))
        live = self.tmpbase / "satchel-tmp-live"
        live.mkdir()
        (live / "owner.pid").write_text(str(os.getpid()))
        result = self.extract()
        self.assertFalse(stale.exists())
        self.assertTrue(live.exists())
        self.assertIn("satchel-tmp-stale", result["stale_temps_removed"])

    def test_failure_after_snapshot_leaves_out_empty(self):
        with mock.patch.object(satchel, "_export_runs", side_effect=satchel.SatchelError("leak", "x")):
            with self.assertRaises(satchel.SatchelError):
                self.extract()
        self.assert_nothing_written()


class VerifyT11(Fixture):
    def masked_archive(self):
        secret = "hunter" + tag(4)
        run = self.store.run_begin()
        bound = Store(self.home, run_id=run["id"])
        server = Server(bound, run_id=run["id"])
        server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "memory_put", "arguments": {"subject": self.me, "predicate": "p",
                                                "value": "password=" + secret, "source": "x"}}})
        bound.close()
        self.store.run_finish(run["id"], "done")
        journal = self.home / "meetings" / "mtg-2" / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        first = json.dumps({"op": "begin", "prev_sha256": None})
        second = json.dumps({"op": "heard", "text": "token: " + secret, "prev_sha256": sha(first.encode())})
        journal.write_text(first + "\n" + second + "\n")
        return self.extract(), journal

    def test_masked_runs_and_journal_reported_not_failed(self):
        result, journal = self.masked_archive()
        report = satchel.verify(result["archive"], temp_base=self.tmpbase)
        self.assertTrue(report["ok"], report["problems"])
        self.assertIn("chain_ok (masked values)", report["runs"])
        entry = report["journals"]["mtg-2"]
        self.assertTrue(entry["masked"])
        self.assertEqual(entry["premask_head"], sha(journal.read_bytes().split(b"\n")[1]))
        self.assertNotEqual(entry["archived_head"], entry["premask_head"])

    def rewrite(self, archive, member, change):
        members = self.members(archive)
        members[member] = change(members[member])
        manifest = json.loads(members["manifest.json"])
        manifest["members"][member]["sha256"] = sha(members[member])
        members["manifest.json"] = (json.dumps(manifest) + "\n").encode()
        names = [n for n in members if n != "SHA256SUMS"]
        members["SHA256SUMS"] = "".join("%s  %s\n" % (sha(members[n]), n) for n in names).encode()
        target = Path(self.temporary.name) / "tampered.tar"
        with tarfile.open(str(target), "w") as tar:
            for name, blob in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
        return target

    def test_unlisted_marker_fails(self):
        (self.home / "prompt.local.md").write_text("hello\n")
        result, _ = self.masked_archive()
        target = self.rewrite(result["archive"], "config/prompt.local.md", lambda b: b + "«masked:pem:999»".encode())
        report = satchel.verify(str(target), temp_base=self.tmpbase)
        self.assertFalse(report["ok"])
        self.assertTrue(any("not listed" in p for p in report["problems"]), report["problems"])

    def test_leftover_secret_fails(self):
        (self.home / "prompt.local.md").write_text("hello\n")
        result, _ = self.masked_archive()
        target = self.rewrite(result["archive"], "config/prompt.local.md", lambda b: b + b"password=abcdefgh")
        report = satchel.verify(str(target), temp_base=self.tmpbase)
        self.assertFalse(report["ok"])
        self.assertTrue(any("detector hit" in p for p in report["problems"]), report["problems"])

    def test_sum_mismatch_and_row_count(self):
        result, _ = self.masked_archive()
        members = self.members(result["archive"])
        members["runs.jsonl"] = members["runs.jsonl"] + b"\n"
        target = Path(self.temporary.name) / "bad.tar"
        with tarfile.open(str(target), "w") as tar:
            for name, blob in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
        report = satchel.verify(str(target), temp_base=self.tmpbase)
        self.assertFalse(report["ok"])
        self.assertTrue(any("sha256 mismatch: runs.jsonl" == p for p in report["problems"]), report["problems"])

    def test_unknown_format(self):
        result, _ = self.masked_archive()
        def bump(blob):
            manifest = json.loads(blob)
            manifest["format"] = 99
            return json.dumps(manifest).encode()
        members = self.members(result["archive"])
        members["manifest.json"] = bump(members["manifest.json"])
        members["SHA256SUMS"] = "".join("%s  %s\n" % (sha(b), n) for n, b in members.items()
                                        if n != "SHA256SUMS").encode()
        target = Path(self.temporary.name) / "fmt.tar"
        with tarfile.open(str(target), "w") as tar:
            for name, blob in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
        report = satchel.verify(str(target), temp_base=self.tmpbase)
        self.assertFalse(report["ok"])
        self.assertIn("unknown format 99", report["problems"])


class ConsistencyT5(Fixture):
    def test_extract_while_a_writer_commits(self):
        for n in range(50):
            self.store.memory_put(self.me, "pre %d" % n, "v", source="t")
        stop = threading.Event()
        written = []

        def writer():
            store = Store(self.home)
            try:
                n = 0
                while not stop.is_set() and n < 2000:
                    store.memory_put(self.me, "live %d" % n, "v", source="t")
                    written.append(n)
                    n += 1
            finally:
                store.close()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            result = self.extract(timeout=60)
        finally:
            stop.set()
            thread.join(30)
        self.assertFalse(thread.is_alive(), "writer watchdog")
        members = self.members(result["archive"])
        db = Path(self.temporary.name) / "c.sqlite3"
        db.write_bytes(members["state.sqlite3"])
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        names = [json.loads(r[0])["predicate"] for r in conn.execute("SELECT data FROM memories ORDER BY rowid")]
        conn.close()
        live = [int(n.split()[1]) for n in names if n.startswith("live ")]
        self.assertEqual(live, list(range(len(live))))  # a consistent prefix
        self.assertEqual(sum(1 for n in names if n.startswith("pre ")), 50)
        self.assertTrue(satchel.verify(result["archive"], temp_base=self.tmpbase)["ok"])


class SessionRefusalT8(Fixture):
    def test_refused_in_session(self):
        from synthetics import cli
        for argv in (["home", "extract", "--out", str(self.out)], ["home", "verify", str(self.out / "x.tar")]):
            with mock.patch.dict(os.environ, {"SYNTHETICS_SESSION_RUN": "run-x"}):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    rc = cli.main(["--home", str(self.home)] + argv)
            self.assertNotEqual(rc, 0)
            self.assertIn("refused inside an employee session", err.getvalue())
        self.assertEqual(os.listdir(str(self.out)), [])

    def test_cli_extract_and_verify(self):
        from synthetics import cli
        env = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(satchel.tempfile, "gettempdir",
                                                                             return_value=str(self.tmpbase)):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(["--home", str(self.home), "home", "extract", "--out", str(self.out)]), 0)
            archive = json.loads(out.getvalue())["archive"]
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(["--home", str(self.home), "home", "verify", archive]), 0)
            self.assertTrue(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()

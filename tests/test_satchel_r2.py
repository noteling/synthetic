"""Satchel P1 r2 (Rowan's #107 review, Codex rulings): F1 Telegram token in its API URL, F2 secrets by JSON key name,
F3 extract runs its own verify before --out, F4 channel key glued to '_'/'-'/hex, F5 overlapping hits merged,
n1 literal markers in the home, n2 the runs.jsonl masker pass. Synthetic fixtures only."""

import io
import json
import secrets
import sqlite3
import tarfile
from pathlib import Path
from unittest import mock
import unittest

from synthetics.mcp import Server
from synthetics.store import Store
from tests.test_satchel_p1 import Fixture, satchel, sha, tag


def tg_token(trailing=""):
    return "%d:%s%s" % (secrets.randbelow(10 ** 9) + 10 ** 8, tag(18)[:35 - len(trailing)], trailing)


class Base(Fixture):
    def db_members(self, archive):
        members = self.members(archive)
        db = Path(self.temporary.name) / ("copy-%s.sqlite3" % tag(3))
        db.write_bytes(members["state.sqlite3"])
        return members, db

    def assert_clean(self, result, secrets_):
        raw = Path(result["archive"]).read_bytes()
        for secret in secrets_:
            self.assertFalse(secret.encode("utf-8") in raw, "secret survived: %s" % secret)
        report = satchel.verify(result["archive"], temp_base=self.tmpbase)
        self.assertTrue(report["ok"], report["problems"])
        return json.loads(self.members(result["archive"])["manifest.json"])


class TelegramUrlF1(Base):
    def test_token_inside_its_api_url_is_masked(self):
        token, dashed = tg_token(), tg_token("-")  # C2: a token ending in '-' still masked whole
        memory = self.store.memory_put(self.me, "err", "urlopen error for https://api.telegram.org/bot%s/getUpdates: "
                                       "timed out; again https://api.telegram.org/bot%s/sendMessage" % (token, dashed),
                                       source="t")
        result = self.extract()
        manifest = self.assert_clean(result, [token, dashed])
        conn = sqlite3.connect(str(self.home / "state.sqlite3"))
        rowid = conn.execute("SELECT rowid FROM memories WHERE id=?", (memory["id"],)).fetchone()[0]
        conn.close()
        kinds = [m["kind"] for m in manifest["masks"] if m.get("table") == "memories" and m["row"] == rowid]
        self.assertEqual(kinds.count("telegram"), 2, kinds)

    def test_url_form_detected_and_whole_digit_run_is_the_bot_id(self):
        token = tg_token()
        hits = satchel.detect("GET https://api.telegram.org/bot%s/getMe" % token)
        self.assertEqual([h[2] for h in hits], ["telegram"])
        text = "bot%s/getMe" % token
        start, end, _ = satchel.detect(text)[0]
        self.assertEqual(text[start:end], token)
        # A digit run longer than a bot id (13+) before ':' is not a token, and no tail of it is taken as one.
        self.assertEqual(satchel.detect("ref 1234567890123:%s." % tag(18)[:35]), [])


class JsonKeysF2(Base):
    def test_config_values_masked_by_key_name(self):
        pw, ak, au, ck, num = ("Corr3ctHorse" + tag(3), "plain" + tag(6), "abc" + tag(6), "camel" + tag(5),
                               secrets.randbelow(10 ** 9) + 10 ** 9)
        spaced = "a b " + tag(4)
        config = {"password": pw, "api_key": ak, "headers": {"Authorization": "Bearer " + au}, "apiKey": ck,
                  "nested": [{"DB_PASSWORD": spaced}], "pin": {"passphrase": num},
                  "port": 8765, "token_budget": 5, "key_sha256": sha(b"z"), "label": "keep me"}
        (self.home / "glance.json").write_text(json.dumps(config, indent=1))
        result = self.extract()
        manifest = self.assert_clean(result, [pw, ak, au, ck, spaced, str(num)])
        archived = json.loads(self.members(result["archive"])["config/glance.json"])
        for path in (("password",), ("api_key",), ("headers", "Authorization"), ("apiKey",), ("pin", "passphrase")):
            value = archived
            for part in path:
                value = value[part]
            self.assertRegex(value, r"\A«masked:assign:\d+»\Z", path)
        self.assertRegex(archived["nested"][0]["DB_PASSWORD"], r"\A«masked:assign:\d+»\Z")
        self.assertEqual((archived["port"], archived["token_budget"], archived["key_sha256"], archived["label"]),
                         (8765, 5, sha(b"z"), "keep me"))
        paths = {m.get("json_path") for m in manifest["masks"] if m["member"] == "config/glance.json"}
        self.assertIn("$.headers.Authorization", paths)
        self.assertIn("$.nested[0].DB_PASSWORD", paths)

    def test_journal_line_and_json_row(self):
        pw, ak = "Journal" + tag(4), "rowkey" + tag(5)
        journal = self.home / "meetings" / "m1" / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps({"op": "heard", "env": {"DB_PASSWORD": pw}}) + "\n")
        self.store.db.execute("CREATE TABLE IF NOT EXISTS capture_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.store.db.execute("INSERT INTO capture_meta(k, v) VALUES (?, ?)",
                              ("test-row", json.dumps({"auth": {"client_secret": ak}})))
        self.store.db.commit()
        result = self.extract()
        manifest = self.assert_clean(result, [pw, ak])
        self.assertTrue(any(m["member"] == "meetings/m1/journal.jsonl" and m["kind"] == "assign"
                            for m in manifest["masks"]))
        self.assertTrue(any(m.get("table") == "capture_meta" and m.get("json_path") == "$.auth.client_secret"
                            for m in manifest["masks"]))

    def test_secret_in_a_dict_key_is_masked(self):
        key = "sk-ant-api03-" + tag(16)
        (self.home / "click.json").write_text(json.dumps({key: 1, "ok": True}))
        result = self.extract()
        self.assert_clean(result, [key])

    def test_key_names(self):
        for key in ("password", "DB_PASSWORD", "apiKey", "X-Api-Key", "api_key", "access_token", "githubToken",
                    "client_secret", "aws_secret_access_key", "Authorization", "private_key", "passphrase", "token"):
            self.assertTrue(satchel.secret_key(key), key)
        for key in ("token_budget", "input_tokens", "key_sha256", "tokens", "password_hint", "token_file", "port",
                    "secrets_dir", "pwd", "author"):
            self.assertFalse(satchel.secret_key(key), key)

    def test_text_form_of_json_authorization(self):
        value = "abc" + tag(6)
        hits = satchel.detect('{"Authorization": "%s"}' % value)
        self.assertEqual(len(hits), 1)


class SelfVerifyF3(Base):
    def test_masker_gap_refuses_and_writes_nothing(self):
        secret = "hunter" + tag(4)
        self.store.memory_put(self.me, "p", "password=" + secret, source="t")
        with mock.patch.object(satchel.Masker, "text", lambda self, text, location: (text, False)):
            with self.assertRaises(satchel.SatchelError) as caught:
                self.extract()
        self.assertEqual(caught.exception.code, "leak")
        self.assertIn("own verify", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assert_nothing_written()

    def test_pasted_channel_key_gap_refuses(self):
        key = self.channel_keys[0]
        self.store.memory_put(self.me, "k", "key %s" % key, source="t")
        real = satchel.Masker.__init__

        def blind(self, channel_hashes=frozenset()):  # the masker loses the hashes; extract's own check still has them
            real(self, frozenset())

        with mock.patch.object(satchel.Masker, "__init__", blind):
            with self.assertRaises(satchel.SatchelError) as caught:
                self.extract()
        self.assertEqual(caught.exception.code, "leak")
        self.assertIn("own verify", str(caught.exception))
        self.assertNotIn(key, str(caught.exception))
        self.assert_nothing_written()

    def test_raw_hash_bytes_in_database_found(self):
        result = self.extract()
        members = self.members(result["archive"])
        stored = Store.credential_hash(self.channel_keys[1])
        members["state.sqlite3"] += stored.encode()  # bytes outside any SQL value, like a freed page
        manifest = json.loads(members["manifest.json"])
        manifest["members"]["state.sqlite3"]["sha256"] = sha(members["state.sqlite3"])
        members["manifest.json"] = json.dumps(manifest).encode()
        members["SHA256SUMS"] = "".join("%s  %s\n" % (sha(b), n) for n, b in members.items()
                                        if n != "SHA256SUMS").encode()
        target = Path(self.temporary.name) / "raw.tar"
        with tarfile.open(str(target), "w") as tar:
            for name, blob in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
        report = satchel.verify(str(target), temp_base=self.tmpbase, channel_hashes={stored})
        self.assertFalse(report["ok"])
        self.assertTrue(any("raw bytes" in p for p in report["problems"]), report["problems"])


class ChannelKeyEdgesF4(Base):
    def test_key_glued_to_underscore_dash_or_hex(self):
        key, other = self.channel_keys
        stored = Store.credential_hash(other)
        self.store.memory_put(self.me, "k1", "export DESK_KEY_%s done" % key, source="t")
        self.store.memory_put(self.me, "k2", "desk-%s" % key, source="t")
        self.store.memory_put(self.me, "k3", "cafe%sbeef" % key, source="t")
        self.store.memory_put(self.me, "k4", "h_%s_x" % stored, source="t")
        result = self.extract()
        manifest = self.assert_clean(result, [key, stored])
        kinds = [m["kind"] for m in manifest["masks"] if m.get("table") == "memories"]
        self.assertEqual(kinds.count("channel-key"), 3, kinds)
        self.assertEqual(kinds.count("channel-key-hash"), 1, kinds)


    def test_non_hex_key_found_by_candidate_tokens(self):
        key = secrets.token_urlsafe(32).replace("-", "a").replace("_", "b")  # a key an operator chose, not hex
        self.store.channel_register("custom", Store.credential_hash(key), ["custom:"], "operator")
        self.store.memory_put(self.me, "k", "custom key: %s." % key, source="t")
        manifest = self.assert_clean(self.extract(), [key])
        self.assertIn("channel-key", [m["kind"] for m in manifest["masks"] if m.get("table") == "memories"])


class OverlapF5(Base):
    def test_union_of_overlapping_hits(self):
        body = "MIIEfake" + tag(20)
        text = "password=abcd-----BEGIN RSA PRIVATE KEY-----\n%s\n-----END RSA PRIVATE KEY----- after" % body
        hits = satchel.detect(text)
        self.assertEqual(len(hits), 1, hits)
        start, end, kind = hits[0]
        self.assertEqual(text[start:start + 4], "abcd")
        self.assertTrue(text[start:end].endswith("-----END RSA PRIVATE KEY-----"))
        self.assertEqual(kind, "pem")
        self.store.memory_put(self.me, "o", text, source="t")
        self.assert_clean(self.extract(), [body, "abcd-----BEGIN"])

    def test_merge(self):
        self.assertEqual(satchel.merge([(0, 5, "a"), (3, 20, "b"), (20, 22, "c")]), [(0, 20, "b"), (20, 22, "c")])


class LiteralMarkerN1(Base):
    def test_literal_marker_in_home_passes_verify(self):
        memory = self.store.memory_put(self.me, "doc", "Satchel writes markers like «masked:assign:1» in place of "
                                       "values", source="t")
        secret = "hunter" + tag(4)
        self.store.memory_put(self.me, "p", "password=" + secret, source="t")
        result = self.extract()
        manifest = self.assert_clean(result, [secret])
        members, db = self.db_members(result["archive"])
        conn = sqlite3.connect(str(db))
        data = conn.execute("SELECT data FROM memories WHERE id=?", (memory["id"],)).fetchone()[0]
        conn.close()
        self.assertIn("«masked-literal:assign:1»", data)
        self.assertEqual(len([l for l in manifest["literal_markers"] if l.get("table") == "memories"]), 1)
        self.assertEqual([m["kind"] for m in manifest["masks"] if m.get("table") == "memories"], ["assign"])


class RunsExportN2(Base):
    def test_kept_argument_field_is_masked(self):
        secret = "xoxb-" + tag(8)
        run = self.store.run_begin()
        bound = Store(self.home, run_id=run["id"])
        server = Server(bound, run_id=run["id"])
        server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "message_send", "arguments": {"to": secret, "body": "hi", "key": "k1"}}})
        bound.close()
        self.store.run_finish(run["id"], "done")
        result = self.extract()
        manifest = self.assert_clean(result, [secret])
        self.assertTrue(any(m["member"] == "runs.jsonl" and m["kind"] == "slack" for m in manifest["masks"]),
                        [m for m in manifest["masks"] if m["member"] == "runs.jsonl"])


if __name__ == "__main__":
    unittest.main()

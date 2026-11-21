"""Satchel P2 (docs/satchel.md): --move seal, recreate, held outbox after a backup restore. Synthetic fixtures only."""

import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest import mock

from synthetics.store import ChannelError, Store

try:  # fail-first on the P1 base: these names don't exist there, so each test fails on its own
    from synthetics import satchel
    from synthetics.engine import HomeSealed, WorkerLock
except ImportError:  # pragma: no cover
    satchel = HomeSealed = WorkerLock = None

NO_SESSION = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}


def canonical(home, tables=("memories", "works", "questions", "reminders", "entities", "identity")):
    conn = sqlite3.connect(str(Path(home) / "state.sqlite3"))
    try:
        return {t: sorted(conn.execute("SELECT id, data FROM %s" % t).fetchall()) for t in tables}
    finally:
        conn.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(satchel, "synthetics.satchel P2 missing")
        self.assertTrue(hasattr(satchel, "recreate"), "satchel.recreate missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "employee"
        self.out = self.root / "out"
        self.out.mkdir()
        self.tmpbase = self.root / "local-tmp"
        self.tmpbase.mkdir()
        self.store = Store(self.home)
        self.store.initialize("Morgan", "Noteling maintainer")
        self.me = self.store.identity()["id"]
        self.key = secrets.token_hex(32)
        self.store.channel_register("desk", Store.credential_hash(self.key), ["desk:"], "operator",
                                    routes=["colleague", "other"])
        self.store.memory_put(self.me, "likes", "tea", source="fixture")
        old = self.store.memory_put(self.me, "lives", "Paris", source="fixture")
        self.store.memory_put(self.me, "lives", "Lyon", source="fixture", replaces=old["id"])
        self.store.work_put(title="ship P2", status="ready")
        self.grant = self.store.access_grant(str(self.root / "repo"), "read", "fixture repo", granted_by="operator")
        self.pending = self.store.message_send("colleague", "pending hello", key="m1")
        self.delivered = self.store.message_send("other", "delivered hello", key="m2")
        self.store.channel_claim(self.key, self.delivered["id"])
        self.store.channel_ack(self.key, self.delivered["id"], "delivered", receipt="r1")
        self.claimed = self.store.message_send("colleague", "claimed hello", key="m3")
        self.store.channel_claim(self.key, self.claimed["id"])
        self.store.close()
        self.label_loaded = False
        patches = [mock.patch.object(satchel, "_label_loaded", lambda label: self.label_loaded),
                   mock.patch.dict(os.environ, NO_SESSION, clear=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temporary.cleanup)

    def extract(self, **kw):
        return satchel.extract(self.home, self.out, temp_base=self.tmpbase, **kw)

    def recreate(self, archive, into="new", **kw):
        return satchel.recreate(archive, self.root / into, temp_base=self.tmpbase, **kw)

    def assertRefused(self, code, fn, *a, **kw):
        with self.assertRaises(satchel.SatchelError) as caught:
            fn(*a, **kw)
        self.assertEqual(caught.exception.code, code, caught.exception)


class RoundTripT1(Fixture):
    def test_backup_round_trip(self):
        archive = self.extract()["archive"]
        result = self.recreate(archive, source_lost=True, attest="disk died")
        new = self.root / "new"
        self.assertEqual(canonical(self.home), canonical(new))
        store = Store(new)
        try:
            self.assertEqual(store.identity()["id"], self.me)
            self.assertEqual(store._all("channels"), [])
            grant = store._get("access_grants", self.grant["id"])
            self.assertEqual(grant["revoked_by"], "satchel-restore")
            self.assertEqual(store.access_list(), [])
            held = {m["id"] for m in store.outbox_held()}
            self.assertEqual(held, {self.pending["id"], self.claimed["id"]})
            self.assertNotIn(self.delivered["id"], held)
            audit = store.db.execute("SELECT data FROM audit WHERE action='satchel.restored'").fetchall()
            self.assertEqual(len(audit), 1)
            self.assertEqual(json.loads(audit[0][0])["attest"], "disk died")
        finally:
            store.close()
        self.assertEqual(sorted(result["held_messages"]), sorted(held))
        text = (new / "REPROVISION.md").read_text()
        self.assertIn("access grant %s" % (self.root / "repo"), text)
        self.assertIn("provision `desk`", text)
        self.assertIn("2 outbox messages are held", text)
        self.assertTrue((new / "restored" / "manifest.json").is_file())

    def test_map_rewrites_grant_paths(self):
        archive = self.extract()["archive"]
        self.recreate(archive, source_lost=True, attest="lost", maps=[(str(self.root), "/Volumes/new")])
        self.assertIn("access grant /Volumes/new/repo ", (self.root / "new" / "REPROVISION.md").read_text())

    def test_running_run_becomes_interrupted(self):
        store = Store(self.home)
        run = store.run_begin()
        store.close()
        archive = self.extract()["archive"]
        result = self.recreate(archive, source_lost=True, attest="lost")
        self.assertEqual(result["interrupted_runs"], [run["id"]])
        store = Store(self.root / "new")
        try:
            restored = store._get("runs", run["id"])
        finally:
            store.close()
        self.assertEqual(restored["status"], "interrupted")
        self.assertIn("restored: interrupted at extract", restored["error"])


class MoveT2(Fixture):
    def test_move_seals_and_recreates_without_hold(self):
        result = satchel.extract_move(self.home, self.out, temp_base=self.tmpbase)
        self.assertTrue((self.home / "SEALED.json").is_file())
        seal = json.loads((self.home / "SEALED.json").read_text())
        self.assertEqual(seal["archive_sha256"], result["sha256"])
        with self.assertRaises(HomeSealed):
            WorkerLock(self.home).__enter__()
        self.assertRefused("sealed", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)
        restored = self.recreate(result["archive"], from_move=True)
        self.assertEqual(restored["held_messages"], [])
        self.assertEqual(restored["kind"], "move")
        store = Store(self.root / "new")
        try:
            self.assertEqual(store.outbox_held(), [])
        finally:
            store.close()
        with WorkerLock(self.root / "new"):
            pass  # the recreated home is not sealed

    def test_move_refused_while_worker_runs_and_leaves_no_seal(self):
        with WorkerLock(self.home):
            self.assertRefused("live", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)
        self.assertFalse((self.home / "SEALED.json").exists())
        self.assertEqual(os.listdir(str(self.out)), [])

    def test_move_refused_while_label_loaded(self):
        self.label_loaded = True
        self.assertRefused("live", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)
        self.assertFalse((self.home / "SEALED.json").exists())

    def test_failed_move_extract_leaves_no_seal(self):
        with mock.patch.object(satchel, "extract", side_effect=satchel.SatchelError("boom", "x")):
            self.assertRefused("boom", satchel.extract_move, self.home, self.out, temp_base=self.tmpbase)
        self.assertFalse((self.home / "SEALED.json").exists())


class RefusalsT4(Fixture):
    def test_refusals(self):
        archive = self.extract()["archive"]
        self.assertRefused("attest_required", self.recreate, archive)
        self.assertRefused("attest_required", self.recreate, archive, source_lost=True, attest="  ")
        self.assertRefused("flag_mismatch", self.recreate, archive, from_move=True)
        (self.root / "busy").mkdir()
        (self.root / "busy" / "x").write_text("x")
        self.assertRefused("not_empty", self.recreate, archive, into="busy", source_lost=True, attest="lost")
        self.label_loaded = True
        self.assertRefused("live_here", self.recreate, archive, into="n2", source_lost=True, attest="lost")
        self.assertFalse((self.root / "n2").exists())

    def test_move_archive_needs_from_move(self):
        archive = satchel.extract_move(self.home, self.out, temp_base=self.tmpbase)["archive"]
        self.assertRefused("flag_mismatch", self.recreate, archive, source_lost=True, attest="lost")

    def test_tampered_and_newer_schema_refused(self):
        archive = Path(self.extract()["archive"])
        data = bytearray(archive.read_bytes())
        at = data.find(b"pending hello")
        data[at] ^= 1
        bad = self.root / "bad.tar"
        bad.write_bytes(bytes(data))
        self.assertRefused("verify_failed", self.recreate, bad, source_lost=True, attest="lost")
        with mock.patch.object(Store, "SCHEMA_VERSION", 1):
            self.assertRefused("newer_schema", self.recreate, archive, into="n3", source_lost=True, attest="lost")
        self.assertFalse((self.root / "n3").exists())


class HeldOutboxT7(Fixture):
    def restored(self):
        archive = self.extract()["archive"]
        self.recreate(archive, source_lost=True, attest="lost")
        store = Store(self.root / "new")
        self.addCleanup(store.close)
        store.channel_register("desk", Store.credential_hash(self.key), ["desk:"], "operator",
                               routes=["colleague", "other"])
        return store

    def test_held_not_pending_until_release(self):
        store = self.restored()
        self.assertEqual(store.channel_pending(self.key), [])
        with self.assertRaises(ChannelError) as caught:
            store.channel_claim(self.key, self.pending["id"])
        self.assertEqual(caught.exception.code, "invalid_state")
        store.outbox_release(self.pending["id"], by="david")
        self.assertEqual([m["message_id"] for m in store.channel_pending(self.key)], [self.pending["id"]])
        with self.assertRaises(ValueError):
            store.outbox_release(self.pending["id"], by="david")

    def test_drop_is_final_and_never_pending(self):
        store = self.restored()
        dropped = store.outbox_drop(self.pending["id"], by="david", reason="sent before loss")
        self.assertTrue(dropped["dropped"])
        self.assertEqual(store.delivery_summary(dropped), "failed")
        self.assertEqual(store.channel_pending(self.key), [])
        store.channel_register("fresh", Store.credential_hash("f" * 64), ["fresh:"], "operator",
                               routes=["colleague"])
        self.assertEqual(store.channel_pending("f" * 64), [])
        with self.assertRaises(ChannelError):
            store.channel_claim("f" * 64, self.pending["id"])

    def test_hold_is_operator_only(self):
        store = self.restored()
        run = store.run_begin()
        bound = Store(self.root / "new", run_id=run["id"])
        self.addCleanup(bound.close)
        for call in (lambda: bound.outbox_release(self.pending["id"], by="x"),
                     lambda: bound.outbox_drop(self.pending["id"], by="x", reason="r"),
                     lambda: bound.outbox_hold_unfinished("r"),
                     lambda: bound.capture_release_held(by="x")):
            with self.assertRaises(ValueError):
                call()


class CliT8(Fixture):
    def cli(self, argv, env=None):
        from synthetics import cli
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(satchel.tempfile, "gettempdir", return_value=str(self.tmpbase)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_refused_in_session(self):
        archive = self.extract()["archive"]
        for argv in (["home", "recreate", archive, "--into", str(self.root / "n"), "--source-lost", "--attest", "x"],
                     ["home", "extract", "--move", "--out", str(self.out)], ["home", "held"],
                     ["home", "release", "message-x"], ["home", "drop", "message-x", "--reason", "r"]):
            rc, _, err = self.cli(["--home", str(self.home)] + argv, env={"SYNTHETICS_SESSION_RUN": "run-x"})
            self.assertNotEqual(rc, 0, argv)
            self.assertIn("refused inside an employee session", err)
        self.assertFalse((self.root / "n").exists())
        self.assertFalse((self.home / "SEALED.json").exists())

    def test_cli_recreate_held_release(self):
        archive = self.extract()["archive"]
        new = self.root / "n"
        rc, out, err = self.cli(["home", "recreate", archive, "--into", str(new), "--source-lost", "--attest", "lost"])
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.cli(["--home", str(new), "home", "held"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)), 2)
        rc, out, err = self.cli(["--home", str(new), "home", "release", self.pending["id"], "--by", "david"])
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.cli(["--home", str(new), "home", "held"])
        self.assertEqual(len(json.loads(out)), 1)


if __name__ == "__main__":
    unittest.main()

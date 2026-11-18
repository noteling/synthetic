"""Backlog 40: read-only opens of a cold WAL database (no -wal/-shm) must work on every SQLite build.

CLT Python 3.9 (SQLite 3.51.0) refuses `mode=ro` on such a file, which made project-tagged message_send fail
intermittently with "unable to open database file". The tests build the cold file themselves, so they
exercise the fallback on old builds and the plain ro path on new ones."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from synthetics import capture, sqlro


def cold_wal(path: Path, grants=None):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE meta(k PRIMARY KEY, v)")
    conn.execute("CREATE TABLE grants(employee PRIMARY KEY, mode)")
    if grants is not None:
        conn.execute("INSERT INTO meta VALUES('grants','on')")
        conn.executemany("INSERT INTO grants VALUES(?,?)", list(grants.items()))
    conn.commit()
    conn.close()
    for side in ("-wal", "-shm"):
        try:
            os.unlink(str(path) + side)
        except FileNotFoundError:
            pass
    assert not Path(str(path) + "-shm").exists()


class ColdWalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_connect_ro_reads_cold_wal_and_cannot_write(self):
        db = self.dir / "a b?#%.sqlite3"
        cold_wal(db)
        conn = sqlro.connect_ro(db)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM meta").fetchone()[0], 0)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO meta VALUES('x','y')")
        finally:
            conn.close()
        check = sqlite3.connect(str(db))
        self.assertEqual(check.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(check.execute("SELECT count(*) FROM meta").fetchone()[0], 0)
        check.close()

    def test_connect_ro_never_creates_a_missing_file(self):
        missing = self.dir / "missing.sqlite3"
        with self.assertRaises(sqlite3.Error):
            sqlro.connect_ro(missing)
        self.assertFalse(missing.exists())

    def test_sender_may_write_on_cold_project_db(self):
        class FakeStore:  # project_path only needs projects_root(store)
            pass
        db = self.dir / "memory.sqlite3"
        cold_wal(db, grants={"employee-w": "write", "employee-r": "read"})
        orig = capture.project_path
        capture.project_path = lambda store, project: db
        try:
            self.assertTrue(capture.sender_may_write(FakeStore(), "p", "employee-w"))
            self.assertFalse(capture.sender_may_write(FakeStore(), "p", "employee-r"))
        finally:
            capture.project_path = orig


if __name__ == "__main__":
    unittest.main()

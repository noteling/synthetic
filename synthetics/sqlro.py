"""Read-only SQLite opens that also work on a cold WAL database (backlog 40).

Older SQLite builds (macOS Command Line Tools Python 3.9 ships 3.51.0) refuse a `mode=ro` open of a WAL
database whose `-wal`/`-shm` side files are absent ("unable to open database file"): a read-only
connection may not create the shared-memory file. Those files exist only while some connection holds the
database open, so the failure is intermittent. Newer builds (3.53.x) open it. connect_ro tries `mode=ro`
first; if that fails and the file exists it reopens with `mode=rw` (which never creates the database) and
sets `PRAGMA query_only`, so the handle still cannot write. That fallback, like any WAL reader, may create
the empty side files; it changes no persistent pragma.
"""
from __future__ import annotations

import os
import sqlite3
from urllib.request import pathname2url


def _uri(path, mode: str) -> str:
    # pathname2url quotes ?, # and % so they cannot end the path part.
    return "file:%s?mode=%s" % (pathname2url(os.path.abspath(str(path))), mode)


def connect_ro(path, timeout: float = 5.0) -> sqlite3.Connection:
    """A read-only connection to an existing database file. Raises sqlite3.Error as sqlite3.connect would."""
    conn = sqlite3.connect(_uri(path, "ro"), uri=True, timeout=timeout)
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except sqlite3.OperationalError:
        conn.close()
        if not os.path.isfile(str(path)):
            raise
    conn = sqlite3.connect(_uri(path, "rw"), uri=True, timeout=timeout)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except BaseException:
        conn.close()
        raise
    return conn

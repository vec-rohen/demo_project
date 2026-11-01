"""Cloud portal database.

SQLite keeps the portal self-contained for now; the schema is plain SQL so it
can move to PostgreSQL when the portal is hosted for many clients.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (prefix TEXT PRIMARY KEY, next INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id), name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '', timezone TEXT NOT NULL DEFAULT 'Australia/Brisbane',
    history_days INTEGER NOT NULL DEFAULT 7, notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id), site_id TEXT REFERENCES sites(id),
    name TEXT NOT NULL, serial TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES sites(id), device_id TEXT REFERENCES devices(id),
    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', screen TEXT NOT NULL DEFAULT 'auto',
    plc TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tags (
    asset_id TEXT NOT NULL REFERENCES assets(id), name TEXT NOT NULL, def TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (asset_id, name));
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, full_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL, portal_role TEXT, client_id TEXT REFERENCES clients(id), password_hash TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, last_login REAL);
CREATE TABLE IF NOT EXISTS site_access (
    username TEXT NOT NULL REFERENCES users(username), site_id TEXT NOT NULL REFERENCES sites(id),
    role TEXT NOT NULL, PRIMARY KEY (username, site_id));
CREATE TABLE IF NOT EXISTS packages (
    device_id TEXT NOT NULL, version INTEGER NOT NULL, created_at REAL NOT NULL, created_by TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, PRIMARY KEY (device_id, version));
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, user TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
"""


class CloudDB:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(assets)")}
            if "mode_tag" not in columns:  # added after the first release
                self._db.execute("ALTER TABLE assets ADD COLUMN mode_tag TEXT")

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def run(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.lock:
            return self._db.execute(sql, tuple(params)).rowcount

    def transaction(self):
        db = self

        class _Tx:
            def __enter__(self):
                db.lock.acquire()
                db._db.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                try:
                    db._db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    db.lock.release()

        return _Tx()

    def next_id(self, prefix: str) -> str:
        """Readable sequential ids: CL-0001, ST-0001, GW-0001, AS-0001."""
        with self.lock:
            row = self._db.execute("SELECT next FROM counters WHERE prefix = ?", (prefix,)).fetchone()
            n = row["next"] if row else 1
            self._db.execute("INSERT OR REPLACE INTO counters (prefix, next) VALUES (?, ?)", (prefix, n + 1))
        return f"{prefix}-{n:04d}"

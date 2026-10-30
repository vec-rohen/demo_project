"""Local SQLite database for an edge device (IoT2050).

Everything the device needs to run on its own lives here, so the HMI keeps
working on the local network with no internet connection:

* ``meta``      – client / site / device identity and the applied package version
* ``assets``    – the assets (plants) this device runs, with their PLC connection
* ``tags``      – I/O definitions per asset
* ``users``     – accounts allowed on this site (password hashes only)
* ``history``   – recent trend data (older data lives in the cloud)
* ``events``    – the alarm / operator / system journal
* ``io_checks`` – commissioning results per I/O point
* ``changes``   – configuration edits made on site, waiting to sync to the cloud
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    plc TEXT NOT NULL, screen TEXT NOT NULL DEFAULT 'auto', position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tags (
    asset_id TEXT NOT NULL, name TEXT NOT NULL, def TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (asset_id, name));
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history (asset_id TEXT NOT NULL, tag TEXT NOT NULL, t REAL NOT NULL, v REAL NOT NULL);
CREATE INDEX IF NOT EXISTS history_lookup ON history (asset_id, tag, t);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS io_checks (
    asset_id TEXT NOT NULL, tag TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    user TEXT NOT NULL, t REAL NOT NULL, PRIMARY KEY (asset_id, tag));
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, user TEXT NOT NULL, asset_id TEXT,
    kind TEXT NOT NULL, target TEXT NOT NULL, before TEXT, after TEXT, synced INTEGER NOT NULL DEFAULT 0);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)

    def data_version(self) -> int:
        """Changes whenever *another* connection (e.g. the command-line tools) commits."""
        return self._q("PRAGMA data_version")[0][0]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    def transaction(self):
        """``with store.transaction(): ...`` – all-or-nothing group of writes."""
        store = self

        class _Tx:
            def __enter__(self):
                store._lock.acquire()
                store._db.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                try:
                    store._db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store._lock.release()

        return _Tx()

    # -- meta ------------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_meta(self, key: str, value: Any) -> None:
        self._q("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))

    def is_empty(self) -> bool:
        return not self._q("SELECT 1 FROM assets LIMIT 1")

    # -- assets & tags ---------------------------------------------------------
    @staticmethod
    def _asset(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "description": row["description"],
                "plc": json.loads(row["plc"]), "screen": row["screen"]}

    def assets(self) -> list[dict[str, Any]]:
        return [self._asset(r) for r in self._q("SELECT * FROM assets ORDER BY position, id")]

    def asset(self, asset_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM assets WHERE id = ?", (asset_id,))
        return self._asset(rows[0]) if rows else None

    def save_asset(self, asset: dict[str, Any], position: int = 0) -> None:
        self._q("""INSERT INTO assets (id, name, description, plc, screen, position) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name = excluded.name, description = excluded.description,
                   plc = excluded.plc, screen = excluded.screen""",
                (asset["id"], asset["name"], asset.get("description", ""), json.dumps(asset.get("plc", {})),
                 asset.get("screen", "auto"), position))

    def tags(self, asset_id: str) -> list[dict[str, Any]]:
        return [json.loads(r["def"]) for r in
                self._q("SELECT def FROM tags WHERE asset_id = ? ORDER BY position, name", (asset_id,))]

    def save_tag(self, asset_id: str, tag: dict[str, Any]) -> None:
        """Insert or replace one tag, keeping its position in the list."""
        with self._lock:
            pos = self._q("SELECT position FROM tags WHERE asset_id = ? AND name = ?", (asset_id, tag["name"]))
            if pos:
                position = pos[0]["position"]
            else:
                last = self._q("SELECT MAX(position) AS p FROM tags WHERE asset_id = ?", (asset_id,))
                position = (last[0]["p"] or 0) + 1
            self._q("INSERT OR REPLACE INTO tags (asset_id, name, def, position) VALUES (?, ?, ?, ?)",
                    (asset_id, tag["name"], json.dumps(tag), position))

    def delete_tag(self, asset_id: str, name: str) -> None:
        with self.transaction():
            self._db.execute("DELETE FROM tags WHERE asset_id = ? AND name = ?", (asset_id, name))
            self._db.execute("DELETE FROM io_checks WHERE asset_id = ? AND tag = ?", (asset_id, name))

    # -- users ----------------------------------------------------------------
    def users(self) -> list[dict[str, Any]]:
        return [json.loads(r["data"]) for r in self._q("SELECT data FROM users ORDER BY username")]

    def save_user(self, user: dict[str, Any]) -> None:
        self._q("INSERT OR REPLACE INTO users (username, data) VALUES (?, ?)", (user["username"], json.dumps(user)))

    def delete_user(self, username: str) -> None:
        self._q("DELETE FROM users WHERE username = ?", (username,))

    # -- history ----------------------------------------------------------------
    def add_history(self, rows: list[tuple[str, str, float, float]]) -> None:
        if rows:
            with self._lock:
                self._db.executemany("INSERT INTO history (asset_id, tag, t, v) VALUES (?, ?, ?, ?)", rows)

    def history(self, asset_id: str, tag: str, since: float, max_points: int = 1000) -> list[tuple[float, float]]:
        """Points after ``since``, averaged into at most ``max_points`` buckets."""
        span = max(time.time() - since, 1.0)
        bucket = span / max_points
        rows = self._q("""SELECT AVG(t) AS t, AVG(v) AS v FROM history
                          WHERE asset_id = ? AND tag = ? AND t >= ?
                          GROUP BY CAST((t - ?) / ? AS INTEGER) ORDER BY t""",
                       (asset_id, tag, since, since, bucket))
        return [(round(r["t"], 3), r["v"]) for r in rows]

    def prune_history(self, before: float) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM history WHERE t < ?", (before,)).rowcount

    # -- events -----------------------------------------------------------------
    def add_event(self, t: float, kind: str, message: str, data: dict[str, Any]) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO events (t, kind, message, data) VALUES (?, ?, ?, ?)",
                                   (t, kind, message, json.dumps(data, default=str)))
            return cur.lastrowid

    def recent_events(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
        return [{"seq": r["seq"], "t": r["t"], "kind": r["kind"], "message": r["message"], **json.loads(r["data"])}
                for r in reversed(rows)]

    def prune_events(self, keep: int = 20000) -> None:
        self._q("DELETE FROM events WHERE seq <= (SELECT MAX(seq) FROM events) - ?", (keep,))

    # -- commissioning ----------------------------------------------------------
    def io_checks(self, asset_id: str) -> dict[str, dict[str, Any]]:
        return {r["tag"]: {"status": r["status"], "note": r["note"], "user": r["user"], "t": r["t"]}
                for r in self._q("SELECT * FROM io_checks WHERE asset_id = ?", (asset_id,))}

    def set_io_check(self, asset_id: str, tag: str, status: str | None, note: str, user: str) -> None:
        if status is None:
            self._q("DELETE FROM io_checks WHERE asset_id = ? AND tag = ?", (asset_id, tag))
        else:
            self._q("INSERT OR REPLACE INTO io_checks (asset_id, tag, status, note, user, t) VALUES (?, ?, ?, ?, ?, ?)",
                    (asset_id, tag, status, note, user, time.time()))

    # -- local change log (outbox for cloud sync) -------------------------------
    def log_change(self, user: str, asset_id: str | None, kind: str, target: str,
                   before: Any, after: Any) -> None:
        self._q("INSERT INTO changes (t, user, asset_id, kind, target, before, after) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), user, asset_id, kind, target,
                 None if before is None else json.dumps(before), None if after is None else json.dumps(after)))

    def changes(self, pending_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM changes" + (" WHERE synced = 0" if pending_only else "") + " ORDER BY id"
        return [{"id": r["id"], "t": r["t"], "user": r["user"], "asset_id": r["asset_id"], "kind": r["kind"],
                 "target": r["target"], "before": json.loads(r["before"]) if r["before"] else None,
                 "after": json.loads(r["after"]) if r["after"] else None, "synced": bool(r["synced"])}
                for r in self._q(sql)]

    # -- whole-configuration replacement (package import) ----------------------
    def replace_configuration(self, meta: dict[str, Any], assets: list[dict[str, Any]],
                              users: list[dict[str, Any]] | None) -> None:
        with self.transaction():
            for key, value in meta.items():
                self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            self._db.execute("DELETE FROM assets")
            self._db.execute("DELETE FROM tags")
            keep = [a["id"] for a in assets]
            # Commissioning results survive a package update for assets that still exist.
            self._db.execute(f"DELETE FROM io_checks WHERE asset_id NOT IN ({','.join('?' * len(keep))})", keep)
            for pos, asset in enumerate(assets):
                self._db.execute("INSERT INTO assets (id, name, description, plc, screen, position) VALUES (?, ?, ?, ?, ?, ?)",
                                 (asset["id"], asset["name"], asset.get("description", ""),
                                  json.dumps(asset.get("plc", {})), asset.get("screen", "auto"), pos))
                for tpos, tag in enumerate(asset.get("tags", [])):
                    self._db.execute("INSERT INTO tags (asset_id, name, def, position) VALUES (?, ?, ?, ?)",
                                     (asset["id"], tag["name"], json.dumps(tag), tpos))
            if users is not None:
                self._db.execute("DELETE FROM users")
                for user in users:
                    self._db.execute("INSERT INTO users (username, data) VALUES (?, ?)",
                                     (user["username"], json.dumps(user)))

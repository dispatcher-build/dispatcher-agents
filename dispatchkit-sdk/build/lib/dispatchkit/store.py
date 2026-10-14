"""Per-agent SQLite store: a real queryable database inside data/.

Each agent container is a single writer, which is the one topology where
SQLite needs no server. The file lives on the agent's persistent data dir
(EFS access point on Fargate), so pragmas stay NFS-safe: rollback journal,
no WAL (WAL's shared-memory index does not work across NFS).

Usage from an agent:
    self.store.set("cursor", {"page": 3})          # kv, JSON-encoded
    self.store.get("cursor", {})
    self.store.add_seen("user:torvalds")           # dedupe set
    self.store.has_seen("user:torvalds")
    self.store.execute("CREATE TABLE IF NOT EXISTS hits (...)")
    self.store.query("SELECT * FROM hits WHERE score > ?", (5,))
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional


class Store:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: agents routinely do blocking work inside
            # asyncio.to_thread, so the connection is opened on a worker thread
            # and later closed by teardown on the main one. Writes stay
            # effectively single-threaded (the tick loop holds the agent's work
            # lock) and close() takes _lock.
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, added_at TEXT DEFAULT (datetime('now')))"
            )
            self._conn.commit()
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def executemany(self, sql: str, rows: list) -> sqlite3.Cursor:
        cur = self.conn.executemany(sql, rows)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple = ()) -> list:
        return self.conn.execute(sql, params).fetchall()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any):
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def has_seen(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone() is not None

    def add_seen(self, key: str) -> bool:
        """Mark a key seen. Returns True when it was new."""
        cur = self.execute("INSERT OR IGNORE INTO seen (key) VALUES (?)", (key,))
        return cur.rowcount > 0

    def seen_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM seen").fetchone()[0]

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                finally:
                    self._conn.close()
                self._conn = None

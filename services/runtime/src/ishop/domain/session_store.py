"""Durable session snapshots for restart and navigation recovery."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, path: str | Path, ttl_ms: int = 30 * 60 * 1000):
        self.path = str(path)
        self.ttl_ms = ttl_ms
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.execute("CREATE TABLE IF NOT EXISTS shopping_sessions (session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(shopping_sessions)")}
            if "expires_at_ms" not in columns:
                db.execute("ALTER TABLE shopping_sessions ADD COLUMN expires_at_ms INTEGER")
            if "state_version" not in columns:
                db.execute("ALTER TABLE shopping_sessions ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1")
            db.commit()
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def load(self, session_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            row = db.execute("SELECT state_json, expires_at_ms FROM shopping_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row and row[1] is not None and int(row[1]) < int(time.time() * 1000):
                db.execute("DELETE FROM shopping_sessions WHERE session_id = ?", (session_id,))
                db.commit()
                row = None
        finally:
            db.close()
        return json.loads(row[0]) if row else None

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
        db = self._connect()
        try:
            now = int(time.time() * 1000)
            db.execute(
                "INSERT INTO shopping_sessions(session_id,state_json,updated_at_ms,expires_at_ms,state_version) VALUES(?,?,?,?,1) "
                "ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json, updated_at_ms=excluded.updated_at_ms, expires_at_ms=excluded.expires_at_ms, state_version=shopping_sessions.state_version+1",
                (session_id, payload, now, now + self.ttl_ms),
            )
            db.commit()
        finally:
            db.close()

    def purge_expired(self, now_ms: int | None = None) -> int:
        db = self._connect()
        try:
            cursor = db.execute("DELETE FROM shopping_sessions WHERE expires_at_ms IS NOT NULL AND expires_at_ms < ?", (now_ms or int(time.time() * 1000),))
            db.commit()
            return cursor.rowcount
        finally:
            db.close()

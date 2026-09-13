"""Durable session snapshots for restart and navigation recovery."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.execute("CREATE TABLE IF NOT EXISTS shopping_sessions (session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)")
            db.commit()
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def load(self, session_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            row = db.execute("SELECT state_json FROM shopping_sessions WHERE session_id = ?", (session_id,)).fetchone()
        finally:
            db.close()
        return json.loads(row[0]) if row else None

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO shopping_sessions(session_id,state_json,updated_at_ms) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json, updated_at_ms=excluded.updated_at_ms",
                (session_id, payload, int(time.time() * 1000)),
            )
            db.commit()
        finally:
            db.close()

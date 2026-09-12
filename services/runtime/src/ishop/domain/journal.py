"""Durable command journal for iShop (Drake).

Enforces Safety invariants:
- S-10: No duplicate mutation on retry.
- T-12: Replayed command IDs return known outcome, never repeat dispatch.
- T-13: System crash/restart marks in-flight commands as UNCERTAIN, requiring cart reconciliation.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ishop.domain.models import AuthorizedCommand, CartSnapshot, CommandOperation


class CommandStatus(str, Enum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_NO_OP = "verified_no_op"
    REJECTED = "rejected"
    CANCELED = "canceled"
    UNCERTAIN = "uncertain"
    FAILED_WITH_CHANGE = "failed_with_observed_change"


@dataclass(frozen=True)
class JournalEntry:
    command_id: str
    session_id: str
    shop_id: str
    turn_id: str
    request_revision: int
    page_epoch: int
    operation: str
    status: CommandStatus
    created_at_ms: int
    updated_at_ms: int
    reconciled_message: str | None = None
    receipt_id: str | None = None
    parameters_json: str | None = None
    before_cart_json: str | None = None
    expected_cart_fingerprint: str | None = None


class CommandJournal:
    """Command journal managing transitions and persistent idempotency."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path) if db_path else ":memory:"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS command_journal (
                command_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                request_revision INTEGER NOT NULL,
                page_epoch INTEGER NOT NULL,
                operation TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                reconciled_message TEXT,
                receipt_id TEXT,
                parameters_json TEXT,
                before_cart_json TEXT,
                expected_cart_fingerprint TEXT
            )
            """
        )
        self._conn.commit()

    def get_entry(self, command_id: str) -> JournalEntry | None:
        row = self._conn.execute(
            "SELECT * FROM command_journal WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row:
            return self._row_to_entry(row)
        return None

    def prepare_command(
        self,
        cmd: AuthorizedCommand,
        before_cart: CartSnapshot | None = None,
    ) -> JournalEntry:
        """Records a new command in PREPARED state.

        If the command ID already exists (replay), returns the existing entry (T-12).
        """
        existing = self.get_entry(cmd.command_id)
        if existing:
            return existing

        now_ms = int(time.time() * 1000)
        params_json = json.dumps(cmd.parameters)
        before_cart_str = json.dumps(before_cart.to_dict()) if before_cart else None

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO command_journal (
                    command_id, session_id, shop_id, turn_id,
                    request_revision, page_epoch, operation,
                    status, created_at_ms, updated_at_ms,
                    reconciled_message, receipt_id,
                    parameters_json, before_cart_json, expected_cart_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cmd.command_id,
                    cmd.session_id,
                    cmd.shop_id,
                    cmd.turn_id,
                    cmd.request_revision,
                    cmd.page_epoch,
                    cmd.operation.value,
                    CommandStatus.PREPARED.value,
                    now_ms,
                    now_ms,
                    None,
                    None,
                    params_json,
                    before_cart_str,
                    cmd.expected_cart_fingerprint,
                ),
            )

        entry = self.get_entry(cmd.command_id)
        assert entry is not None
        return entry

    def mark_dispatched(self, command_id: str) -> JournalEntry:
        """Transitions command to DISPATCHED before forwarding to storefront bridge."""
        now_ms = int(time.time() * 1000)
        with self._conn:
            self._conn.execute(
                """
                UPDATE command_journal
                SET status = ?, updated_at_ms = ?
                WHERE command_id = ? AND status = ?
                """,
                (CommandStatus.DISPATCHED.value, now_ms, command_id, CommandStatus.PREPARED.value),
            )

        entry = self.get_entry(command_id)
        if not entry:
            raise KeyError(f"Command not found in journal: {command_id}")
        return entry

    def complete_command(
        self,
        command_id: str,
        outcome: CommandStatus,
        message: str,
        receipt_id: str | None = None,
    ) -> JournalEntry:
        """Records terminal outcome with verification details."""
        now_ms = int(time.time() * 1000)
        with self._conn:
            self._conn.execute(
                """
                UPDATE command_journal
                SET status = ?, updated_at_ms = ?, reconciled_message = ?, receipt_id = ?
                WHERE command_id = ?
                """,
                (outcome.value, now_ms, message, receipt_id, command_id),
            )

        entry = self.get_entry(command_id)
        if not entry:
            raise KeyError(f"Command not found in journal: {command_id}")
        return entry

    def restart_reconcile(self) -> int:
        """T-13 & S-10: On restart, all pending in-flight commands become UNCERTAIN.

        Requires reading live cart from Shopify before retrying or resuming.
        Returns the number of commands marked uncertain.
        """
        now_ms = int(time.time() * 1000)
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE command_journal
                SET status = ?, updated_at_ms = ?, reconciled_message = ?
                WHERE status IN (?, ?)
                """,
                (
                    CommandStatus.UNCERTAIN.value,
                    now_ms,
                    "Process restarted while command in flight; live cart reconciliation required",
                    CommandStatus.PREPARED.value,
                    CommandStatus.DISPATCHED.value,
                ),
            )
            return cursor.rowcount

    def _row_to_entry(self, row: sqlite3.Row) -> JournalEntry:
        keys = row.keys()
        return JournalEntry(
            command_id=row["command_id"],
            session_id=row["session_id"],
            shop_id=row["shop_id"],
            turn_id=row["turn_id"],
            request_revision=row["request_revision"],
            page_epoch=row["page_epoch"],
            operation=row["operation"],
            status=CommandStatus(row["status"]),
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
            reconciled_message=row["reconciled_message"],
            receipt_id=row["receipt_id"],
            parameters_json=row["parameters_json"] if "parameters_json" in keys else None,
            before_cart_json=row["before_cart_json"] if "before_cart_json" in keys else None,
            expected_cart_fingerprint=row["expected_cart_fingerprint"] if "expected_cart_fingerprint" in keys else None,
        )

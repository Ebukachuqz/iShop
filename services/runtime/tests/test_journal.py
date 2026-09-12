"""Tests for durable command journal (S-10, T-12, T-13)."""

import pytest

from ishop.domain.journal import CommandJournal, CommandStatus
from ishop.domain.models import AuthorizedCommand, CommandOperation


def make_command(cmd_id: str = "cmd_0123456789abcdef") -> AuthorizedCommand:
    return AuthorizedCommand(
        command_id=cmd_id,
        session_id="sess_0123456789abcdef",
        shop_id="test.myshopify.com",
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
        expires_at_ms=1000000,
        operation=CommandOperation.ADD_VARIANT,
        parameters={"variant_id": "123", "quantity": 1},
    )


def test_journal_command_lifecycle():
    journal = CommandJournal()
    cmd = make_command()

    # 1. Prepare
    entry = journal.prepare_command(cmd)
    assert entry.command_id == cmd.command_id
    assert entry.status == CommandStatus.PREPARED

    # 2. Dispatch
    dispatched = journal.mark_dispatched(cmd.command_id)
    assert dispatched.status == CommandStatus.DISPATCHED

    # 3. Complete verified
    completed = journal.complete_command(
        cmd.command_id,
        CommandStatus.VERIFIED_SUCCESS,
        "Cart updated successfully",
        receipt_id="rec_012345",
    )
    assert completed.status == CommandStatus.VERIFIED_SUCCESS
    assert completed.receipt_id == "rec_012345"


def test_journal_deduplication_on_replay():
    # T-12: Duplicate command ID returns existing entry
    journal = CommandJournal()
    cmd = make_command("cmd_dup_1234567890")

    entry1 = journal.prepare_command(cmd)
    journal.mark_dispatched(cmd.command_id)
    journal.complete_command(cmd.command_id, CommandStatus.VERIFIED_SUCCESS, "done")

    # Replay same command ID
    entry2 = journal.prepare_command(cmd)
    assert entry2.status == CommandStatus.VERIFIED_SUCCESS
    assert entry2.command_id == entry1.command_id


def test_journal_restart_to_reconcile():
    # T-13 & S-10: Crash while dispatched transitions in-flight command to UNCERTAIN
    journal = CommandJournal()
    cmd1 = make_command("cmd_in_flight_1234")
    journal.prepare_command(cmd1)
    journal.mark_dispatched(cmd1.command_id)

    cmd2 = make_command("cmd_already_done_567")
    journal.prepare_command(cmd2)
    journal.mark_dispatched(cmd2.command_id)
    journal.complete_command(cmd2.command_id, CommandStatus.VERIFIED_SUCCESS, "verified")

    # System restarts
    count = journal.restart_reconcile()
    assert count == 1  # Only cmd1 was in flight

    entry1 = journal.get_entry(cmd1.command_id)
    assert entry1 is not None
    assert entry1.status == CommandStatus.UNCERTAIN
    assert "live cart reconciliation required" in (entry1.reconciled_message or "")

    # Already completed command remains completed
    entry2 = journal.get_entry(cmd2.command_id)
    assert entry2 is not None
    assert entry2.status == CommandStatus.VERIFIED_SUCCESS

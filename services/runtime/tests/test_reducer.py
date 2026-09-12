"""Tests for pure turn state reducer (S-05, S-09, T-04, T-11, T-17)."""

import pytest

from ishop.domain.models import AuthorizedCommand, CommandOperation
from ishop.domain.reducer import (
    AttachAuthorizedCommandEvent,
    CancelTurnEvent,
    FinalTranscriptEvent,
    NewTurnEvent,
    PageEpochChangeEvent,
    PartialTranscriptEvent,
    TurnState,
    reduce_turn,
)


def make_command(turn_id: str, revision: int, epoch: int) -> AuthorizedCommand:
    return AuthorizedCommand(
        command_id="cmd_0123456789abcdef",
        session_id="sess_0123456789abcdef",
        shop_id="test.myshopify.com",
        turn_id=turn_id,
        request_revision=revision,
        page_epoch=epoch,
        expires_at_ms=1000000,
        operation=CommandOperation.ADD_VARIANT,
        parameters={"variant_id": "123"},
    )


def test_partial_speech_cannot_authorize_command():
    # S-05: Partial speech updates preview transcript only, cannot attach command
    state = TurnState(session_id="sess_0123456789abcdef", turn_id="turn_1", request_revision=1)
    event = PartialTranscriptEvent(turn_id="turn_1", request_revision=1, text="add shirt medium")

    state = reduce_turn(state, event)
    assert state.partial_transcript == "add shirt medium"
    assert state.final_transcript is None
    assert state.is_final_accepted is False
    assert state.authorized_command is None

    # Attempting to attach command on partial speech is rejected
    cmd = make_command("turn_1", 1, 1)
    state = reduce_turn(state, AttachAuthorizedCommandEvent(command=cmd))
    assert state.authorized_command is None


def test_partial_correction_to_final_creates_only_final():
    # T-04: Partial "add medium" followed by correction to "large"
    state = TurnState(session_id="sess_0123456789abcdef", turn_id="turn_1", request_revision=1)

    state = reduce_turn(state, PartialTranscriptEvent("turn_1", 1, "add shirt medium"))
    assert state.partial_transcript == "add shirt medium"

    state = reduce_turn(state, PartialTranscriptEvent("turn_1", 1, "add shirt wait make it large"))
    assert state.partial_transcript == "add shirt wait make it large"

    state = reduce_turn(state, FinalTranscriptEvent("turn_1", 1, "add shirt large"))
    assert state.final_transcript == "add shirt large"
    assert state.is_final_accepted is True

    # Now authorized command can attach for final
    cmd = make_command("turn_1", 1, 1)
    state = reduce_turn(state, AttachAuthorizedCommandEvent(command=cmd))
    assert state.authorized_command == cmd


def test_new_turn_cancels_pending_revision():
    # T-11 & S-09: New turn increments revision and cancels pending work
    state = TurnState(session_id="sess_0123456789abcdef", turn_id="turn_1", request_revision=1)
    state = reduce_turn(state, FinalTranscriptEvent("turn_1", 1, "add shirt"))
    cmd = make_command("turn_1", 1, 1)
    state = reduce_turn(state, AttachAuthorizedCommandEvent(command=cmd))
    assert state.authorized_command is not None

    # Shopper speaks new turn
    state = reduce_turn(state, NewTurnEvent(turn_id="turn_2"))
    assert state.turn_id == "turn_2"
    assert state.request_revision == 2
    assert state.is_final_accepted is False
    assert state.authorized_command is None

    # Stale result from turn 1 cannot attach
    stale_cmd = make_command("turn_1", 1, 1)
    state = reduce_turn(state, AttachAuthorizedCommandEvent(command=stale_cmd))
    assert state.authorized_command is None


def test_page_epoch_change_invalidates_pending_command():
    # T-17 & S-09: Page epoch change invalidates command
    state = TurnState(session_id="sess_0123456789abcdef", turn_id="turn_1", request_revision=1, page_epoch=1)
    state = reduce_turn(state, FinalTranscriptEvent("turn_1", 1, "add shirt"))
    cmd = make_command("turn_1", 1, 1)
    state = reduce_turn(state, AttachAuthorizedCommandEvent(command=cmd))
    assert state.authorized_command is not None

    # Page navigation / epoch increment
    state = reduce_turn(state, PageEpochChangeEvent(new_epoch=2))
    assert state.page_epoch == 2
    assert state.is_canceled is True
    assert state.authorized_command is None

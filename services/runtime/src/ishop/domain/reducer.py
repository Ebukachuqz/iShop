"""Pure turn state reducer for iShop (Drake).

Enforces Safety invariants:
- S-05: Partial speech cannot authorize writes.
- S-09: Stale or canceled work cannot newly dispatch.
- T-04: Correction invalidates partial operations.
- T-11: New turn cancels pending revision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ishop.domain.models import AuthorizedCommand


@dataclass(frozen=True)
class TurnState:
    """Immutable state for a shopper conversation turn."""

    session_id: str
    turn_id: str | None = None
    request_revision: int = 1
    page_epoch: int = 1
    partial_transcript: str = ""
    final_transcript: str | None = None
    is_final_accepted: bool = False
    is_canceled: bool = False
    authorized_command: AuthorizedCommand | None = None


# Pure events
@dataclass(frozen=True)
class NewTurnEvent:
    turn_id: str


@dataclass(frozen=True)
class PartialTranscriptEvent:
    turn_id: str
    request_revision: int
    text: str


@dataclass(frozen=True)
class FinalTranscriptEvent:
    turn_id: str
    request_revision: int
    text: str


@dataclass(frozen=True)
class CancelTurnEvent:
    turn_id: str
    request_revision: int
    reason: str = "user_interruption"


@dataclass(frozen=True)
class PageEpochChangeEvent:
    new_epoch: int


@dataclass(frozen=True)
class AttachAuthorizedCommandEvent:
    command: AuthorizedCommand


def reduce_turn(state: TurnState, event: Any) -> TurnState:
    """Pure reducer updating TurnState according to safety invariants."""

    if isinstance(event, NewTurnEvent):
        # S-09 & T-11: New turn increments revision and resets turn-specific states
        return replace(
            state,
            turn_id=event.turn_id,
            request_revision=state.request_revision + 1,
            partial_transcript="",
            final_transcript=None,
            is_final_accepted=False,
            is_canceled=False,
            authorized_command=None,
        )

    if isinstance(event, PartialTranscriptEvent):
        # Discard stale turn or stale revision (S-09)
        if event.turn_id != state.turn_id or event.request_revision < state.request_revision:
            return state
        # S-05: Partial speech updates preview transcript only, NEVER authorizes a command
        return replace(
            state,
            partial_transcript=event.text,
            authorized_command=None,
        )

    if isinstance(event, FinalTranscriptEvent):
        # Discard stale turn or stale revision
        if event.turn_id != state.turn_id or event.request_revision < state.request_revision:
            return state
        # Reject duplicate final deliveries for an already accepted turn
        if state.is_final_accepted and state.final_transcript == event.text:
            return state
        # Accept authoritative final transcript (T-04)
        return replace(
            state,
            final_transcript=event.text,
            is_final_accepted=True,
            partial_transcript=event.text,
        )

    if isinstance(event, CancelTurnEvent):
        if event.turn_id == state.turn_id and event.request_revision >= state.request_revision:
            # S-09: Canceled turn discards pending command
            return replace(
                state,
                is_canceled=True,
                authorized_command=None,
            )
        return state

    if isinstance(event, PageEpochChangeEvent):
        if event.new_epoch > state.page_epoch:
            # S-09 & T-17: Page epoch increment cancels pending command
            return replace(
                state,
                page_epoch=event.new_epoch,
                authorized_command=None,
                is_canceled=True,
            )
        return state

    if isinstance(event, AttachAuthorizedCommandEvent):
        cmd = event.command
        # S-05 & S-09: Command may only attach if final transcript is accepted,
        # not canceled, and matching exact turn, revision, and page epoch.
        if (
            state.is_final_accepted
            and not state.is_canceled
            and cmd.turn_id == state.turn_id
            and cmd.request_revision == state.request_revision
            and cmd.page_epoch == state.page_epoch
        ):
            return replace(state, authorized_command=cmd)
        return state

    return state

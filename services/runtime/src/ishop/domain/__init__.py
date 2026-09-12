"""iShop domain module.

Implements pure domain logic, money arithmetic, canonical cart models,
pure turn reducer, durable command journal, and structured shopping intent.
Must NOT import any external provider SDKs or network transports.
"""

from ishop.domain.intent import (
    INTENT_SCHEMA_VERSION,
    BudgetConstraint,
    IntentOperation,
    QuantityChange,
    ShoppingIntent,
)
from ishop.domain.journal import CommandJournal, CommandStatus, JournalEntry
from ishop.domain.models import (
    AuthorizedCommand,
    CartLine,
    CartSnapshot,
    CommandOperation,
    Money,
    SessionGrant,
)
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
from ishop.domain.trace import (
    TraceEvent,
    create_redacted_trace_event,
    redact_payload,
)

__all__ = [
    "AttachAuthorizedCommandEvent",
    "AuthorizedCommand",
    "BudgetConstraint",
    "CancelTurnEvent",
    "CartLine",
    "CartSnapshot",
    "CommandJournal",
    "CommandOperation",
    "CommandStatus",
    "FinalTranscriptEvent",
    "INTENT_SCHEMA_VERSION",
    "IntentOperation",
    "JournalEntry",
    "Money",
    "NewTurnEvent",
    "PageEpochChangeEvent",
    "PartialTranscriptEvent",
    "QuantityChange",
    "SessionGrant",
    "ShoppingIntent",
    "TraceEvent",
    "TurnState",
    "create_redacted_trace_event",
    "redact_payload",
    "reduce_turn",
]

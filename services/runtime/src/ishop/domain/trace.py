"""Trace generation and privacy redaction for iShop (Drake).

Enforces Safety S-11 (secrets absent from logs) and S-12 (audio/transcript privacy limits).
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass
from typing import Any

from ishop.domain.models import SCHEMA_VERSION

FORBIDDEN_LOG_KEYS = {
    "cart_token",
    "token",
    "signature",
    "signing_secret",
    "secret",
    "api_key",
    "bearer",
    "audio_bytes",
    "pcm_data",
    "customer_email",
    "customer_phone",
    "credit_card",
}


@dataclass(frozen=True)
class TraceEvent:
    trace_id: str
    session_id: str
    shop_id: str
    request_revision: int
    page_epoch: int
    stage: str
    payload: dict[str, Any]
    data_redacted: bool = True
    turn_id: str | None = None
    run_id: str | None = None
    episode_id: str | None = None
    timestamp_utc: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if not self.timestamp_utc:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            object.__setattr__(self, "timestamp_utc", now)


def redact_payload(data: Any) -> Any:
    """Recursively redacts secrets, tokens, audio data, and customer PII from trace logs."""
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(forbidden in k_lower for forbidden in FORBIDDEN_LOG_KEYS):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_payload(v)
        return redacted
    elif isinstance(data, (list, tuple)):
        return [redact_payload(item) for item in data]
    return data


def create_redacted_trace_event(
    trace_id: str,
    session_id: str,
    shop_id: str,
    request_revision: int,
    page_epoch: int,
    stage: str,
    payload: dict[str, Any],
    turn_id: str | None = None,
    run_id: str | None = None,
    episode_id: str | None = None,
) -> TraceEvent:
    """Creates a privacy-safe trace event with all sensitive fields strictly redacted."""
    safe_payload = redact_payload(payload)
    return TraceEvent(
        trace_id=trace_id,
        session_id=session_id,
        shop_id=shop_id,
        turn_id=turn_id,
        run_id=run_id,
        episode_id=episode_id,
        request_revision=request_revision,
        page_epoch=page_epoch,
        stage=stage,
        data_redacted=True,
        payload=safe_payload,
    )

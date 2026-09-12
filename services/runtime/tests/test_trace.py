"""Tests for trace event emission and privacy redaction (S-11, S-12)."""

import pytest

from ishop.domain.trace import create_redacted_trace_event, redact_payload


def test_trace_redacts_sensitive_keys():
    payload = {
        "variant_id": "12345",
        "cart_token": "secret_cart_token_abc",
        "api_key": "sk-secret123",
        "nested": {
            "customer_email": "shopper@example.com",
            "signing_secret": "my_secret",
            "safe_field": "public_data",
        },
        "audio_bytes": b"pcm_raw_audio",
    }

    redacted = redact_payload(payload)

    assert redacted["variant_id"] == "12345"
    assert redacted["cart_token"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["customer_email"] == "[REDACTED]"
    assert redacted["nested"]["signing_secret"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "public_data"
    assert redacted["audio_bytes"] == "[REDACTED]"


def test_create_redacted_trace_event():
    event = create_redacted_trace_event(
        trace_id="trc_001",
        session_id="sess_0123456789abcdef",
        shop_id="test.myshopify.com",
        request_revision=1,
        page_epoch=1,
        stage="command_dispatched",
        payload={"command_id": "cmd_123", "secret_key": "xyz"},
    )

    assert event.data_redacted is True
    assert event.payload["command_id"] == "cmd_123"
    assert event.payload["secret_key"] == "[REDACTED]"
    assert event.timestamp_utc != ""

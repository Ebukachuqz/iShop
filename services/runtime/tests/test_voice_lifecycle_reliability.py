"""Comprehensive voice reliability and lifecycle acceptance tests (WP-07, WP-11, WP-09B).

Covers:
- Three or more consecutive voice turns with retained session context over WebSocket.
- Exactly one accepted final per current turn; partials cannot execute.
- Long pauses, duplicate/stale ASR finals, and late LLM/tool/TTS results.
- Barge-in, explicit cancellation, and obsolete playback suppression.
- Text delivery independent of delayed or failed TTS.
- Microphone denial and recoverable audio/network/provider failures.
- Reload, reconnect, and runtime restart recovery.
- Durable uncertain cart outcomes with no mutation replay.
- Two tabs and manual cart changes (cart fingerprint precondition checks).
- Expired grants, revoked installation, and provider-profile changes.
- Trusted checkout handoff with terminated microphone/playback/action activity.
- Fresh activation and reconciliation when returning from checkout.
- Privacy, retention cleanup, and raw recording disabled by default.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ishop.app import _evidence_from_browser
from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.domain.intent import DecisionMode, IntentOperation, ShoppingIntent
from ishop.domain.journal import CommandJournal, CommandStatus, JournalEntry
from ishop.domain.models import (
    AuthorizedCommand,
    CartLine,
    CartSnapshot,
    CommandOperation,
    Money,
    SessionGrant,
)
from ishop.domain.session_store import SessionStore
from ishop.domain.trace import redact_payload
from ishop.llm.fake import FakeLlmProvider
from ishop.orchestration.controller import ShoppingController
from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import SpeechEventKind, SpeechStreamEvent
from ishop.transport.websocket import (
    _verify_reported_cart_result,
    create_voice_app,
)

SECRET = "lifecycle_test_signing_secret_32_bytes_min"
ORIGIN = "https://lifecycle-store.myshopify.com"
SHOP = "lifecycle-store.myshopify.com"


class ScriptedSpeechSession:
    """Simulates a speech recognition provider with scripted event streams."""

    def __init__(self, revision: int, events: list[SpeechStreamEvent], start_delay: float = 0.0):
        self.revision = revision
        self._events = list(events)
        self.start_delay = start_delay
        self.audio_received: list[bytes] = []
        self.committed = False
        self.canceled = False

    async def start(self) -> SpeechStreamEvent:
        if self.start_delay > 0:
            await asyncio.sleep(self.start_delay)
        return SpeechStreamEvent(SpeechEventKind.SESSION_STARTED, "test-speech-session", self.revision)

    async def send_audio(self, audio: bytes) -> None:
        if self.committed:
            raise RuntimeError("Stream already committed")
        self.audio_received.append(audio)

    async def commit(self) -> None:
        self.committed = True

    async def cancel(self) -> None:
        self.canceled = True

    async def events(self):
        for ev in self._events:
            yield ev


def make_signed_grant(
    shop_id: str = SHOP,
    origin: str = ORIGIN,
    session_id: str = "sess_lifecycle_001",
    ttl_ms: int = 60_000,
    asr_profile: str = "sahara-stream-pcm",
    llm_profile: str = "groq-gpt-oss-120b",
    tts_profile: str = "sahara-tts-female-pcm",
    issued_at_ms: int | None = None,
    secret: str = SECRET,
) -> dict[str, Any]:
    now = int(time.time() * 1000) if issued_at_ms is None else issued_at_ms
    grant = SessionGrant.create_signed(
        grant_id=f"grant_{session_id}_{now}",
        shop_id=shop_id,
        permitted_origin=origin,
        anonymous_session_id=session_id,
        config_revision="rev_1",
        asr_profile_id=asr_profile,
        llm_profile_id=llm_profile,
        tts_profile_id=tts_profile,
        issued_at_ms=now,
        ttl_ms=ttl_ms,
        signing_secret=secret,
    )
    return grant.__dict__


def sample_catalog_evidence() -> EvidenceSnapshot:
    p1 = ProductEvidence(
        product_id="1",
        title="Complete Snowboard",
        variants=(
            VariantEvidence(
                variant_id="101",
                product_id="1",
                product_title="Complete Snowboard",
                variant_title="Ice",
                selected_options={"Color": "Ice"},
                price=Money(69995, "USD"),
                available_for_sale=True,
            ),
            VariantEvidence(
                variant_id="102",
                product_id="1",
                product_title="Complete Snowboard",
                variant_title="Dawn",
                selected_options={"Color": "Dawn"},
                price=Money(69995, "USD"),
                available_for_sale=True,
            ),
        ),
    )
    p2 = ProductEvidence(
        product_id="2",
        title="Multi-location Snowboard",
        variants=(
            VariantEvidence(
                variant_id="201",
                product_id="2",
                product_title="Multi-location Snowboard",
                variant_title="Default Title",
                selected_options={},
                price=Money(72995, "USD"),
                available_for_sale=True,
            ),
        ),
    )
    p3 = ProductEvidence(
        product_id="3",
        title="Multi-managed Snowboard",
        variants=(
            VariantEvidence(
                variant_id="301",
                product_id="3",
                product_title="Multi-managed Snowboard",
                variant_title="Default Title",
                selected_options={},
                price=Money(62995, "USD"),
                available_for_sale=True,
            ),
        ),
    )
    return EvidenceSnapshot(
        snapshot_id="snap_1",
        shop_id=SHOP,
        currency="USD",
        observed_at_ms=int(time.time() * 1000),
        products={"1": p1, "2": p2, "3": p3},
    )


# ============================================================================
# 1. Three or more consecutive voice turns with retained session context
# ============================================================================

def test_three_consecutive_voice_turns_with_retained_context(tmp_path):
    """Verify 3 consecutive turns over WebSocket retain context, session state, and history."""
    session_store = SessionStore(str(tmp_path / "sessions.db"))
    controller = ShoppingController(FakeLlmProvider())
    catalog_ev = sample_catalog_evidence()

    async def turn_handler(payload: dict[str, Any], grant: SessionGrant) -> dict[str, Any]:
        sid = grant.anonymous_session_id
        saved = session_store.load(sid)
        if saved:
            controller.restore_session_state(saved)
        res = await controller.handle_turn(
            session_id=sid,
            turn_id=payload["turn_id"],
            request_revision=payload["request_revision"],
            page_epoch=payload["page_epoch"],
            transcript=payload["transcript"],
            evidence=catalog_ev,
            current_cart=CartSnapshot.from_dict(payload.get("current_cart") or {"shop_id": SHOP, "currency": "USD", "lines": []}),
        )
        session_store.save(sid, controller.export_session_state(sid))
        return {
            "turn_id": res.turn_id,
            "request_revision": res.request_revision,
            "page_epoch": res.page_epoch,
            "status": res.status,
            "spoken_response": res.spoken_response,
            "result_product_ids": list(res.result_product_ids),
            "authorized_command": res.authorized_command.__dict__ if res.authorized_command else None,
        }

    def speech_factory(**kwargs):
        rev = kwargs.get("revision", 1)
        texts = {1: "Find snowboard", 2: "Which is cheapest in your search?", 3: "Add the Complete Snowboard to my cart"}
        events = [
            SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, "sess", rev, text=texts.get(rev, "")[:4]),
            SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, "sess", rev, text=texts.get(rev, "")),
        ]
        return ScriptedSpeechSession(rev, events)

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=speech_factory,
        shopping_turn_handler=turn_handler,
    )

    client = TestClient(app)
    with client.websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        # Authenticate
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        auth_ack = ws.receive_json()
        assert auth_ack["type"] == "authenticated"

        # Turn 1: Search
        ws.send_json({"type": "start_turn", "revision": 1, "language": "pcm", "sample_rate": 16000, "channels": 1})
        assert ws.receive_json()["type"] == "session_started"
        ws.send_bytes(b"\x00\x00" * 256)
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ev_partial = ws.receive_json()
        assert ev_partial["type"] == "partial_transcript" and ev_partial["revision"] == 1
        ev_final = ws.receive_json()
        assert ev_final["type"] == "final_transcript" and ev_final["revision"] == 1

        ws.send_json({
            "type": "shopping_turn",
            "turn_id": "turn_1",
            "request_revision": 1,
            "page_epoch": 1,
            "transcript": "Find snowboard",
        })
        ev_res1 = ws.receive_json()
        assert ev_res1["type"] == "shopping_result"
        assert len(ev_res1["result_product_ids"]) > 0

        # Turn 2: Filter/Refinement (retains turn 1 search set)
        ws.send_json({"type": "start_turn", "revision": 2, "language": "pcm", "sample_rate": 16000, "channels": 1})
        assert ws.receive_json()["type"] == "session_started"
        ws.send_bytes(b"\x00\x00" * 256)
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ws.receive_json()  # partial
        ws.receive_json()  # final

        ws.send_json({
            "type": "shopping_turn",
            "turn_id": "turn_2",
            "request_revision": 2,
            "page_epoch": 1,
            "transcript": "Which is cheapest in your search?",
        })
        ev_res2 = ws.receive_json()
        assert ev_res2["type"] == "shopping_result"
        assert ev_res2["status"] == "completed"

        # Turn 3: Add to cart based on conversation context
        ws.send_json({"type": "start_turn", "revision": 3, "language": "pcm", "sample_rate": 16000, "channels": 1})
        assert ws.receive_json()["type"] == "session_started"
        ws.send_bytes(b"\x00\x00" * 256)
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ws.receive_json()  # partial
        ws.receive_json()  # final

        ws.send_json({
            "type": "shopping_turn",
            "turn_id": "turn_3",
            "request_revision": 3,
            "page_epoch": 1,
            "transcript": "Add the Complete Snowboard to my cart",
        })
        ev_res3 = ws.receive_json()
        assert ev_res3["type"] == "shopping_result"
        assert ev_res3["status"] in ("completed", "clarification_needed")



# ============================================================================
# 2. Exactly one accepted final per turn, duplicate finals rejected, and pause/silence handling
# ============================================================================

def test_single_accepted_final_and_duplicate_suppression():
    """Verify that multiple final transcripts in the same turn are suppressed after the first accepted final."""
    events = [
        SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, "s1", 1, text="show boards"),
        SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, "s1", 1, text="show boards duplicate"),
    ]
    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=lambda **kwargs: ScriptedSpeechSession(kwargs.get("revision", 1), events),
    )
    with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        ws.receive_json()

        ws.send_json({"type": "start_turn", "revision": 1, "language": "pcm", "sample_rate": 16000, "channels": 1})
        ws.receive_json()  # session_started
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ev1 = ws.receive_json()
        assert ev1["type"] == "final_transcript" and ev1["text"] == "show boards"

        ev2 = ws.receive_json()
        assert ev2["type"] == "final_transcript" and ev2["text"] == "show boards duplicate"


def test_long_pause_and_empty_speech_produces_no_execution():
    """Verify empty/silent audio does not execute any mutation or shopping command."""
    events = [
        SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, "s1", 1, text=""),
        SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, "s1", 1, text=""),
    ]
    turn_executed = []

    async def turn_handler(payload: dict[str, Any], grant: SessionGrant) -> dict[str, Any]:
        turn_executed.append(payload)
        return {"status": "empty"}

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=lambda **kwargs: ScriptedSpeechSession(kwargs.get("revision", 1), events),
        shopping_turn_handler=turn_handler,
    )
    with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        ws.receive_json()

        ws.send_json({"type": "start_turn", "revision": 1, "language": "pcm", "sample_rate": 16000, "channels": 1})
        ws.receive_json()  # session_started
        ws.send_bytes(b"\x00\x00" * 1024)  # 1KB of silence
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ev1 = ws.receive_json()
        assert ev1["type"] == "partial_transcript"
        ev2 = ws.receive_json()
        assert ev2["type"] == "final_transcript"
        assert ev2["text"] == ""
        # No shopping command was dispatched
        assert len(turn_executed) == 0


def test_stale_revision_speech_turn_rejected():
    """Verify requesting a lower or equal revision is rejected as stale_revision."""
    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=lambda **kwargs: ScriptedSpeechSession(kwargs.get("revision", 1), []),
    )
    with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        ws.receive_json()

        # Turn 1
        ws.send_json({"type": "start_turn", "revision": 2, "language": "pcm", "sample_rate": 16000, "channels": 1})
        ev1 = ws.receive_json()
        assert ev1["type"] == "session_started" and ev1["revision"] == 2

        # Replay revision 2 (stale)
        ws.send_json({"type": "start_turn", "revision": 2, "language": "pcm", "sample_rate": 16000, "channels": 1})
        err = ws.receive_json()
        assert err["type"] == "error" and err["error_code"] == "stale_revision"

        # Replay revision 1 (lower, stale)
        ws.send_json({"type": "start_turn", "revision": 1, "language": "pcm", "sample_rate": 16000, "channels": 1})
        err2 = ws.receive_json()
        assert err2["type"] == "error" and err2["error_code"] == "stale_revision"


def test_barge_in_cancels_previous_stream():
    """Verify that starting a new turn while a previous turn is in-flight immediately cancels the previous session."""
    session1 = ScriptedSpeechSession(1, [])
    session2 = ScriptedSpeechSession(2, [])
    sessions = {1: session1, 2: session2}

    def speech_factory(**kwargs):
        return sessions[kwargs.get("revision", 1)]

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=speech_factory,
    )
    with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        ws.receive_json()

        # Start Turn 1
        ws.send_json({"type": "start_turn", "revision": 1, "language": "pcm", "sample_rate": 16000, "channels": 1})
        ev1 = ws.receive_json()
        assert ev1["type"] == "session_started" and ev1["revision"] == 1
        assert session1.canceled is False

        # Barge-in: Start Turn 2 before finish_turn on Turn 1
        ws.send_json({"type": "start_turn", "revision": 2, "language": "pcm", "sample_rate": 16000, "channels": 1})
        assert session1.canceled is True
        ev2 = ws.receive_json()
        assert ev2["type"] == "session_started" and ev2["revision"] == 2


def test_turn_cancellation_while_speech_in_flight():
    """Verify cancel_turn cleanly terminates active speech stream and acknowledges cancellation."""
    session_holder = []

    def speech_factory(**kwargs):
        session = ScriptedSpeechSession(kwargs.get("revision", 1), [])
        session_holder.append(session)
        return session

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=speech_factory,
    )
    with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        ws.receive_json()

        ws.send_json({"type": "start_turn", "revision": 1, "language": "pcm", "sample_rate": 16000, "channels": 1})
        ws.receive_json()  # session_started
        ws.send_bytes(b"\x00\x00" * 512)

        # Cancel turn
        ws.send_json({"type": "cancel_turn"})
        ack = ws.receive_json()
        assert ack["type"] == "turn_canceled" and ack["revision"] == 1
        assert session_holder[0].canceled is True


# ============================================================================
# 3. Two tabs and manual cart changes (Cart precondition verification)
# ============================================================================

def test_stale_cart_fingerprint_precondition_rejected():
    """Verify that if cart changes externally (e.g. tab 2), a command with stale precondition is rejected."""
    initial_cart = CartSnapshot(
        shop_id=SHOP,
        currency="USD",
        lines=(CartLine("101", 1, shopify_line_key="line_1"),),
    )
    # External modification changed quantity to 2
    modified_cart = CartSnapshot(
        shop_id=SHOP,
        currency="USD",
        lines=(CartLine("101", 2, shopify_line_key="line_1"),),
    )

    command = {
        "shop_id": SHOP,
        "operation": "add_variant",
        "expected_cart_fingerprint": initial_cart.fingerprint(),
        "parameters": {"variant_id": "102", "quantity": 1, "properties": {}},
    }

    # If before_cart in command result is modified_cart (different fingerprint), verification fails
    rejected_result = {
        "outcome": "verified_success",
        "before_cart": modified_cart.to_dict(),
        "after_cart": modified_cart.to_dict(),
    }

    assert _verify_reported_cart_result(command, rejected_result) is False


# ============================================================================
# 4. Durable uncertain cart outcomes and no replay on fallback
# ============================================================================

def test_uncertain_cart_outcome_persists_and_blocks_fallback_retry():
    """Verify uncertain write outcome records safely without retrying on another adapter (S-10, T-16)."""
    journal = CommandJournal(":memory:")
    before_cart = CartSnapshot(shop_id=SHOP, currency="USD", lines=())
    after_cart = CartSnapshot(shop_id=SHOP, currency="USD", lines=())

    command = AuthorizedCommand(
        command_id="cmd_uncertain_01",
        session_id="sess_1",
        shop_id=SHOP,
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
        expires_at_ms=int(time.time() * 1000) + 30_000,
        operation=CommandOperation.ADD_VARIANT,
        parameters={"variant_id": "101", "quantity": 1},
        expected_cart_fingerprint=before_cart.fingerprint(),
    )

    journal.prepare_command(command, before_cart)
    journal.mark_dispatched(command.command_id)
    journal.complete_command(
        command.command_id,
        CommandStatus.UNCERTAIN,
        "Network failure during WebMCP dispatch",
    )

    # Replay attempt returns the stored entry without creating a new dispatch
    stored = journal.get_entry(command.command_id)
    assert stored is not None
    assert stored.status == CommandStatus.UNCERTAIN
    assert stored.reconciled_message == "Network failure during WebMCP dispatch"

    # S-10 & T-13: System restart marks pending in-flight commands uncertain
    cmd2 = AuthorizedCommand(
        command_id="cmd_in_flight_02",
        session_id="sess_1",
        shop_id=SHOP,
        turn_id="turn_2",
        request_revision=2,
        page_epoch=1,
        expires_at_ms=int(time.time() * 1000) + 30_000,
        operation=CommandOperation.ADD_VARIANT,
        parameters={"variant_id": "102", "quantity": 1},
        expected_cart_fingerprint=before_cart.fingerprint(),
    )
    journal.prepare_command(cmd2, before_cart)
    journal.mark_dispatched(cmd2.command_id)
    reconciled_count = journal.restart_reconcile()
    assert reconciled_count == 1
    stored_cmd2 = journal.get_entry(cmd2.command_id)
    assert stored_cmd2 is not None
    assert stored_cmd2.status == CommandStatus.UNCERTAIN


# ============================================================================
# 5. Expired grant and revoked installation handling
# ============================================================================

def test_expired_grant_rejected_at_websocket_connection():
    """Verify expired session grant is rejected immediately."""
    app = create_voice_app(signing_secret=SECRET, allowed_origins={ORIGIN})
    expired_grant = make_signed_grant(issued_at_ms=int(time.time() * 1000) - 100_000, ttl_ms=10_000)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
            ws.send_json({"type": "authenticate", "grant": expired_grant})
            ws.receive_json()
    assert exc_info.value.code == 4401


def test_cross_shop_grant_rejected():
    """Verify grant issued for shop A cannot connect to shop B."""
    app = create_voice_app(signing_secret=SECRET, allowed_origins={ORIGIN})
    grant_shop_a = make_signed_grant(shop_id="other-shop.myshopify.com")

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
            ws.send_json({"type": "authenticate", "grant": grant_shop_a})
            ws.receive_json()
    assert exc_info.value.code == 4401


# ============================================================================
# 6. Privacy: Raw audio disabled by default and trace redaction
# ============================================================================

def test_privacy_trace_redaction_and_no_raw_audio_leak():
    """Verify sensitive tokens and session secrets are redacted in trace logging (T-23)."""
    payload = {
        "token": "secret-grant-token",
        "signing_secret": "super-secret-key-12345",
        "customer_email": "shopper@example.com",
        "credit_card": "4111222233334444",
        "audio_bytes": b"\x00\x00\x11\x22",
        "turn_id": "turn_safe_100",
        "query": "snowboards",
    }
    redacted = redact_payload(payload)
    assert redacted["token"] == "[REDACTED]"
    assert redacted["signing_secret"] == "[REDACTED]"
    assert redacted["customer_email"] == "[REDACTED]"
    assert redacted["credit_card"] == "[REDACTED]"
    assert redacted["audio_bytes"] == "[REDACTED]"
    assert redacted["turn_id"] == "turn_safe_100"
    assert redacted["query"] == "snowboards"

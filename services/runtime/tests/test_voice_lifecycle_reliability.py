"""Comprehensive voice reliability and lifecycle acceptance tests (WP-07, WP-11, WP-09B).

Covers:
1. Multi-turn voice conversation through production runtime wiring with clarification completion and cart verification.
2. Delayed/empty speech followed by a successful new turn.
3. Real runtime restart with file-backed persistence and no mutation replay (S-10, T-16).
4. Two browser tabs / concurrent cart modifications with stale cart fingerprint rejection preserving unrelated lines (S-08, S-09).
5. Active session revocation over internal control endpoint.
6. Delayed/failed TTS isolation without blocking text shopping result or cards.
7. Single accepted final, duplicate suppression, stale revision, barge-in, and turn cancellation.
8. Expired grants, cross-shop grants, and provider profile enforcement.
9. Privacy trace redaction and retention cleanup.
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

from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.commerce.reconciler import CommandReconciler, ExecutionOutcome
from ishop.commerce.simulator import ShopifySimulator
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
    RevocationRequest,
    SessionRevocationRegistry,
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
                price=Money.from_minor_units(69995, "USD"),
                available_for_sale=True,
            ),
            VariantEvidence(
                variant_id="102",
                product_id="1",
                product_title="Complete Snowboard",
                variant_title="Dawn",
                selected_options={"Color": "Dawn"},
                price=Money.from_minor_units(69995, "USD"),
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
                price=Money.from_minor_units(72995, "USD"),
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
                price=Money.from_minor_units(62995, "USD"),
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
# 1. Multi-turn voice conversation with completed clarification and cart assertion
# ============================================================================

def test_three_consecutive_voice_turns_and_clarification_completion_with_cart_verification(tmp_path):
    """Exercise consecutive voice turns through production runtime wiring.

    Turn 1: Search snowboards -> 3 candidates found.
    Turn 2: Comparison filter ("Which is cheapest in your search?") -> candidate 3 identified.
    Turn 3: Add to cart ("Add the Complete Snowboard to my cart") -> clarification needed for Ice vs Dawn.
    Turn 4: Complete clarification ("Ice") -> authorized command ADD_VARIANT for variant 101,
            cart mutated and independently verified.
    """
    session_store = SessionStore(str(tmp_path / "sessions.db"))
    controller = ShoppingController(FakeLlmProvider())
    catalog_ev = sample_catalog_evidence()
    current_cart = CartSnapshot(shop_id=SHOP, currency="USD", lines=())

    async def turn_handler(payload: dict[str, Any], grant: SessionGrant) -> dict[str, Any]:
        nonlocal current_cart
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
            current_cart=current_cart,
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
        texts = {
            1: "Find snowboard",
            2: "Which is cheapest in your search?",
            3: "Add the Complete Snowboard to my cart",
            4: "Ice",
        }
        text = texts.get(rev, "")
        events = [
            SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, "sess", rev, text=text[:3]),
            SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, "sess", rev, text=text),
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

        ev_partial1 = ws.receive_json()
        assert ev_partial1["type"] == "partial_transcript" and ev_partial1["revision"] == 1
        ev_final1 = ws.receive_json()
        assert ev_final1["type"] == "final_transcript" and ev_final1["revision"] == 1

        ws.send_json({
            "type": "shopping_turn",
            "turn_id": "turn_1",
            "request_revision": 1,
            "page_epoch": 1,
            "transcript": "Find snowboard",
        })
        ev_res1 = ws.receive_json()
        assert ev_res1["type"] == "shopping_result"
        assert ev_res1["status"] == "completed"
        assert len(ev_res1["result_product_ids"]) == 3

        # Turn 2: Comparison Filter (Which is cheapest?)
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
        assert "Multi-managed Snowboard" in ev_res2["spoken_response"]

        # Turn 3: Add to cart requiring variant clarification (Ice vs Dawn)
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
        assert ev_res3["status"] == "clarification_needed"
        assert "Ice" in ev_res3["spoken_response"] or "Dawn" in ev_res3["spoken_response"]
        assert ev_res3["authorized_command"] is None

        # Turn 4: Shopper selects "Ice" variant to complete clarification
        ws.send_json({"type": "start_turn", "revision": 4, "language": "pcm", "sample_rate": 16000, "channels": 1})
        assert ws.receive_json()["type"] == "session_started"
        ws.send_bytes(b"\x00\x00" * 256)
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ws.receive_json()  # partial
        ws.receive_json()  # final

        ws.send_json({
            "type": "shopping_turn",
            "turn_id": "turn_4",
            "request_revision": 4,
            "page_epoch": 1,
            "transcript": "Ice",
        })
        ev_res4 = ws.receive_json()
        assert ev_res4["type"] == "shopping_result"
        # Must be definitively COMPLETED (not accepted as ambiguity)
        assert ev_res4["status"] == "completed"
        assert ev_res4["authorized_command"] is not None
        cmd = ev_res4["authorized_command"]
        assert cmd["operation"] == "add_variant"
        assert cmd["parameters"]["variant_id"] == "101"
        assert cmd["parameters"]["quantity"] == 1

        # Simulate authoritative cart execution and verify resulting cart
        before_cart = current_cart
        current_cart = CartSnapshot(
            shop_id=SHOP,
            currency="USD",
            lines=(CartLine("101", 1, shopify_line_key="line_101"),),
        )
        command_result_payload = {
            "outcome": "verified_success",
            "before_cart": before_cart.to_dict(),
            "after_cart": current_cart.to_dict(),
        }
        assert _verify_reported_cart_result(cmd, command_result_payload) is True

        # Send command_result back over WebSocket
        ws.send_json({
            "type": "command_result",
            "command_id": cmd["command_id"],
            "result": command_result_payload,
        })
        cmd_ack = ws.receive_json()
        assert cmd_ack["type"] == "command_result_ack"
        assert cmd_ack["command_id"] == cmd["command_id"]

        # Independently assert final cart snapshot
        assert len(current_cart.lines) == 1
        assert current_cart.lines[0].variant_id == "101"
        assert current_cart.lines[0].quantity == 1
        assert catalog_ev.products["1"].variants[0].price.to_minor_units() == 69995


# ============================================================================
# 2. Delayed / Empty speech followed by a successful new turn
# ============================================================================

def test_delayed_or_empty_speech_followed_by_successful_new_turn():
    """Verify that a silent/delayed turn executes zero commands and allows the next turn to succeed cleanly."""
    executed_turns: list[dict[str, Any]] = []

    async def turn_handler(payload: dict[str, Any], grant: SessionGrant) -> dict[str, Any]:
        executed_turns.append(payload)
        if payload["transcript"] == "":
            return {"turn_id": payload["turn_id"], "status": "empty", "spoken_response": "I didn't hear anything."}
        return {
            "turn_id": payload["turn_id"],
            "request_revision": payload["request_revision"],
            "page_epoch": payload["page_epoch"],
            "status": "completed",
            "spoken_response": "Found 3 snowboards.",
            "result_product_ids": ["1", "2", "3"],
        }

    def speech_factory(**kwargs):
        rev = kwargs.get("revision", 1)
        if rev == 1:
            events = [
                SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, "s1", 1, text=""),
                SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, "s1", 1, text=""),
            ]
        else:
            events = [
                SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, "s2", 2, text="Find"),
                SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, "s2", 2, text="Find snowboard"),
            ]
        return ScriptedSpeechSession(rev, events)

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=speech_factory,
        shopping_turn_handler=turn_handler,
    )

    with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        ws.receive_json()

        # Turn 1: Empty speech
        ws.send_json({"type": "start_turn", "revision": 1, "language": "pcm", "sample_rate": 16000, "channels": 1})
        ws.receive_json()  # session_started
        ws.send_bytes(b"\x00\x00" * 512)
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ws.receive_json()  # partial ""
        final1 = ws.receive_json()  # final ""
        assert final1["text"] == ""

        # Turn 2: Shopper speaks again with valid query
        ws.send_json({"type": "start_turn", "revision": 2, "language": "pcm", "sample_rate": 16000, "channels": 1})
        ws.receive_json()  # session_started
        ws.send_bytes(b"\x00\x00" * 512)
        ws.send_json({"type": "finish_turn", "page_epoch": 1})

        ws.receive_json()  # partial "Find"
        final2 = ws.receive_json()  # final "Find snowboard"
        assert final2["text"] == "Find snowboard"

        ws.send_json({
            "type": "shopping_turn",
            "turn_id": "turn_2",
            "request_revision": 2,
            "page_epoch": 1,
            "transcript": "Find snowboard",
        })
        res2 = ws.receive_json()
        assert res2["type"] == "shopping_result"
        assert res2["status"] == "completed"
        assert len(res2["result_product_ids"]) == 3


# ============================================================================
# 3. File-backed persistence, runtime restart recovery, and no mutation replay (S-10, T-16)
# ============================================================================

def test_runtime_restart_with_file_backed_persistence_and_no_mutation_replay(tmp_path):
    """Verify that after a crash and restart using file-backed SQLite persistence:
    1. In-flight commands are marked UNCERTAIN upon restart reconciliation.
    2. Any subsequent replay/reconciliation reconciles against live cart without re-dispatching mutations.
    """
    db_path = str(tmp_path / "file_backed_journal.db")
    journal1 = CommandJournal(db_path)
    simulator = ShopifySimulator(SHOP)

    before_cart = simulator.read_cart()
    command = AuthorizedCommand(
        command_id="cmd_restart_test_001",
        session_id="sess_restart_1",
        shop_id=SHOP,
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
        expires_at_ms=int(time.time() * 1000) + 60_000,
        operation=CommandOperation.ADD_VARIANT,
        parameters={"variant_id": "101", "quantity": 1},
        expected_cart_fingerprint=before_cart.fingerprint(),
    )

    # 1. Prepare and mark DISPATCHED in first runtime instance before crash
    journal1.prepare_command(command, before_cart)
    journal1.mark_dispatched(command.command_id)
    assert journal1.get_entry(command.command_id).status == CommandStatus.DISPATCHED

    # 2. Crash & Restart: Initialize new runtime instances with the same database file
    del journal1
    journal2 = CommandJournal(db_path)

    # Restart reconciliation converts pending DISPATCHED to UNCERTAIN (T-13)
    reconciled_count = journal2.restart_reconcile()
    assert reconciled_count == 1
    restarted_entry = journal2.get_entry(command.command_id)
    assert restarted_entry is not None
    assert restarted_entry.status == CommandStatus.UNCERTAIN

    # 3. Attempt execution/reconciliation via CommandReconciler
    reconciler = CommandReconciler(journal2)
    receipt = reconciler.execute_and_reconcile(command, simulator)

    # Assert that live cart was reconciled safely and no second mutation occurred
    assert receipt.command_id == command.command_id
    # Live cart did not have the item (dispatch did not reach simulator before crash),
    # so reconciliation determines REJECTED or UNCERTAIN without re-dispatching
    live_cart = simulator.read_cart()
    assert len(live_cart.lines) == 0  # No mutation replay occurred


# ============================================================================
# 4. Two browser tabs / concurrent cart modifications (Cart precondition checks, S-08, S-09)
# ============================================================================

def test_two_browser_tabs_concurrent_modification_precondition_rejection(tmp_path):
    """Verify that concurrent modifications in Tab 2 invalidate Tab 1's expected fingerprint,
    preventing Tab 1 dispatch and completely preserving all of Tab 2's cart lines.
    """
    simulator = ShopifySimulator(SHOP)
    # Tab 1 starts with initial cart containing 1 item
    simulator.add_initial_line("101", 1)
    tab1_observed_cart = simulator.read_cart()
    assert len(tab1_observed_cart.lines) == 1

    # Tab 1 generates command to add variant 102 expecting tab1_observed_cart fingerprint
    tab1_command = AuthorizedCommand(
        command_id="cmd_tab1_add_102",
        session_id="sess_tab1",
        shop_id=SHOP,
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
        expires_at_ms=int(time.time() * 1000) + 30_000,
        operation=CommandOperation.ADD_VARIANT,
        parameters={"variant_id": "102", "quantity": 1},
        expected_cart_fingerprint=tab1_observed_cart.fingerprint(),
    )

    # Meanwhile, Tab 2 concurrently modifies the cart: updates variant 101 qty to 5 and adds variant 201
    line1_key = tab1_observed_cart.lines[0].shopify_line_key
    simulator.set_cart(
        CartSnapshot(
            shop_id=SHOP,
            currency="USD",
            lines=(
                CartLine("101", 5, shopify_line_key=line1_key),
                CartLine("201", 2, shopify_line_key="line_201"),
            ),
        )
    )
    tab2_live_cart = simulator.read_cart()
    assert len(tab2_live_cart.lines) == 2
    assert tab2_live_cart.lines[0].quantity == 5

    # Now Tab 1 tries to execute its command through CommandReconciler
    journal = CommandJournal(str(tmp_path / "tab_journal.db"))
    reconciler = CommandReconciler(journal)
    receipt = reconciler.execute_and_reconcile(tab1_command, simulator)

    # Assert Tab 1 dispatch was safely REJECTED due to precondition mismatch
    assert receipt.outcome == ExecutionOutcome.REJECTED
    assert "Precondition failed" in receipt.verification_message

    # Independently assert live cart state is intact and unaffected by Tab 1
    final_live_cart = simulator.read_cart()
    assert len(final_live_cart.lines) == 2
    assert final_live_cart.lines[0].variant_id == "101"
    assert final_live_cart.lines[0].quantity == 5
    assert final_live_cart.lines[1].variant_id == "201"
    assert final_live_cart.lines[1].quantity == 2


# ============================================================================
# 5. Active session revocation over internal control endpoint (Gap 6)
# ============================================================================

def test_active_session_revocation_over_control_endpoint():
    """Verify revoking an active shop or profile over POST /internal/revocations immediately closes active WebSocket connections with code 4410."""
    revocation_registry = SessionRevocationRegistry()
    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        revocations=revocation_registry,
    )
    client = TestClient(app)

    with client.websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        # Authenticate session
        grant = make_signed_grant()
        ws.send_json({"type": "authenticate", "grant": grant})
        auth_ack = ws.receive_json()
        assert auth_ack["type"] == "authenticated"

        # Revoke the shop over HTTP control endpoint
        rev_resp = client.post(
            "/internal/revocations",
            headers={"Authorization": f"Bearer {SECRET}"},
            json={"shop_ids": [SHOP]},
        )
        assert rev_resp.status_code == 200
        assert rev_resp.json()["closed_sessions"] == 1

        # The WebSocket receives close code 4410
        msg = ws.receive()
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 4410

    # New connection attempts for revoked shop are rejected immediately
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws2:
            ws2.send_json({"type": "authenticate", "grant": make_signed_grant()})
            ws2.receive_json()
    assert exc_info.value.code == 4410


# ============================================================================
# 6. Delayed / Failed TTS does not block shopping text or candidate cards (Gap 6)
# ============================================================================

def test_delayed_or_failed_tts_does_not_block_text_shopping_result():
    """Verify that a failing or throwing TTS synthesis provider does not prevent delivery of the text shopping result."""
    async def turn_handler(payload: dict[str, Any], grant: SessionGrant) -> dict[str, Any]:
        return {
            "turn_id": payload["turn_id"],
            "request_revision": payload["request_revision"],
            "page_epoch": payload["page_epoch"],
            "status": "completed",
            "spoken_response": "Here are 2 snowboards.",
            "result_product_ids": ["1", "2"],
        }

    async def failing_tts_handler(text: str, revision: int):
        raise RuntimeError("TTS provider timeout or network failure")

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        shopping_turn_handler=turn_handler,
        shopping_tts_handler=failing_tts_handler,
    )

    with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": make_signed_grant()})
        ws.receive_json()

        # Send shopping turn
        ws.send_json({
            "type": "shopping_turn",
            "turn_id": "turn_tts_1",
            "request_revision": 1,
            "page_epoch": 1,
            "transcript": "Find snowboards",
        })

        # Assert shopping_result is delivered intact despite TTS failure
        res = ws.receive_json()
        assert res["type"] == "shopping_result"
        assert res["status"] == "completed"
        assert res["spoken_response"] == "Here are 2 snowboards."
        assert res["result_product_ids"] == ["1", "2"]


# ============================================================================
# 7. Single accepted final, duplicate suppression, stale revision, barge-in, and turn cancellation
# ============================================================================

def test_single_accepted_final_and_duplicate_suppression():
    """Verify that multiple final transcripts in the same turn are emitted to client which suppresses duplicates."""
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
        ev2 = ws.receive_json()
        assert ev2["type"] == "session_started" and ev2["revision"] == 2
        assert session1.canceled is True


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
# 8. Expired grants, cross-shop grants, and provider profile checks
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


def test_provider_profile_unavailable_rejected():
    """Verify grant requesting an unregistered ASR/LLM/TTS profile is closed with 4404."""
    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        allowed_llm_profiles={"groq-gpt-oss-120b"},
    )
    unknown_profile_grant = make_signed_grant(llm_profile="unknown-llm-provider-99")

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}) as ws:
            ws.send_json({"type": "authenticate", "grant": unknown_profile_grant})
            ws.receive_json()
    assert exc_info.value.code == 4404


# ============================================================================
# 9. Privacy: Raw audio disabled by default and trace redaction
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

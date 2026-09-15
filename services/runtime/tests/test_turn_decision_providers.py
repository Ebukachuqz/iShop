"""Production provider contract tests for conversation-first decisions."""

from __future__ import annotations

import asyncio
import json

import pytest

from ishop.domain.intent import DecisionMode, TurnDecision
from ishop.llm.base import LlmIntentRequest, LlmProviderError
from ishop.llm.fake import FakeLlmProvider
from ishop.orchestration.controller import ShoppingController, ControllerTurnResult
from ishop.commerce.catalog import EvidenceSnapshot
from ishop.domain.models import CartSnapshot
from ishop.llm.gemini import GeminiLlmProvider
from ishop.llm.groq import GroqLlmProvider


def _request(text: str = "How are you, Drake?") -> LlmIntentRequest:
    return LlmIntentRequest(transcript=text, turn_id="turn")


def _respond_payload() -> dict:
    return {
        "mode": "respond",
        "response_text": "I am ready to help.",
        "response_purpose": "greeting",
        "clarification_options": [],
        "clarification_fields": [],
        "intent": None,
        "target_reference": None,
        "grounded_facts": [],
    }


def test_groq_requests_and_parses_turn_decision(monkeypatch):
    captured = {}

    def fake_post(url, body, headers, timeout):
        captured.update(json.loads(body))
        return json.dumps({"choices": [{"message": {"content": json.dumps(_respond_payload())}}], "usage": {}})

    monkeypatch.setattr("ishop.llm.groq._http_post_json", fake_post)
    result = asyncio.run(GroqLlmProvider(api_key="configured-key").interpret_intent(_request()))
    assert result.decision is not None and result.decision.mode == DecisionMode.RESPOND
    system = captured["messages"][0]["content"]
    assert '"respond"' in system and "Do not return a bare ShoppingIntent" in system


def test_gemini_requests_and_parses_turn_decision(monkeypatch):
    captured = {}

    def fake_post(url, body, headers, timeout):
        captured.update(json.loads(body))
        return json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(_respond_payload())}]}}], "usageMetadata": {}})

    monkeypatch.setattr("ishop.llm.gemini._http_post_json", fake_post)
    result = asyncio.run(GeminiLlmProvider(api_key="configured-key").interpret_intent(_request()))
    assert result.decision is not None and result.decision.mode == DecisionMode.RESPOND
    prompt = captured["contents"][0]["parts"][0]["text"]
    assert '"respond"' in prompt and "Do not return a bare ShoppingIntent" in prompt
    assert captured["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "temperature" not in captured["generationConfig"]


def test_turn_decision_rejects_invalid_mode_shapes():
    with pytest.raises(ValueError):
        TurnDecision.from_dict({"mode": "respond", "response_purpose": "greeting"})
    with pytest.raises(ValueError):
        TurnDecision.from_dict({"mode": "clarify"})
    with pytest.raises(ValueError):
        TurnDecision.from_dict({"mode": "act"})


def test_turn_decision_accepts_nullable_optional_arrays():
    decision = TurnDecision.from_dict({
        "mode": "respond",
        "response_text": "Hello",
        "response_purpose": "greeting",
        "clarification_options": None,
        "clarification_fields": None,
        "grounded_facts": None,
    })
    assert decision.clarification_options == ()
    assert decision.grounded_facts == ()


def test_assistant_outcome_is_added_to_bounded_session_history():
    controller = ShoppingController(FakeLlmProvider())
    result = ControllerTurnResult("session", "turn", 1, 1, "completed", "Hello there")
    controller.record_assistant_outcome("session", result)
    assert controller.get_session_state("session").conversation_history == [
        {"role": "assistant", "content": "Hello there"}
    ]


def test_interpretation_failure_is_logged_and_does_not_poison_next_turn(caplog):
    class FailOnceProvider(FakeLlmProvider):
        failed = False

        async def interpret_intent(self, request):
            if not self.failed:
                self.failed = True
                raise LlmProviderError("Failed to parse or validate TurnDecision", self.profile.profile_id, retryable=True)
            return await super().interpret_intent(request)

    provider = FailOnceProvider()
    controller = ShoppingController(provider, max_llm_retries=0)
    evidence = EvidenceSnapshot("empty", "shop.test", "USD", 1, {})
    cart = CartSnapshot("shop.test", "USD", ())
    first = asyncio.run(controller.handle_turn("session", "turn-1", 1, 1, "find snowboards", evidence, cart))
    second = asyncio.run(controller.handle_turn("session", "turn-2", 2, 1, "How are you?", evidence, cart))
    assert first.failure_code == "invalid_decision"
    assert second.status == "completed"
    assert "invalid_decision" in caplog.text


def test_cached_browser_search_does_not_claim_fresh_constraint_coverage():
    provider = FakeLlmProvider()
    controller = ShoppingController(provider)
    evidence = EvidenceSnapshot("cached", "shop.test", "USD", 1, {}, query="snowboards")
    cart = CartSnapshot("shop.test", "USD", ())
    result = asyncio.run(controller.handle_turn(
        "session", "turn", 1, 1, "find snowboards over 700 dollars", evidence, cart,
        available_tools={"search_catalog"},
    ))
    assert result.status == "evidence_required"
    assert result.selected_tool == "search_catalog"


def test_single_qualified_tool_does_not_require_second_llm_call():
    class IntentOnlyProvider(FakeLlmProvider):
        async def select_tool(self, request):
            raise AssertionError("A single qualified tool must be selected deterministically")

    provider = IntentOnlyProvider()
    controller = ShoppingController(provider)
    evidence = EvidenceSnapshot("empty", "shop.test", "USD", 1, {})
    cart = CartSnapshot("shop.test", "USD", ())
    result = asyncio.run(controller.handle_turn(
        "session", "turn", 1, 1, "find snowboards", evidence, cart,
        available_tools={"search_catalog"},
    ))
    assert result.status == "evidence_required"
    assert result.selected_tool == "search_catalog"


def test_providers_reject_legacy_bare_intent(monkeypatch):
    bare = {
        "schema_version": "1.0.0", "intent_id": "legacy", "operation": "search",
        "is_explicit_checkout_request": False, "supporting_transcript_span": "find boards",
        "unresolved_fields": [],
    }
    monkeypatch.setattr(
        "ishop.llm.groq._http_post_json",
        lambda *args: json.dumps({"choices": [{"message": {"content": json.dumps(bare)}}]}),
    )
    with pytest.raises(Exception, match="TurnDecision envelope"):
        asyncio.run(GroqLlmProvider(api_key="configured-key").interpret_intent(_request("find boards")))

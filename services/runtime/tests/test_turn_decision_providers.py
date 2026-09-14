"""Production provider contract tests for conversation-first decisions."""

from __future__ import annotations

import asyncio
import json

import pytest

from ishop.domain.intent import DecisionMode, TurnDecision
from ishop.llm.base import LlmIntentRequest
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


def test_turn_decision_rejects_invalid_mode_shapes():
    with pytest.raises(ValueError):
        TurnDecision.from_dict({"mode": "respond", "response_purpose": "greeting"})
    with pytest.raises(ValueError):
        TurnDecision.from_dict({"mode": "clarify"})
    with pytest.raises(ValueError):
        TurnDecision.from_dict({"mode": "act"})


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

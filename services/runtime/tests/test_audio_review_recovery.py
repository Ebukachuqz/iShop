"""Regressions from the shopper's voice/cart-detail review."""
import asyncio
import io
import json
import urllib.error

import pytest

from ishop.domain.intent import IntentOperation, ShoppingIntent, TargetReference, ReferenceKind
from ishop.commerce.catalog import EvidenceSnapshot
from ishop.domain.models import CartLine, CartSnapshot
from ishop.llm.base import LlmIntentRequest, LlmProviderError, LlmToolSelectionRequest
from ishop.llm.fake import FakeLlmProvider
from ishop.llm.groq import GroqLlmProvider
from ishop.orchestration.controller import ShoppingController


def test_cart_detail_response_keeps_fingerprint_and_reports_exact_line_prices():
    cart = CartSnapshot("shop", "USD", (CartLine("v1", 2, shopify_line_key="line"),))
    fingerprint = cart.fingerprint()
    intent = ShoppingIntent(intent_id="i", operation=IntentOperation.VIEW_CART,
                            is_explicit_checkout_request=False,
                            supporting_transcript_span="Details of my cart")
    result = ShoppingController(FakeLlmProvider())._handle_view_cart(
        "session", "turn", 1, 1, intent, cart,
        [{"title": "Snowboard", "variant_title": "Ice", "quantity": 2,
          "unit_price_minor": 60000, "line_total_minor": 120000}],
    )
    assert "Snowboard (Ice), quantity 2" in result.spoken_response
    assert "600.00 USD each" in result.spoken_response
    assert "1200.00 USD line total" in result.spoken_response
    assert result.authorized_command is None
    assert cart.fingerprint() == fingerprint


def test_tool_rate_limit_preserves_status_and_cooldown_recovers(monkeypatch):
    calls = []
    clock = [100.0]
    monkeypatch.setattr("ishop.llm.groq.time.monotonic", lambda: clock[0])
    def post(*args):
        calls.append(args)
        if len(calls) == 1:
            raise urllib.error.HTTPError("https://example.invalid", 429, "limit",
                                        {"Retry-After": "5"}, io.BytesIO(b"{}"))
        return json.dumps({"choices": [{"message": {"content": json.dumps({
            "mode": "respond", "response_text": "Hello!", "response_purpose": "greeting"
        })}}]})
    monkeypatch.setattr("ishop.llm.groq._http_post_json", post)
    provider = GroqLlmProvider(api_key="configured")
    intent = ShoppingIntent(intent_id="i", operation=IntentOperation.VIEW_CART,
                            is_explicit_checkout_request=False, supporting_transcript_span="Show cart")
    request = LlmToolSelectionRequest(intent=intent, qualified_tools=(), resolved_context={},
                                     turn_id="t", request_revision=1)
    async def run():
        with pytest.raises(LlmProviderError) as error:
            await provider.select_tool(request)
        assert error.value.status_code == 429
        with pytest.raises(LlmProviderError):
            await provider.interpret_intent(LlmIntentRequest(transcript="Hello"))
        assert len(calls) == 1
        clock[0] += 6
        result = await provider.interpret_intent(LlmIntentRequest(transcript="Hello"))
        assert result.decision.response_text == "Hello!"
    asyncio.run(run())


def test_unknown_cart_reference_resolves_from_cart_without_search_or_page_identity():
    provider = FakeLlmProvider()
    provider.register_custom_intent("details", ShoppingIntent(
        intent_id="i", operation=IntentOperation.DESCRIBE_PRODUCT,
        is_explicit_checkout_request=False, supporting_transcript_span="details",
        target_reference=TargetReference(ReferenceKind.CART_LINE, "unknown"),
        unresolved_fields=("product_query",),
    ))
    cart = CartSnapshot("shop", "USD", (CartLine("v1", 1, shopify_line_key="line"),))
    result = asyncio.run(ShoppingController(provider).handle_turn(
        "s", "t", 1, 1, "details", EvidenceSnapshot("e", "shop", "USD", 1, {}), cart,
        current_product_id="unrelated-page", available_tools={"get_cart"},
        cart_details=[{"line_key": "line", "variant_id": "v1", "quantity": 1,
                       "title": "Snowboard", "variant_title": "Ice", "unit_price_minor": 60000}],
    ))
    assert result.status == "completed"
    assert "Snowboard (Ice)" in result.spoken_response
    assert result.authorized_command is None
    assert result.evidence_query is None


def test_mismatched_cart_presentation_never_supplies_product_facts():
    cart = CartSnapshot("shop", "USD", (CartLine("v1", 1, shopify_line_key="line"),))
    result = asyncio.run(ShoppingController(FakeLlmProvider()).handle_turn(
        "s", "t", 1, 1, "Show cart", EvidenceSnapshot("e", "shop", "USD", 1, {}), cart,
        available_tools={"get_cart"},
        cart_details=[{"line_key": "wrong", "variant_id": "v1", "quantity": 1, "title": "Unrelated"}],
    ))
    assert "Unrelated" not in result.spoken_response
    assert "unavailable" in result.spoken_response

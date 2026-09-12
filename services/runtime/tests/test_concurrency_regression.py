"""Regression tests for R2 and R7 (concurrency, cancellation, non-blocking I/O).

Findings:
- R2 (P0): Pending turn executes after newer turn arrives or cancellation.
- R7 (P1): Blocking provider calls block event loop / no cancellation.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import pytest

from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.domain.intent import IntentOperation, ShoppingIntent
from ishop.domain.models import CartLine, CartSnapshot, Money
from ishop.llm.base import (
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProfile,
    LlmProvider,
    LlmUsage,
)
from ishop.orchestration.controller import ShoppingController
from ishop.llm.gemini import GeminiLlmProvider
from ishop.llm.groq import GroqLlmProvider


class DelayedLlmProvider(LlmProvider):
    """LLM provider that introduces an asynchronous delay to simulate network latency."""

    def __init__(self, delay_s: float = 0.05):
        self.delay_s = delay_s
        self._profile = LlmProfile(
            profile_id="delayed-test",
            provider_name="test",
            model_name="test-delay",
            enabled=True,
        )

    @property
    def profile(self) -> LlmProfile:
        return self._profile

    def check_readiness(self) -> tuple[bool, str | None]:
        return True, None

    async def interpret_intent(self, request: LlmIntentRequest) -> LlmInterpretationResult:
        await asyncio.sleep(self.delay_s)
        intent = ShoppingIntent(
            intent_id="int_delayed",
            operation=IntentOperation.ADD_TO_CART,
            product_query="hoodie",
            selected_variant_attributes={"color": "black", "size": "large"},
            is_explicit_checkout_request=False,
            supporting_transcript_span="hoodie",
        )
        return LlmInterpretationResult(
            intent=intent,
            raw_response_text="{}",
            usage=LlmUsage(),
            profile_id=self.profile.profile_id,
        )

    async def generate_grounded_response(self, context) -> str:
        return "Added hoodie."


@pytest.fixture
def sample_evidence() -> EvidenceSnapshot:
    variant = VariantEvidence(
        variant_id="var_hoodie_black_l",
        product_id="prod_hoodie",
        product_title="Drake Hoodie",
        variant_title="Black / Large",
        price=Money(12000, "NGN"),
        available_for_sale=True,
        selected_options={"color": "black", "size": "large"},
    )
    product = ProductEvidence(
        product_id="prod_hoodie",
        title="Drake Hoodie",
        variants=(variant,),
        options=("color", "size"),
    )
    return EvidenceSnapshot(
        snapshot_id="snap_1",
        observed_at_ms=1000,
        shop_id="drake-test.myshopify.com",
        currency="NGN",
        products={"prod_hoodie": product},
    )


@pytest.fixture
def empty_cart() -> CartSnapshot:
    return CartSnapshot(shop_id="drake-test.myshopify.com", currency="NGN")


def test_r02_overlapping_newer_turn_aborts_pending_turn(sample_evidence, empty_cart):
    """R2 (P0): When turn 2 arrives while turn 1 is awaiting LLM, turn 1 must not authorize actions."""
    async def _test():
        provider = DelayedLlmProvider(delay_s=0.06)
        controller = ShoppingController(llm_provider=provider)

        # Start turn 1 in background
        task1 = asyncio.create_task(
            controller.handle_turn(
                session_id="sess_1",
                turn_id="turn_1",
                request_revision=1,
                page_epoch=1,
                transcript="Add black large hoodie",
                evidence=sample_evidence,
                current_cart=empty_cart,
            )
        )

        # Wait 0.02s, then start turn 2 (superseding turn 1)
        await asyncio.sleep(0.02)
        task2 = asyncio.create_task(
            controller.handle_turn(
                session_id="sess_1",
                turn_id="turn_2",
                request_revision=2,
                page_epoch=1,
                transcript="Add black large hoodie",
                evidence=sample_evidence,
                current_cart=empty_cart,
            )
        )

        res2 = await task2
        res1 = await task1

        assert res2.status == "completed"
        assert res2.authorized_command is not None

        # Turn 1 must have been aborted / rejected as stale; MUST NOT authorize a command!
        assert res1.status in ("stale", "aborted", "cancelled")
        assert res1.authorized_command is None, "Turn 1 was superseded but authorized a command!"

    asyncio.run(_test())


def test_r02_cancellation_during_turn_prevents_authorization(sample_evidence, empty_cart):
    """R2 (P0): Cancelling a turn while awaiting LLM must prevent command authorization."""
    async def _test():
        provider = DelayedLlmProvider(delay_s=0.06)
        controller = ShoppingController(llm_provider=provider)

        task1 = asyncio.create_task(
            controller.handle_turn(
                session_id="sess_1",
                turn_id="turn_1",
                request_revision=1,
                page_epoch=1,
                transcript="Add black large hoodie",
                evidence=sample_evidence,
                current_cart=empty_cart,
            )
        )

        await asyncio.sleep(0.02)
        # User cancels turn
        controller.cancel_turn("sess_1", "turn_1")

        res1 = await task1
        assert res1.status in ("stale", "cancelled", "aborted")
        assert res1.authorized_command is None, "Cancelled turn authorized a cart mutation!"

    asyncio.run(_test())


def test_r02_page_epoch_change_during_turn_aborts_authorization(sample_evidence, empty_cart):
    """R2 (P0): Navigation / page epoch change during async work invalidates command authorization."""
    async def _test():
        provider = DelayedLlmProvider(delay_s=0.06)
        controller = ShoppingController(llm_provider=provider)

        task1 = asyncio.create_task(
            controller.handle_turn(
                session_id="sess_1",
                turn_id="turn_1",
                request_revision=1,
                page_epoch=1,
                transcript="Add black large hoodie",
                evidence=sample_evidence,
                current_cart=empty_cart,
            )
        )

        await asyncio.sleep(0.02)
        # Shopper navigates to new page
        controller.update_page_epoch("sess_1", 2)

        res1 = await task1
        assert res1.status in ("stale", "cancelled", "aborted")
        assert res1.authorized_command is None, "Turn on stale page epoch authorized a command!"

    asyncio.run(_test())


def test_r02_session_scoped_state_isolation(sample_evidence, empty_cart):
    """R2: Activity in session 2 must not corrupt or invalidate session 1's lower revisions."""
    async def _test():
        provider = DelayedLlmProvider(delay_s=0.04)
        controller = ShoppingController(llm_provider=provider)

        # Session 2 reaches revision 10
        res_b = await controller.handle_turn(
            session_id="sess_b",
            turn_id="turn_b1",
            request_revision=10,
            page_epoch=1,
            transcript="Add black large hoodie",
            evidence=sample_evidence,
            current_cart=empty_cart,
        )
        assert res_b.status == "completed"

        # Session A is at revision 1. It must NOT be treated as stale because sess_b reached 10!
        res_a = await controller.handle_turn(
            session_id="sess_a",
            turn_id="turn_a1",
            request_revision=1,
            page_epoch=1,
            transcript="Add black large hoodie",
            evidence=sample_evidence,
            current_cart=empty_cart,
        )
        assert res_a.status == "completed"
        assert res_a.authorized_command is not None

    asyncio.run(_test())


def test_r07_provider_methods_are_async_and_use_thread_offloading():
    """R7 (P1): Gemini and Groq providers must not run blocking socket calls on event loop thread."""
    gemini = GeminiLlmProvider(api_key="test-key")
    groq = GroqLlmProvider(api_key="test-key")

    assert inspect.iscoroutinefunction(gemini.interpret_intent)
    assert inspect.iscoroutinefunction(gemini.generate_grounded_response)
    assert inspect.iscoroutinefunction(groq.interpret_intent)
    assert inspect.iscoroutinefunction(groq.generate_grounded_response)

    # Inspect source code of gemini.py and groq.py to ensure urllib is offloaded via asyncio.to_thread
    gemini_src = inspect.getsource(gemini.interpret_intent)
    groq_src = inspect.getsource(groq.interpret_intent)

    assert "asyncio.to_thread" in gemini_src, "Gemini provider does not offload HTTP call to thread"
    assert "asyncio.to_thread" in groq_src, "Groq provider does not offload HTTP call to thread"

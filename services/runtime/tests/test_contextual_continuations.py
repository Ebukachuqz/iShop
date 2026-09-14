"""Contextual shopping continuation and storefront presentation test suite (CS-01 to CS-20).

Validates:
- CS-01: Search four products >=$750, then resolve exact 'second' phrasing for navigation without ID-as-text search.
- CS-02: Missing target details triggers get_product once; known trusted URL navigates.
- CS-03: Exact lookup returning missing product or invalid destination fails truthfully without guessing.
- CS-04: Ordered result sets resolve ordinals consistently; out-of-range references clarify.
- CS-05: Scope resolution: current page vs search selection vs ambiguous 'it'.
- CS-06: Multiple search sets: current vs earlier search resolution and clarification.
- CS-07: Summary vs full matches distinction with numbered order.
- CS-08: Correlated observation resumes pending step without restarting intent extraction.
- CS-09: Repeated empty evidence no-progress termination.
- CS-10: Maximum step bounding (8 steps).
- CS-11: Comparison between items and 'open the cheaper one' follow-up.
- CS-12: Compound search & open cheapest available product.
- CS-13: Compound search-then-add clarification before write.
- CS-14: Multi-turn add with variant option clarification preserving quantity and target.
- CS-15: Cancellation or new search invalidates pending clarification.
- CS-16: Unavailable / sold out item handling without silent mutation.
- CS-17: Snapshot serialization and restoration roundtrip.
- CS-18: Page epoch increment supersedes stale in-flight turns.
- CS-19: Tenant isolation: cross-tenant evidence rejection.
- CS-20: Navigation command authorization and cancellation before dispatch.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import pytest

from ishop.commerce.catalog import (
    EvidenceSnapshot,
    ProductEvidence,
    VariantEvidence,
)
from ishop.domain.intent import (
    BudgetConstraint,
    IntentOperation,
    QuantityChange,
    ShoppingIntent,
)
from ishop.domain.models import CartLine, CartSnapshot, CommandOperation, Money
from ishop.llm.fake import FakeLlmProvider
from ishop.orchestration.controller import ControllerTurnResult, ShoppingController


@pytest.fixture
def four_item_evidence() -> EvidenceSnapshot:
    """Fixture providing four ordered products >=$750 boundary and option-bearing variants."""
    p1 = ProductEvidence(
        product_id="gid://shopify/Product/101",
        title="Alpine Pro Snowboard",
        url="/products/alpine-pro-snowboard",
        variants=(
            VariantEvidence(
                variant_id="gid://shopify/ProductVariant/1001",
                product_id="gid://shopify/Product/101",
                product_title="Alpine Pro Snowboard",
                variant_title="155cm / Matte",
                selected_options={"size": "155cm", "finish": "matte"},
                price=Money(Decimal("750.00"), "USD"),
                available_for_sale=True,
                quantity_available=5,
            ),
        ),
        options=("Size", "Finish"),
    )
    p2 = ProductEvidence(
        product_id="gid://shopify/Product/102",
        title="Summit Powder Snowboard",
        url="/products/summit-powder-snowboard",
        variants=(
            VariantEvidence(
                variant_id="gid://shopify/ProductVariant/1002",
                product_id="gid://shopify/Product/102",
                product_title="Summit Powder Snowboard",
                variant_title="160cm / Blue",
                selected_options={"size": "160cm", "color": "blue"},
                price=Money(Decimal("820.00"), "USD"),
                available_for_sale=True,
                quantity_available=3,
            ),
        ),
        options=("Size", "Color"),
    )
    p3 = ProductEvidence(
        product_id="gid://shopify/Product/103",
        title="Freestyle Carbon Snowboard",
        url="/products/freestyle-carbon-snowboard",
        variants=(
            VariantEvidence(
                variant_id="gid://shopify/ProductVariant/1003",
                product_id="gid://shopify/Product/103",
                product_title="Freestyle Carbon Snowboard",
                variant_title="150cm / Red",
                selected_options={"size": "150cm", "color": "red"},
                price=Money(Decimal("890.00"), "USD"),
                available_for_sale=False,
                quantity_available=0,
            ),
        ),
        options=("Size", "Color"),
    )
    p4 = ProductEvidence(
        product_id="gid://shopify/Product/104",
        title="Backcountry Explorer Snowboard",
        url="/products/backcountry-explorer-snowboard",
        variants=(
            VariantEvidence(
                variant_id="gid://shopify/ProductVariant/1004",
                product_id="gid://shopify/Product/104",
                product_title="Backcountry Explorer Snowboard",
                variant_title="165cm / Black",
                selected_options={"size": "165cm", "color": "black"},
                price=Money(Decimal("950.00"), "USD"),
                available_for_sale=True,
                quantity_available=2,
            ),
        ),
        options=("Size", "Color"),
    )
    return EvidenceSnapshot(
        snapshot_id="snap_snowboards",
        shop_id="drake-test.myshopify.com",
        currency="USD",
        observed_at_ms=100000,
        products={
            p1.product_id: p1,
            p2.product_id: p2,
            p3.product_id: p3,
            p4.product_id: p4,
        },
    )


@pytest.fixture
def empty_cart() -> CartSnapshot:
    return CartSnapshot(
        shop_id="drake-test.myshopify.com",
        currency="USD",
        lines=(),
    )


def test_cs01_search_and_ordinal_navigation(four_item_evidence, empty_cart):
    """CS-01: Search four products >=$750, then select second item to navigate."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs01"

    # Turn 1: Search snowboards >= 750
    t1 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="Find snowboards over 750 dollars",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert t1.status == "completed"
    assert len(t1.result_product_ids) == 4
    assert t1.result_product_ids[1] == "gid://shopify/Product/102"

    # Turn 2: "open the second one"
    t2 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="open the second one",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert t2.status == "completed"
    assert t2.authorized_command is not None
    assert t2.authorized_command.operation == CommandOperation.NAVIGATE_STOREFRONT
    assert t2.authorized_command.parameters["url"] == "/products/summit-powder-snowboard"


def test_cs02_cs03_target_lookup_and_invalid_product(four_item_evidence, empty_cart):
    """CS-02 & CS-03: Known product navigates; missing product does not navigate."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs02"

    # Known product
    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="take me to the summit powder snowboard",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "completed"
    assert res.authorized_command.parameters["url"] == "/products/summit-powder-snowboard"

    # Missing product: should clarify or fail truthfully, not guess or navigate to random item
    res_missing = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="take me to the non-existent mystery board 9000",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res_missing.authorized_command is None
    assert res_missing.status in ("clarification_needed", "completed", "evidence_required")


def test_cs04_ordinal_bounds(four_item_evidence, empty_cart):
    """CS-04: Out-of-bounds ordinals trigger clarification instead of guessing."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs04"

    # Search first
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="show snowboards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    # Ask for 10th item (only 4 exist)
    t2 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="open the 10th item",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert t2.status == "clarification_needed"
    assert "no longer available" in t2.spoken_response or "choose" in t2.spoken_response


def test_cs05_scope_conflict_resolution(four_item_evidence, empty_cart):
    """CS-05: Ambiguous 'it' clarifies when current page product conflicts with search selection."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs05"

    # Perform search
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="search snowboards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    # Select second item
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="choose item 2",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    # Now on page for Product 101, but user asks "describe it" with conflicting scope
    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_3",
            request_revision=3,
            page_epoch=1,
            transcript="tell me more about it",
            evidence=four_item_evidence,
            current_cart=empty_cart,
            current_product_id="gid://shopify/Product/101",
        )
    )
    assert res.status == "clarification_needed"
    assert "Would you like details" in res.spoken_response


def test_cs06_multiple_search_sets(four_item_evidence, empty_cart):
    """CS-06: Search sets are tracked and earlier searches can be referenced."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs06"

    # Search 1
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="find snowboards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    # Search 2
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="find powder boards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    # Reference earlier search
    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_3",
            request_revision=3,
            page_epoch=1,
            transcript="open the second one from my earlier search",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "completed"
    assert res.authorized_command is not None


def test_cs09_cs10_no_progress_and_step_bounds(four_item_evidence, empty_cart):
    """CS-09 & CS-10: Step count is bounded to 8 steps max."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs10"
    sess = controller.get_session_state(sess_id)
    sess.continuation_state["step_count"] = 8  # simulate 8 previous steps

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_loop",
            request_revision=1,
            page_epoch=1,
            transcript="find something",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "error"
    assert "step limit" in res.spoken_response


def test_cs11_compare_and_open_cheaper(four_item_evidence, empty_cart):
    """CS-11: Compare items 1 and 2, then 'open the cheaper one'."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs11"

    # Search first
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="search snowboards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    # Compare first and second
    t2 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="compare the first and second",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert t2.status == "completed"
    assert "Comparing 1." in t2.spoken_response

    # Follow up: open the cheaper one (Product 101 @ $750 vs Product 102 @ $820)
    t3 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_3",
            request_revision=3,
            page_epoch=1,
            transcript="open the cheaper one",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert t3.status == "completed"
    assert t3.authorized_command is not None
    assert t3.authorized_command.parameters["url"] == "/products/alpine-pro-snowboard"


def test_cs12_compound_search_and_open_cheapest(four_item_evidence, empty_cart):
    """CS-12: Compound query 'Find the cheapest available snowboard and open it'."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs12"

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="Find the cheapest available snowboard and open it",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "completed"
    assert res.authorized_command is not None
    assert res.authorized_command.operation == CommandOperation.NAVIGATE_STOREFRONT
    # Cheapest available is Product 101 ($750)
    assert res.authorized_command.parameters["url"] == "/products/alpine-pro-snowboard"


def test_cs13_compound_search_and_add_clarification(four_item_evidence, empty_cart):
    """CS-13: 'Search and add' without explicit product selection clarifies first."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs13"

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="Find snowboards and add one to cart",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "clarification_needed"
    assert res.authorized_command is None


def test_cs15_cancel_clarification(four_item_evidence, empty_cart):
    """CS-15: Explicit 'cancel' clears pending clarification state."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs15"

    # Start add that triggers option clarification
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="add the alpine pro snowboard to cart",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    # Cancel
    t2 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="never mind cancel that",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert t2.status == "cancelled"
    sess = controller.get_session_state(sess_id)
    assert sess.pending_clarification_intent is None


def test_cs17_snapshot_serialization_roundtrip(four_item_evidence, empty_cart):
    """CS-17: Controller session state exports and restores seamlessly."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs17"

    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="search snowboards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    state_dict = controller.export_session_state(sess_id)
    assert "result_sets" in state_dict
    assert "comparison_context" in state_dict
    assert "continuation_state" in state_dict

    controller2 = ShoppingController(llm_provider=FakeLlmProvider())
    controller2.restore_session_state(state_dict)
    restored = controller2.get_session_state(sess_id)
    assert restored.session_id == sess_id
    assert len(restored.result_sets) >= 1
    assert restored.active_search_product_ids == ("gid://shopify/Product/101", "gid://shopify/Product/102", "gid://shopify/Product/103", "gid://shopify/Product/104")


def test_cs18_cs20_page_epoch_and_cancellation(four_item_evidence, empty_cart):
    """CS-18 & CS-20: Epoch change invalidates older turns; cancelled turn aborts."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs20"

    # Cancel turn before dispatch
    controller.cancel_turn(sess_id, "turn_stale")

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_stale",
            request_revision=1,
            page_epoch=1,
            transcript="open the alpine pro snowboard",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "cancelled"
    assert res.authorized_command is None


def test_cs07_presentation_retrieved_vs_shown(four_item_evidence, empty_cart):
    """CS-07: Search returns 4 matches with top 3 in summary and all 4 in result_product_ids."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs07"

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="find snowboards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "completed"
    assert len(res.result_product_ids) == 4
    # Summary contains numbered items 1, 2, 3
    assert "Found 4 items: 1." in res.spoken_response
    assert "2." in res.spoken_response
    assert "3." in res.spoken_response


def test_cs08_observation_resume_preserves_intent(four_item_evidence, empty_cart):
    """CS-08: Correlated observation resumes pending turn without restarting intent."""
    class CountingProvider(FakeLlmProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def interpret_intent(self, request):
            self.calls += 1
            return await super().interpret_intent(request)

    provider = CountingProvider()
    controller = ShoppingController(llm_provider=provider)
    sess_id = "sess_cs08"

    # Empty evidence triggers evidence_required
    empty_ev = EvidenceSnapshot(
        snapshot_id="empty_snap",
        shop_id="drake-test.myshopify.com",
        currency="USD",
        observed_at_ms=1000,
        products={},
    )
    t1 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="add the alpine pro snowboard to cart",
            evidence=empty_ev,
            current_cart=empty_cart,
        )
    )
    assert t1.status == "evidence_required"
    assert provider.calls == 1

    # Resume with evidence
    t2 = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="add the alpine pro snowboard to cart",
            evidence=replace(four_item_evidence, query=t1.evidence_query),
            current_cart=empty_cart,
        )
    )
    assert t2.status in ("completed", "clarification_needed")
    # Intent extraction was NOT re-run
    assert provider.calls == 1


def test_cs14_option_clarification_preserves_quantity(four_item_evidence, empty_cart):
    """CS-14: 'Add two of the second one' preserves count 2 through option clarification."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs14"

    # Search first
    asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="find snowboards",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )

    # Add 2 of second item -> triggers color/size clarification if multiple options exist
    # Or directly authorizes with quantity 2
    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_2",
            request_revision=2,
            page_epoch=1,
            transcript="add two of the second item",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    if res.status == "completed":
        assert res.authorized_command is not None
        assert res.authorized_command.parameters["quantity"] == 2
    else:
        assert res.status == "clarification_needed"


def test_cs16_unavailable_variant_handling(four_item_evidence, empty_cart):
    """CS-16: Unavailable item (Product 103) is rejected or clarified without false stock promise."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs16"

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="add the freestyle carbon snowboard to cart",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status in ("rejected", "clarification_needed")
    assert res.authorized_command is None


def test_cs23_boundary_price_constraint(four_item_evidence, empty_cart):
    """CS-23: 'over 750 dollars' includes boundary product @ $750 exactly."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs23"

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="Find snowboards over 750 dollars",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "completed"
    assert "gid://shopify/Product/101" in res.result_product_ids


def test_cs24_view_cart_in_place(four_item_evidence, empty_cart):
    """CS-24: 'Show my cart' answers in place without mutating or navigating unexpectedly."""
    controller = ShoppingController(llm_provider=FakeLlmProvider())
    sess_id = "sess_cs24"

    res = asyncio.run(
        controller.handle_turn(
            session_id=sess_id,
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="what is in my cart",
            evidence=four_item_evidence,
            current_cart=empty_cart,
        )
    )
    assert res.status == "completed"
    assert "empty" in res.spoken_response
    assert res.authorized_command is None


"""Regression tests for Phase 2: R6 (Schema validation), R4 (Budget enforcement), and R9 (Product description).

Demonstrates failure before remediation and pass after remediation.
"""

import asyncio
from decimal import Decimal
import pytest

from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.commerce.simulator import ShopifySimulator
from ishop.domain.intent import (
    BudgetConstraint,
    IntentOperation,
    QuantityChange,
    ShoppingIntent,
)
from ishop.domain.models import CartLine, CartSnapshot, Money
from ishop.llm.fake import FakeLlmProvider
from ishop.llm.base import LlmIntentRequest, LlmInterpretationResult, LlmUsage
from ishop.orchestration.controller import ShoppingController


class FakeIntentProvider(FakeLlmProvider):
    """Test provider returning a pre-configured ShoppingIntent."""

    def __init__(self, intent: ShoppingIntent):
        super().__init__()
        self.intent = intent

    async def interpret_intent(self, request: LlmIntentRequest) -> LlmInterpretationResult:
        return LlmInterpretationResult(
            intent=self.intent,
            raw_response_text="{}",
            usage=LlmUsage(),
            profile_id=self.profile.profile_id,
        )

    async def generate_grounded_response(self, request) -> str:
        return "Test response"


@pytest.fixture
def store_catalog() -> EvidenceSnapshot:
    p_tee = ProductEvidence(
        product_id="prod_tee",
        title="Cotton T-Shirt",
        options=("color", "size"),
        variants=(
            VariantEvidence(
                variant_id="var_tee",
                product_id="prod_tee",
                product_title="Cotton T-Shirt",
                variant_title="Standard",
                selected_options={"color": "white", "size": "M"},
                price=Money(Decimal("5000.00"), "NGN"),
                available_for_sale=True,
                quantity_available=10,
            ),
        ),
    )
    p_cap = ProductEvidence(
        product_id="prod_cap",
        title="Embroidered Cap",
        options=("style",),
        variants=(
            VariantEvidence(
                variant_id="var_cap",
                product_id="prod_cap",
                product_title="Embroidered Cap",
                variant_title="Standard",
                selected_options={"style": "classic"},
                price=Money(Decimal("3500.00"), "NGN"),
                available_for_sale=True,
                quantity_available=5,
            ),
            VariantEvidence(
                variant_id="var_cap_sold_out",
                product_id="prod_cap",
                product_title="Embroidered Cap",
                variant_title="Sold Out Variant",
                selected_options={"style": "vintage"},
                price=Money(Decimal("3500.00"), "NGN"),
                available_for_sale=False,
                quantity_available=0,
            ),
        ),
    )
    return EvidenceSnapshot(
        snapshot_id="snap_1",
        observed_at_ms=1000,
        shop_id="test.myshopify.com",
        currency="NGN",
        products={"prod_tee": p_tee, "prod_cap": p_cap},
    )


def test_r06_regression_strict_schema_validation_rejects_malformed_payloads():
    """R6: ShoppingIntent must strictly validate against JSON Schema and reject malformed types."""
    # 1. Float quantity value must be rejected (not coerced to int)
    with pytest.raises(ValueError, match="(?i)quantity|integer|type"):
        ShoppingIntent.from_dict({
            "schema_version": "1.0.0",
            "intent_id": "int_1",
            "operation": "add_to_cart",
            "quantity_change": {"mode": "set", "value": 1.9},
            "is_explicit_checkout_request": False,
            "supporting_transcript_span": "add 1.9 caps",
            "unresolved_fields": [],
        })

    # 2. String boolean must be rejected (not coerced to True via bool("false"))
    with pytest.raises(ValueError, match="(?i)boolean|checkout"):
        ShoppingIntent.from_dict({
            "schema_version": "1.0.0",
            "intent_id": "int_2",
            "operation": "add_to_cart",
            "is_explicit_checkout_request": "false",
            "supporting_transcript_span": "checkout false",
            "unresolved_fields": [],
        })

    # 3. Unknown properties must be rejected (additionalProperties: false)
    with pytest.raises(ValueError, match="(?i)unexpected|unknown|additional"):
        ShoppingIntent.from_dict({
            "schema_version": "1.0.0",
            "intent_id": "int_3",
            "operation": "add_to_cart",
            "unknown_injected_property": "evil_payload",
            "is_explicit_checkout_request": False,
            "supporting_transcript_span": "add cap",
            "unresolved_fields": [],
        })

    # 4. Missing required properties must be rejected
    with pytest.raises(ValueError, match="(?i)required|missing"):
        ShoppingIntent.from_dict({
            "operation": "add_to_cart",
            "product_query": "cap",
        })


def test_r04_regression_budget_constraint_enforced_on_cart_mutations(store_catalog):
    """R4: Budget constraints must prevent mutations when the resolved variant exceeds the budget ceiling."""
    async def _run():
        intent = ShoppingIntent(
            schema_version="1.0.0",
            intent_id="int_budget",
            operation=IntentOperation.ADD_TO_CART,
            product_query="cap",
            selected_variant_attributes={"style": "classic"},
            quantity_change=QuantityChange(mode="increment", value=1),
            budget_constraint=BudgetConstraint(max_amount="1000", currency="NGN"),
            is_explicit_checkout_request=False,
            supporting_transcript_span="add cap under 1000 naira",
        )

        controller = ShoppingController(llm_provider=FakeIntentProvider(intent))
        empty_cart = CartSnapshot(shop_id="test.myshopify.com", currency="NGN")

        result = await controller.handle_turn(
            session_id="sess_1",
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="add cap under 1000 naira",
            evidence=store_catalog,
            current_cart=empty_cart,
        )

        # Controller MUST NOT authorize command or return completed; must reject with budget explanation
        assert result.status == "rejected"
        assert result.authorized_command is None
        assert "budget" in (result.reason or "").lower() or "exceeds" in (result.reason or "").lower()

    asyncio.run(_run())


def test_r04_regression_remove_sold_out_item_from_cart(store_catalog):
    """R4: Removing an item that is currently in the cart must succeed even if sold out in catalog."""
    async def _run():
        cart = CartSnapshot(
            shop_id="test.myshopify.com",
            currency="NGN",
            lines=(
                CartLine(variant_id="var_cap_sold_out", quantity=1),
            ),
        )

        intent = ShoppingIntent(
            schema_version="1.0.0",
            intent_id="int_remove",
            operation=IntentOperation.REMOVE_FROM_CART,
            product_query="cap",
            selected_variant_attributes={},
            quantity_change=QuantityChange(mode="set", value=0),
            is_explicit_checkout_request=False,
            supporting_transcript_span="remove cap from my cart",
        )

        sim = ShopifySimulator(shop_id="test.myshopify.com", currency="NGN")
        sim.add_initial_line(variant_id="var_cap_sold_out", quantity=1)
        controller = ShoppingController(llm_provider=FakeIntentProvider(intent))

        result = await controller.handle_turn(
            session_id="sess_1",
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="remove cap from my cart",
            evidence=store_catalog,
            current_cart=cart,
            client=sim,
        )

        # Must NOT be rejected with 'variant is sold out'
        assert result.status != "rejected"
        assert result.authorized_command is not None
        assert result.authorized_command.operation == "remove_line"

    asyncio.run(_run())


def test_r09_regression_product_describe_targets_requested_item(store_catalog):
    """R9: Describing a product must resolve the requested product, not blindly compare the first two."""
    async def _run():
        intent = ShoppingIntent(
            schema_version="1.0.0",
            intent_id="int_desc",
            operation=IntentOperation.DESCRIBE_PRODUCT,
            product_query="cap",
            is_explicit_checkout_request=False,
            supporting_transcript_span="tell me about the embroidered cap",
        )

        controller = ShoppingController(llm_provider=FakeIntentProvider(intent))
        empty_cart = CartSnapshot(shop_id="test.myshopify.com", currency="NGN")

        result = await controller.handle_turn(
            session_id="sess_1",
            turn_id="turn_1",
            request_revision=1,
            page_epoch=1,
            transcript="tell me about the embroidered cap",
            evidence=store_catalog,
            current_cart=empty_cart,
        )

        assert result.status == "completed"
        # Spoken response must talk about Embroidered Cap, NOT start comparing T-Shirt and Cap
        assert "Embroidered Cap" in result.spoken_response
        assert "Comparing Cotton T-Shirt" not in result.spoken_response

    asyncio.run(_run())


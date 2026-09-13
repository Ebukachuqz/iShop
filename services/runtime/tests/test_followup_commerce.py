"""F2–F6: independent state, exact removals/locators and affordable budgets."""
import asyncio

import pytest

from test_llm_controller import empty_cart, store_evidence
from ishop.commerce.reconciler import CommandReconciler, ExecutionOutcome
from ishop.commerce.simulator import ShopifySimulator, SimulationResult
from ishop.commerce.verifier import CartVerifier, ProposedCartAction, QuantityOperation
from ishop.domain.journal import CommandJournal
from ishop.domain.models import CartLine, CartSnapshot, CommandOperation, Money
from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.domain.intent import IntentOperation, QuantityChange, ShoppingIntent
from ishop.llm.fake import FakeLlmProvider
from ishop.orchestration.controller import ShoppingController


def test_f2_write_payload_is_not_independent_readback(empty_cart):
    reconciler = CommandReconciler(CommandJournal())
    command = CartVerifier.verify_action(
        empty_cart, ProposedCartAction(QuantityOperation.INCREMENT, variant_id="cap"),
        "session", "turn", 1, 1,
    ).command

    class LyingAdapter:
        def read_cart(self):
            return empty_cart

        def execute_command(self, cmd):
            return SimulationResult(
                status_code=200, cart_snapshot=reconciler.compute_expected_cart(empty_cart, cmd)
            )

    receipt = reconciler.execute_and_reconcile(command, LyingAdapter())
    assert receipt.outcome == ExecutionOutcome.REJECTED


def test_f2_recovery_without_before_state_remains_uncertain(empty_cart):
    command = CartVerifier.verify_action(
        empty_cart, ProposedCartAction(QuantityOperation.INCREMENT, variant_id="cap"),
        "session", "turn", 1, 1,
    ).command
    sim = ShopifySimulator(empty_cart.shop_id, empty_cart.currency)
    sim.add_initial_line("cap", 1)
    sim.add_initial_line("unintended", 10)
    receipt = CommandReconciler(CommandJournal()).reconcile_uncertain(command, sim)
    assert receipt.outcome == ExecutionOutcome.UNCERTAIN


def run_turn(evidence, sim, transcript):
    controller = ShoppingController(FakeLlmProvider(), CommandReconciler(CommandJournal()))
    return asyncio.run(controller.handle_turn(
        "session", "turn", 1, 1, transcript, evidence, sim.read_cart(), client=sim,
    ))


def test_f3_single_wrong_color_is_not_removed(store_evidence):
    sim = ShopifySimulator(store_evidence.shop_id, store_evidence.currency)
    sim.add_initial_line("var_red_m", 1)
    before = sim.read_cart()
    result = run_turn(store_evidence, sim, "Remove blue cotton t-shirt medium")
    assert result.authorized_command is None
    assert sim.read_cart().is_equivalent(before)


def test_f4_removal_returns_verified_receipt(store_evidence):
    sim = ShopifySimulator(store_evidence.shop_id, store_evidence.currency)
    sim.add_initial_line("var_cap", 1)
    result = run_turn(store_evidence, sim, "Remove embroidered cap")
    assert result.receipt.outcome == ExecutionOutcome.VERIFIED_SUCCESS
    assert "Removed" in result.spoken_response
    assert not sim.read_cart().lines


@pytest.mark.parametrize("locator,variant,allowed", [
    ("v:realkey", "v", True), ("stale", "v", False), ("v:realkey", "wrong", False),
])
def test_f5_locator_resolution(empty_cart, locator, variant, allowed):
    cart = CartSnapshot(empty_cart.shop_id, empty_cart.currency, (
        CartLine("v", 1, shopify_line_key="v:realkey"),
    ))
    result = CartVerifier.verify_action(
        cart, ProposedCartAction(QuantityOperation.SET, variant_id=variant,
                                 line_key=locator, quantity=2), "s", "t", 1, 1,
    )
    assert result.allowed is allowed
    if allowed:
        assert result.command.operation == CommandOperation.SET_LINE_QUANTITY
        assert result.command.parameters["target_line_key"] == "v:realkey"


def test_f5_simulator_preserves_locator(empty_cart):
    sim = ShopifySimulator(empty_cart.shop_id, empty_cart.currency)
    sim.set_cart(CartSnapshot(empty_cart.shop_id, empty_cart.currency, (
        CartLine("v", 1, shopify_line_key="v:realkey"),
    )))
    assert sim.read_cart().lines[0].shopify_line_key == "v:realkey"


@pytest.mark.parametrize("quantity,budget,expected", [(1, 10000, 1), (2, 10000, 2), (2, 6000, 0)])
def test_f6_budget_affordable_and_excessive(store_evidence, quantity, budget, expected):
    sim = ShopifySimulator(store_evidence.shop_id, store_evidence.currency)
    result = run_turn(store_evidence, sim, f"Add {quantity} embroidered cap under {budget} naira")
    assert sum(line.quantity for line in sim.read_cart().lines) == expected
    assert (result.status == "completed") is bool(expected)


@pytest.mark.parametrize("scope,expected", [("total", 0), ("per_item", 2), ("unknown", 0)])
def test_f6_budget_scope_is_explicit(store_evidence, scope, expected):
    from dataclasses import replace

    class ScopedProvider(FakeLlmProvider):
        async def interpret_intent(self, request):
            result = await super().interpret_intent(request)
            return replace(result, intent=replace(result.intent, budget_constraint=replace(
                result.intent.budget_constraint, scope=scope,
            )))

    sim = ShopifySimulator(store_evidence.shop_id, store_evidence.currency)
    controller = ShoppingController(ScopedProvider(), CommandReconciler(CommandJournal()))
    result = asyncio.run(controller.handle_turn(
        "s", "t", 1, 1, "Add 2 embroidered cap under 4000 naira",
        store_evidence, sim.read_cart(), client=sim,
    ))
    assert sum(line.quantity for line in sim.read_cart().lines) == expected
    if scope == "unknown":
        assert result.status == "clarification_needed"


def test_option_only_reply_resumes_pending_product_clarification():
    variants = tuple(
        VariantEvidence(f"var_{color.lower()}", "prod_board", "Complete Snowboard", color,
                        {"color": color}, Money(69995, "USD"), True)
        for color in ("Ice", "Dawn")
    )
    evidence = EvidenceSnapshot("snap-board", "shop.myshopify.com", "USD", 1,
        {"prod_board": ProductEvidence("prod_board", "Complete Snowboard", variants, ("color",))})
    cart = CartSnapshot(evidence.shop_id, evidence.currency, ())
    provider = FakeLlmProvider()
    provider.register_custom_intent("complete snowboard", ShoppingIntent(
        "int-board", IntentOperation.ADD_TO_CART, False, "complete snowboard",
        product_query="Complete Snowboard", quantity_change=QuantityChange("increment", 1),
    ))
    controller = ShoppingController(provider)
    first = asyncio.run(controller.handle_turn("s", "t1", 1, 1,
        "Add the Complete Snowboard", evidence, cart))
    assert first.status == "clarification_needed"
    assert "color" in first.clarification_fields
    second = asyncio.run(controller.handle_turn("s", "t2", 2, 1, "Ice", evidence, cart))
    assert second.authorized_command is not None
    assert second.authorized_command.parameters["variant_id"] == "var_ice"

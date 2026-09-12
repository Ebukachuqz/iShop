"""Integration tests for iShop commerce catalog resolution and cart reconciliation.

Covers:
- T-02: Invented product ID, wrong shop/currency and stale product evidence rejected.
- T-03: Variant ambiguity clarification & no explicit option substitution.
- T-05: Quantity increment ("add two" to 1 -> 3), set ("make it two" -> 2), and zero removal.
- T-07: Sold-out variant, inventory shortage, and unknown stock handling.
- T-08: HTTP 200 with userErrors cannot produce successful receipt.
- T-10: Two lines sharing variant ID targeted and preserved independently.
- T-12: Duplicate command replay idempotent without repeat mutation.
- T-13: Process restart recovery reconciles live cart without repeat add.
- T-14: Lost response and timeout recovery without duplicate writes.
- T-16: Safe adapter fallback and refusal to retry uncertain writes.
"""

from __future__ import annotations

import time
import uuid
import pytest

from ishop.commerce.catalog import (
    CatalogResolver,
    EvidenceSnapshot,
    IntentTarget,
    ProductEvidence,
    ResolutionStatus,
    VariantEvidence,
)
from ishop.commerce.reconciler import (
    CommandReconciler,
    ExecutionOutcome,
)
from ishop.commerce.simulator import (
    FaultMode,
    ShopifySimulator,
)
from ishop.commerce.verifier import (
    CartVerifier,
    ProposedCartAction,
    QuantityOperation,
)
from ishop.domain.journal import CommandJournal, CommandStatus
from ishop.domain.models import (
    CartLine,
    CartSnapshot,
    Money,
)


@pytest.fixture
def sample_evidence() -> EvidenceSnapshot:
    v_red_s = VariantEvidence(
        variant_id="var_red_s",
        product_id="prod_shirt",
        product_title="Cotton T-Shirt",
        variant_title="Red / S",
        selected_options={"color": "red", "size": "s"},
        price=Money.from_string("25.00", "USD"),
        available_for_sale=True,
        quantity_available=10,
        inventory_policy="DENY",
    )
    v_red_m = VariantEvidence(
        variant_id="var_red_m",
        product_id="prod_shirt",
        product_title="Cotton T-Shirt",
        variant_title="Red / M",
        selected_options={"color": "red", "size": "m"},
        price=Money.from_string("25.00", "USD"),
        available_for_sale=True,
        quantity_available=2,
        inventory_policy="DENY",
    )
    v_blue_l = VariantEvidence(
        variant_id="var_blue_l",
        product_id="prod_shirt",
        product_title="Cotton T-Shirt",
        variant_title="Blue / L",
        selected_options={"color": "blue", "size": "l"},
        price=Money.from_string("28.00", "USD"),
        available_for_sale=False,  # Sold out (T-07)
        quantity_available=0,
        inventory_policy="DENY",
    )
    v_green_unknown = VariantEvidence(
        variant_id="var_green_u",
        product_id="prod_shirt",
        product_title="Cotton T-Shirt",
        variant_title="Green / OneSize",
        selected_options={"color": "green", "size": "onesize"},
        price=Money.from_string("30.00", "USD"),
        available_for_sale=True,
        quantity_available=None,  # Unknown stock (T-07)
        inventory_policy="CONTINUE",
    )

    product = ProductEvidence(
        product_id="prod_shirt",
        title="Cotton T-Shirt",
        variants=(v_red_s, v_red_m, v_blue_l, v_green_unknown),
        options=("Color", "Size"),
    )

    return EvidenceSnapshot(
        snapshot_id="snap_001",
        shop_id="test-store.myshopify.com",
        currency="USD",
        observed_at_ms=1700000000000,
        products={"prod_shirt": product},
    )


@pytest.fixture
def memory_journal() -> CommandJournal:
    return CommandJournal(":memory:")


# --- T-02 Tests ---
def test_t02_invented_product_id_rejected(sample_evidence: EvidenceSnapshot):
    target = IntentTarget(product_id="prod_invented_fake", quantity=1)
    res = CatalogResolver.resolve(sample_evidence, target)
    assert res.status == ResolutionStatus.REJECT
    assert "Invented or nonexistent product ID" in res.reason


def test_t02_wrong_currency_and_shop_rejected(sample_evidence: EvidenceSnapshot):
    target = IntentTarget(product_id="prod_shirt", selected_options={"color": "red", "size": "s"})
    # Wrong currency
    res = CatalogResolver.resolve(sample_evidence, target, expected_currency="EUR")
    assert res.status == ResolutionStatus.REJECT
    assert "currency mismatch" in res.reason

    # Wrong shop
    res2 = CatalogResolver.resolve(sample_evidence, target, expected_shop_id="other.myshopify.com")
    assert res2.status == ResolutionStatus.REJECT
    assert "shop mismatch" in res2.reason


# --- T-03 Tests ---
def test_t03_variant_ambiguity_returns_clarify(sample_evidence: EvidenceSnapshot):
    # Only "Red" is specified, but Red has S and M -> incomplete variant
    target = IntentTarget(title_query="Cotton T-Shirt", selected_options={"color": "red"})
    res = CatalogResolver.resolve(sample_evidence, target)
    assert res.status == ResolutionStatus.CLARIFY
    assert "size" in res.missing_options
    assert len(res.candidate_variants) == 2


def test_t03_no_silent_variant_substitution(sample_evidence: EvidenceSnapshot):
    # Target requests "Yellow", which is not available for this product
    target = IntentTarget(title_query="Cotton T-Shirt", selected_options={"color": "yellow", "size": "m"})
    res = CatalogResolver.resolve(sample_evidence, target)
    assert res.status == ResolutionStatus.REJECT
    assert "Silent substitution forbidden" in res.reason


# --- T-05 Tests ---
def test_t05_quantity_arithmetic():
    cart = CartSnapshot(
        shop_id="test-store.myshopify.com",
        currency="USD",
        lines=(CartLine(variant_id="var_red_s", quantity=1),),
    )

    # 1. "add two" -> increment by 2 -> target 3
    action_inc = ProposedCartAction(
        operation=QuantityOperation.INCREMENT,
        variant_id="var_red_s",
        quantity=2,
    )
    outcome_inc = CartVerifier.verify_action(cart, action_inc, "sess_1", "turn_1", 1, 1)
    assert outcome_inc.allowed is True
    assert outcome_inc.target_quantity == 3
    assert outcome_inc.command.parameters["quantity"] == 3

    # 2. "make it two" -> set to 2 -> target 2
    action_set = ProposedCartAction(
        operation=QuantityOperation.SET,
        variant_id="var_red_s",
        quantity=2,
    )
    outcome_set = CartVerifier.verify_action(cart, action_set, "sess_1", "turn_1", 1, 1)
    assert outcome_set.allowed is True
    assert outcome_set.target_quantity == 2
    assert outcome_set.command.parameters["quantity"] == 2

    # 3. "remove" -> target 0
    action_rem = ProposedCartAction(
        operation=QuantityOperation.REMOVE,
        variant_id="var_red_s",
    )
    outcome_rem = CartVerifier.verify_action(cart, action_rem, "sess_1", "turn_1", 1, 1)
    assert outcome_rem.allowed is True
    assert outcome_rem.target_quantity == 0
    assert outcome_rem.command.operation.value == "remove_line"


# --- T-07 Tests ---
def test_t07_sold_out_and_inventory_shortage(sample_evidence: EvidenceSnapshot):
    # 1. Sold out variant
    target_sold = IntentTarget(product_id="prod_shirt", selected_options={"color": "blue", "size": "l"})
    res_sold = CatalogResolver.resolve(sample_evidence, target_sold)
    assert res_sold.status == ResolutionStatus.REJECT
    assert "sold out" in res_sold.reason

    # 2. Stock shortage (available: 2, requested: 5)
    target_short = IntentTarget(product_id="prod_shirt", selected_options={"color": "red", "size": "m"}, quantity=5)
    res_short = CatalogResolver.resolve(sample_evidence, target_short)
    assert res_short.status == ResolutionStatus.REJECT
    assert "Inventory shortage" in res_short.reason

    # 3. Unknown stock: resolves without false in-stock guarantee
    target_unknown = IntentTarget(product_id="prod_shirt", selected_options={"color": "green", "size": "onesize"}, quantity=1)
    res_unknown = CatalogResolver.resolve(sample_evidence, target_unknown)
    assert res_unknown.status == ResolutionStatus.RESOLVED
    assert res_unknown.stock_known is False


# --- T-08 Tests ---
def test_t08_http_200_with_user_errors_rejected(memory_journal: CommandJournal):
    sim = ShopifySimulator()
    reconciler = CommandReconciler(memory_journal)

    cart = sim.read_cart()
    action = ProposedCartAction(operation=QuantityOperation.INCREMENT, variant_id="var_red_s", quantity=1)
    ver = CartVerifier.verify_action(cart, action, "sess_1", "turn_1", 1, 1)

    # Inject HTTP 200 with userErrors fault
    sim.set_fault(FaultMode.USER_ERRORS)

    receipt = reconciler.execute_and_reconcile(ver.command, sim)
    assert receipt.outcome == ExecutionOutcome.REJECTED
    assert "Storefront userErrors" in receipt.verification_message
    assert len(receipt.errors) > 0


# --- T-10 Tests ---
def test_t10_two_lines_sharing_variant_targeted_and_preserved(memory_journal: CommandJournal):
    sim = ShopifySimulator()
    # Line 1: variant_1 with monogram "Alice"
    key1 = sim.add_initial_line(variant_id="var_1", quantity=1, properties={"monogram": "Alice"})
    # Line 2: variant_1 with monogram "Bob"
    key2 = sim.add_initial_line(variant_id="var_1", quantity=2, properties={"monogram": "Bob"})

    cart_before = sim.read_cart()
    assert len(cart_before.lines) == 2

    # Target specifically Line 1 (Alice) to increase quantity to 4
    action = ProposedCartAction(
        operation=QuantityOperation.SET,
        variant_id="var_1",
        properties={"monogram": "Alice"},
        quantity=4,
    )
    ver = CartVerifier.verify_action(cart_before, action, "sess_1", "turn_1", 1, 1)
    assert ver.allowed is True

    reconciler = CommandReconciler(memory_journal)
    receipt = reconciler.execute_and_reconcile(ver.command, sim)

    assert receipt.outcome == ExecutionOutcome.VERIFIED_SUCCESS
    cart_after = sim.read_cart()

    # Verify Line 1 became quantity 4, while Line 2 (Bob) remained preserved at quantity 2! (T-10, S-08)
    lines_by_prop = {list(l.properties.values())[0]: l.quantity for l in cart_after.lines}
    assert lines_by_prop["Alice"] == 4
    assert lines_by_prop["Bob"] == 2


# --- T-12 Tests ---
def test_t12_duplicate_command_replay_idempotent(memory_journal: CommandJournal):
    sim = ShopifySimulator()
    reconciler = CommandReconciler(memory_journal)

    cart = sim.read_cart()
    action = ProposedCartAction(operation=QuantityOperation.INCREMENT, variant_id="var_red_s", quantity=1)
    ver = CartVerifier.verify_action(cart, action, "sess_1", "turn_1", 1, 1)

    receipt1 = reconciler.execute_and_reconcile(ver.command, sim)
    assert receipt1.outcome == ExecutionOutcome.VERIFIED_SUCCESS

    # Replay same command ID
    receipt2 = reconciler.execute_and_reconcile(ver.command, sim)
    assert receipt2.outcome == ExecutionOutcome.VERIFIED_SUCCESS
    assert receipt2.receipt_id == receipt1.receipt_id

    # Verify variant was added only ONCE (quantity is 1, not 2)
    cart_final = sim.read_cart()
    assert cart_final.lines[0].quantity == 1


# --- T-13 Tests ---
def test_t13_process_restart_recovery(memory_journal: CommandJournal):
    sim = ShopifySimulator()
    reconciler = CommandReconciler(memory_journal)

    cart = sim.read_cart()
    action = ProposedCartAction(operation=QuantityOperation.INCREMENT, variant_id="var_1", quantity=1)
    ver = CartVerifier.verify_action(cart, action, "sess_1", "turn_1", 1, 1)

    # 1. Command prepares and dispatches
    memory_journal.prepare_command(ver.command, before_cart=cart)
    memory_journal.mark_dispatched(ver.command.command_id)

    # 2. Simulator executed mutation, but process died before receipt was recorded
    sim.add_initial_line(variant_id="var_1", quantity=1)

    # 3. Simulate process restart recovery
    reconciled_count = memory_journal.restart_reconcile()
    assert reconciled_count == 1
    assert memory_journal.get_entry(ver.command.command_id).status == CommandStatus.UNCERTAIN

    # 4. Reconcile uncertain command
    receipt = reconciler.execute_and_reconcile(ver.command, sim)
    assert receipt.outcome == ExecutionOutcome.VERIFIED_SUCCESS
    assert "Reconciled uncertain command" in receipt.verification_message

    # Verify no duplicate addition occurred
    assert sim.read_cart().lines[0].quantity == 1


# --- T-14 Tests ---
def test_t14_lost_response_and_timeout_recovery(memory_journal: CommandJournal):
    sim = ShopifySimulator()
    reconciler = CommandReconciler(memory_journal)

    # Case A: Lost response after successful server commit
    sim.set_fault(FaultMode.LOST_RESPONSE)
    cart = sim.read_cart()
    action = ProposedCartAction(operation=QuantityOperation.INCREMENT, variant_id="var_1", quantity=2)
    ver = CartVerifier.verify_action(cart, action, "sess_1", "turn_1", 1, 1)

    receipt = reconciler.execute_and_reconcile(ver.command, sim)
    # Reconciler detected network loss, checked live cart, saw mutation succeeded, marked VERIFIED_SUCCESS!
    assert receipt.outcome == ExecutionOutcome.VERIFIED_SUCCESS
    assert sim.read_cart().lines[0].quantity == 2

    # Case B: Timeout before commit reached server
    sim.set_fault(FaultMode.TIMEOUT_BEFORE_COMMIT)
    action2 = ProposedCartAction(operation=QuantityOperation.INCREMENT, variant_id="var_2", quantity=1)
    ver2 = CartVerifier.verify_action(sim.read_cart(), action2, "sess_1", "turn_2", 2, 1)

    receipt2 = reconciler.execute_and_reconcile(ver2.command, sim)
    # Server never processed commit; live cart doesn't have var_2; outcome is UNCERTAIN (never repeat!)
    assert receipt2.outcome == ExecutionOutcome.UNCERTAIN
    assert "Mutation not confirmed on live cart" in receipt2.errors
    assert not any(l.variant_id == "var_2" for l in sim.read_cart().lines)


# --- T-16 Tests ---
def test_t16_safe_adapter_fallback_and_uncertain_retry_refusal(memory_journal: CommandJournal):
    primary_sim = ShopifySimulator()
    fallback_sim = ShopifySimulator()
    reconciler = CommandReconciler(memory_journal)

    # Inject timeout fault on primary adapter causing uncertain write
    primary_sim.set_fault(FaultMode.TIMEOUT_BEFORE_COMMIT)

    cart = primary_sim.read_cart()
    action = ProposedCartAction(operation=QuantityOperation.INCREMENT, variant_id="var_1", quantity=1)
    ver = CartVerifier.verify_action(cart, action, "sess_1", "turn_1", 1, 1)

    # Attempt execution with fallback
    receipt = reconciler.execute_with_fallback(
        command=ver.command,
        primary_adapter=primary_sim,
        fallback_adapter=fallback_sim,
    )

    # T-16 & S-10: Must NOT fallback-retry an uncertain write!
    assert receipt.outcome == ExecutionOutcome.UNCERTAIN
    # Verify fallback adapter was NOT written to
    assert len(fallback_sim.read_cart().lines) == 0

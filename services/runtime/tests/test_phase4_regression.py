"""Regression tests for R3 and R5 (Cart reconciliation correctness & line locators).

Findings:
- R3 (P0): Incomplete cart reconciliation falsely reports success (ignores extra lines & unrelated drops).
- R5 (P1): Flawed cart line locators, custom attributes, and shop identity derivation.
"""

from __future__ import annotations

import json
import pytest

from ishop.commerce.reconciler import CommandReconciler, ExecutionOutcome
from ishop.commerce.simulator import ShopifySimulator, SimulationResult
from ishop.commerce.verifier import CartVerifier, ProposedCartAction, QuantityOperation
from ishop.domain.journal import CommandJournal, CommandStatus
from ishop.domain.models import AuthorizedCommand, CartLine, CartSnapshot, CommandOperation, Money


class MutatingSimulator(ShopifySimulator):
    """Simulator that can inject unintended cart lines or drop existing lines."""

    def __init__(self, shop_id: str, currency: str, initial_cart: CartSnapshot):
        super().__init__(shop_id=shop_id, currency=currency)
        self.set_cart(initial_cart)
        self.injected_extra_line: CartLine | None = None
        self.drop_unrelated_line: bool = False

    def execute_command(self, command: AuthorizedCommand) -> SimulationResult:
        res = super().execute_command(command)
        current = self.read_cart()
        new_lines = list(current.lines)

        if self.injected_extra_line:
            new_lines.append(self.injected_extra_line)

        if self.drop_unrelated_line and len(new_lines) > 1:
            # Drop the first line (corrupting unrelated line)
            new_lines.pop(0)

        modified_cart = CartSnapshot(
            shop_id=current.shop_id,
            currency=current.currency,
            lines=tuple(new_lines),
        )
        self.set_cart(modified_cart)
        return SimulationResult(cart_snapshot=modified_cart, user_errors=[])


def test_r03_unintended_extra_line_fails_reconciliation():
    """R3 (P0): If storefront returns target line AND an unintended extra line, reconciler must reject."""
    shop_id = "drake-test.myshopify.com"
    currency = "NGN"
    journal = CommandJournal()
    reconciler = CommandReconciler(journal=journal)

    # Initial cart: Hoodie qty 1
    hoodie_line = CartLine(variant_id="var_hoodie", quantity=1)
    before_cart = CartSnapshot(shop_id=shop_id, currency=currency, lines=(hoodie_line,))

    sim = MutatingSimulator(shop_id=shop_id, currency=currency, initial_cart=before_cart)
    # Simulator will inject unexpected bonus line (e.g. Socks)
    sim.injected_extra_line = CartLine(variant_id="var_unintended_socks", quantity=1)

    # Command: Add Cap qty 1
    action = ProposedCartAction(
        operation=QuantityOperation.INCREMENT,
        variant_id="var_cap",
        quantity=1,
    )
    verification = CartVerifier.verify_action(
        current_cart=before_cart,
        action=action,
        session_id="sess_1",
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
    )
    assert verification.allowed and verification.command is not None

    receipt = reconciler.execute_and_reconcile(
        command=verification.command,
        adapter=sim,
    )

    # Reconciler MUST NOT report verified_success when an unintended extra line was added!
    assert receipt.outcome != ExecutionOutcome.VERIFIED_SUCCESS, (
        "Reconciler falsely reported verified_success despite unintended extra line C in cart!"
    )
    assert receipt.outcome == ExecutionOutcome.FAILED_WITH_CHANGE


def test_r03_unrelated_line_drop_fails_reconciliation():
    """R3 (P0): If storefront drops an unrelated existing line during mutation, reconciler must detect it."""
    shop_id = "drake-test.myshopify.com"
    currency = "NGN"
    journal = CommandJournal()
    reconciler = CommandReconciler(journal=journal)

    # Initial cart: Shirt qty 1 AND Hoodie qty 1
    shirt_line = CartLine(variant_id="var_shirt", quantity=1)
    hoodie_line = CartLine(variant_id="var_hoodie", quantity=1)
    before_cart = CartSnapshot(shop_id=shop_id, currency=currency, lines=(shirt_line, hoodie_line))

    sim = MutatingSimulator(shop_id=shop_id, currency=currency, initial_cart=before_cart)
    # Simulator drops the unrelated shirt during cap addition
    sim.drop_unrelated_line = True

    action = ProposedCartAction(
        operation=QuantityOperation.INCREMENT,
        variant_id="var_cap",
        quantity=1,
    )
    verification = CartVerifier.verify_action(
        current_cart=before_cart,
        action=action,
        session_id="sess_1",
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
    )
    assert verification.allowed and verification.command is not None

    receipt = reconciler.execute_and_reconcile(
        command=verification.command,
        adapter=sim,
    )

    assert receipt.outcome != ExecutionOutcome.VERIFIED_SUCCESS, (
        "Reconciler falsely reported verified_success when unrelated line was dropped!"
    )


def test_r03_uncertain_recovery_detects_unintended_extra_lines():
    """R3 (P0): In reconcile_uncertain, the entire cart multiset must be checked, not just target line."""
    shop_id = "drake-test.myshopify.com"
    currency = "NGN"
    journal = CommandJournal()
    reconciler = CommandReconciler(journal=journal)

    # Initial cart: Hoodie qty 1
    hoodie_line = CartLine(variant_id="var_hoodie", quantity=1)
    before_cart = CartSnapshot(shop_id=shop_id, currency=currency, lines=(hoodie_line,))

    # Live cart has Hoodie qty 1, Cap qty 1, AND Unintended line qty 1
    cap_line = CartLine(variant_id="var_cap", quantity=1)
    socks_line = CartLine(variant_id="var_unintended_socks", quantity=1)
    divergent_live_cart = CartSnapshot(
        shop_id=shop_id,
        currency=currency,
        lines=(hoodie_line, cap_line, socks_line),
    )

    sim = ShopifySimulator(shop_id=shop_id, currency=currency)
    sim.set_cart(divergent_live_cart)

    # Command was adding Cap
    action = ProposedCartAction(
        operation=QuantityOperation.INCREMENT,
        variant_id="var_cap",
        quantity=1,
    )
    verification = CartVerifier.verify_action(
        current_cart=before_cart,
        action=action,
        session_id="sess_1",
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
    )
    cmd = verification.command

    # Run uncertain recovery with before_cart known
    receipt = reconciler.reconcile_uncertain(
        command=cmd,
        adapter=sim,
        before_cart=before_cart,
    )

    # MUST NOT report verified_success because live cart contains unexpected extra line
    assert receipt.outcome != ExecutionOutcome.VERIFIED_SUCCESS, (
        "reconcile_uncertain falsely reported verified_success because it only checked target line!"
    )


def test_r05_line_key_preserves_shopify_locator():
    """R5 (P1): Target line key for Shopify mutations must use shopify_line_key when available."""
    shop_id = "drake-test.myshopify.com"
    currency = "NGN"

    # Line has a real Shopify line key
    shopify_key = "gid://shopify/CartLine/48592384?cart=c1_abc"
    line = CartLine(
        variant_id="var_hoodie",
        quantity=2,
        properties={"custom_engraving": "Drake"},
        shopify_line_key=shopify_key,
    )
    cart = CartSnapshot(shop_id=shop_id, currency=currency, lines=(line,))

    # Target line for update by canonical or shopify key
    action = ProposedCartAction(
        operation=QuantityOperation.SET,
        line_key=line.canonical_key,
        quantity=3,
    )

    verification = CartVerifier.verify_action(
        current_cart=cart,
        action=action,
        session_id="sess_1",
        turn_id="turn_1",
        request_revision=1,
        page_epoch=1,
    )
    assert verification.allowed
    cmd = verification.command
    assert cmd is not None

    # The command parameters must expose shopify_line_key as target_line_key for Shopify cart API
    assert cmd.parameters["target_line_key"] == shopify_key, (
        f"Expected target_line_key to be shopify_line_key '{shopify_key}', got '{cmd.parameters['target_line_key']}'"
    )
    assert cmd.parameters["shopify_line_key"] == shopify_key

"""Cart action verifier and command authorization for iShop (Drake).

Enforces Safety invariants:
- T-05: Quantity increment vs set vs zero removal.
- T-10: Multi-line targeting distinguishing lines sharing variant ID by properties/plan.
- S-08: Preserve unrelated cart contents; target exact line identity.
- S-09: Precondition cart fingerprint binding and bounded lease.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ishop.domain.models import (
    AuthorizedCommand,
    CartLine,
    CartSnapshot,
    CommandOperation,
)


class QuantityOperation(str, Enum):
    INCREMENT = "increment"
    SET = "set"
    REMOVE = "remove"


@dataclass(frozen=True)
class ProposedCartAction:
    """Action proposed after intent interpretation and catalog resolution."""

    operation: QuantityOperation
    variant_id: str | None = None
    line_key: str | None = None
    quantity: int = 1
    properties: dict[str, str] = field(default_factory=dict)
    selling_plan_id: str | None = None

    def __post_init__(self):
        if self.operation != QuantityOperation.REMOVE and self.quantity < 0:
            raise ValueError(f"Quantity cannot be negative: {self.quantity}")
        norm_props = {str(k): str(v) for k, v in sorted(self.properties.items())}
        object.__setattr__(self, "properties", norm_props)


@dataclass(frozen=True)
class VerificationOutcome:
    """Outcome of cart action verification."""

    allowed: bool
    command: AuthorizedCommand | None = None
    target_quantity: int = 0
    target_line_key: str | None = None
    reason: str | None = None


class CartVerifier:
    """Pure verifier ensuring safe quantity arithmetic and line targeting."""

    @staticmethod
    def verify_action(
        current_cart: CartSnapshot,
        action: ProposedCartAction,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
        now_ms: int | None = None,
        ttl_ms: int = 30000,
    ) -> VerificationOutcome:
        now = now_ms if now_ms is not None else int(time.time() * 1000)

        # 1. Identify targeted cart line if already in cart (T-10, S-08)
        target_line: CartLine | None = None

        if action.line_key:
            for line in current_cart.lines:
                if line.canonical_key == action.line_key:
                    target_line = line
                    break
        elif action.variant_id:
            # Match by variant_id plus properties and selling_plan_id (T-10)
            candidate_lines = [
                l for l in current_cart.lines if l.variant_id == action.variant_id
            ]
            if len(candidate_lines) == 1:
                # If properties or plan explicitly match or no options were set
                cand = candidate_lines[0]
                if not action.properties or cand.properties == action.properties:
                    if action.selling_plan_id is None or cand.selling_plan_id == action.selling_plan_id:
                        target_line = cand
            elif len(candidate_lines) > 1:
                # Multiple distinct lines share the variant ID (T-10)
                exact_matches = [
                    l for l in candidate_lines
                    if l.properties == action.properties and l.selling_plan_id == action.selling_plan_id
                ]
                if len(exact_matches) == 1:
                    target_line = exact_matches[0]
                else:
                    return VerificationOutcome(
                        allowed=False,
                        reason=(
                            f"Multiple lines share variant '{action.variant_id}' with different "
                            f"properties/plans. Precise line key required to disambiguate (T-10, S-08)."
                        ),
                    )

        # 2. Compute effective target quantity (T-05)
        current_qty = target_line.quantity if target_line else 0

        if action.operation == QuantityOperation.INCREMENT:
            target_qty = current_qty + action.quantity
        elif action.operation == QuantityOperation.SET:
            target_qty = action.quantity
        elif action.operation == QuantityOperation.REMOVE:
            target_qty = 0
        else:
            return VerificationOutcome(
                allowed=False,
                reason=f"Unknown quantity operation '{action.operation}'",
            )

        # Handle removals
        if target_qty <= 0:
            if not target_line:
                # Removing non-existent item is a no-op / rejected
                return VerificationOutcome(
                    allowed=False,
                    reason="Cannot remove item: target line is not present in cart",
                )
            command_op = CommandOperation.REMOVE_LINE
            target_qty = 0
            resolved_variant = target_line.variant_id
            resolved_props = target_line.properties
            resolved_plan = target_line.selling_plan_id
            shopify_key = target_line.shopify_line_key
            canonical_key = target_line.canonical_key
            target_key = shopify_key or canonical_key
        elif target_line:
            # Updating existing line
            command_op = CommandOperation.SET_LINE_QUANTITY
            resolved_variant = target_line.variant_id
            resolved_props = target_line.properties
            resolved_plan = target_line.selling_plan_id
            shopify_key = target_line.shopify_line_key
            canonical_key = target_line.canonical_key
            target_key = shopify_key or canonical_key
        else:
            # Adding new variant line
            if not action.variant_id:
                return VerificationOutcome(
                    allowed=False,
                    reason="Cannot add item without variant_id",
                )
            command_op = CommandOperation.ADD_VARIANT
            resolved_variant = action.variant_id
            resolved_props = action.properties
            resolved_plan = action.selling_plan_id
            dummy_line = CartLine(
                variant_id=resolved_variant,
                quantity=target_qty,
                selling_plan_id=resolved_plan,
                properties=resolved_props,
            )
            canonical_key = dummy_line.canonical_key
            target_key = canonical_key
            shopify_key = None

        # 3. Mint AuthorizedCommand (S-01, S-09)
        cmd_id = f"cmd_{uuid.uuid4().hex}"
        parameters: dict[str, Any] = {
            "variant_id": resolved_variant,
            "quantity": target_qty,
            "properties": resolved_props,
            "selling_plan_id": resolved_plan,
            "target_line_key": target_key,
            "canonical_line_key": canonical_key,
            "shopify_line_key": shopify_key,
        }

        command = AuthorizedCommand(
            command_id=cmd_id,
            session_id=session_id,
            shop_id=current_cart.shop_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            expires_at_ms=now + ttl_ms,
            operation=command_op,
            parameters=parameters,
            expected_cart_fingerprint=current_cart.fingerprint(),
        )

        return VerificationOutcome(
            allowed=True,
            command=command,
            target_quantity=target_qty,
            target_line_key=target_key,
            reason="Action authorized with verified precondition fingerprint",
        )

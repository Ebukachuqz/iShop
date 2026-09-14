"""Deterministic Shopify Storefront boundary simulator for iShop (Drake).

Provides offline integration testing with full fidelity:
- Full cart read-back via canonical CartSnapshot.
- Line key preservation and multiple lines sharing variant ID (T-10).
- Inventory validation, shortage rejection, and userErrors (T-07, T-08).
- Fault injection: response loss, network timeout, and concurrent edits (T-13, T-14, S-09).
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ishop.domain.models import (
    AuthorizedCommand,
    CartLine,
    CartSnapshot,
    CommandOperation,
    Money,
)


class FaultMode(str, Enum):
    NONE = "none"
    LOST_RESPONSE = "lost_response"  # Mutates on server, but connection drops before response returned (T-13, T-14)
    TIMEOUT_BEFORE_COMMIT = "timeout_before_commit"  # Drops before server processes mutation (T-14)
    USER_ERRORS = "user_errors"  # HTTP 200 with GraphQL/Storefront userErrors payload (T-08)
    CONCURRENT_EDIT = "concurrent_edit"  # External edit between read and write causing stale precondition (S-09)


class SimulationNetworkError(Exception):
    """Simulated transport or connection failure."""


@dataclass
class SimulatedLine:
    line_key: str
    variant_id: str
    quantity: int
    properties: dict[str, str] = field(default_factory=dict)
    selling_plan_id: str | None = None
    price_minor: int = 2500  # Default $25.00 / 2500 NGN

    def to_cart_line(self) -> CartLine:
        return CartLine(
            variant_id=self.variant_id,
            quantity=self.quantity,
            selling_plan_id=self.selling_plan_id,
            properties=self.properties,
            shopify_line_key=self.line_key,
        )


@dataclass
class SimulationResult:
    status_code: int
    user_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cart_snapshot: CartSnapshot | None = None
    transport: str = "simulator"


class ShopifySimulator:
    """In-memory Shopify Storefront cart & inventory simulator."""

    def __init__(
        self,
        shop_id: str = "test-store.myshopify.com",
        currency: str = "USD",
        inventory: dict[str, int] | None = None,
        inventory_policies: dict[str, str] | None = None,
    ):
        self.shop_id = shop_id
        self.currency = currency
        self.inventory = inventory or {}  # variant_id -> available stock
        self.inventory_policies = inventory_policies or {}  # variant_id -> "DENY" | "CONTINUE"
        self._lines: list[SimulatedLine] = []
        self.active_fault: FaultMode = FaultMode.NONE

    def set_fault(self, mode: FaultMode):
        self.active_fault = mode

    def add_initial_line(
        self,
        variant_id: str,
        quantity: int,
        properties: dict[str, str] | None = None,
        selling_plan_id: str | None = None,
    ) -> str:
        key = f"line_{uuid.uuid4().hex[:12]}"
        self._lines.append(
            SimulatedLine(
                line_key=key,
                variant_id=variant_id,
                quantity=quantity,
                properties=properties or {},
                selling_plan_id=selling_plan_id,
            )
        )
    def set_cart(self, cart: CartSnapshot):
        """Replace simulator lines with snapshot lines."""
        self.shop_id = cart.shop_id
        self.currency = cart.currency
        self._lines = [
            SimulatedLine(
                line_key=getattr(line, "shopify_line_key", None) or f"line_{uuid.uuid4().hex[:12]}",
                variant_id=line.variant_id,
                quantity=line.quantity,
                properties=dict(line.properties),
                selling_plan_id=line.selling_plan_id,
            )
            for line in cart.lines
        ]

    def read_cart(self) -> CartSnapshot:
        cart_lines = tuple(line.to_cart_line() for line in self._lines if line.quantity > 0)
        return CartSnapshot(
            shop_id=self.shop_id,
            currency=self.currency,
            lines=cart_lines,
        )

    def execute_command(self, command: AuthorizedCommand) -> SimulationResult:
        # 1. Fault injection before mutation
        if self.active_fault == FaultMode.TIMEOUT_BEFORE_COMMIT:
            # Server drops connection before processing write (T-14)
            raise SimulationNetworkError("Connection timed out before commit reached Shopify server (T-14)")

        if self.active_fault == FaultMode.CONCURRENT_EDIT:
            # Simulate a concurrent manual shopper edit in another tab
            self.add_initial_line(
                variant_id="var_manual_shopper_addition",
                quantity=1,
                properties={"source": "shopper_tab"},
            )
            # Reset fault so next read reflects reality
            self.active_fault = FaultMode.NONE

        if self.active_fault == FaultMode.USER_ERRORS:
            # Return HTTP 200 with userErrors (T-08)
            return SimulationResult(
                status_code=200,
                user_errors=["The requested item has limited stock and cannot be added."],
                cart_snapshot=self.read_cart(),
            )

        # 2. Process command operations
        params = command.parameters
        variant_id = params.get("variant_id")
        quantity = params.get("quantity", 1)
        properties = params.get("properties", {})
        selling_plan_id = params.get("selling_plan_id")
        target_line_key = params.get("target_line_key")

        if command.operation == CommandOperation.ADD_VARIANT:
            # Inventory check (T-07)
            avail = self.inventory.get(variant_id)
            policy = self.inventory_policies.get(variant_id, "DENY")
            if avail is not None and quantity > avail and policy == "DENY":
                return SimulationResult(
                    status_code=200,
                    user_errors=[f"Inventory shortage for variant '{variant_id}': requested {quantity}, available {avail}"],
                    cart_snapshot=self.read_cart(),
                )

            # Check if matching line with SAME variant, properties, AND plan exists
            existing_line: SimulatedLine | None = None
            for line in self._lines:
                norm_props = {str(k): str(v) for k, v in sorted(properties.items())}
                line_norm = {str(k): str(v) for k, v in sorted(line.properties.items())}
                if (
                    line.variant_id == variant_id
                    and line_norm == norm_props
                    and line.selling_plan_id == selling_plan_id
                ):
                    existing_line = line
                    break

            if existing_line:
                existing_line.quantity = quantity
            else:
                # Creates distinct line preserving others sharing variant ID (T-10)
                new_key = f"line_{uuid.uuid4().hex[:12]}"
                self._lines.append(
                    SimulatedLine(
                        line_key=new_key,
                        variant_id=variant_id,
                        quantity=quantity,
                        properties=copy.deepcopy(properties),
                        selling_plan_id=selling_plan_id,
                    )
                )

        elif command.operation == CommandOperation.SET_LINE_QUANTITY:
            target: SimulatedLine | None = None
            for line in self._lines:
                cand_cl = line.to_cart_line()
                if line.line_key == target_line_key or cand_cl.canonical_key == target_line_key:
                    target = line
                    break

            if not target:
                return SimulationResult(
                    status_code=200,
                    user_errors=[f"Target line key '{target_line_key}' not found in cart."],
                    cart_snapshot=self.read_cart(),
                )

            avail = self.inventory.get(target.variant_id)
            policy = self.inventory_policies.get(target.variant_id, "DENY")
            if avail is not None and quantity > avail and policy == "DENY":
                return SimulationResult(
                    status_code=200,
                    user_errors=[f"Inventory shortage: cannot set quantity to {quantity}"],
                    cart_snapshot=self.read_cart(),
                )

            if quantity <= 0:
                self._lines = [l for l in self._lines if l is not target]
            else:
                target.quantity = quantity

        elif command.operation == CommandOperation.REMOVE_LINE:
            self._lines = [
                l for l in self._lines
                if l.line_key != target_line_key and l.to_cart_line().canonical_key != target_line_key
            ]

        elif command.operation == CommandOperation.READ_CART:
            pass  # read only

        elif command.operation == CommandOperation.CLEAR_CART:
            if params.get("explicit_whole_cart") is not True:
                return SimulationResult(status_code=400, user_errors=["Whole-cart clearing requires explicit scope"], cart_snapshot=self.read_cart())
            self._lines = []

        else:
            return SimulationResult(
                status_code=400,
                user_errors=[f"Unsupported simulation operation '{command.operation}'"],
                cart_snapshot=self.read_cart(),
            )

        # 3. Post-mutation fault injection (e.g. response lost after write committed)
        if self.active_fault == FaultMode.LOST_RESPONSE:
            # Write committed on server, but network severed before client got response (T-13, T-14)
            raise SimulationNetworkError("Connection severed after server committed mutation; response lost (T-13, T-14)")

        return SimulationResult(
            status_code=200,
            user_errors=[],
            warnings=[],
            cart_snapshot=self.read_cart(),
        )

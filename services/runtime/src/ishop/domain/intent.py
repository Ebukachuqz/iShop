"""Domain data models for shopping intent extraction.

Conforms strictly to packages/contracts/schema/shopping_intent.json.
Maintains domain purity with zero external provider SDK or network dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

INTENT_SCHEMA_VERSION = "1.0.0"


class IntentOperation(str, Enum):
    """Permitted shopping intent operations."""

    SEARCH = "search"
    BROWSE = "browse"
    DESCRIBE_PRODUCT = "describe_product"
    CHECK_AVAILABILITY = "check_availability"
    VIEW_CART = "view_cart"
    ADD_TO_CART = "add_to_cart"
    UPDATE_QUANTITY = "update_quantity"
    REMOVE_FROM_CART = "remove_from_cart"
    NAVIGATE = "navigate"
    REQUEST_CHECKOUT = "request_checkout"


@dataclass(frozen=True)
class QuantityChange:
    """Quantity change specification with set or increment semantics (T-05)."""

    mode: str  # "set" or "increment"
    value: int

    def __post_init__(self):
        if self.mode not in ("set", "increment"):
            raise ValueError(f"Invalid quantity mode '{self.mode}'; expected 'set' or 'increment'")
        if self.mode == "increment" and self.value <= 0:
            raise ValueError(f"Increment quantity must be positive, got {self.value}")
        if self.mode == "set" and self.value < 0:
            raise ValueError(f"Set quantity cannot be negative, got {self.value}")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "value": self.value}


@dataclass(frozen=True)
class BudgetConstraint:
    """Shopper budget limit constraint."""

    max_amount: str
    currency: str

    def __post_init__(self):
        curr = self.currency.upper()
        object.__setattr__(self, "currency", curr)
        if not curr.isalpha() or len(curr) != 3:
            raise ValueError(f"Invalid ISO currency code: {self.currency}")

    def to_dict(self) -> dict[str, str]:
        return {"max_amount": self.max_amount, "currency": self.currency}


@dataclass(frozen=True)
class ShoppingIntent:
    """Structured intent extracted from shopper speech or dialogue turns."""

    intent_id: str
    operation: IntentOperation
    is_explicit_checkout_request: bool
    supporting_transcript_span: str
    unresolved_fields: tuple[str, ...] = ()
    product_query: str | None = None
    selected_variant_attributes: dict[str, str] = field(default_factory=dict)
    quantity_change: QuantityChange | None = None
    target_line_key: str | None = None
    budget_constraint: BudgetConstraint | None = None
    original_language_wording: str | None = None
    schema_version: str = INTENT_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != INTENT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema version '{self.schema_version}'")
        if not self.intent_id:
            raise ValueError("intent_id cannot be empty")
        norm_attrs = {str(k).lower(): str(v).lower() for k, v in sorted(self.selected_variant_attributes.items())}
        object.__setattr__(self, "selected_variant_attributes", norm_attrs)
        object.__setattr__(self, "unresolved_fields", tuple(self.unresolved_fields))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "operation": self.operation.value,
            "product_query": self.product_query,
            "selected_variant_attributes": dict(self.selected_variant_attributes),
            "quantity_change": self.quantity_change.to_dict() if self.quantity_change else None,
            "target_line_key": self.target_line_key,
            "budget_constraint": self.budget_constraint.to_dict() if self.budget_constraint else None,
            "is_explicit_checkout_request": self.is_explicit_checkout_request,
            "supporting_transcript_span": self.supporting_transcript_span,
            "original_language_wording": self.original_language_wording,
            "unresolved_fields": list(self.unresolved_fields),
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShoppingIntent:
        qty_dict = data.get("quantity_change")
        qty = (
            QuantityChange(mode=qty_dict["mode"], value=int(qty_dict["value"]))
            if qty_dict
            else None
        )

        budget_dict = data.get("budget_constraint")
        budget = (
            BudgetConstraint(
                max_amount=str(budget_dict["max_amount"]),
                currency=str(budget_dict["currency"]),
            )
            if budget_dict
            else None
        )

        op_raw = data.get("operation")
        try:
            op = IntentOperation(op_raw)
        except ValueError:
            raise ValueError(f"Unknown intent operation '{op_raw}'")

        return cls(
            schema_version=data.get("schema_version", INTENT_SCHEMA_VERSION),
            intent_id=str(data["intent_id"]),
            operation=op,
            product_query=data.get("product_query"),
            selected_variant_attributes=data.get("selected_variant_attributes") or {},
            quantity_change=qty,
            target_line_key=data.get("target_line_key"),
            budget_constraint=budget,
            is_explicit_checkout_request=bool(data.get("is_explicit_checkout_request", False)),
            supporting_transcript_span=str(data.get("supporting_transcript_span", "")),
            original_language_wording=data.get("original_language_wording"),
            unresolved_fields=tuple(data.get("unresolved_fields") or ()),
        )

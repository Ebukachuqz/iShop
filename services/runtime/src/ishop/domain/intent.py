"""Domain data models for shopping intent extraction.

Conforms strictly to packages/contracts/schema/shopping_intent.json.
Maintains domain purity with zero external provider SDK or network dependencies.
"""

from __future__ import annotations

import re
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
    CANCEL_CART = "cancel_cart"
    MANAGE_ORDERS = "manage_orders"
    STORE_INFORMATION = "store_information"
    SHOW_VARIANT = "show_variant"


class ReferenceKind(str, Enum):
    """Kinds of grounded contextual shopping references."""

    SEARCH_QUERY = "search_query"
    OBSERVED_PRODUCT = "observed_product"
    RESULT_POSITION = "result_position"
    CURRENT_PAGE = "current_page"
    CART_LINE = "cart_line"
    COLLECTION = "collection"
    COMPARISON_SELECTION = "comparison_selection"


@dataclass(frozen=True)
class TargetReference:
    """Explicitly tagged shopping target reference."""

    kind: ReferenceKind
    value: str
    result_set_id: str | None = None
    position: int | None = None
    handle: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "value": self.value,
            "result_set_id": self.result_set_id,
            "position": self.position,
            "handle": self.handle,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TargetReference:
        return cls(
            kind=ReferenceKind(data["kind"]),
            value=str(data["value"]),
            result_set_id=data.get("result_set_id"),
            position=data.get("position"),
            handle=data.get("handle"),
            url=data.get("url"),
        )


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
    """Shopper price relationship, kept separate from catalog evidence."""

    max_amount: str | None
    currency: str
    scope: str = "total"
    min_amount: str | None = None
    comparison: str = "max"

    def __post_init__(self):
        if self.scope not in ("total", "per_item", "unknown"):
            raise ValueError("Invalid budget scope")
        if self.comparison not in ("max", "min", "range", "approximate", "exact"):
            raise ValueError("Invalid budget comparison")
        if self.max_amount is None and self.min_amount is None:
            raise ValueError("Budget requires a minimum or maximum amount")
        curr = self.currency.upper()
        object.__setattr__(self, "currency", curr)
        if not curr.isalpha() or len(curr) != 3:
            raise ValueError(f"Invalid ISO currency code: {self.currency}")

    def to_dict(self) -> dict[str, str]:
        return {
            "max_amount": self.max_amount,
            "min_amount": self.min_amount,
            "currency": self.currency,
            "scope": self.scope,
            "comparison": self.comparison,
        }


def validate_shopping_intent_payload(data: Any) -> list[str]:
    """Strictly validates a raw dict payload against packages/contracts/schema/shopping_intent.json."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["ShoppingIntent payload must be a JSON object"]

    allowed_properties = {
        "schema_version",
        "intent_id",
        "operation",
        "product_query",
        "selected_variant_attributes",
        "quantity_change",
        "target_line_key",
        "budget_constraint",
        "is_explicit_checkout_request",
        "supporting_transcript_span",
        "original_language_wording",
        "target_reference",
        "unresolved_fields",
    }
    unexpected_keys = set(data.keys()) - allowed_properties
    if unexpected_keys:
        errors.append(f"Unexpected additional properties: {sorted(unexpected_keys)}")

    required_properties = [
        "schema_version",
        "intent_id",
        "operation",
        "is_explicit_checkout_request",
        "supporting_transcript_span",
        "unresolved_fields",
    ]
    for req in required_properties:
        if req not in data:
            errors.append(f"Missing required property: '{req}'")

    if "schema_version" in data:
        if not isinstance(data["schema_version"], str) or data["schema_version"] != INTENT_SCHEMA_VERSION:
            errors.append(f"Invalid schema_version '{data.get('schema_version')}'; expected '{INTENT_SCHEMA_VERSION}'")

    if "intent_id" in data:
        if not isinstance(data["intent_id"], str) or not data["intent_id"].strip():
            errors.append("intent_id must be a non-empty string")

    if "operation" in data:
        valid_ops = {op.value for op in IntentOperation}
        if data["operation"] not in valid_ops:
            errors.append(f"Invalid operation '{data.get('operation')}'; must be one of {sorted(valid_ops)}")

    if "is_explicit_checkout_request" in data:
        if not isinstance(data["is_explicit_checkout_request"], bool):
            errors.append(f"is_explicit_checkout_request must be a boolean, got {type(data['is_explicit_checkout_request']).__name__}")

    if "supporting_transcript_span" in data:
        if not isinstance(data["supporting_transcript_span"], str):
            errors.append(f"supporting_transcript_span must be a string, got {type(data['supporting_transcript_span']).__name__}")

    if "unresolved_fields" in data:
        if not isinstance(data["unresolved_fields"], (list, tuple)):
            errors.append(f"unresolved_fields must be an array, got {type(data['unresolved_fields']).__name__}")
        else:
            for item in data["unresolved_fields"]:
                if not isinstance(item, str):
                    errors.append(f"unresolved_fields items must be strings, got {type(item).__name__}")
                    break

    if "product_query" in data and data["product_query"] is not None:
        if not isinstance(data["product_query"], str):
            errors.append(f"product_query must be a string or null, got {type(data['product_query']).__name__}")

    if "target_line_key" in data and data["target_line_key"] is not None:
        if not isinstance(data["target_line_key"], str):
            errors.append(f"target_line_key must be a string or null, got {type(data['target_line_key']).__name__}")

    if "original_language_wording" in data and data["original_language_wording"] is not None:
        if not isinstance(data["original_language_wording"], str):
            errors.append(f"original_language_wording must be a string or null, got {type(data['original_language_wording']).__name__}")

    if "selected_variant_attributes" in data and data["selected_variant_attributes"] is not None:
        if not isinstance(data["selected_variant_attributes"], dict):
            errors.append(f"selected_variant_attributes must be an object, got {type(data['selected_variant_attributes']).__name__}")
        else:
            for k, v in data["selected_variant_attributes"].items():
                if not isinstance(k, str) or not isinstance(v, str):
                    errors.append("selected_variant_attributes keys and values must be strings")
                    break

    if "quantity_change" in data and data["quantity_change"] is not None:
        qc = data["quantity_change"]
        if not isinstance(qc, dict):
            errors.append(f"quantity_change must be an object or null, got {type(qc).__name__}")
        else:
            qc_unexpected = set(qc.keys()) - {"mode", "value"}
            if qc_unexpected:
                errors.append(f"quantity_change has unexpected additional properties: {sorted(qc_unexpected)}")
            if "mode" not in qc:
                errors.append("quantity_change missing required property 'mode'")
            elif qc["mode"] not in ("set", "increment"):
                errors.append(f"quantity_change mode must be 'set' or 'increment', got '{qc['mode']}'")
            if "value" not in qc:
                errors.append("quantity_change missing required property 'value'")
            elif not isinstance(qc["value"], int) or isinstance(qc["value"], bool):
                errors.append(f"quantity_change value must be an integer, got {type(qc['value']).__name__}")

    if "budget_constraint" in data and data["budget_constraint"] is not None:
        bc = data["budget_constraint"]
        if not isinstance(bc, dict):
            errors.append(f"budget_constraint must be an object or null, got {type(bc).__name__}")
        else:
            bc_unexpected = set(bc.keys()) - {"max_amount", "min_amount", "currency", "scope", "comparison"}
            if bc_unexpected:
                errors.append(f"budget_constraint has unexpected additional properties: {sorted(bc_unexpected)}")
            if bc.get("scope", "total") not in ("total", "per_item", "unknown"):
                errors.append("budget_constraint scope must be total, per_item or unknown")
            if bc.get("max_amount") is not None and not isinstance(bc["max_amount"], str):
                errors.append(f"budget_constraint max_amount must be a string, got {type(bc['max_amount']).__name__}")
            if bc.get("min_amount") is not None and not isinstance(bc["min_amount"], str):
                errors.append(f"budget_constraint min_amount must be a string, got {type(bc['min_amount']).__name__}")
            if bc.get("max_amount") is None and bc.get("min_amount") is None:
                errors.append("budget_constraint requires max_amount or min_amount")
            if bc.get("comparison", "max") not in ("max", "min", "range", "approximate", "exact"):
                errors.append("budget_constraint comparison is invalid")
            if "currency" not in bc:
                errors.append("budget_constraint missing required property 'currency'")
            elif not isinstance(bc["currency"], str) or not re.match(r"^[A-Z]{3}$", bc["currency"]):
                errors.append(f"budget_constraint currency must be 3 uppercase letters, got '{bc.get('currency')}'")

    if "target_reference" in data and data["target_reference"] is not None:
        tr = data["target_reference"]
        if not isinstance(tr, dict):
            errors.append(f"target_reference must be an object, got {type(tr).__name__}")
        else:
            if "kind" not in tr or not isinstance(tr["kind"], str):
                errors.append("target_reference missing valid 'kind'")
            if "value" not in tr or not isinstance(tr["value"], str):
                errors.append("target_reference missing valid 'value'")

    return errors


class DecisionMode(str, Enum):
    """Modes of conversation-first turn decision."""

    RESPOND = "respond"
    CLARIFY = "clarify"
    ACT = "act"


class ResponsePurpose(str, Enum):
    """Purposes for conversational respond turns."""

    GREETING = "greeting"
    CAPABILITY_HELP = "capability_help"
    GENERAL_ASSISTANCE = "general_assistance"
    REFUSAL = "refusal"
    STORE_INFO = "store_info"
    INFORMATIONAL = "informational"


@dataclass(frozen=True)
class TurnDecision:
    """Conversation-first top-level decision."""

    mode: DecisionMode
    response_text: str | None = None
    response_purpose: ResponsePurpose | None = None
    clarification_question: str | None = None
    clarification_options: tuple[str, ...] = ()
    clarification_fields: tuple[str, ...] = ()
    intent: ShoppingIntent | None = None
    target_reference: TargetReference | None = None
    grounded_facts: tuple[dict[str, Any], ...] = ()

    def __post_init__(self):
        if not isinstance(self.mode, DecisionMode):
            if isinstance(self.mode, str):
                try:
                    object.__setattr__(self, "mode", DecisionMode(self.mode))
                except ValueError:
                    raise ValueError(f"Invalid decision mode: {self.mode}")
            else:
                raise ValueError(f"Decision mode must be a DecisionMode enum, got {type(self.mode)}")
        if self.mode == DecisionMode.ACT and self.intent is None:
            raise ValueError("Act decisions require a validated shopping intent")
        if self.mode == DecisionMode.RESPOND and not (self.response_text and self.response_text.strip()):
            raise ValueError("Respond decisions require response text")
        if self.mode == DecisionMode.CLARIFY and not (
            (self.clarification_question and self.clarification_question.strip())
            or (self.response_text and self.response_text.strip())
        ):
            raise ValueError("Clarify decisions require a clarification question")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "response_text": self.response_text,
            "response_purpose": self.response_purpose.value if self.response_purpose else None,
            "clarification_question": self.clarification_question,
            "clarification_options": list(self.clarification_options),
            "clarification_fields": list(self.clarification_fields),
            "intent": self.intent.to_dict() if self.intent else None,
            "target_reference": self.target_reference.to_dict() if self.target_reference else None,
            "grounded_facts": list(self.grounded_facts),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TurnDecision:
        intent_raw = data.get("intent")
        target_raw = data.get("target_reference")
        purpose_raw = data.get("response_purpose")
        return cls(
            mode=DecisionMode(data["mode"]),
            response_text=data.get("response_text"),
            response_purpose=ResponsePurpose(purpose_raw) if purpose_raw else None,
            clarification_question=data.get("clarification_question"),
            clarification_options=tuple(str(x) for x in data.get("clarification_options", [])),
            clarification_fields=tuple(str(x) for x in data.get("clarification_fields", [])),
            intent=ShoppingIntent.from_dict(intent_raw) if intent_raw else None,
            target_reference=TargetReference.from_dict(target_raw) if target_raw else None,
            grounded_facts=tuple(dict(x) for x in data.get("grounded_facts", []) if isinstance(x, dict)),
        )


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
    target_reference: TargetReference | None = None
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
            "target_reference": self.target_reference.to_dict() if self.target_reference else None,
            "unresolved_fields": list(self.unresolved_fields),
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShoppingIntent:
        errors = validate_shopping_intent_payload(data)
        if errors:
            raise ValueError(f"ShoppingIntent schema validation failed: {'; '.join(errors)}")

        qty_dict = data.get("quantity_change")
        qty = (
            QuantityChange(mode=qty_dict["mode"], value=qty_dict["value"])
            if qty_dict
            else None
        )

        budget_dict = data.get("budget_constraint")
        budget = (
            BudgetConstraint(
                max_amount=str(budget_dict["max_amount"]) if budget_dict.get("max_amount") is not None else None,
                currency=str(budget_dict["currency"]),
                scope=budget_dict.get("scope", "total"),
                min_amount=str(budget_dict["min_amount"]) if budget_dict.get("min_amount") is not None else None,
                comparison=budget_dict.get("comparison", "max"),
            )
            if budget_dict
            else None
        )

        op = IntentOperation(data["operation"])
        target_ref = (
            TargetReference.from_dict(data["target_reference"])
            if data.get("target_reference")
            else None
        )

        return cls(
            schema_version=data["schema_version"],
            intent_id=str(data["intent_id"]),
            operation=op,
            product_query=data.get("product_query"),
            selected_variant_attributes=data.get("selected_variant_attributes") or {},
            quantity_change=qty,
            target_line_key=data.get("target_line_key"),
            budget_constraint=budget,
            is_explicit_checkout_request=data["is_explicit_checkout_request"],
            supporting_transcript_span=str(data["supporting_transcript_span"]),
            original_language_wording=data.get("original_language_wording"),
            target_reference=target_ref,
            unresolved_fields=tuple(data["unresolved_fields"]),
        )

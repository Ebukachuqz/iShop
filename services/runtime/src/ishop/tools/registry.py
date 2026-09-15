"""Qualified shopping-tool catalog and strict proposal validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


STRING = {"type": "string", "minLength": 1, "maxLength": 500}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 20}


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]
    mutating: bool = False
    navigation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": self.description,
            "input_schema": self.input_schema,
            "side_effect": "mutation" if self.mutating else ("navigation" if self.navigation else "read"),
        }


@dataclass(frozen=True)
class ToolProposal:
    name: str
    arguments: dict[str, Any]
    rationale: str = ""


TOOL_DESCRIPTORS: tuple[ToolDescriptor, ...] = (
    ToolDescriptor("search_catalog", "Show matching products in the native storefront search page, then observe its displayed order.", _object_schema({
        "query": STRING, "resource_types": {"type": "array", "items": {"enum": ["product", "collection", "article", "page"]}, "maxItems": 4},
        "limit": LIMIT, "cursor": STRING, "sort_by": {"enum": ["relevance", "price-ascending", "price-descending"]},
        "min_price": STRING, "max_price": STRING,
    }), navigation=True),
    ToolDescriptor("browse_store", "List collections or browse products in one grounded collection.", _object_schema({
        "mode": {"enum": ["list_collections", "collection_products"]}, "collection_reference": STRING,
        "limit": LIMIT, "cursor": STRING, "navigate": {"type": "boolean"},
    }, ("mode",))),
    ToolDescriptor("get_product", "Read verified product, variant, price, option, and availability details.", _object_schema({
        "product_reference": STRING, "navigate": {"type": "boolean"},
    }, ("product_reference",))),
    ToolDescriptor("show_variant", "Open a grounded product with a verified variant or option selection.", _object_schema({
        "product_reference": STRING, "variant_reference": STRING,
        "options": {"type": "object", "additionalProperties": {"type": "string"}, "maxProperties": 8},
    }, ("product_reference",)), navigation=True),
    ToolDescriptor("get_cart", "Read the authoritative browser cart; the runtime supplies its binding.", _object_schema({})),
    ToolDescriptor("update_cart", "Add a grounded variant, set quantity, or remove a grounded cart line.", _object_schema({
        "operation": {"enum": ["add", "set_quantity", "remove"]}, "product_reference": STRING,
        "variant_reference": STRING, "line_reference": STRING,
        "quantity": {"type": "integer", "minimum": 0, "maximum": 100},
        "options": {"type": "object", "additionalProperties": {"type": "string"}, "maxProperties": 8},
    }, ("operation",)), mutating=True),
    ToolDescriptor("cancel_cart", "Clear the whole cart only for an explicit whole-cart objective.", _object_schema({
        "explicit_whole_cart": {"const": True},
    }, ("explicit_whole_cart",)), mutating=True),
    ToolDescriptor("proceed_to_checkout", "Navigate to trusted checkout after a fresh nonempty-cart check.", _object_schema({}), navigation=True),
    ToolDescriptor("manage_orders", "Navigate to the store's trusted Shopify account/order-history experience.", _object_schema({}), navigation=True),
    ToolDescriptor("search_shop_policies_and_faqs", "Search current store policy and FAQ content and return attributable evidence.", _object_schema({
        "query": STRING, "categories": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 6}, "limit": LIMIT,
    }, ("query",))),
)


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> None:
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is outside the allowed values")
    expected = schema.get("type")
    if expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000):
            raise ValueError(f"{path} has invalid length")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{path} must be an integer")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ValueError(f"{path} is outside the allowed range")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    elif expected == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        if len(value) > schema.get("maxItems", len(value)):
            raise ValueError(f"{path} has too many items")
        for index, item in enumerate(value):
            _validate_schema_value(item, schema.get("items", {}), f"{path}[{index}]")
    elif expected == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        if len(value) > schema.get("maxProperties", len(value)):
            raise ValueError(f"{path} has too many properties")
        additional = schema.get("additionalProperties", True)
        properties = schema.get("properties", {})
        unknown = set(value) - set(properties)
        if additional is False and unknown:
            raise ValueError(f"{path} contains unknown fields: {', '.join(sorted(unknown))}")
        if isinstance(additional, dict):
            for key in unknown:
                _validate_schema_value(value[key], additional, f"{path}.{key}")
        for key, child in properties.items():
            if key in value:
                _validate_schema_value(value[key], child, f"{path}.{key}")
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise ValueError(f"{path} is missing: {', '.join(sorted(missing))}")


class ToolRegistry:
    def __init__(self, descriptors: tuple[ToolDescriptor, ...] = TOOL_DESCRIPTORS):
        self._descriptors = {d.name: d for d in descriptors}

    def qualified(self, *, available: set[str] | None = None, read_only: bool = False) -> list[ToolDescriptor]:
        return [d for d in self._descriptors.values() if (available is None or d.name in available) and not (read_only and d.mutating)]

    def validate(self, proposal: ToolProposal, *, available: set[str] | None = None) -> ToolDescriptor:
        descriptor = self._descriptors.get(proposal.name)
        if descriptor is None:
            raise ValueError(f"Unknown shopping tool: {proposal.name}")
        if available is not None and proposal.name not in available:
            raise ValueError(f"Shopping tool is unavailable: {proposal.name}")
        if not isinstance(proposal.arguments, dict):
            raise ValueError("Tool arguments must be an object")
        _validate_schema_value(proposal.arguments, descriptor.input_schema, f"arguments for {proposal.name}")
        if proposal.name == "show_variant" and not (proposal.arguments.get("variant_reference") or proposal.arguments.get("options")):
            raise ValueError("show_variant requires a variant reference or option selection")
        return descriptor

    def hydrate_and_validate(
        self,
        proposal: ToolProposal,
        trusted_arguments: dict[str, Any],
        *,
        available: set[str] | None = None,
    ) -> ToolProposal:
        """Hydrate a model proposal from validated intent without trusting invented bindings.

        The model still chooses a qualified tool and may propose optional fields. The
        application rejects unknown fields, then overlays arguments derived from the
        validated intent. This keeps missing model fields from breaking a safe action
        while ensuring the model cannot override operation, quantity, or references.
        """
        descriptor = self._descriptors.get(proposal.name)
        if descriptor is None:
            raise ValueError(f"Unknown shopping tool: {proposal.name}")
        if available is not None and proposal.name not in available:
            raise ValueError(f"Shopping tool is unavailable: {proposal.name}")
        if not isinstance(proposal.arguments, dict):
            raise ValueError("Tool arguments must be an object")
        allowed_fields = set(descriptor.input_schema.get("properties", {}))
        unknown = set(proposal.arguments) - allowed_fields
        if unknown:
            raise ValueError(f"arguments for {proposal.name} contains unknown fields: {', '.join(sorted(unknown))}")
        hydrated = {**proposal.arguments, **trusted_arguments}
        normalized = ToolProposal(proposal.name, hydrated, proposal.rationale)
        self.validate(normalized, available=available)
        return normalized

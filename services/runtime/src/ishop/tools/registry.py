"""Qualified Shopify tool catalog and proposal validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str
    required_parameters: tuple[str, ...] = ()
    mutating: bool = False
    navigation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": self.description,
            "input_schema": {
                "type": "object",
                "required": list(self.required_parameters),
                "additionalProperties": True,
            },
            "side_effect": "mutation" if self.mutating else ("navigation" if self.navigation else "read"),
        }


@dataclass(frozen=True)
class ToolProposal:
    name: str
    arguments: dict[str, Any]
    rationale: str = ""


TOOL_DESCRIPTORS: tuple[ToolDescriptor, ...] = (
    ToolDescriptor("search_catalog", "Search products and store content; a constraint-only search may omit free text."),
    ToolDescriptor("browse_store", "Browse collections or a collection's products."),
    ToolDescriptor("get_product", "Read verified product and variant details from a resolved product reference."),
    ToolDescriptor("show_variant", "Open a product with a verified variant selected."),
    ToolDescriptor("get_cart", "Read the authoritative cart."),
    ToolDescriptor("update_cart", "Add, change, or remove a resolved cart line or variant.", ("operation",), True),
    ToolDescriptor("cancel_cart", "Clear the entire cart only when explicitly requested.", (), True),
    ToolDescriptor("proceed_to_checkout", "Navigate to checkout after verifying a non-empty cart.", (), False, True),
    ToolDescriptor("manage_orders", "Navigate to trusted order history or login.", (), False, True),
    ToolDescriptor("search_shop_policies_and_faqs", "Search the store's policies and FAQs.", ("query",)),
)


class ToolRegistry:
    def __init__(self, descriptors: tuple[ToolDescriptor, ...] = TOOL_DESCRIPTORS):
        self._descriptors = {d.name: d for d in descriptors}

    def qualified(self, *, available: set[str] | None = None, read_only: bool = False) -> list[ToolDescriptor]:
        result = []
        for descriptor in self._descriptors.values():
            if available is not None and descriptor.name not in available:
                continue
            if read_only and descriptor.mutating:
                continue
            result.append(descriptor)
        return result

    def validate(self, proposal: ToolProposal, *, available: set[str] | None = None) -> ToolDescriptor:
        descriptor = self._descriptors.get(proposal.name)
        if descriptor is None:
            raise ValueError(f"Unknown shopping tool: {proposal.name}")
        if available is not None and proposal.name not in available:
            raise ValueError(f"Shopping tool is unavailable: {proposal.name}")
        missing = [key for key in descriptor.required_parameters if key not in proposal.arguments]
        if missing:
            raise ValueError(f"Missing tool arguments for {proposal.name}: {', '.join(missing)}")
        return descriptor

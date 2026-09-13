"""Provider-neutral Shopify tool wrapper contracts.

Adapters remain the only layer allowed to translate these calls to WebMCP,
Standard Actions, or Ajax. The wrappers validate intent and return typed
proposals; they never perform a cart mutation themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ishop.tools.registry import ToolProposal, ToolRegistry


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    data: dict[str, Any]
    error: str | None = None
    observation_id: str | None = None


class ShopifyToolExecutor:
    def __init__(self, handlers: dict[str, Callable[[dict[str, Any]], ToolResult]] | None = None):
        self.registry = ToolRegistry()
        self.handlers = handlers or {}

    def execute(self, proposal: ToolProposal, *, available: set[str] | None = None) -> ToolResult:
        descriptor = self.registry.validate(proposal, available=available)
        handler = self.handlers.get(descriptor.name)
        if handler is None:
            return ToolResult(descriptor.name, False, {}, "Tool adapter is unavailable")
        result = handler(dict(proposal.arguments))
        if result.tool != descriptor.name:
            raise ValueError("Tool adapter returned a mismatched tool name")
        return result


def tool_result(name: str, data: dict[str, Any], observation_id: str) -> ToolResult:
    return ToolResult(name, True, data, observation_id=observation_id)


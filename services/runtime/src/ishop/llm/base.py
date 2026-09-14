"""Base interfaces, data structures, and registry for provider-neutral LLMs.

Adheres to:
- S-01, S-02: No provider tool calls directly execute payments or scripts.
- T-28: Structured output validation, explicit unknowns, bounded retry.
- T-30: Optional missing key does not break other providers or crash startup.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ishop.domain.intent import IntentOperation, ShoppingIntent


@dataclass(frozen=True)
class LlmProfile:
    """Configuration and capability metadata for an LLM profile."""

    profile_id: str
    provider_name: str
    model_name: str
    enabled: bool = True
    disabled_reason: str | None = None
    is_free_tier: bool = False
    supports_streaming: bool = False
    supports_structured_output: bool = True
    max_context_tokens: int = 8192

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "is_free_tier": self.is_free_tier,
            "supports_streaming": self.supports_streaming,
            "supports_structured_output": self.supports_structured_output,
            "max_context_tokens": self.max_context_tokens,
        }


@dataclass(frozen=True)
class LlmUsage:
    """Token usage and latency metrics for an LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0


class LlmProviderError(Exception):
    """Provider-neutral exception for LLM failures."""

    def __init__(
        self,
        message: str,
        profile_id: str,
        status_code: int | None = None,
        retryable: bool = False,
        cause: Exception | None = None,
    ):
        super().__init__(message)
        self.profile_id = profile_id
        self.status_code = status_code
        self.retryable = retryable
        self.cause = cause


@dataclass(frozen=True)
class LlmIntentRequest:
    """Request payload for extracting structured intent from shopper turns."""

    transcript: str
    conversation_history: tuple[dict[str, str], ...] = ()
    catalog_context: tuple[str, ...] = ()
    cart_summary: str | None = None
    current_product_id: str | None = None
    budget_currency: str = "NGN"
    turn_id: str = ""
    request_revision: int = 1


@dataclass(frozen=True)
class LlmInterpretationResult:
    """Validated interpretation output from an LLM provider."""

    intent: ShoppingIntent
    raw_response_text: str
    usage: LlmUsage
    profile_id: str
    grounded_response_text: str | None = None


@dataclass(frozen=True)
class GroundedResponseContext:
    """Context for generating grounded customer-facing answers."""

    operation: str
    requested_intent: ShoppingIntent
    evidence_summary: str
    execution_receipt_summary: str | None = None
    cart_summary: str | None = None
    error_reason: str | None = None


@dataclass(frozen=True)
class LlmToolSelectionRequest:
    intent: ShoppingIntent
    qualified_tools: tuple[dict[str, Any], ...]
    resolved_context: Mapping[str, Any] = field(default_factory=dict)
    turn_id: str = ""
    request_revision: int = 1


@dataclass(frozen=True)
class LlmToolSelectionResult:
    tool_name: str
    arguments: dict[str, Any]
    rationale: str
    raw_response_text: str


def tool_arguments_for_intent(intent: ShoppingIntent, tool_name: str) -> dict[str, Any]:
    """Build provider-neutral proposal arguments without minting store identifiers."""
    query = (intent.product_query or intent.supporting_transcript_span or "").strip()
    if tool_name == "search_catalog":
        return {"query": query, "resource_types": ["product"], "limit": 8}
    if tool_name == "browse_store":
        return ({"mode": "collection_products", "collection_reference": query, "limit": 8}
                if intent.product_query else {"mode": "list_collections", "limit": 8})
    if tool_name == "get_product":
        return {"product_reference": query}
    if tool_name == "show_variant":
        arguments: dict[str, Any] = {"product_reference": query}
        if intent.selected_variant_attributes:
            arguments["options"] = dict(intent.selected_variant_attributes)
        return arguments
    if tool_name == "get_cart" or tool_name in {"proceed_to_checkout", "manage_orders"}:
        return {}
    if tool_name == "update_cart":
        operation = {
            IntentOperation.ADD_TO_CART: "add",
            IntentOperation.UPDATE_QUANTITY: "set_quantity",
            IntentOperation.REMOVE_FROM_CART: "remove",
        }[intent.operation]
        arguments = {"operation": operation}
        if query:
            arguments["product_reference"] = query
        if intent.selected_variant_attributes:
            arguments["options"] = dict(intent.selected_variant_attributes)
        if intent.quantity_change is not None:
            arguments["quantity"] = intent.quantity_change.value
        return arguments
    if tool_name == "cancel_cart":
        return {"explicit_whole_cart": True}
    if tool_name == "search_shop_policies_and_faqs":
        return {"query": query, "limit": 5}
    return {}


class LlmProvider(abc.ABC):
    """Provider-neutral interface for LLM shopping reasoning."""

    @property
    @abc.abstractmethod
    def profile(self) -> LlmProfile:
        """Capability and configuration metadata for this provider."""
        ...

    @abc.abstractmethod
    async def interpret_intent(self, request: LlmIntentRequest) -> LlmInterpretationResult:
        """Extract structured intent with explicit unknowns from shopper speech."""
        ...

    @abc.abstractmethod
    async def generate_grounded_response(self, context: GroundedResponseContext) -> str:
        """Generate response prose grounded strictly in verified evidence or receipts."""
        ...

    async def select_tool(self, request: LlmToolSelectionRequest) -> LlmToolSelectionResult:
        """Compatibility selector for test providers; production adapters override this with a model call."""
        from ishop.domain.intent import IntentOperation
        mapping = {
            IntentOperation.SEARCH: "search_catalog", IntentOperation.BROWSE: "browse_store",
            IntentOperation.DESCRIBE_PRODUCT: "get_product", IntentOperation.CHECK_AVAILABILITY: "get_product",
            IntentOperation.VIEW_CART: "get_cart", IntentOperation.ADD_TO_CART: "update_cart",
            IntentOperation.UPDATE_QUANTITY: "update_cart", IntentOperation.REMOVE_FROM_CART: "update_cart",
            IntentOperation.NAVIGATE: "get_product", IntentOperation.REQUEST_CHECKOUT: "proceed_to_checkout",
            IntentOperation.CANCEL_CART: "cancel_cart", IntentOperation.MANAGE_ORDERS: "manage_orders",
            IntentOperation.STORE_INFORMATION: "search_shop_policies_and_faqs", IntentOperation.SHOW_VARIANT: "show_variant",
        }
        name = mapping[request.intent.operation]
        if request.intent.operation == IntentOperation.BROWSE and request.intent.product_query:
            name = "search_catalog"
        arguments = tool_arguments_for_intent(request.intent, name)
        return LlmToolSelectionResult(name, arguments, "Provider compatibility selection", "")

    @abc.abstractmethod
    def check_readiness(self) -> tuple[bool, str | None]:
        """Check if provider credentials and network prerequisites are ready."""
        ...


class LlmRegistry:
    """Registry of configured LLM providers.
    
    Preserves T-30: An unavailable or disabled profile is recorded with reason,
    without crashing global startup.
    """

    def __init__(self):
        self._providers: dict[str, LlmProvider] = {}
        self._active_profile_id: str | None = None

    def register(self, provider: LlmProvider, set_active: bool = False) -> None:
        profile_id = provider.profile.profile_id
        self._providers[profile_id] = provider
        if set_active or self._active_profile_id is None:
            if provider.profile.enabled:
                self._active_profile_id = profile_id

    def set_active_profile(self, profile_id: str) -> None:
        if profile_id not in self._providers:
            raise KeyError(f"LLM profile '{profile_id}' not found in registry")
        provider = self._providers[profile_id]
        if not provider.profile.enabled:
            raise ValueError(
                f"Cannot activate disabled LLM profile '{profile_id}': "
                f"{provider.profile.disabled_reason}"
            )
        self._active_profile_id = profile_id

    def get(self, profile_id: str) -> LlmProvider | None:
        return self._providers.get(profile_id)

    def get_active(self) -> LlmProvider:
        if not self._active_profile_id or self._active_profile_id not in self._providers:
            # Fall back to first enabled provider
            for p in self._providers.values():
                if p.profile.enabled:
                    return p
            raise RuntimeError("No enabled LLM provider profiles available in registry")
        return self._providers[self._active_profile_id]

    def list_profiles(self) -> list[LlmProfile]:
        return [p.profile for p in self._providers.values()]

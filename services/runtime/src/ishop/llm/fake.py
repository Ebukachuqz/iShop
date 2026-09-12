"""Deterministic offline Fake LLM provider for testing and offline development.

Zero external dependencies or API keys.
Accurately models:
- T-05: Quantity semantics (increment, set, zero removal).
- T-06: Negation scope ('not blue'), self-correction ('medium wait no large'), budget constraints.
- T-09: Prompt injection resistance against malicious catalog/user instructions.
- T-28: Schema validation and structured outputs with explicit unknowns.
- T-30: Provider independence and offline test execution.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from ishop.domain.intent import (
    BudgetConstraint,
    IntentOperation,
    QuantityChange,
    ShoppingIntent,
)
from ishop.llm.base import (
    GroundedResponseContext,
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProfile,
    LlmProvider,
    LlmProviderError,
    LlmUsage,
)

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous\s+)?instructions", re.I),
    re.compile(r"leak\s+(?:system\s+)?(?:secret|prompt|key|token)", re.I),
    re.compile(r"(?:charge|submit|swipe)\s+(?:admin\s+)?card", re.I),
    re.compile(r"execute\s+(?:arbitrary\s+)?(?:script|code|eval)", re.I),
    re.compile(r"bypass\s+safety", re.I),
]


class FakeLlmProvider(LlmProvider):
    """Deterministic LLM provider for test execution and offline development."""

    def __init__(
        self,
        profile_id: str = "fake-offline-dev",
        model_name: str = "deterministic-v1",
        simulate_malformed_json_once: bool = False,
        simulate_rate_limit: bool = False,
    ):
        self._profile = LlmProfile(
            profile_id=profile_id,
            provider_name="fake",
            model_name=model_name,
            enabled=True,
            is_free_tier=False,
            supports_streaming=False,
            supports_structured_output=True,
        )
        self.simulate_malformed_json_once = simulate_malformed_json_once
        self.simulate_rate_limit = simulate_rate_limit
        self.invocation_count = 0
        self.custom_intents: dict[str, ShoppingIntent] = {}

    @property
    def profile(self) -> LlmProfile:
        return self._profile

    def check_readiness(self) -> tuple[bool, str | None]:
        return (True, None)

    def register_custom_intent(self, transcript_pattern: str, intent: ShoppingIntent) -> None:
        self.custom_intents[transcript_pattern.lower()] = intent

    async def interpret_intent(self, request: LlmIntentRequest) -> LlmInterpretationResult:
        self.invocation_count += 1
        t_start = time.perf_counter()

        if self.simulate_rate_limit:
            raise LlmProviderError(
                message="Simulated rate limit exceeded (429)",
                profile_id=self.profile.profile_id,
                status_code=429,
                retryable=True,
            )

        if self.simulate_malformed_json_once:
            self.simulate_malformed_json_once = False
            raw_malformed = "NOT_JSON_<<<ERROR>>>"
            raise LlmProviderError(
                message="Malformed JSON output from model",
                profile_id=self.profile.profile_id,
                status_code=400,
                retryable=True,
            )

        transcript = request.transcript.strip()
        transcript_lower = transcript.lower()

        # Check for pre-registered canned intent
        for pat, canned in self.custom_intents.items():
            if pat in transcript_lower:
                latency = (time.perf_counter() - t_start) * 1000
                usage = LlmUsage(prompt_tokens=40, completion_tokens=30, total_tokens=70, latency_ms=latency)
                return LlmInterpretationResult(
                    intent=canned,
                    raw_response_text=json.dumps(canned.to_dict()),
                    usage=usage,
                    profile_id=self.profile.profile_id,
                )

        intent = self._parse_semantic_intent(transcript, request)
        latency = (time.perf_counter() - t_start) * 1000
        usage = LlmUsage(prompt_tokens=50, completion_tokens=45, total_tokens=95, latency_ms=latency)

        return LlmInterpretationResult(
            intent=intent,
            raw_response_text=json.dumps(intent.to_dict()),
            usage=usage,
            profile_id=self.profile.profile_id,
        )

    def _parse_semantic_intent(self, text: str, request: LlmIntentRequest) -> ShoppingIntent:
        lower = text.lower()
        intent_id = f"int_{uuid.uuid4().hex[:12]}"
        unresolved: list[str] = []

        # 1. Check for prompt injection attempts (T-09)
        for pat in INJECTION_PATTERNS:
            if pat.search(text):
                return ShoppingIntent(
                    intent_id=intent_id,
                    operation=IntentOperation.BROWSE,
                    is_explicit_checkout_request=False,
                    supporting_transcript_span=text,
                    unresolved_fields=("prompt_injection_attempt",),
                    original_language_wording=text,
                )

        # 2. Explicit checkout request (T-20, T-21)
        if any(phrase in lower for phrase in ("checkout", "proceed to checkout", "ready to pay", "take me to checkout")):
            return ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.REQUEST_CHECKOUT,
                is_explicit_checkout_request=True,
                supporting_transcript_span=text,
                unresolved_fields=(),
            )

        # 3. View cart
        if any(phrase in lower for phrase in ("view cart", "show cart", "what is in my cart", "check cart")):
            return ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.VIEW_CART,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
                unresolved_fields=(),
            )

        # 4. Describe / Compare product
        if "compare" in lower:
            return ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.DESCRIBE_PRODUCT,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
                product_query="compare",
                unresolved_fields=(),
            )

        # 5. Quantity and removal parsing (T-05)
        is_remove = any(w in lower for w in ("remove", "delete", "take out", "clear line"))
        quantity_change: QuantityChange | None = None

        if is_remove:
            quantity_change = QuantityChange(mode="set", value=0)
            operation = IntentOperation.REMOVE_FROM_CART
        else:
            # Check for set vs increment quantity semantics
            # "make it two", "set to 3", "change quantity to 2" -> set
            set_match = re.search(r"(?:make it\s+|set\s+(?:quantity\s+to\s+)?|change\s+to\s+)(\d+|one|two|three|four|five)", lower)
            # "add two", "add 3 more", "buy two" -> increment
            inc_match = re.search(r"(?:add|give me|buy|want)\s+(\d+|one|two|three|four|five)", lower)

            num_words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "kan": 1, "meji": 2}

            if set_match:
                val_raw = set_match.group(1)
                val = int(val_raw) if val_raw.isdigit() else num_words.get(val_raw, 1)
                quantity_change = QuantityChange(mode="set", value=val)
                operation = IntentOperation.UPDATE_QUANTITY
            elif inc_match:
                val_raw = inc_match.group(1)
                val = int(val_raw) if val_raw.isdigit() else num_words.get(val_raw, 1)
                quantity_change = QuantityChange(mode="increment", value=val)
                operation = IntentOperation.ADD_TO_CART
            elif any(re.search(rf"\b{w}\b", lower) for w in ("add", "buy", "put", "want", "abeg", "ra")):
                val = 1
                if re.search(r"\bkan\b", lower):
                    val = 1
                elif re.search(r"\bmeji\b", lower):
                    val = 2
                quantity_change = QuantityChange(mode="increment", value=val)
                operation = IntentOperation.ADD_TO_CART
            elif "update" in lower:
                quantity_change = QuantityChange(mode="set", value=1)
                operation = IntentOperation.UPDATE_QUANTITY
            elif any(phrase in lower for phrase in ("find", "search", "show me", "looking for")):
                operation = IntentOperation.SEARCH
            else:
                operation = IntentOperation.BROWSE

        # 6. Budget constraint parsing (T-06)
        budget: BudgetConstraint | None = None
        budget_match = re.search(r"(?:under|below|max|less than|budget(?: of)?)\s+(\d+)\s*(?:naira|ngn|\$|usd|eur)?", lower)
        if budget_match:
            amount = budget_match.group(1)
            budget = BudgetConstraint(max_amount=amount, currency=request.budget_currency)

        # 7. Attributes extraction, self-correction, and negation (T-06)
        # Colors: red, blue, green, black, white, yellow, pupa (red in Yoruba)
        # Sizes: small, medium, large, xl, xxl, kekere/kékeré (small in Yoruba)
        selected_attrs: dict[str, str] = {}

        # Handle self-correction for size/color:
        # e.g., "medium wait no make it large", "red no blue"
        correction_match = re.search(r"(\w+)\s+(?:wait\s+no|no\s+make\s+it|actually|rather)\s+(\w+)", lower)
        corrected_word: str | None = None
        if correction_match:
            # Overrule prior word with corrected word
            corrected_word = correction_match.group(2)

        # Negation check: "not blue", "don't give me red", "anything but green"
        negated_colors: set[str] = set()
        neg_matches = re.findall(r"(?:not|don't\s+add|except|but\s+not|anything\s+except)\s+(\w+)", lower)
        for nm in neg_matches:
            negated_colors.add(nm.lower())

        colors = ["red", "blue", "green", "black", "white", "yellow", "pupa"]
        sizes = ["small", "medium", "large", "xl", "xxl", "kekere", "kékeré"]

        for color in colors:
            if re.search(rf"\b{color}\b", lower):
                canon_color = "red" if color == "pupa" else color
                if color in negated_colors or canon_color in negated_colors:
                    selected_attrs["color"] = f"!{canon_color}"
                    continue
                if corrected_word and color != corrected_word and corrected_word in colors:
                    continue
                selected_attrs["color"] = canon_color

        for size in sizes:
            if re.search(rf"\b{size}\b", lower):
                canon_size = "small" if size in ("kekere", "kékeré") else size
                if size in negated_colors or canon_size in negated_colors:
                    selected_attrs["size"] = f"!{canon_size}"
                    continue
                if corrected_word and size != corrected_word and corrected_word in sizes:
                    continue
                selected_attrs["size"] = canon_size

        # 8. Product query / reference extraction
        products_vocab = [
            "t-shirt", "shirt", "agbada", "kaftan", "cap", "dress",
            "hoodie", "jeans", "trousers", "shoes", "jacket"
        ]
        detected_products = [p for p in products_vocab if re.search(rf"\b{p}s?\b", lower)]
        product_query: str | None = None

        if detected_products:
            product_query = detected_products[0]
        elif any(phrase in lower for phrase in ("that one", "that item", "the one", "it")):
            # Ambiguous reference (T-03, T-06)
            if request.current_product_id:
                product_query = request.current_product_id
            else:
                product_query = None
                unresolved.append("product_query")
        elif operation in (IntentOperation.ADD_TO_CART, IntentOperation.UPDATE_QUANTITY):
            if not product_query:
                unresolved.append("product_query")

        return ShoppingIntent(
            intent_id=intent_id,
            operation=operation,
            product_query=product_query,
            selected_variant_attributes=selected_attrs,
            quantity_change=quantity_change,
            budget_constraint=budget,
            is_explicit_checkout_request=False,
            supporting_transcript_span=text,
            unresolved_fields=tuple(unresolved),
        )

    async def generate_grounded_response(self, context: GroundedResponseContext) -> str:
        if context.error_reason:
            return f"I could not complete that: {context.error_reason}."

        if context.execution_receipt_summary:
            # Verified receipt output (S-06)
            return f"Done! {context.execution_receipt_summary}."

        if context.requested_intent.unresolved_fields:
            missing = ", ".join(context.requested_intent.unresolved_fields)
            return f"I need a bit more detail about {missing}. Could you clarify?"

        if context.operation == IntentOperation.REQUEST_CHECKOUT.value:
            return "Taking you to checkout now. Please complete your order on the secure checkout page."

        if context.operation == IntentOperation.VIEW_CART.value:
            cart = context.cart_summary or "Your cart is empty."
            return f"Here is your cart: {cart}."

        return f"Found matching options: {context.evidence_summary}."

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
    DecisionMode,
    IntentOperation,
    QuantityChange,
    ReferenceKind,
    ResponsePurpose,
    ShoppingIntent,
    TargetReference,
    TurnDecision,
)
from ishop.llm.base import (
    GroundedResponseContext,
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProfile,
    LlmProvider,
    LlmProviderError,
    LlmUsage,
    LlmToolSelectionRequest,
    LlmToolSelectionResult,
    tool_arguments_for_intent,
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
        self.custom_decisions: dict[str, TurnDecision] = {}

    @property
    def profile(self) -> LlmProfile:
        return self._profile

    def check_readiness(self) -> tuple[bool, str | None]:
        return (True, None)

    def register_custom_intent(self, transcript_pattern: str, intent: ShoppingIntent) -> None:
        self.custom_intents[transcript_pattern.lower()] = intent

    def register_custom_decision(self, transcript_pattern: str, decision: TurnDecision) -> None:
        self.custom_decisions[transcript_pattern.lower()] = decision

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

        # Check for pre-registered canned decision or intent
        for pat, canned_dec in self.custom_decisions.items():
            if pat in transcript_lower:
                latency = (time.perf_counter() - t_start) * 1000
                usage = LlmUsage(prompt_tokens=40, completion_tokens=30, total_tokens=70, latency_ms=latency)
                intent = canned_dec.intent or ShoppingIntent(
                    intent_id=f"int_{uuid.uuid4().hex[:12]}",
                    operation=IntentOperation.BROWSE,
                    is_explicit_checkout_request=False,
                    supporting_transcript_span=transcript,
                )
                return LlmInterpretationResult(
                    intent=intent,
                    raw_response_text=json.dumps(canned_dec.to_dict()),
                    usage=usage,
                    profile_id=self.profile.profile_id,
                    grounded_response_text=canned_dec.response_text,
                    decision=canned_dec,
                )

        for pat, canned in self.custom_intents.items():
            if pat in transcript_lower:
                latency = (time.perf_counter() - t_start) * 1000
                usage = LlmUsage(prompt_tokens=40, completion_tokens=30, total_tokens=70, latency_ms=latency)
                decision = TurnDecision(mode=DecisionMode.ACT, intent=canned)
                return LlmInterpretationResult(
                    intent=canned,
                    raw_response_text=json.dumps(canned.to_dict()),
                    usage=usage,
                    profile_id=self.profile.profile_id,
                    decision=decision,
                )

        intent, decision = self._parse_semantic_intent_and_decision(transcript, request)
        latency = (time.perf_counter() - t_start) * 1000
        usage = LlmUsage(prompt_tokens=50, completion_tokens=45, total_tokens=95, latency_ms=latency)

        return LlmInterpretationResult(
            intent=intent,
            raw_response_text=json.dumps(decision.to_dict()),
            usage=usage,
            profile_id=self.profile.profile_id,
            grounded_response_text=decision.response_text,
            decision=decision,
        )

    def _parse_semantic_intent(self, text: str, request: LlmIntentRequest) -> ShoppingIntent:
        intent, _ = self._parse_semantic_intent_and_decision(text, request)
        return intent

    def _parse_semantic_intent_and_decision(self, text: str, request: LlmIntentRequest) -> tuple[ShoppingIntent, TurnDecision]:
        lower = text.lower().strip()
        intent_id = f"int_{uuid.uuid4().hex[:12]}"
        unresolved: list[str] = []

        # 0. Conversational Respond Branches (Milestone 2 & CF-01..CF-04)
        # Dangerous / weapon refusal
        if any(w in lower for w in ("bomb", "weapon", "explosive", "gun", "ammunition", "grenade", "dangerous materials")):
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.BROWSE,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
            )
            decision = TurnDecision(
                mode=DecisionMode.RESPOND,
                response_purpose=ResponsePurpose.REFUSAL,
                response_text="I cannot assist with requests involving weapons, explosives, or dangerous materials. I can help you search for store items.",
                intent=intent,
            )
            return intent, decision

        # Capability question
        if any(phrase in lower for phrase in ("what can you do", "what are your capabilities", "how can you help", "what do you do", "can you help me")) or lower in ("help", "help me", "help me please"):
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.STORE_INFORMATION,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
            )
            decision = TurnDecision(
                mode=DecisionMode.RESPOND,
                response_purpose=ResponsePurpose.CAPABILITY_HELP,
                response_text="I can help you search products, compare items, check prices and availability, navigate the store, add items to your cart, and proceed to checkout.",
                intent=intent,
            )
            return intent, decision

        # Harmless writing assistance
        if any(phrase in lower for phrase in ("write me a letter", "can you write a letter", "write a letter", "draft an email", "draft a message", "write an email", "help me write", "write a note", "write a thank you", "can you write", "draft a note")):
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.STORE_INFORMATION,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
            )
            decision = TurnDecision(
                mode=DecisionMode.RESPOND,
                response_purpose=ResponsePurpose.GENERAL_ASSISTANCE,
                response_text="I can help draft a brief note. Who is the recipient and what message or topic would you like to include?",
                intent=intent,
            )
            return intent, decision

        # Greetings
        if any(phrase in lower for phrase in ("how are you, drake", "how are you", "hi drake", "hello drake", "hey drake", "good morning", "good afternoon", "good evening", "what's up")) or lower in ("hi", "hello", "hey"):
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.BROWSE,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
            )
            decision = TurnDecision(
                mode=DecisionMode.RESPOND,
                response_purpose=ResponsePurpose.GREETING,
                response_text="Hello! I'm Drake, your shopping assistant. How can I help you today?",
                intent=intent,
            )
            return intent, decision

        # Courtesies / thanks
        if lower in ("thanks", "thank you", "ok thanks", "thanks drake", "thank you drake", "great thanks") or lower.startswith("thanks") or lower.startswith("thank you"):
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.BROWSE,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
            )
            decision = TurnDecision(
                mode=DecisionMode.RESPOND,
                response_purpose=ResponsePurpose.GENERAL_ASSISTANCE,
                response_text="You're welcome! Let me know if you need anything else.",
                intent=intent,
            )
            return intent, decision


        # 1. Check for prompt injection attempts (T-09)
        for pat in INJECTION_PATTERNS:
            if pat.search(text):
                intent = ShoppingIntent(
                    intent_id=intent_id,
                    operation=IntentOperation.BROWSE,
                    is_explicit_checkout_request=False,
                    supporting_transcript_span=text,
                    unresolved_fields=("prompt_injection_attempt",),
                    original_language_wording=text,
                )
                return intent, TurnDecision(mode=DecisionMode.RESPOND, response_purpose=ResponsePurpose.REFUSAL, response_text="I cannot process instructions that attempt to alter safety controls.", intent=intent)

        # 2. Explicit checkout request (T-20, T-21)
        if any(phrase in lower for phrase in ("empty my entire cart", "clear my entire cart", "empty the whole cart", "clear the whole cart")):
            intent = ShoppingIntent(intent_id=intent_id, operation=IntentOperation.CANCEL_CART,
                is_explicit_checkout_request=False, supporting_transcript_span=text)
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)
        if any(phrase in lower for phrase in ("order history", "my orders", "manage orders", "where is my order")):
            intent = ShoppingIntent(intent_id=intent_id, operation=IntentOperation.MANAGE_ORDERS,
                is_explicit_checkout_request=False, supporting_transcript_span=text)
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)
        if any(word in lower for word in ("return policy", "shipping policy", "store hours", "faq")):
            intent = ShoppingIntent(intent_id=intent_id, operation=IntentOperation.STORE_INFORMATION,
                product_query=text, is_explicit_checkout_request=False, supporting_transcript_span=text)
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)
        if any(phrase in lower for phrase in ("show variant", "preview the", "select the color")):
            product = next((name for name in ("Multi-location Snowboard", "Multi-managed Snowboard", "Complete Snowboard") if name.lower() in lower), None)
            product = product or next((name for name in ("snowboard", "shirt", "hoodie", "cap", "dress", "shoes") if name in lower), None)
            option = next((name for name in ("ice", "dawn", "powder", "electric", "red", "blue", "green", "black", "white") if name in lower), None)
            intent = ShoppingIntent(intent_id=intent_id, operation=IntentOperation.SHOW_VARIANT,
                product_query=product, selected_variant_attributes=({"color": option} if option else {}),
                is_explicit_checkout_request=False, supporting_transcript_span=text,
                unresolved_fields=(() if product else ("product_query",)))
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)

        if any(phrase in lower for phrase in ("checkout", "proceed to checkout", "ready to pay", "take me to checkout")):
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.REQUEST_CHECKOUT,
                is_explicit_checkout_request=True,
                supporting_transcript_span=text,
                unresolved_fields=(),
            )
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)

        # Home / Back navigation
        if any(phrase in lower for phrase in ("go home", "take me home", "store home", "homepage")) or lower == "home":
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.NAVIGATE,
                product_query="home",
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
            )
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)

        if any(phrase in lower for phrase in ("go back", "previous page", "return to previous")) or lower == "back":
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.NAVIGATE,
                product_query="back",
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
            )
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)

        target_ref: TargetReference | None = None
        if "cheaper" in lower or "cheapest" in lower:
            target_ref = TargetReference(kind=ReferenceKind.COMPARISON_SELECTION, value="cheaper")

        # Check for ordinals
        ord_match = re.search(r"\b(1st|2nd|3rd|4th|first|second|third|fourth)\b", lower)
        if ord_match:
            pos_map = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3, "4th": 4, "fourth": 4}
            val = ord_match.group(1)
            target_ref = TargetReference(kind=ReferenceKind.RESULT_POSITION, value=val, position=pos_map.get(val))

        if any(phrase in lower for phrase in ("open the", "open product", "go to the", "go to", "take me to the product", "take me to the", "take me to", "open")):
            product = next((name for name in ("Multi-location Snowboard", "Multi-managed Snowboard", "Complete Snowboard") if name.lower() in lower), None)
            if not product:
                m = re.search(r"\b(?:take me to (?:the )?|go to (?:the )?|open (?:the )?)(.+)$", lower)
                if m:
                    extracted = m.group(1).strip()
                    if extracted and extracted not in ("it", "cheaper one", "first one", "second one", "third one", "fourth one", "1st one", "2nd one", "3rd one", "4th one"):
                        product = extracted
            product = product or next((name for name in ("snowboard", "shirt", "hoodie", "cap", "dress", "shoes") if name in lower), None)
            intent = ShoppingIntent(
                intent_id=intent_id, operation=IntentOperation.NAVIGATE,
                product_query=product, is_explicit_checkout_request=False,
                supporting_transcript_span=text,
                target_reference=target_ref,
                unresolved_fields=(() if (product or target_ref) else ("product_query",)),
            )
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent, target_reference=target_ref)

        # 3. View cart
        if any(phrase in lower for phrase in ("view cart", "show cart", "show my cart", "what is in my cart", "check cart")):
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.VIEW_CART,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
                unresolved_fields=(),
            )
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)

        # 4. Describe / Compare product
        if "compare" in lower:
            intent = ShoppingIntent(
                intent_id=intent_id,
                operation=IntentOperation.DESCRIBE_PRODUCT,
                is_explicit_checkout_request=False,
                supporting_transcript_span=text,
                product_query="compare",
                unresolved_fields=(),
            )
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)

        if any(phrase in lower for phrase in ("describe", "details about", "tell me about", "tell me more about")):
            product = next((name for name in ("Multi-location Snowboard", "Multi-managed Snowboard", "Complete Snowboard", "Alpine Pro Snowboard", "Summit Powder Snowboard", "Freestyle Carbon Snowboard", "Beginner Park Snowboard") if name.lower() in lower), None)
            if not product:
                m = re.search(r"\b(?:describe|details about|tell me about|tell me more about)(?:\s+the)?\s+(.+)$", lower)
                if m:
                    extracted = m.group(1).strip()
                    if extracted and extracted not in ("it", "that", "this", "that one", "this one", "the one", "item", "product", "cheaper one", "cheapest one", "first one", "second one", "third one", "fourth one"):
                        product = extracted
            if not product:
                product = next((name for name in ("snowboard", "shirt", "hoodie", "cap", "dress", "shoes") if name in lower), None)
            intent = ShoppingIntent(
                intent_id=intent_id, operation=IntentOperation.DESCRIBE_PRODUCT,
                is_explicit_checkout_request=False, supporting_transcript_span=text,
                product_query=product, unresolved_fields=(() if product else ("product_query",)),
            )
            return intent, TurnDecision(mode=DecisionMode.ACT, intent=intent)

        # 5. Quantity and removal parsing (T-05)
        is_remove = any(w in lower for w in ("remove", "delete", "take out", "clear line"))
        quantity_change: QuantityChange | None = None

        # Check for category-level "buy snowboards" (CF-05: discovery search, NOT add!)
        is_category_discovery = bool(re.search(r"^(?:i want to |i'd like to |looking to )?buy\s+(?:some\s+)?(snowboards?|shirts?|hoodies?|caps?|dresses?|shoes|jackets?)\s*$", lower))

        if is_remove:
            quantity_change = QuantityChange(mode="set", value=0)
            operation = IntentOperation.REMOVE_FROM_CART
        elif is_category_discovery:
            operation = IntentOperation.SEARCH
        else:
            # Check for set vs increment quantity semantics
            set_match = re.search(r"(?:make it\s+|set\s+(?:quantity\s+to\s+)?|change\s+to\s+)(\d+|one|two|three|four|five)", lower)
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
            elif any(phrase in lower for phrase in ("browse store", "browse the store", "browse collection", "browse the store collections", "browse the collections", "list collections", "store collections")):
                operation = IntentOperation.BROWSE
            elif any(phrase in lower for phrase in ("find", "search", "show me", "looking for", "browse")):
                operation = IntentOperation.SEARCH
            else:
                operation = IntentOperation.BROWSE

        # 6. Budget constraint parsing (T-06)
        budget: BudgetConstraint | None = None
        budget_match = re.search(r"(?:under|below|max|less than|at most|up to|budget(?: of)?)\s*\$?\s*(\d+)\s*(?:naira|ngn|\$|usd|eur|dollars)?", lower)
        if budget_match:
            amount = budget_match.group(1)
            budget = BudgetConstraint(max_amount=amount, currency=request.budget_currency)
        else:
            min_match = re.search(r"(?:over|above|more than|at least)\s*\$?\s*(\d+)\s*(?:naira|ngn|\$|usd|eur|dollars)?", lower)
            approx_match = re.search(r"(?:around|about|approximately|closest\s+to)\s*\$?\s*(\d+)\s*(?:naira|ngn|usd|eur|dollars)?", lower)
            if min_match:
                budget = BudgetConstraint(max_amount=None, min_amount=min_match.group(1), currency=request.budget_currency, comparison="min")
            elif approx_match:
                budget = BudgetConstraint(max_amount=approx_match.group(1), currency=request.budget_currency, comparison="approximate")

        # 7. Attributes extraction, self-correction, and negation (T-06)
        selected_attrs: dict[str, str] = {}
        correction_match = re.search(r"(\w+)\s+(?:wait\s+no|no\s+make\s+it|actually|rather)\s+(\w+)", lower)
        corrected_word: str | None = None
        if correction_match:
            corrected_word = correction_match.group(2)

        negated_colors: set[str] = set()
        neg_matches = re.findall(r"(?:not|don't\s+add|except|but\s+not|anything\s+except)\s+(\w+)", lower)
        for nm in neg_matches:
            negated_colors.add(nm.lower())

        colors = ["red", "blue", "green", "black", "white", "yellow", "pupa", "ice", "dawn", "powder", "electric"]
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
            "hoodie", "jeans", "trousers", "shoes", "jacket", "snowboard"
        ]
        detected_products = [p for p in products_vocab if re.search(rf"\b{p}s?\b", lower)]
        product_query: str | None = None

        if "multi-location snowboard" in lower:
            product_query = "Multi-location Snowboard"
        elif "multi-managed snowboard" in lower:
            product_query = "Multi-managed Snowboard"
        elif "complete snowboard" in lower:
            product_query = "Complete Snowboard"
        elif detected_products:
            product_query = detected_products[0]
        elif any(phrase in lower for phrase in ("that one", "that item", "the one", "it")):
            if request.current_product_id:
                product_query = request.current_product_id
            else:
                product_query = None
                unresolved.append("product_query")
        elif operation in (IntentOperation.ADD_TO_CART, IntentOperation.UPDATE_QUANTITY):
            if not product_query and not target_ref:
                unresolved.append("product_query")

        intent = ShoppingIntent(
            intent_id=intent_id,
            operation=operation,
            product_query=product_query,
            selected_variant_attributes=selected_attrs,
            quantity_change=quantity_change,
            budget_constraint=budget,
            is_explicit_checkout_request=False,
            supporting_transcript_span=text,
            target_reference=target_ref,
            unresolved_fields=tuple(unresolved),
        )
        decision = TurnDecision(mode=DecisionMode.ACT, intent=intent, target_reference=target_ref)
        return intent, decision

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

    async def select_tool(self, request: LlmToolSelectionRequest) -> LlmToolSelectionResult:
        mapping = {
            IntentOperation.SEARCH: "search_catalog",
            IntentOperation.BROWSE: "browse_store",
            IntentOperation.DESCRIBE_PRODUCT: "get_product",
            IntentOperation.CHECK_AVAILABILITY: "get_product",
            IntentOperation.VIEW_CART: "get_cart",
            IntentOperation.ADD_TO_CART: "update_cart",
            IntentOperation.UPDATE_QUANTITY: "update_cart",
            IntentOperation.REMOVE_FROM_CART: "update_cart",
            IntentOperation.NAVIGATE: "get_product",
            IntentOperation.REQUEST_CHECKOUT: "proceed_to_checkout",
            IntentOperation.CANCEL_CART: "cancel_cart",
            IntentOperation.MANAGE_ORDERS: "manage_orders",
            IntentOperation.STORE_INFORMATION: "search_shop_policies_and_faqs",
            IntentOperation.SHOW_VARIANT: "show_variant",
        }
        name = mapping[request.intent.operation]
        if request.intent.operation == IntentOperation.BROWSE and request.intent.product_query:
            name = "search_catalog"
        allowed = {str(tool["name"]) for tool in request.qualified_tools}
        if name not in allowed:
            raise LlmProviderError(f"Qualified tool unavailable: {name}", self.profile.profile_id)
        arguments = tool_arguments_for_intent(request.intent, name)
        return LlmToolSelectionResult(name, arguments, "Deterministic offline tool selection", json.dumps({"tool_name": name, "arguments": arguments}))

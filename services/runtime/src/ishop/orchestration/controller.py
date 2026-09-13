"""Typed async shopping orchestrator and grounded controller for iShop (Drake).

Enforces:
- S-01, S-02: Zero payment or order execution. Hand off only to trusted checkout destination (T-20, T-21).
- S-03: Evidence binding; reject invented product IDs or mismatched currency/shop (T-02).
- S-04: No silent variant substitution; incomplete options trigger focused clarification (T-03).
- S-06: No success wording before verified cart execution receipt is confirmed.
- S-09: Precondition cart fingerprint binding and bounded lease.
- T-05: Strict quantity arithmetic (increment vs set vs zero removal).
- T-06: Negation scope, self-correction, ambiguous references, budget scope.
- T-09: Prompt injection resistance against malicious catalog/user instructions.
- T-11: Turn revision and page epoch freshness; stale requests cannot dispatch or speak.
- T-28: Complete structured output validation with bounded retry.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from ishop.commerce.catalog import (
    CatalogResolver,
    EvidenceSnapshot,
    IntentTarget,
    ProductEvidence,
    ResolutionResult,
    ResolutionStatus,
    VariantEvidence,
    product_title_matches,
)
from ishop.commerce.reconciler import (
    CommandReconciler,
    ExecutionOutcome,
    ExecutionReceipt,
)
from ishop.commerce.simulator import ShopifySimulator
from ishop.commerce.verifier import (
    CartVerifier,
    ProposedCartAction,
    QuantityOperation,
    VerificationOutcome,
)
from ishop.domain.intent import (
    BudgetConstraint,
    IntentOperation,
    QuantityChange,
    ShoppingIntent,
)
from ishop.domain.models import (
    AuthorizedCommand,
    CartLine,
    CartSnapshot,
    CommandOperation,
    Money,
)
from ishop.llm.base import (
    GroundedResponseContext,
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProvider,
    LlmProviderError,
)

logger = logging.getLogger(__name__)

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous\s+)?instructions", re.I),
    re.compile(r"leak\s+(?:system\s+)?(?:secret|prompt|key|token)", re.I),
    re.compile(r"(?:charge|submit|swipe)\s+(?:admin\s+)?card", re.I),
    re.compile(r"execute\s+(?:arbitrary\s+)?(?:script|code|eval)", re.I),
    re.compile(r"bypass\s+safety", re.I),
]


@dataclass
class ControllerSessionState:
    """Session-scoped concurrency and freshness state (R2, S-09)."""

    session_id: str
    active_turn_id: str | None = None
    latest_request_revision: int = 0
    latest_page_epoch: int = 0
    is_cancelled: bool = False
    cancelled_turns: set[str] = field(default_factory=set)
    pending_intents: dict[tuple[str, int], ShoppingIntent] = field(default_factory=dict)
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    active_search_query: str | None = None
    active_search_product_ids: tuple[str, ...] = ()
    pending_clarification_intent: ShoppingIntent | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "active_turn_id": self.active_turn_id,
            "latest_request_revision": self.latest_request_revision,
            "latest_page_epoch": self.latest_page_epoch,
            "is_cancelled": self.is_cancelled,
            "cancelled_turns": sorted(self.cancelled_turns),
            "pending_intents": [
                {"turn_id": turn_id, "revision": revision, "intent": intent.to_dict()}
                for (turn_id, revision), intent in self.pending_intents.items()
            ],
            "conversation_history": list(self.conversation_history),
            "active_search_query": self.active_search_query,
            "active_search_product_ids": list(self.active_search_product_ids),
            "pending_clarification_intent": (
                self.pending_clarification_intent.to_dict()
                if self.pending_clarification_intent else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControllerSessionState":
        pending = {}
        for item in data.get("pending_intents", []):
            pending[(str(item["turn_id"]), int(item["revision"]))] = ShoppingIntent.from_dict(item["intent"])
        pending_raw = data.get("pending_clarification_intent")
        return cls(
            session_id=str(data["session_id"]),
            active_turn_id=data.get("active_turn_id"),
            latest_request_revision=int(data.get("latest_request_revision", 0)),
            latest_page_epoch=int(data.get("latest_page_epoch", 0)),
            is_cancelled=bool(data.get("is_cancelled", False)),
            cancelled_turns={str(value) for value in data.get("cancelled_turns", [])},
            pending_intents=pending,
            conversation_history=list(data.get("conversation_history", [])),
            active_search_query=data.get("active_search_query"),
            active_search_product_ids=tuple(str(value) for value in data.get("active_search_product_ids", [])),
            pending_clarification_intent=(ShoppingIntent.from_dict(pending_raw) if pending_raw else None),
        )


@dataclass(frozen=True)
class ControllerTurnResult:
    """Outcome of a single shopping dialogue turn."""

    session_id: str
    turn_id: str
    request_revision: int
    page_epoch: int
    status: str  # "completed", "clarification_needed", "rejected", "stale", "cancelled", "error"
    spoken_response: str
    extracted_intent: ShoppingIntent | None = None
    receipt: ExecutionReceipt | None = None
    authorized_command: AuthorizedCommand | None = None
    clarification_options: tuple[str, ...] = ()
    reason: str | None = None
    clarification_fields: tuple[str, ...] = ()
    evidence_query: str | None = None
    result_product_ids: tuple[str, ...] = ()


class ShoppingController:
    """Provider-neutral async shopping reasoning and execution controller."""

    def __init__(
        self,
        llm_provider: LlmProvider,
        reconciler: CommandReconciler | None = None,
        max_llm_retries: int = 1,
    ):
        self.llm_provider = llm_provider
        self.reconciler = reconciler
        self.max_llm_retries = max_llm_retries

        # Session-scoped state tracking for revision and epoch bounds (R2, T-11, T-17, S-09)
        self._sessions: dict[str, ControllerSessionState] = {}

    def get_session_state(self, session_id: str) -> ControllerSessionState:
        """Get or initialize session-scoped state."""
        if session_id not in self._sessions:
            self._sessions[session_id] = ControllerSessionState(session_id=session_id)
        return self._sessions[session_id]

    def export_session_state(self, session_id: str) -> dict[str, Any]:
        return self.get_session_state(session_id).to_dict()

    def restore_session_state(self, data: dict[str, Any]) -> None:
        restored = ControllerSessionState.from_dict(data)
        self._sessions[restored.session_id] = restored

    def cancel_turn(self, session_id: str, turn_id: str) -> None:
        """Cancel an in-flight or pending turn (R2, S-09)."""
        sess = self.get_session_state(session_id)
        sess.cancelled_turns.add(turn_id)

    def cancel_session(self, session_id: str) -> None:
        """Cancel all pending work in a session (R2, S-09)."""
        sess = self.get_session_state(session_id)
        sess.is_cancelled = True

    def update_page_epoch(self, session_id: str, new_epoch: int) -> None:
        """Record page navigation / epoch increment (R2, T-17, S-09)."""
        sess = self.get_session_state(session_id)
        if new_epoch > sess.latest_page_epoch:
            sess.latest_page_epoch = new_epoch

    def _check_turn_staleness(
        self,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
    ) -> tuple[bool, str, str]:
        """Check if turn is stale, cancelled, or epoch-superseded.

        Returns (is_invalid, status, reason).
        """
        sess = self.get_session_state(session_id)
        if sess.is_cancelled:
            return True, "cancelled", "Session was cancelled (S-09)."
        if turn_id in sess.cancelled_turns:
            return True, "cancelled", f"Turn {turn_id} was cancelled (S-09)."
        if page_epoch < sess.latest_page_epoch:
            return True, "stale", f"Stale page epoch {page_epoch} < latest {sess.latest_page_epoch} (S-09, T-17)"
        if request_revision < sess.latest_request_revision:
            return True, "stale", f"Stale request revision {request_revision} < latest {sess.latest_request_revision} (S-09, T-11)"
        return False, "", ""

    async def handle_turn(
        self,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
        transcript: str,
        evidence: EvidenceSnapshot,
        current_cart: CartSnapshot,
        client: ShopifyClient | None = None,
        current_product_id: str | None = None,
        now_ms: int | None = None,
    ) -> ControllerTurnResult:
        """Handle an accepted final transcript through LLM reasoning, resolution, and execution."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)

        # 1. Turn revision and page epoch freshness check (T-11, T-17, S-09, R2)
        is_invalid, status, reason = self._check_turn_staleness(
            session_id, turn_id, request_revision, page_epoch
        )
        if is_invalid:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status=status,
                spoken_response="This request was superseded by a newer turn or cancelled.",
                reason=reason,
            )

        # Update session active turn pointers
        sess = self.get_session_state(session_id)
        sess.active_turn_id = turn_id
        sess.latest_request_revision = max(sess.latest_request_revision, request_revision)
        sess.latest_page_epoch = max(sess.latest_page_epoch, page_epoch)
        pending_key = (turn_id, request_revision)
        for key in tuple(sess.pending_intents):
            if key != pending_key and key[1] < request_revision:
                del sess.pending_intents[key]

        # 2. Prompt injection defence pre-check (T-09)
        for pat in INJECTION_PATTERNS:
            if pat.search(transcript):
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="rejected",
                    spoken_response="I am a shopping assistant and cannot execute payment or administrative commands.",
                    reason="Prompt injection pattern detected in transcript (T-09)",
                )

        # 3. Build catalog context for LLM
        catalog_summaries: list[str] = []
        for p in evidence.products.values():
            variants_str = ", ".join(
                f"{v.variant_title} ({v.price})" for v in p.variants[:3]
            )
            catalog_summaries.append(f"{p.title}: {variants_str}")

        # Summarize current cart
        cart_lines_desc = [
            f"{l.quantity}x {l.canonical_key}" for l in current_cart.lines
        ]
        cart_summary_str = "; ".join(cart_lines_desc) if cart_lines_desc else "Empty"

        is_resume = pending_key in sess.pending_intents and evidence.query is not None
        if not is_resume:
            sess.conversation_history.append({"role": "user", "content": transcript.strip()})
            sess.conversation_history = sess.conversation_history[-8:]

        req = LlmIntentRequest(
            transcript=transcript,
            conversation_history=tuple(sess.conversation_history),
            catalog_context=tuple(catalog_summaries),
            cart_summary=cart_summary_str,
            current_product_id=current_product_id,
            budget_currency=evidence.currency,
            turn_id=turn_id,
            request_revision=request_revision,
        )

        # 4. Interpret once, then retain the validated intent while read-only
        # storefront evidence is fetched for this same turn.
        intent = sess.pending_intents.get(pending_key) if evidence.query is not None else None
        if intent is None:
            intent_result: LlmInterpretationResult | None = None
            attempts = 0
            last_err: Exception | None = None
            while attempts <= self.max_llm_retries:
                attempts += 1
                try:
                    intent_result = await self.llm_provider.interpret_intent(req)
                    break
                except LlmProviderError as e:
                    last_err = e
                    if not e.retryable or attempts > self.max_llm_retries:
                        break
                except Exception as e:
                    last_err = e
                    break
            if not intent_result:
                err_reason = str(last_err) if last_err else "Failed to parse structured intent"
                return ControllerTurnResult(
                    session_id=session_id, turn_id=turn_id,
                    request_revision=request_revision, page_epoch=page_epoch,
                    status="error",
                    spoken_response="I had trouble processing that shopping request. Could you rephrase it?",
                    reason=f"LLM interpretation failure: {err_reason}",
                )
            intent = intent_result.intent

            is_invalid, status, reason = self._check_turn_staleness(
                session_id, turn_id, request_revision, page_epoch
            )
            if is_invalid:
                return ControllerTurnResult(
                    session_id=session_id, turn_id=turn_id,
                    request_revision=request_revision, page_epoch=page_epoch,
                    status=status,
                    spoken_response="This request was superseded by a newer turn or cancelled.",
                    reason=f"Aborted after async reasoning: {reason}",
                )

        lower_transcript = transcript.casefold()
        pending_clarification = sess.pending_clarification_intent
        starts_new_read = any(
            re.search(rf"\b{word}\b", lower_transcript)
            for word in ("find", "search", "show", "browse", "compare", "describe")
        )
        if (
            pending_clarification is not None
            and pending_clarification.operation
            in {
                IntentOperation.ADD_TO_CART,
                IntentOperation.UPDATE_QUANTITY,
                IntentOperation.REMOVE_FROM_CART,
            }
            and intent.operation
            in {IntentOperation.SEARCH, IntentOperation.BROWSE, IntentOperation.DESCRIBE_PRODUCT}
            and intent.product_query
            and not starts_new_read
        ):
            # A bare product title/selection answers the preceding cart
            # clarification. Preserve the authorized operation instead of
            # allowing a fresh LLM classification to turn it into a search.
            intent = replace(
                pending_clarification,
                intent_id=intent.intent_id,
                product_query=intent.product_query,
                selected_variant_attributes=(
                    intent.selected_variant_attributes
                    or pending_clarification.selected_variant_attributes
                ),
                supporting_transcript_span=transcript.strip(),
                original_language_wording=intent.original_language_wording,
                unresolved_fields=tuple(
                    field_name
                    for field_name in pending_clarification.unresolved_fields
                    if field_name not in {"product_query", "product_selection"}
                ),
            )

        asks_for_result_ranking = any(
            phrase in lower_transcript for phrase in ("cheapest", "lowest", "least expensive", "closest to")
        )
        if asks_for_result_ranking and not intent.product_query and sess.active_search_query:
            intent = replace(intent, product_query=sess.active_search_query)

        if starts_new_read and intent.operation in {
            IntentOperation.SEARCH,
            IntentOperation.BROWSE,
            IntentOperation.DESCRIBE_PRODUCT,
        }:
            sess.pending_clarification_intent = None

        if (
            intent.operation
            in {
                IntentOperation.SEARCH,
                IntentOperation.BROWSE,
                IntentOperation.DESCRIBE_PRODUCT,
                IntentOperation.CHECK_AVAILABILITY,
                IntentOperation.ADD_TO_CART,
                IntentOperation.UPDATE_QUANTITY,
                IntentOperation.REMOVE_FROM_CART,
                IntentOperation.NAVIGATE,
            }
            and intent.product_query
            and not evidence.products
            and evidence.query is None
        ):
            sess.pending_intents[pending_key] = intent
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="evidence_required",
                spoken_response="",
                extracted_intent=intent,
                reason="Catalog evidence must be retrieved for the structured product query",
                evidence_query=intent.product_query,
            )

        sess.pending_intents.pop(pending_key, None)

        # Do not silently discard a second requested action when the provider
        # returns only the first operation. A bounded plan contract can be
        # introduced later; until then, ask before executing either side.
        if intent.operation in (IntentOperation.SEARCH, IntentOperation.BROWSE) and "add" in lower_transcript and ("search" in lower_transcript or "find" in lower_transcript):
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="clarification_needed",
                spoken_response="I can search first, then add a selected result. Which product should I add?",
                extracted_intent=intent,
                clarification_fields=("product_selection",),
                reason="Compound search-and-add request requires an explicit selected product",
            )

        # 5. Dispatch based on extracted intent operation
        if intent.operation in (IntentOperation.SEARCH, IntentOperation.BROWSE):
            result = self._handle_search_and_browse(
                session_id, turn_id, request_revision, page_epoch, intent, evidence
            )
            if result.status == "completed" and result.result_product_ids:
                sess.active_search_query = (intent.product_query or sess.active_search_query or "").strip() or None
                sess.active_search_product_ids = result.result_product_ids
            return result

        elif intent.operation == IntentOperation.DESCRIBE_PRODUCT:
            return self._handle_describe_and_compare(
                session_id, turn_id, request_revision, page_epoch, intent, evidence
            )

        elif intent.operation == IntentOperation.VIEW_CART:
            return self._handle_view_cart(
                session_id, turn_id, request_revision, page_epoch, intent, current_cart
            )

        elif intent.operation in (
            IntentOperation.ADD_TO_CART,
            IntentOperation.UPDATE_QUANTITY,
            IntentOperation.REMOVE_FROM_CART,
        ):
            result = await self._handle_cart_mutation(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                intent=intent,
                evidence=evidence,
                current_cart=current_cart,
                client=client,
                now_ms=now,
            )
            if (
                result.status == "clarification_needed"
                and result.clarification_fields
            ):
                sess.pending_clarification_intent = intent
            elif result.status != "evidence_required":
                sess.pending_clarification_intent = None
            return result

        elif intent.operation == IntentOperation.REQUEST_CHECKOUT:
            return self._handle_checkout(
                session_id, turn_id, request_revision, page_epoch, intent, current_cart
            )

        elif intent.operation == IntentOperation.NAVIGATE:
            matches = [p for p in evidence.products.values() if intent.product_query and product_title_matches(intent.product_query, p.title)]
            if len(matches) != 1 or not matches[0].url:
                return ControllerTurnResult(
                    session_id=session_id, turn_id=turn_id,
                    request_revision=request_revision, page_epoch=page_epoch,
                    status="clarification_needed",
                    spoken_response="Which product would you like me to open?",
                    extracted_intent=intent,
                    clarification_options=tuple(p.title for p in matches[:4]),
                    reason="Navigation requires one verified Shopify product URL",
                )
            command = AuthorizedCommand(
                command_id=f"cmd_{uuid.uuid4().hex}", session_id=session_id,
                shop_id=current_cart.shop_id, turn_id=turn_id,
                request_revision=request_revision, page_epoch=page_epoch,
                expires_at_ms=now + 30_000,
                operation=CommandOperation.NAVIGATE_STOREFRONT,
                parameters={"url": matches[0].url},
                expected_cart_fingerprint=current_cart.fingerprint(),
            )
            return ControllerTurnResult(
                session_id=session_id, turn_id=turn_id,
                request_revision=request_revision, page_epoch=page_epoch,
                status="completed",
                spoken_response=f"Opening {matches[0].title}.",
                extracted_intent=intent, authorized_command=command,
            )

        else:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="completed",
                spoken_response="How else can I help you find something in the store?",
                extracted_intent=intent,
            )

    def _handle_search_and_browse(
        self,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
        intent: ShoppingIntent,
        evidence: EvidenceSnapshot,
    ) -> ControllerTurnResult:
        query = (intent.product_query or "").strip().lower()
        ranking_text = f"{intent.supporting_transcript_span} {query}".casefold()
        wants_cheapest = any(word in ranking_text for word in ("cheapest", "lowest", "least expensive"))
        query = re.sub(r"\b(?:cheapest|lowest|least\s+expensive)\b", "", query).strip()
        matching: list[ProductEvidence] = []
        eligible_prices: dict[str, tuple[Money, ...]] = {}
        price_evidence_error = False

        if not query and not evidence.products:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="clarification_needed",
                spoken_response="What kind of product should I look for?",
                extracted_intent=intent,
                clarification_fields=("product_query",),
                reason="Catalog subject is missing and no browse evidence is available",
            )

        for p in evidence.products.values():
            if not query or product_title_matches(query, p.title):
                # Apply budget constraint if specified (T-06)
                if intent.budget_constraint:
                    try:
                        budget = intent.budget_constraint
                        max_amt = Money.from_string(budget.max_amount, budget.currency) if budget.max_amount is not None else None
                        min_amt = Money.from_string(budget.min_amount, budget.currency) if budget.min_amount is not None else None
                        def eligible(price: Money) -> bool:
                            if price.currency != budget.currency:
                                raise ValueError("Catalog price currency does not match the requested currency")
                            if budget.comparison == "approximate":
                                return True
                            if budget.comparison == "exact":
                                target = max_amt or min_amt
                                return target is not None and price == target
                            if budget.comparison in ("min",) and min_amt is not None:
                                return price >= min_amt
                            if max_amt is not None and price > max_amt:
                                return False
                            if min_amt is not None and price < min_amt:
                                return False
                            return True
                        prices = tuple(v.price for v in p.variants if eligible(v.price))
                        if prices:
                            matching.append(p)
                            eligible_prices[p.product_id] = prices
                    except (ValueError, ArithmeticError):
                        price_evidence_error = True
                        continue
                else:
                    matching.append(p)
                    eligible_prices[p.product_id] = tuple(v.price for v in p.variants)

        if not matching and price_evidence_error:
            return ControllerTurnResult(
                session_id=session_id, turn_id=turn_id, request_revision=request_revision,
                page_epoch=page_epoch, status="error",
                spoken_response="I couldn't verify the catalog prices for that request. Please try again.",
                extracted_intent=intent, reason="Catalog price evidence is malformed or uses a different currency",
            )

        if not matching:
            resp = f"I couldn't find any products in the catalog matching '{query}'"
            if intent.budget_constraint:
                budget = intent.budget_constraint
                relation = {
                    "min": "over",
                    "approximate": "around",
                    "exact": "at",
                }.get(budget.comparison, "under")
                amount = budget.max_amount or budget.min_amount or "the requested amount"
                resp += f" {relation} {amount} {budget.currency}"
            resp += "."
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="completed",
                spoken_response=resp,
                extracted_intent=intent,
            )

        def display_price(product: ProductEvidence) -> Money:
            prices = eligible_prices.get(product.product_id, ())
            if not prices:
                raise ValueError("Product has no eligible verified price")
            return min(prices)
        if intent.budget_constraint and intent.budget_constraint.comparison in ("approximate", "exact"):
            target = Money.from_string(intent.budget_constraint.max_amount or "0", intent.budget_constraint.currency)
            matching.sort(key=lambda p: abs(display_price(p).amount - target.amount))
        elif wants_cheapest:
            matching.sort(key=lambda p: display_price(p).amount)
        names = [f"{p.title} (from {display_price(p)})" for p in matching[:3]]
        resp = f"Found {len(matching)} items: {', '.join(names)}."
        return ControllerTurnResult(
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            status="completed",
            spoken_response=resp,
            extracted_intent=intent,
            result_product_ids=tuple(p.product_id for p in matching),
        )

    def _handle_describe_and_compare(
        self,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
        intent: ShoppingIntent,
        evidence: EvidenceSnapshot,
    ) -> ControllerTurnResult:
        query = (intent.product_query or "").strip().lower()
        is_explicit_compare = (
            "compare" in query
            or "comparison" in query
            or "vs" in query
            or "difference between" in query
        )

        all_products = list(evidence.products.values())
        if not all_products:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="completed",
                spoken_response="There are no products in the catalog to describe.",
                extracted_intent=intent,
            )

        if is_explicit_compare:
            # Find which products were requested to compare
            matching: list[ProductEvidence] = []
            for p in all_products:
                if p.title.lower() in query:
                    matching.append(p)
            if len(matching) < 2 and len(all_products) >= 2:
                matching = all_products[:2]

            if len(matching) >= 2:
                p1, p2 = matching[0], matching[1]
                desc = (
                    f"Comparing {p1.title} and {p2.title}: "
                    f"{p1.title} is priced at {p1.variants[0].price} with options {', '.join(p1.options or ['standard'])}; "
                    f"{p2.title} is priced at {p2.variants[0].price} with options {', '.join(p2.options or ['standard'])}."
                )
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="completed",
                    spoken_response=desc,
                    extracted_intent=intent,
                )

        # Single product describe request (R9)
        if query:
            matching = [
                p for p in all_products
                if query in p.title.lower() or p.title.lower() in query
            ]
            if matching:
                p = matching[0]
                opts = ", ".join(p.options) if p.options else "standard"
                desc = f"{p.title} is {p.variants[0].price}, available in {opts}."
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="completed",
                    spoken_response=desc,
                    extracted_intent=intent,
                )

        # Default: describe first product
        p = all_products[0]
        opts = ", ".join(p.options) if p.options else "standard"
        desc = f"{p.title} is {p.variants[0].price}, available in {opts}."
        return ControllerTurnResult(
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            status="completed",
            spoken_response=desc,
            extracted_intent=intent,
        )

    def _handle_view_cart(
        self,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
        intent: ShoppingIntent,
        current_cart: CartSnapshot,
    ) -> ControllerTurnResult:
        total_items = sum(l.quantity for l in current_cart.lines)
        if total_items == 0:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="completed",
                spoken_response="Your cart is currently empty.",
                extracted_intent=intent,
            )

        lines_summary = [f"{l.quantity} item(s)" for l in current_cart.lines]
        resp = f"You have {total_items} items in your cart across {len(current_cart.lines)} line(s)."
        return ControllerTurnResult(
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            status="completed",
            spoken_response=resp,
            extracted_intent=intent,
        )

    async def _handle_cart_mutation(
        self,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
        intent: ShoppingIntent,
        evidence: EvidenceSnapshot,
        current_cart: CartSnapshot,
        client: ShopifyClient | None,
        now_ms: int,
    ) -> ControllerTurnResult:
        # Check if product is unresolved or missing (T-03, T-06)
        if "product_query" in intent.unresolved_fields or not intent.product_query:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="clarification_needed",
                spoken_response="Which item would you like to add or update?",
                extracted_intent=intent,
                reason="Unresolved product reference requires clarification (T-03)",
                clarification_fields=("product_query",),
            )

        qty = intent.quantity_change.value if intent.quantity_change else 1
        positive_opts = {}
        excluded_opts = {}
        for k, v in intent.selected_variant_attributes.items():
            if v.startswith("!"):
                excluded_opts[k] = v[1:].strip()
            elif v.startswith("not "):
                excluded_opts[k] = v[4:].strip()
            else:
                positive_opts[k] = v

        exact_product_id = intent.product_query if intent.product_query in evidence.products else None
        target = IntentTarget(
            product_id=exact_product_id,
            title_query=None if exact_product_id else intent.product_query,
            selected_options=positive_opts,
            excluded_options=excluded_opts,
            quantity=max(1, qty),
        )

        is_removal = intent.operation == IntentOperation.REMOVE_FROM_CART or (
            intent.quantity_change and intent.quantity_change.value == 0
        )

        variant = None
        target_line = None

        if is_removal:
            # Match existing line in current_cart by key, product_query, or variant attributes
            matching_lines: list[CartLine] = []
            if intent.target_line_key:
                matching_lines = [
                    l for l in current_cart.lines
                    if l.canonical_key == intent.target_line_key
                    or getattr(l, "shopify_line_key", None) == intent.target_line_key
                ]
            if not intent.target_line_key and intent.product_query:
                pq = intent.product_query.lower()
                matching_prod_ids = {
                    p.product_id for p in evidence.products.values()
                    if pq in p.title.lower() or p.title.lower() in pq
                }
                matching_variant_ids = {
                    v.variant_id for p in evidence.products.values()
                    if p.product_id in matching_prod_ids
                    for v in p.variants
                }
                matching_lines = [l for l in current_cart.lines if l.variant_id in matching_variant_ids]

            if not matching_lines:
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="rejected",
                    spoken_response=f"There is no {intent.product_query or 'item'} in your cart to remove.",
                    extracted_intent=intent,
                    reason="Target item for removal not found in current cart (T-05)",
                )

            if intent.selected_variant_attributes:
                filtered = []
                for line in matching_lines:
                    v_ev = None
                    for p in evidence.products.values():
                        for v in p.variants:
                            if v.variant_id == line.variant_id:
                                v_ev = v
                                break
                        if v_ev:
                            break
                    options = {k.lower(): v.lower() for k, v in v_ev.selected_options.items()} if v_ev else {}
                    if v_ev and all(options.get(k) == val for k, val in positive_opts.items()) and all(
                        k in options and options[k] != val for k, val in excluded_opts.items()
                    ):
                        filtered.append(line)
                matching_lines = filtered
                if not matching_lines:
                    return ControllerTurnResult(
                        session_id, turn_id, request_revision, page_epoch, "rejected",
                        "No cart item matches the variant you asked to remove.",
                        extracted_intent=intent, reason="Requested variant absent from cart",
                    )

            if len(matching_lines) > 1:
                opts = [l.variant_id for l in matching_lines]
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="clarification_needed",
                    spoken_response=f"You have multiple {intent.product_query} items in your cart. Which one would you like to remove?",
                    extracted_intent=intent,
                    clarification_options=tuple(opts),
                    reason="Multiple matching cart lines for removal require clarification (T-03)",
                )

            target_line = matching_lines[0]
            action = ProposedCartAction(
                operation=QuantityOperation.REMOVE,
                variant_id=target_line.variant_id,
                line_key=target_line.canonical_key,
                quantity=0,
                properties=target_line.properties,
                selling_plan_id=target_line.selling_plan_id,
            )
        else:
            # 6. Resolve through proven CatalogResolver (T-02, T-03, S-03, S-04, T-07)
            resolution = CatalogResolver.resolve(
                evidence=evidence,
                target=target,
                expected_shop_id=evidence.shop_id,
                expected_currency=evidence.currency,
            )

            if resolution.status == ResolutionStatus.CLARIFY:
                # Focused clarification question (T-03, S-04)
                missing = ", ".join(resolution.missing_options)
                opts = [f"{v.variant_title}" for v in resolution.candidate_variants[:4]]
                if missing:
                    spoken = f"Please select your {missing} for {intent.product_query or 'that product'}. Options include: {', '.join(opts)}."
                else:
                    spoken = f"Which product would you like: {', '.join(opts)}?"
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="clarification_needed",
                    spoken_response=spoken,
                    extracted_intent=intent,
                    clarification_options=tuple(opts),
                    clarification_fields=tuple(resolution.missing_options),
                    reason=resolution.reason,
                )

            elif resolution.status == ResolutionStatus.REJECT:
                # Truthful rejection (T-02, T-07, S-04)
                spoken = f"I cannot add that: {resolution.reason}."
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="rejected",
                    spoken_response=spoken,
                    extracted_intent=intent,
                    reason=resolution.reason,
                )

            # Exactly resolved variant
            variant = resolution.variant
            assert variant is not None

            # Enforce budget constraint (R4, T-06)
            if intent.budget_constraint:
                if intent.budget_constraint.scope == "unknown":
                    return ControllerTurnResult(
                        session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                        "Is that budget for each item or the total requested quantity?",
                        extracted_intent=intent, reason="Unresolved budget scope",
                        clarification_fields=("budget_scope",),
                    )
                try:
                    budget = intent.budget_constraint
                    max_money = Money.from_string(budget.max_amount, budget.currency) if budget.max_amount is not None else None
                    min_money = Money.from_string(budget.min_amount, budget.currency) if budget.min_amount is not None else None
                    budget_money = max_money or min_money
                    assert budget_money is not None
                    if variant.price.currency != budget_money.currency:
                        return ControllerTurnResult(
                            session_id=session_id,
                            turn_id=turn_id,
                            request_revision=request_revision,
                            page_epoch=page_epoch,
                            status="rejected",
                            spoken_response=f"Currency mismatch: your budget is in {budget_money.currency}, but this store prices items in {variant.price.currency}.",
                            extracted_intent=intent,
                            reason=f"Budget currency mismatch: {budget_money.currency} vs {variant.price.currency}",
                        )
                    if max_money is not None and variant.price > max_money:
                        return ControllerTurnResult(
                            session_id=session_id,
                            turn_id=turn_id,
                            request_revision=request_revision,
                            page_epoch=page_epoch,
                            status="rejected",
                            spoken_response=f"I cannot add {variant.product_title} ({variant.variant_title}) because its price of {variant.price} exceeds your budget limit of {max_money}.",
                            extracted_intent=intent,
                            reason=f"Variant price {variant.price} exceeds budget constraint {max_money} (T-06)",
                        )
                    if min_money is not None and variant.price < min_money:
                        return ControllerTurnResult(
                            session_id=session_id,
                            turn_id=turn_id,
                            request_revision=request_revision,
                            page_epoch=page_epoch,
                            status="rejected",
                            spoken_response=f"I cannot add that item because its price of {variant.price} is below your minimum of {min_money}.",
                            extracted_intent=intent,
                            reason=f"Variant price {variant.price} is below budget constraint {min_money} (T-06)",
                        )
                    total_cost = variant.price * target.quantity
                    if max_money is not None and intent.budget_constraint.scope == "total" and total_cost > max_money:
                        return ControllerTurnResult(
                            session_id=session_id,
                            turn_id=turn_id,
                            request_revision=request_revision,
                            page_epoch=page_epoch,
                            status="rejected",
                            spoken_response=f"Adding {target.quantity} of {variant.product_title} totals {total_cost}, which exceeds your budget limit of {max_money}.",
                            extracted_intent=intent,
                            reason=f"Total cost {total_cost} exceeds budget constraint {budget_money} (T-06)",
                        )
                except Exception as e:
                    return ControllerTurnResult(
                        session_id=session_id,
                        turn_id=turn_id,
                        request_revision=request_revision,
                        page_epoch=page_epoch,
                        status="rejected",
                        spoken_response=f"Invalid budget constraint: {e}",
                        extracted_intent=intent,
                        reason=f"Budget constraint parsing error: {e}",
                    )

            # 7. Map quantity operation (T-05)
            if intent.quantity_change and intent.quantity_change.mode == "set":
                q_op = QuantityOperation.SET
            else:
                q_op = QuantityOperation.INCREMENT

            action = ProposedCartAction(
                operation=q_op,
                variant_id=variant.variant_id,
                line_key=intent.target_line_key,
                quantity=target.quantity,
                properties={},  # R5: preserve genuine custom properties, do not turn variant options into properties
            )

        # 8. Re-check session freshness before CartVerifier (R2, S-09)
        is_invalid, status, reason = self._check_turn_staleness(
            session_id, turn_id, request_revision, page_epoch
        )
        if is_invalid:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status=status,
                spoken_response="Cart update aborted: superseded or cancelled.",
                reason=f"Aborted immediately before cart verification: {reason}",
            )

        verification = CartVerifier.verify_action(
            current_cart=current_cart,
            action=action,
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            now_ms=now_ms,
        )

        if not verification.allowed or not verification.command:
            spoken = f"Cart verification refused: {verification.reason}."
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="rejected",
                spoken_response=spoken,
                extracted_intent=intent,
                reason=verification.reason,
            )

        cmd = verification.command

        # 9. Re-check session freshness immediately before command authorization/dispatch (R2, S-09)
        is_invalid, status, reason = self._check_turn_staleness(
            session_id, turn_id, request_revision, page_epoch
        )
        if is_invalid:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status=status,
                spoken_response="Cart update aborted: superseded or cancelled.",
                reason=f"Aborted immediately before command dispatch: {reason}",
            )

        if not self.reconciler or not client:
            # In unit-test / offline mode without client: return authorized command directly
            item_name = f"{variant.product_title} - {variant.variant_title}" if variant else f"item {action.line_key or action.variant_id}"
            spoken = f"Prepared verified action for {item_name}."
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="completed",
                spoken_response=spoken,
                extracted_intent=intent,
                authorized_command=cmd,
            )

        receipt = self.reconciler.execute_and_reconcile(
            command=cmd,
            adapter=client,
        )

        # 10. Generate grounded response from verified receipt (S-06)
        if receipt.outcome in (ExecutionOutcome.VERIFIED_SUCCESS, ExecutionOutcome.VERIFIED_NO_OP):
            after_cart = client.read_cart()
            new_count = sum(l.quantity for l in after_cart.lines)
            item_label = variant.variant_title if variant else f"item {action.line_key or action.variant_id}"
            if cmd.operation == CommandOperation.REMOVE_LINE:
                spoken = f"Removed {item_label} from your cart. You now have {new_count} items in your cart."
            else:
                spoken = f"Added {item_label} to your cart. You now have {new_count} items in your cart."

            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="completed",
                spoken_response=spoken,
                extracted_intent=intent,
                authorized_command=cmd,
                receipt=receipt,
            )
        else:
            err_text = "; ".join(receipt.errors) if receipt.errors else receipt.verification_message
            spoken = f"Could not update cart: {err_text}."
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="rejected",
                spoken_response=spoken,
                extracted_intent=intent,
                authorized_command=cmd,
                receipt=receipt,
                reason=err_text,
            )

    def _handle_checkout(
        self,
        session_id: str,
        turn_id: str,
        request_revision: int,
        page_epoch: int,
        intent: ShoppingIntent,
        current_cart: CartSnapshot,
    ) -> ControllerTurnResult:
        # Re-check session freshness before checkout handoff (R2, S-09)
        is_invalid, status, reason = self._check_turn_staleness(
            session_id, turn_id, request_revision, page_epoch
        )
        if is_invalid:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status=status,
                spoken_response="Checkout request aborted: superseded or cancelled.",
                reason=f"Aborted before checkout: {reason}",
            )

        # Empty cart checkout refused (T-21)
        if not current_cart.lines:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="rejected",
                spoken_response="Your cart is empty. Please add items to your cart before proceeding to checkout.",
                extracted_intent=intent,
                reason="Empty cart checkout refused (T-21)",
            )

        cmd = AuthorizedCommand(
            command_id=f"cmd_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            shop_id=current_cart.shop_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            expires_at_ms=int(time.time() * 1000) + 30000,
            operation=CommandOperation.HANDOFF_TO_CHECKOUT,
            parameters={"checkout_url": "/checkout"},
            expected_cart_fingerprint=current_cart.fingerprint(),
        )

        return ControllerTurnResult(
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            status="completed",
            spoken_response="Taking you to checkout now. Please complete your payment securely on the checkout page.",
            extracted_intent=intent,
            authorized_command=cmd,
        )

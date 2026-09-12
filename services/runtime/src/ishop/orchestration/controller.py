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
from dataclasses import dataclass, field
from typing import Any

from ishop.commerce.catalog import (
    CatalogResolver,
    EvidenceSnapshot,
    IntentTarget,
    ProductEvidence,
    ResolutionResult,
    ResolutionStatus,
    VariantEvidence,
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


@dataclass(frozen=True)
class ControllerTurnResult:
    """Outcome of a single shopping dialogue turn."""

    session_id: str
    turn_id: str
    request_revision: int
    page_epoch: int
    status: str  # "completed", "clarification_needed", "rejected", "stale", "error"
    spoken_response: str
    extracted_intent: ShoppingIntent | None = None
    receipt: ExecutionReceipt | None = None
    authorized_command: AuthorizedCommand | None = None
    clarification_options: tuple[str, ...] = ()
    reason: str | None = None


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

        # Active session state tracking for revision and epoch bounds (T-11)
        self._active_session_id: str | None = None
        self._active_turn_id: str | None = None
        self._active_request_revision: int = 0
        self._active_page_epoch: int = 0

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

        # 1. Turn revision and page epoch freshness check (T-11)
        if (
            self._active_session_id == session_id
            and (
                page_epoch < self._active_page_epoch
                or request_revision < self._active_request_revision
            )
        ):
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="stale",
                spoken_response="This request was superseded by a newer turn.",
                reason=f"Stale request revision {request_revision} < active {self._active_request_revision} (T-11)",
            )

        # Update active turn pointers
        self._active_session_id = session_id
        self._active_turn_id = turn_id
        self._active_request_revision = request_revision
        self._active_page_epoch = page_epoch

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

        req = LlmIntentRequest(
            transcript=transcript,
            catalog_context=tuple(catalog_summaries),
            cart_summary=cart_summary_str,
            current_product_id=current_product_id,
            budget_currency=evidence.currency,
            turn_id=turn_id,
            request_revision=request_revision,
        )

        # 4. LLM structured intent extraction with bounded retry (T-28)
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
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="error",
                spoken_response="I had trouble processing that shopping request. Could you rephrase it?",
                reason=f"LLM interpretation failure: {err_reason}",
            )

        intent = intent_result.intent

        # 5. Dispatch based on extracted intent operation
        if intent.operation in (IntentOperation.SEARCH, IntentOperation.BROWSE):
            return self._handle_search_and_browse(
                session_id, turn_id, request_revision, page_epoch, intent, evidence
            )

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
            return await self._handle_cart_mutation(
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

        elif intent.operation == IntentOperation.REQUEST_CHECKOUT:
            return self._handle_checkout(
                session_id, turn_id, request_revision, page_epoch, intent, current_cart
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
        matching: list[ProductEvidence] = []

        for p in evidence.products.values():
            if not query or query in p.title.lower():
                # Apply budget constraint if specified (T-06)
                if intent.budget_constraint:
                    try:
                        max_amt = Money.from_string(
                            intent.budget_constraint.max_amount,
                            intent.budget_constraint.currency,
                        )
                        # Keep product if any variant is within budget
                        if any(v.price <= max_amt for v in p.variants):
                            matching.append(p)
                    except Exception:
                        matching.append(p)
                else:
                    matching.append(p)

        if not matching:
            resp = f"I couldn't find any products in the catalog matching '{query}'"
            if intent.budget_constraint:
                resp += f" under {intent.budget_constraint.max_amount} {intent.budget_constraint.currency}"
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

        names = [f"{p.title} (from {p.variants[0].price})" for p in matching[:3]]
        resp = f"Found {len(matching)} items: {', '.join(names)}."
        return ControllerTurnResult(
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            status="completed",
            spoken_response=resp,
            extracted_intent=intent,
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
        products = list(evidence.products.values())
        if "compare" in (intent.product_query or "").lower() or len(products) >= 2:
            # Two-product side-by-side comparison (WP-09 task 4)
            p1 = products[0]
            p2 = products[1] if len(products) > 1 else products[0]
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

        if products:
            p = products[0]
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

        return ControllerTurnResult(
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            status="completed",
            spoken_response="There are no products in the catalog to describe.",
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

        target = IntentTarget(
            title_query=intent.product_query,
            selected_options=positive_opts,
            excluded_options=excluded_opts,
            quantity=max(1, qty),
        )

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
            spoken = f"Please select your {missing}. Options include: {', '.join(opts)}."
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="clarification_needed",
                spoken_response=spoken,
                extracted_intent=intent,
                clarification_options=tuple(opts),
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

        # 7. Map quantity operation (T-05)
        if intent.operation == IntentOperation.REMOVE_FROM_CART or (
            intent.quantity_change and intent.quantity_change.value == 0
        ):
            q_op = QuantityOperation.REMOVE
        elif intent.quantity_change and intent.quantity_change.mode == "set":
            q_op = QuantityOperation.SET
        else:
            q_op = QuantityOperation.INCREMENT

        action = ProposedCartAction(
            operation=q_op,
            variant_id=variant.variant_id,
            line_key=intent.target_line_key,
            quantity=target.quantity,
            properties=variant.selected_options,
        )

        # 8. Verify action through CartVerifier (T-05, T-10, S-08, S-09)
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

        # 9. Execute through CommandReconciler if present
        if not self.reconciler or not client:
            # In unit-test / offline mode without client: return authorized command directly
            spoken = f"Prepared verified action for {variant.product_title} - {variant.variant_title}."
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
            if q_op == QuantityOperation.REMOVE:
                spoken = f"Removed {variant.variant_title} from your cart. You now have {new_count} items in your cart."
            else:
                spoken = f"Added {variant.variant_title} to your cart. You now have {new_count} items in your cart."

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

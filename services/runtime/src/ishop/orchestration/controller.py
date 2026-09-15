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
    ResolutionStatus,
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
)
from ishop.domain.intent import (
    BudgetConstraint,
    DecisionMode,
    IntentOperation,
    ReferenceKind,
    ResponsePurpose,
    ShoppingIntent,
    TargetReference,
)
from ishop.domain.models import (
    AuthorizedCommand,
    CartLine,
    CartSnapshot,
    CommandOperation,
    Money,
)
from ishop.llm.base import (
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProvider,
    LlmProviderError,
    LlmToolSelectionRequest,
    LlmToolSelectionResult,
    tool_arguments_for_intent,
)
from ishop.tools.registry import ToolProposal, ToolRegistry

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
    active_result_set_id: str | None = None
    result_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    selected_product_id: str | None = None
    active_constraints: dict[str, Any] = field(default_factory=dict)
    pending_clarification_intent: ShoppingIntent | None = None
    pending_allowed_answers: dict[str, dict[str, str]] = field(default_factory=dict)
    current_page: dict[str, Any] | None = None
    previous_page: dict[str, Any] | None = None
    tool_observations: list[dict[str, Any]] = field(default_factory=list)
    comparison_context: dict[str, Any] | None = None
    continuation_state: dict[str, Any] = field(default_factory=dict)

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
            "active_result_set_id": self.active_result_set_id,
            "result_sets": dict(self.result_sets),
            "selected_product_id": self.selected_product_id,
            "active_constraints": dict(self.active_constraints),
            "pending_clarification_intent": (
                self.pending_clarification_intent.to_dict()
                if self.pending_clarification_intent else None
            ),
            "pending_allowed_answers": dict(self.pending_allowed_answers),
            "current_page": self.current_page,
            "previous_page": self.previous_page,
            "tool_observations": list(self.tool_observations),
            "comparison_context": dict(self.comparison_context) if self.comparison_context else None,
            "continuation_state": dict(self.continuation_state),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControllerSessionState":
        pending = {}
        for item in data.get("pending_intents", []):
            pending[(str(item["turn_id"]), int(item["revision"]))] = ShoppingIntent.from_dict(item["intent"])
        pending_raw = data.get("pending_clarification_intent")
        comp_raw = data.get("comparison_context")
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
            active_result_set_id=data.get("active_result_set_id"),
            result_sets=dict(data.get("result_sets", {})),
            selected_product_id=data.get("selected_product_id"),
            active_constraints=dict(data.get("active_constraints", {})),
            pending_clarification_intent=(ShoppingIntent.from_dict(pending_raw) if pending_raw else None),
            pending_allowed_answers={str(key): {str(k): str(v) for k, v in value.items()}
                                     for key, value in data.get("pending_allowed_answers", {}).items()
                                     if isinstance(value, dict)},
            current_page=data.get("current_page"),
            previous_page=data.get("previous_page"),
            tool_observations=list(data.get("tool_observations", []))[-16:],
            comparison_context=dict(comp_raw) if isinstance(comp_raw, dict) else None,
            continuation_state=dict(data.get("continuation_state", {})),
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
    selected_tool: str | None = None
    result_set_id: str | None = None
    tool_request: dict[str, Any] | None = None
    failure_code: str | None = None


class ShoppingController:
    """Provider-neutral async shopping reasoning and execution controller."""

    MAX_TURN_STEPS = 8

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
        self.tool_registry = ToolRegistry()

    def get_session_state(self, session_id: str) -> ControllerSessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = ControllerSessionState(session_id=session_id)
        return self._sessions[session_id]

    def export_session_state(self, session_id: str) -> dict[str, Any]:
        return self.get_session_state(session_id).to_dict()

    def restore_session_state(self, data: dict[str, Any]) -> None:
        restored = ControllerSessionState.from_dict(data)
        self._sessions[restored.session_id] = restored

    def record_assistant_outcome(self, session_id: str, result: ControllerTurnResult) -> None:
        """Persist visible assistant meaning once, excluding silent continuation envelopes."""
        if not result.spoken_response or result.status in {"context_required", "evidence_required", "tool_required"}:
            return
        sess = self.get_session_state(session_id)
        entry = {"role": "assistant", "content": result.spoken_response.strip()[:1000]}
        if not sess.conversation_history or sess.conversation_history[-1] != entry:
            sess.conversation_history.append(entry)
            sess.conversation_history = sess.conversation_history[-8:]

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
        client: ShopifySimulator | None = None,
        current_product_id: str | None = None,
        page_context: dict[str, Any] | None = None,
        available_tools: set[str] | None = None,
        tool_observation: dict[str, Any] | None = None,
        context_phase: str = "action",
        now_ms: int | None = None,
        cart_details: list[dict[str, Any]] | None = None,
        displayed_search: dict[str, Any] | None = None,
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
        if page_context and page_context != sess.current_page:
            sess.previous_page = sess.current_page
            sess.current_page = dict(page_context)
        if displayed_search is not None:
            ordered_ids = tuple(evidence.products)
            if displayed_search.get("rendered_count") != len(ordered_ids):
                return ControllerTurnResult(
                    session_id, turn_id, request_revision, page_epoch, "error",
                    "I could not validate the displayed store results.",
                    failure_code="displayed_search_mismatch",
                )
            signature = f"{displayed_search.get('actual_url')}|{'|'.join(ordered_ids)}"
            active = sess.result_sets.get(sess.active_result_set_id or "", {})
            if active.get("display_signature") != signature:
                result_set_id = f"rs_{uuid.uuid4().hex}"
                sess.result_sets[result_set_id] = {
                    "result_set_id": result_set_id, "query": displayed_search.get("query"),
                    "product_ids": list(ordered_ids), "observed_at_ms": evidence.observed_at_ms,
                    "shop_id": evidence.shop_id, "currency": evidence.currency,
                    "entries": [{"position": index + 1, "product_id": product_id}
                                for index, product_id in enumerate(ordered_ids)],
                    "rank_reason": displayed_search.get("sort_by", "relevance"),
                    "coverage": "rendered_page", "display_signature": signature,
                    "native_search": dict(displayed_search),
                }
                sess.active_result_set_id = result_set_id
                sess.active_search_product_ids = ordered_ids
                sess.active_search_query = str(displayed_search.get("query") or "") or None
        pending_key = (turn_id, request_revision)
        for key in tuple(sess.pending_intents):
            if key != pending_key and key[1] < request_revision:
                del sess.pending_intents[key]

        is_resume = pending_key in sess.pending_intents or tool_observation is not None
        if not is_resume:
            sess.continuation_state["step_count"] = 0
            sess.conversation_history.append({"role": "user", "content": transcript.strip()})
            sess.conversation_history = sess.conversation_history[-8:]

        # Step count & no-progress bounding per turn (CS-09, CS-10)
        step_count = int(sess.continuation_state.get("step_count", 0)) + 1
        sess.continuation_state["step_count"] = step_count
        if step_count > 8:
            sess.pending_intents.pop(pending_key, None)
            sess.continuation_state.clear()
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="error",
                spoken_response="The request could not be completed within the step limit. Please try again.",
                reason="Exceeded maximum of 8 continuous reasoning/retrieval steps without completion (CS-10)",
            )

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
        cart_summary_str = None if context_phase == "decision" else ("; ".join(cart_lines_desc) if cart_lines_desc else "Empty")

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
        intent = sess.pending_intents.get(pending_key) if (evidence.query is not None or context_phase == "action") else None
        intent_result: LlmInterpretationResult | None = None
        if intent is None:
            attempts = 0
            last_err: Exception | None = None
            failure_code = "interpretation_failed"
            attempt_request = req
            while attempts <= self.max_llm_retries:
                attempts += 1
                try:
                    intent_result = await self.llm_provider.interpret_intent(attempt_request)
                    break
                except LlmProviderError as e:
                    last_err = e
                    if e.status_code == 429:
                        failure_code = "reasoning_rate_limited"
                        break
                    if e.status_code is not None:
                        failure_code = "reasoning_provider_error"
                    elif "parse or validate" in str(e).casefold() or "turndecision" in str(e).casefold():
                        failure_code = "invalid_decision"
                        attempt_request = replace(attempt_request, validation_feedback=str(e))
                    else:
                        failure_code = "reasoning_unavailable"
                    if not e.retryable or attempts > self.max_llm_retries:
                        break
                except Exception as e:
                    last_err = e
                    failure_code = "invalid_decision"
                    break
            if not intent_result:
                logger.warning(
                    "Shopping interpretation failed code=%s profile=%s turn=%s revision=%s detail=%s",
                    failure_code,
                    self.llm_provider.profile.profile_id,
                    turn_id,
                    request_revision,
                    type(last_err).__name__,
                )
                shopper_message = {
                    "reasoning_rate_limited": "I’m receiving too many requests right now. Please wait a moment and try again.",
                    "reasoning_provider_error": "My reasoning service is temporarily unavailable. Please try again shortly.",
                    "reasoning_unavailable": "My reasoning service is temporarily unavailable. Please try again shortly.",
                    "invalid_decision": "I couldn’t interpret that request reliably. Please try saying it another way.",
                }.get(failure_code, "I couldn’t process that request right now. Please try again.")
                return ControllerTurnResult(
                    session_id=session_id, turn_id=turn_id,
                    request_revision=request_revision, page_epoch=page_epoch,
                    status="error",
                    spoken_response=shopper_message,
                    reason=f"LLM interpretation failure ({failure_code})",
                    failure_code=failure_code,
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

        decision = intent_result.get_decision() if intent_result else None
        if decision is not None:
            if intent is not None and decision.target_reference is not None and intent.target_reference is None:
                intent = replace(intent, target_reference=decision.target_reference)
                decision = replace(decision, intent=intent)
            if decision.mode == DecisionMode.RESPOND:
                is_refusal = bool(decision.response_purpose and decision.response_purpose == ResponsePurpose.REFUSAL)
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="rejected" if is_refusal else "completed",
                    spoken_response=decision.response_text,
                    extracted_intent=intent,
                    reason=f"Conversational response ({decision.response_purpose.value if decision.response_purpose else 'general'}) with zero side effects",
                )
            elif decision.mode == DecisionMode.CLARIFY:
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="clarification_needed",
                    spoken_response=decision.clarification_question or decision.response_text or "Could you clarify what you would like?",
                    extracted_intent=intent,
                    clarification_options=decision.clarification_options,
                    reason="Model requested clarification before acting",
                )

            if decision.mode == DecisionMode.ACT and context_phase == "decision":
                sess.pending_intents[pending_key] = intent
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="context_required",
                    spoken_response="",
                    extracted_intent=intent,
                    reason="Shopping action requires browser context after conversation-first decision",
                )

        normalized_transcript = transcript.strip().casefold()
        if any(phrase in normalized_transcript for phrase in ("cancel", "stop", "never mind", "nevermind", "abort")):
            sess = self.get_session_state(session_id)
            sess.pending_clarification_intent = None
            sess.pending_allowed_answers.clear()
            sess.pending_intents.clear()
            return ControllerTurnResult(
                session_id=session_id, turn_id=turn_id, request_revision=request_revision,
                page_epoch=page_epoch, status="cancelled", spoken_response="Okay, I cancelled that pending request.",
                reason="Explicit pending-turn cancellation; cart unchanged",
            )

        lower_transcript = transcript.casefold()

        pending_price_choice = sess.pending_clarification_intent
        if (pending_price_choice and pending_price_choice.budget_constraint
                and pending_price_choice.budget_constraint.comparison == "approximate"):
            answer = lower_transcript.strip(" .?!")
            if answer in {"maximum", "maximum budget", "use it as a maximum", "under", "under that"}:
                old_budget = pending_price_choice.budget_constraint
                intent = replace(
                    pending_price_choice, intent_id=intent.intent_id,
                    supporting_transcript_span=transcript.strip(),
                    budget_constraint=replace(old_budget, comparison="max", min_amount=None),
                )
                sess.pending_clarification_intent = None

        if (intent.operation in {IntentOperation.SEARCH, IntentOperation.BROWSE}
                and intent.budget_constraint
                and intent.budget_constraint.comparison == "approximate"):
            sess.pending_intents.pop(pending_key, None)
            sess.pending_clarification_intent = intent
            return ControllerTurnResult(
                session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                "Native store search cannot rank by closest price. Should I use that amount as a maximum budget, or would you like to give me a price range?",
                intent, clarification_fields=("budget_constraint",),
                reason="Native search does not support nearest-price ranking",
            )

        pending_cart_choices = sess.continuation_state.pop("cart_detail_choices", None)
        cart_choice = re.fullmatch(r"(?:the )?(first|second|third|[1-9][0-9]*)(?: one| item)?[.!]?", lower_transcript.strip())
        if pending_cart_choices and cart_choice:
            word = cart_choice.group(1)
            position = {"first": 1, "second": 2, "third": 3}.get(word, int(word) if word.isdigit() else 0)
            if 1 <= position <= len(pending_cart_choices):
                intent = replace(intent, operation=IntentOperation.VIEW_CART,
                    target_reference=TargetReference(kind=ReferenceKind.CART_LINE, value=pending_cart_choices[position - 1]))

        # Presentation observations never participate in cart fingerprints or writes.
        details = []
        for entry in (cart_details or [])[:100]:
            if not isinstance(entry, dict):
                continue
            line = next((line for line in current_cart.lines
                         if str(entry.get("line_key")) == line.shopify_line_key
                         and str(entry.get("variant_id")) == line.variant_id
                         and entry.get("quantity") == line.quantity), None)
            if line is not None:
                details.append(entry)
        if intent.target_reference and intent.target_reference.kind == ReferenceKind.CART_LINE:
            ref = intent.target_reference
            matches = [entry for entry in details if str(entry.get("line_key")) == ref.value]
            if not matches and ref.position is not None and 1 <= ref.position <= len(details):
                matches = [details[ref.position - 1]]
            if not matches and len(details) == 1 and not pending_cart_choices and ref.position is None:
                matches = details
            if not matches and pending_cart_choices:
                return ControllerTurnResult(
                    session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                    "That cart item has changed or is no longer available. Please ask to see the current cart.", intent)
            if intent.operation in {IntentOperation.DESCRIBE_PRODUCT, IntentOperation.VIEW_CART}:
                if len(matches) != 1 and len(current_cart.lines) > 1:
                    sess.continuation_state["cart_detail_choices"] = [entry["line_key"] for entry in details]
                    return ControllerTurnResult(
                        session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                        "Which cart item would you like details for? " + " ".join(
                            f"{index}. {entry.get('title', 'Item')}" for index, entry in enumerate(details, 1)),
                        intent, clarification_fields=("cart_line",))
                details = matches or details
                intent = replace(intent, operation=IntentOperation.VIEW_CART, unresolved_fields=())

        # Resolve provider-proposed typed references from trusted session/page state.
        # The model proposes the reference kind; deterministic state owns identity.
        if intent.target_reference is not None:
            target_ref = intent.target_reference
            if target_ref.kind == ReferenceKind.CURRENT_PAGE:
                trusted_page_product = current_product_id or (page_context or {}).get("product_id")
                if trusted_page_product:
                    intent = replace(intent, product_query=str(trusted_page_product))
            elif target_ref.kind == ReferenceKind.RESULT_POSITION and target_ref.position is not None:
                position = target_ref.position
                result_ids = sess.active_search_product_ids
                if target_ref.result_set_id:
                    snapshot = sess.result_sets.get(target_ref.result_set_id, {})
                    result_ids = tuple(str(value) for value in snapshot.get("product_ids", []))
                if 1 <= position <= len(result_ids):
                    intent = replace(intent, product_query=result_ids[position - 1])
            elif target_ref.kind == ReferenceKind.OBSERVED_PRODUCT and target_ref.value:
                observed_id = next(
                    (
                        product.product_id for product in evidence.products.values()
                        if product.product_id == target_ref.value or product_title_matches(target_ref.value, product.title)
                    ),
                    None,
                )
                if observed_id:
                    intent = replace(intent, product_query=observed_id)
        pending_clarification = sess.pending_clarification_intent
        starts_new_read = any(
            re.search(rf"\b{word}\b", lower_transcript)
            for word in ("find", "search", "show", "browse", "compare", "describe", "open")
        )
        if pending_clarification is not None and not starts_new_read:
            answer = transcript.strip().casefold()
            matched_attributes: dict[str, str] = dict(sess.pending_allowed_answers.get(answer, {}))
            for product in evidence.products.values():
                if (pending_clarification.product_query
                        and pending_clarification.product_query != product.product_id
                        and not product_title_matches(pending_clarification.product_query, product.title)):
                    continue
                for variant in product.variants:
                    for key, value in variant.selected_options.items():
                        if answer == str(value).strip().casefold():
                            matched_attributes[str(key).casefold()] = str(value).casefold()
            if matched_attributes:
                merged_attributes = dict(pending_clarification.selected_variant_attributes)
                merged_attributes.update(matched_attributes)
                intent = replace(
                    pending_clarification,
                    intent_id=intent.intent_id,
                    selected_variant_attributes=merged_attributes,
                    supporting_transcript_span=transcript.strip(),
                    original_language_wording=intent.original_language_wording,
                    unresolved_fields=tuple(
                        field_name for field_name in pending_clarification.unresolved_fields
                        if field_name.casefold() not in matched_attributes
                    ),
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

        # 1. Comparison detection
        is_comparison_turn = bool(re.search(r"\bcompare\b", lower_transcript))
        word_ordinals = {
            "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "sixth": 6, "seventh": 7, "eighth": 8
        }
        if is_comparison_turn:
            matched_ordinals = []
            for word, val in word_ordinals.items():
                if re.search(rf"\b{word}\b", lower_transcript):
                    matched_ordinals.append(val)
            for m in re.finditer(r"\b(?:item|result|option|number)\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\b", lower_transcript):
                matched_ordinals.append(int(m.group(1) or m.group(2)))
            matched_ordinals = sorted(set(matched_ordinals))
            if len(matched_ordinals) >= 2 and sess.active_search_product_ids:
                p_ids = sess.active_search_product_ids
                c1_idx = matched_ordinals[0] - 1
                c2_idx = matched_ordinals[1] - 1
                if 0 <= c1_idx < len(p_ids) and 0 <= c2_idx < len(p_ids):
                    id1 = p_ids[c1_idx]
                    id2 = p_ids[c2_idx]
                    sess.comparison_context = {"product_ids": [id1, id2]}
                    intent = replace(intent, operation=IntentOperation.DESCRIBE_PRODUCT, product_query=f"{id1} and {id2}")

        # Follow-up: "open the cheaper one" / "take me to the cheaper one" / "cheaper one"
        wants_cheaper_followup = any(phrase in lower_transcript for phrase in ("cheaper one", "cheapest one", "cheaper item", "cheaper"))
        if wants_cheaper_followup:
            target_prod = None
            if sess.comparison_context and "product_ids" in sess.comparison_context:
                comp_ids = sess.comparison_context["product_ids"]
                if len(comp_ids) == 2 and all(cid in evidence.products for cid in comp_ids):
                    prod1 = evidence.products[comp_ids[0]]
                    prod2 = evidence.products[comp_ids[1]]
                    price1 = min(v.price for v in prod1.variants)
                    price2 = min(v.price for v in prod2.variants)
                    if price1 < price2:
                        target_prod = prod1
                    elif price2 < price1:
                        target_prod = prod2
                    else:
                        target_prod = None
                        return ControllerTurnResult(
                            session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                            f"Both {prod1.title} and {prod2.title} are priced at {price1}. Which one would you like to open?",
                            intent,
                            reason="Compared products have identical prices; tie requires clarification",
                            clarification_options=(prod1.title, prod2.title),
                        )
            elif sess.active_search_product_ids:
                search_prods = [evidence.products[pid] for pid in sess.active_search_product_ids if pid in evidence.products]
                if search_prods:
                    search_prods.sort(key=lambda p: min(v.price for v in p.variants))
                    target_prod = search_prods[0]

            if target_prod is not None:
                wants_nav = any(phrase in lower_transcript for phrase in ("open", "take me", "go to", "show", "navigate", "preview"))
                intent = replace(
                    intent,
                    operation=IntentOperation.NAVIGATE if wants_nav else IntentOperation.DESCRIBE_PRODUCT,
                    product_query=target_prod.product_id,
                    unresolved_fields=tuple(field for field in intent.unresolved_fields if field != "product_query"),
                )

        # 2. Ordinal resolution
        ordinal_match = re.search(
            r"\b(?:choose|result|option|item|number)\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+(?:one|result|item|product)?\b",
            lower_transcript,
        )
        ordinal = int(ordinal_match.group(1) or ordinal_match.group(2)) if ordinal_match else next(
            (value for word, value in word_ordinals.items() if re.search(rf"\b{word}\b", lower_transcript)), None
        )
        refers_to_earlier_search = any(phrase in lower_transcript for phrase in ("earlier search", "previous search", "first search"))

        if (ordinal is not None and not is_comparison_turn
                and not (intent.target_reference and intent.target_reference.kind == ReferenceKind.CART_LINE)):
            product_ids = sess.active_search_product_ids
            if refers_to_earlier_search:
                historical_sets = [
                    s for s_id, s in sess.result_sets.items()
                    if s_id != sess.active_result_set_id and s.get("product_ids")
                ]
                if len(historical_sets) > 1:
                    return ControllerTurnResult(
                        session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                        "Which earlier search would you like to choose from?", intent,
                        reason="Multiple earlier search result sets exist",
                    )
                elif len(historical_sets) == 1:
                    product_ids = tuple(historical_sets[0]["product_ids"])

            if ordinal < 1 or ordinal > len(product_ids):
                return ControllerTurnResult(
                    session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                    "That result number is no longer available. Please choose from the current results.", intent,
                    reason="Ordinal reference is outside the active immutable result set",
                    clarification_options=tuple(str(index) for index in range(1, len(product_ids) + 1)),
                )
            sess.selected_product_id = product_ids[ordinal - 1]
            wants_nav = any(phrase in lower_transcript for phrase in ("take me to", "open", "go to", "show page", "page of", "preview"))
            intent_op = IntentOperation.NAVIGATE if wants_nav else intent.operation
            intent = replace(intent, operation=intent_op, product_query=sess.selected_product_id,
                             unresolved_fields=tuple(field for field in intent.unresolved_fields if field != "product_query"))

        # 3. Compound search & open (e.g. "Find the cheapest available snowboard and open it")
        if (
            any(phrase in lower_transcript for phrase in ("open it", "open the cheapest", "take me to it", "open"))
            and any(phrase in lower_transcript for phrase in ("cheapest", "lowest", "least expensive"))
        ):
            target_query = intent.product_query if (intent.product_query and intent.product_query not in ("it", "compare")) else None
            matching_available = [
                p for p in evidence.products.values()
                if (not target_query or product_title_matches(target_query, p.title))
                and any(v.available_for_sale for v in p.variants)
            ]
            if matching_available:
                matching_available.sort(key=lambda p: min(v.price.amount for v in p.variants if v.available_for_sale))
                cheapest_item = matching_available[0]
                intent = replace(
                    intent,
                    operation=IntentOperation.NAVIGATE,
                    product_query=cheapest_item.product_id,
                    unresolved_fields=tuple(field for field in intent.unresolved_fields if field != "product_query"),
                )

        explicit_current_page = any(
            phrase in lower_transcript
            for phrase in (
                "this product", "this item", "this one", "current product", "one on this page",
                "on this page", "on its page", "page we are on", "page we're on",
            )
        )

        refers_to_selected_result = bool(re.search(
            r"\b(?:it|its|that one|the one|that product|that item)\b", lower_transcript
        ))
        if refers_to_selected_result and not intent.product_query:
            if (
                current_product_id
                and sess.selected_product_id
                and current_product_id != sess.selected_product_id
                and not explicit_current_page
                and not ("from" in lower_transcript and "search" in lower_transcript)
            ):
                return ControllerTurnResult(
                    session_id, turn_id, request_revision, page_epoch, "clarification_needed",
                    "Would you like details for the product on this page or the one from your search?",
                    intent,
                    reason="Ambiguous reference 'it' conflicts between current page and search selection",
                    clarification_options=("This product on page", "Item from search"),
                )
            elif sess.selected_product_id and sess.selected_product_id in evidence.products:
                intent = replace(
                    intent,
                    product_query=sess.selected_product_id,
                    unresolved_fields=tuple(field for field in intent.unresolved_fields if field != "product_query"),
                )

        if intent.operation in {IntentOperation.SEARCH, IntentOperation.BROWSE}:
            reset_budget = any(phrase in lower_transcript for phrase in ("no budget", "remove the price limit", "any price", "reset price"))
            changes_subject = bool(starts_new_read and intent.product_query and sess.active_search_query
                                   and intent.product_query.casefold() != sess.active_search_query.casefold())
            if reset_budget or changes_subject:
                sess.active_constraints.pop("budget", None)
            if intent.budget_constraint is not None:
                sess.active_constraints["budget"] = intent.budget_constraint.to_dict()
            elif "budget" in sess.active_constraints and not reset_budget:
                intent = replace(intent, budget_constraint=BudgetConstraint(**sess.active_constraints["budget"]))

        implicit_page_quantity = (
            intent.operation in {IntentOperation.ADD_TO_CART, IntentOperation.UPDATE_QUANTITY}
            and intent.quantity_change is not None
            and not intent.product_query
        )
        if (explicit_current_page or implicit_page_quantity) and not intent.product_query and current_product_id:
            if current_product_id in evidence.products:
                intent = replace(
                    intent,
                    product_query=current_product_id,
                    unresolved_fields=tuple(field for field in intent.unresolved_fields if field != "product_query"),
                )

        if intent.product_query and intent.selected_variant_attributes:
            product_matches = [
                product for product in evidence.products.values()
                if product.product_id == intent.product_query or product_title_matches(intent.product_query, product.title)
            ]
            if len(product_matches) == 1:
                valid_option_names = {name.casefold() for name in product_matches[0].options}
                valid_option_names.update(
                    str(name).casefold()
                    for variant in product_matches[0].variants
                    for name in variant.selected_options
                )
                grounded_attributes = {
                    name: value for name, value in intent.selected_variant_attributes.items()
                    if name.casefold() in valid_option_names
                }
                if grounded_attributes != intent.selected_variant_attributes:
                    intent = replace(intent, selected_variant_attributes=grounded_attributes)

        if starts_new_read and intent.operation in {
            IntentOperation.SEARCH,
            IntentOperation.BROWSE,
            IntentOperation.DESCRIBE_PRODUCT,
        }:
            sess.pending_clarification_intent = None
            sess.pending_allowed_answers.clear()

        if (
            intent.operation
            in {
                IntentOperation.CHECK_AVAILABILITY,
                IntentOperation.ADD_TO_CART,
                IntentOperation.UPDATE_QUANTITY,
                IntentOperation.REMOVE_FROM_CART,
                IntentOperation.NAVIGATE,
                IntentOperation.SHOW_VARIANT,
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

        is_home = (intent.product_query or "").strip().casefold() in ("home", "home page", "storefront root", "/") or any(p in lower_transcript for p in ("go home", "take me home", "home page"))
        is_back = (intent.product_query or "").strip().casefold() in ("back", "previous", "previous page") or any(p in lower_transcript for p in ("go back", "take me back", "previous page"))
        if intent.operation == IntentOperation.NAVIGATE and (is_home or is_back):
            if is_home:
                command = AuthorizedCommand(
                    command_id=f"cmd_{uuid.uuid4().hex}", session_id=session_id,
                    shop_id=current_cart.shop_id, turn_id=turn_id,
                    request_revision=request_revision, page_epoch=page_epoch,
                    expires_at_ms=now + 30_000,
                    operation=CommandOperation.NAVIGATE_STOREFRONT,
                    parameters={"url": "/"},
                    expected_cart_fingerprint=current_cart.fingerprint(),
                )
                return ControllerTurnResult(
                    session_id=session_id, turn_id=turn_id,
                    request_revision=request_revision, page_epoch=page_epoch,
                    status="completed",
                    spoken_response="Navigating to the home page.",
                    extracted_intent=intent, authorized_command=command,
                    selected_tool=None,
                )
            if is_back:
                prev_path = sess.previous_page.get("path") if sess.previous_page else (page_context.get("previous_path") if page_context else None)
                if prev_path and isinstance(prev_path, str) and prev_path.startswith("/") and not prev_path.startswith("/checkout") and not prev_path.startswith("/account"):
                    command = AuthorizedCommand(
                        command_id=f"cmd_{uuid.uuid4().hex}", session_id=session_id,
                        shop_id=current_cart.shop_id, turn_id=turn_id,
                        request_revision=request_revision, page_epoch=page_epoch,
                        expires_at_ms=now + 30_000,
                        operation=CommandOperation.NAVIGATE_STOREFRONT,
                        parameters={"url": prev_path},
                        expected_cart_fingerprint=current_cart.fingerprint(),
                    )
                    return ControllerTurnResult(
                        session_id=session_id, turn_id=turn_id,
                        request_revision=request_revision, page_epoch=page_epoch,
                        status="completed",
                        spoken_response="Navigating back to the previous page.",
                        extracted_intent=intent, authorized_command=command,
                        selected_tool=None,
                    )
                else:
                    return ControllerTurnResult(
                        session_id=session_id, turn_id=turn_id,
                        request_revision=request_revision, page_epoch=page_epoch,
                        status="completed",
                        spoken_response="I don't have a previous store page recorded in this session. Would you like to go to the home page?",
                        extracted_intent=intent,
                        reason="No valid previous store page recorded in session",
                    )

        expected_tools = {
            IntentOperation.SEARCH: {"search_catalog"},
            IntentOperation.BROWSE: {"browse_store", "search_catalog"},
            IntentOperation.DESCRIBE_PRODUCT: {"get_product"},
            IntentOperation.CHECK_AVAILABILITY: {"get_product"},
            IntentOperation.VIEW_CART: {"get_cart"},
            IntentOperation.ADD_TO_CART: {"update_cart"},
            IntentOperation.UPDATE_QUANTITY: {"update_cart"},
            IntentOperation.REMOVE_FROM_CART: {"update_cart"},
            IntentOperation.NAVIGATE: {"get_product", "show_variant", "browse_store"},
            IntentOperation.REQUEST_CHECKOUT: {"proceed_to_checkout"},
            IntentOperation.CANCEL_CART: {"cancel_cart"},
            IntentOperation.MANAGE_ORDERS: {"manage_orders"},
            IntentOperation.STORE_INFORMATION: {"search_shop_policies_and_faqs"},
            IntentOperation.SHOW_VARIANT: {"show_variant"},
        }
        qualified_names = available_tools
        qualified = self.tool_registry.qualified(available=qualified_names)
        if not expected_tools[intent.operation].intersection(tool.name for tool in qualified):
            return ControllerTurnResult(
                session_id=session_id, turn_id=turn_id,
                request_revision=request_revision, page_epoch=page_epoch,
                status="error", spoken_response="That store capability is unavailable right now.",
                extracted_intent=intent, reason="No qualified tool is available for the validated intent",
            )
        selection = None
        proposal = None
        selection_error: Exception | None = None
        selection_request = LlmToolSelectionRequest(
            intent=intent,
            qualified_tools=tuple(tool.to_dict() for tool in qualified),
            resolved_context={
                "current_product_id": current_product_id,
                "evidence_product_ids": list(evidence.products),
                "active_result_product_ids": list(sess.active_search_product_ids),
            },
            turn_id=turn_id,
            request_revision=request_revision,
        )
        for _attempt in range(2):
            try:
                saved_selection = sess.continuation_state.get("selected_tool")
                if (tool_observation is not None and saved_selection
                        and saved_selection.get("turn_id") == turn_id
                        and saved_selection.get("revision") == request_revision):
                    selection = LlmToolSelectionResult(
                        saved_selection["name"], saved_selection["arguments"], "Resumed validated step", "")
                else:
                    selection = await self.llm_provider.select_tool(selection_request)
                if selection.tool_name not in expected_tools[intent.operation]:
                    raise ValueError("selected tool does not match the validated shopping intent")
                proposal = self.tool_registry.hydrate_and_validate(
                    ToolProposal(selection.tool_name, selection.arguments, selection.rationale),
                    tool_arguments_for_intent(intent, selection.tool_name),
                    available=qualified_names,
                )
                break
            except (LlmProviderError, ValueError, KeyError) as exc:
                selection_error = exc
                if isinstance(exc, LlmProviderError) and exc.status_code == 429:
                    return ControllerTurnResult(
                        session_id, turn_id, request_revision, page_epoch, "error",
                        "My reasoning service is temporarily rate limited. Please wait before trying again.",
                        intent, failure_code="reasoning_rate_limited")
                selection_request = replace(selection_request, validation_feedback=str(exc))
                selection = None
                proposal = None
        if selection is None or proposal is None:
            logger.warning(
                "Shopping tool selection failed profile=%s turn=%s revision=%s operation=%s detail=%s",
                self.llm_provider.profile.profile_id,
                turn_id,
                request_revision,
                intent.operation.value,
                type(selection_error).__name__,
            )
            return ControllerTurnResult(
                session_id=session_id, turn_id=turn_id,
                request_revision=request_revision, page_epoch=page_epoch,
                status="error", spoken_response="I could not safely choose a shopping action. Please try again.",
                extracted_intent=intent, reason="Tool selection rejected after validated repair attempt",
                failure_code="tool_selection_failed",
            )

        read_tools = {"search_catalog", "browse_store", "get_product", "search_shop_policies_and_faqs"}
        if tool_observation is not None:
            if (
                not isinstance(tool_observation, dict)
                or tool_observation.get("tool") != selection.tool_name
                or not isinstance(tool_observation.get("ok"), bool)
                or not isinstance(tool_observation.get("data", {}), dict)
            ):
                return ControllerTurnResult(
                    session_id, turn_id, request_revision, page_epoch, "error",
                    "I could not validate the store response for that request.", intent,
                    reason="Tool observation did not match the selected qualified tool",
                    selected_tool=selection.tool_name,
                )
            observation_record = {
                "tool": selection.tool_name,
                "ok": bool(tool_observation["ok"]),
                "source": str(tool_observation.get("source", "unknown"))[:80],
                "data": dict(tool_observation.get("data", {})),
                "error": str(tool_observation.get("error", ""))[:300] or None,
                "observed_at_ms": int(tool_observation.get("observed_at_ms", now)),
            }
            sess.tool_observations.append(observation_record)
            sess.tool_observations = sess.tool_observations[-16:]
            if not observation_record["ok"]:
                sess.pending_intents.pop(pending_key, None)
                return ControllerTurnResult(
                    session_id, turn_id, request_revision, page_epoch, "error",
                    "That store capability is unavailable right now.", intent,
                    reason=observation_record["error"] or "Qualified browser tool failed",
                    selected_tool=selection.tool_name,
                )

        reference = (intent.product_query or "").casefold()
        if sess.comparison_context and sess.comparison_context.get("product_ids"):
            comp_ids = sess.comparison_context["product_ids"]
            has_product = all(cid in evidence.products for cid in comp_ids)
        elif reference:
            has_product = any(product.product_id == intent.product_query or product_title_matches(reference, product.title)
                              for product in evidence.products.values())
        else:
            has_product = bool(evidence.products)

        observed_tool = str((tool_observation or {}).get("tool", ""))
        observed_ok = bool((tool_observation or {}).get("ok"))
        read_satisfied = {
            "search_catalog": (
                (observed_tool == "search_catalog" and observed_ok)
                or (available_tools is None and bool(evidence.products))
            ),
            "browse_store": tool_observation is not None or (available_tools is None and evidence.query is not None and bool(evidence.products)),
            "get_product": has_product,
            "search_shop_policies_and_faqs": tool_observation is not None,
        }
        needs_browser_read = selection.tool_name in read_tools and not read_satisfied[selection.tool_name]
        if needs_browser_read:
            sess.continuation_state["selected_tool"] = {
                "turn_id": turn_id, "revision": request_revision,
                "name": selection.tool_name, "arguments": dict(proposal.arguments),
            }
            sess.pending_intents[pending_key] = intent
            return ControllerTurnResult(
                session_id, turn_id, request_revision, page_epoch,
                ("evidence_required" if selection.tool_name in {"search_catalog", "get_product"} else "tool_required"),
                "", intent,
                reason="Qualified browser read is required before responding",
                evidence_query=(intent.product_query if selection.tool_name in {"search_catalog", "get_product"} else None),
                selected_tool=selection.tool_name,
                tool_request={"name": selection.tool_name, "arguments": dict(proposal.arguments)},
            )

        sess.pending_intents.pop(pending_key, None)

        # 5. Dispatch based on extracted intent operation
        if intent.operation in (IntentOperation.SEARCH, IntentOperation.BROWSE):
            collections = (tool_observation or {}).get("data", {}).get("collections", [])
            if selection.tool_name == "browse_store" and collections:
                names = [str(item.get("title", "")).strip() for item in collections if isinstance(item, dict) and item.get("title")]
                return ControllerTurnResult(
                    session_id, turn_id, request_revision, page_epoch, "completed",
                    f"Store collections: {', '.join(names[:8])}.", intent,
                    reason=f"Observed {len(names)} collection references",
                    selected_tool=selection.tool_name,
                )
            result = self._handle_search_and_browse(
                session_id, turn_id, request_revision, page_epoch, intent, evidence,
                native_search=(tool_observation or {}).get("data", {}).get("native_search"),
            )
            if result.status == "completed" and result.result_product_ids:
                sess.active_search_query = (intent.product_query or sess.active_search_query or "").strip() or None
                sess.active_search_product_ids = result.result_product_ids
                if asks_for_result_ranking and result.result_product_ids:
                    sess.selected_product_id = result.result_product_ids[0]
                result_set_id = f"rs_{uuid.uuid4().hex}"
                sess.active_result_set_id = result_set_id
                sess.result_sets[result_set_id] = {
                    "result_set_id": result_set_id,
                    "query": sess.active_search_query,
                    "product_ids": list(result.result_product_ids),
                    "observed_at_ms": now,
                    "shop_id": evidence.shop_id,
                    "currency": evidence.currency,
                    "entries": [
                        {"position": index + 1, "product_id": product_id,
                         "eligible_variant_ids": [variant.variant_id for variant in evidence.products[product_id].variants
                                                  if variant.available_for_sale]}
                        for index, product_id in enumerate(result.result_product_ids)
                        if product_id in evidence.products
                    ],
                    "rank_reason": ("closest_to_price" if intent.budget_constraint and intent.budget_constraint.comparison == "approximate"
                                    else "cheapest" if any(word in lower_transcript for word in ("cheapest", "lowest", "least expensive"))
                                    else "store_order"),
                    "coverage": (tool_observation or {}).get("data", {}).get("coverage", "bounded_snapshot"),
                    "native_search": (tool_observation or {}).get("data", {}).get("native_search"),
                    "display_signature": (
                        f"{(tool_observation or {}).get('data', {}).get('native_search', {}).get('actual_url')}|"
                        f"{'|'.join(result.result_product_ids)}"
                        if (tool_observation or {}).get("data", {}).get("native_search") else None
                    ),
                }
                if len(sess.result_sets) > 8:
                    oldest = next(iter(sess.result_sets))
                    if oldest != result_set_id:
                        del sess.result_sets[oldest]
            return replace(result, selected_tool=selection.tool_name, result_set_id=sess.active_result_set_id)

        elif intent.operation == IntentOperation.DESCRIBE_PRODUCT:
            return replace(self._handle_describe_and_compare(
                session_id, turn_id, request_revision, page_epoch, intent, evidence
            ), selected_tool=selection.tool_name)

        elif intent.operation == IntentOperation.VIEW_CART:
            return replace(self._handle_view_cart(
                session_id, turn_id, request_revision, page_epoch, intent, current_cart, details
            ), selected_tool=selection.tool_name)

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
                sess.pending_clarification_intent = replace(
                    intent,
                    unresolved_fields=tuple(dict.fromkeys((*intent.unresolved_fields, *result.clarification_fields))),
                )
                allowed_answers: dict[str, dict[str, str]] = {}
                for product in evidence.products.values():
                    if (intent.product_query and intent.product_query != product.product_id
                            and not product_title_matches(intent.product_query, product.title)):
                        continue
                    for variant in product.variants:
                        for key, value in variant.selected_options.items():
                            if not result.clarification_fields or key.casefold() in {field.casefold() for field in result.clarification_fields}:
                                allowed_answers[str(value).strip().casefold()] = {str(key).casefold(): str(value).casefold()}
                sess.pending_allowed_answers = allowed_answers
            elif result.status != "evidence_required":
                sess.pending_clarification_intent = None
                sess.pending_allowed_answers.clear()
            return replace(result, selected_tool=selection.tool_name)

        elif intent.operation == IntentOperation.REQUEST_CHECKOUT:
            return replace(self._handle_checkout(
                session_id, turn_id, request_revision, page_epoch, intent, current_cart
            ), selected_tool=selection.tool_name)

        elif intent.operation == IntentOperation.CANCEL_CART:
            if not current_cart.lines:
                return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                    "completed", "Your cart is already empty.", intent, selected_tool=selection.tool_name)
            command = AuthorizedCommand(
                command_id=f"cmd_{uuid.uuid4().hex}", session_id=session_id, shop_id=current_cart.shop_id,
                turn_id=turn_id, request_revision=request_revision, page_epoch=page_epoch,
                expires_at_ms=now + 30_000, operation=CommandOperation.CLEAR_CART,
                parameters={"explicit_whole_cart": True}, expected_cart_fingerprint=current_cart.fingerprint(),
            )
            return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                "completed", "I’m clearing the entire cart and will verify it.", intent,
                authorized_command=command, selected_tool=selection.tool_name)

        elif intent.operation == IntentOperation.MANAGE_ORDERS:
            command = AuthorizedCommand(
                command_id=f"cmd_{uuid.uuid4().hex}", session_id=session_id, shop_id=current_cart.shop_id,
                turn_id=turn_id, request_revision=request_revision, page_epoch=page_epoch,
                expires_at_ms=now + 30_000, operation=CommandOperation.MANAGE_ORDERS,
                parameters={"url": "/account"}, expected_cart_fingerprint=current_cart.fingerprint(),
            )
            return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                "completed", "Opening Shopify account and order history. Shopify may ask you to sign in.", intent,
                authorized_command=command, selected_tool=selection.tool_name)

        elif intent.operation == IntentOperation.STORE_INFORMATION:
            entries = (tool_observation or {}).get("data", {}).get("entries", [])
            usable = [entry for entry in entries if isinstance(entry, dict) and entry.get("text") and entry.get("url")]
            if not usable:
                return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                    "completed", "I could not find a current store policy that answers that question.", intent,
                    reason="Store policy tool returned no attributable evidence", selected_tool=selection.tool_name)
            entry = usable[0]
            text = re.sub(r"\s+", " ", str(entry["text"])).strip()[:500]
            sentences = re.split(r"(?<=[.!?])\s+", text)
            text = " ".join(sentence for sentence in sentences if not any(pattern.search(sentence) for pattern in INJECTION_PATTERNS)).strip()
            if not text:
                return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                    "completed", "I could not find a safe, current store policy answer for that question.", intent,
                    reason="Store policy content contained only untrusted instructions", selected_tool=selection.tool_name)
            title = str(entry.get("title", "Store policy")).strip()[:120]
            url = str(entry["url"]).strip()[:500]
            return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                "completed", f"According to {title}: {text} Source: {url}", intent,
                reason="Answer grounded in current store policy observation", selected_tool=selection.tool_name)

        elif intent.operation == IntentOperation.SHOW_VARIANT:
            matches = [
                p for p in evidence.products.values()
                if intent.product_query
                and (p.product_id == intent.product_query or product_title_matches(intent.product_query, p.title))
            ]
            if len(matches) != 1 or not matches[0].url:
                return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                    "clarification_needed", "Which product variant would you like me to show?", intent,
                    clarification_options=tuple(p.title for p in matches[:4]), selected_tool=selection.tool_name)
            resolution = CatalogResolver.resolve(
                evidence=evidence,
                target=IntentTarget(title_query=intent.product_query, selected_options=intent.selected_variant_attributes),
            )
            if resolution.status != ResolutionStatus.RESOLVED or resolution.variant is None:
                return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                    "clarification_needed", resolution.clarification_question or "Which options should I select?", intent,
                    clarification_options=resolution.clarification_options, clarification_fields=resolution.missing_options,
                    selected_tool=selection.tool_name)
            separator = "&" if "?" in matches[0].url else "?"
            command = AuthorizedCommand(
                command_id=f"cmd_{uuid.uuid4().hex}", session_id=session_id, shop_id=current_cart.shop_id,
                turn_id=turn_id, request_revision=request_revision, page_epoch=page_epoch,
                expires_at_ms=now + 30_000, operation=CommandOperation.NAVIGATE_STOREFRONT,
                parameters={"url": f"{matches[0].url}{separator}variant={resolution.variant.variant_id}"},
                expected_cart_fingerprint=current_cart.fingerprint(),
            )
            return ControllerTurnResult(session_id, turn_id, request_revision, page_epoch,
                "completed", f"Opening {matches[0].title} with that variant selected.", intent,
                authorized_command=command, selected_tool=selection.tool_name)

        elif intent.operation == IntentOperation.NAVIGATE:
            matches = [
                p for p in evidence.products.values()
                if intent.product_query
                and (p.product_id == intent.product_query or product_title_matches(intent.product_query, p.title))
            ]
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
                selected_tool=selection.tool_name,
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
        native_search: dict[str, Any] | None = None,
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
        # Native storefront observation order owns positional references. The
        # browser applies any supported price sort before producing evidence.
        names = [f"{i+1}. {p.title} (from {display_price(p)})" for i, p in enumerate(matching[:3])]
        if wants_cheapest and (native_search or {}).get("sort_by") == "price-ascending":
            others = f" Other matches in price order: {', '.join(names[1:])}." if len(names) > 1 else ""
            resp = f"Cheapest among {len(matching)} matching items is {names[0]}.{others}"
        else:
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
        sess = self.get_session_state(session_id)
        is_explicit_compare = (
            "compare" in query
            or "comparison" in query
            or "vs" in query
            or "difference between" in query
            or bool(sess.comparison_context and sess.comparison_context.get("product_ids"))
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
            matching: list[ProductEvidence] = []
            comp_ids = (sess.comparison_context or {}).get("product_ids", [])
            for cid in comp_ids:
                if cid in evidence.products:
                    matching.append(evidence.products[cid])
            if len(matching) < 2:
                for p in all_products:
                    if p.title.lower() in query or p.product_id in query:
                        if p not in matching:
                            matching.append(p)
            if len(matching) < 2:
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="clarification_needed",
                    spoken_response="Which two products would you like me to compare?",
                    extracted_intent=intent,
                    clarification_fields=("comparison_selection",),
                    reason="Two grounded comparison targets were not available",
                )

            if len(matching) >= 2:
                p1, p2 = matching[0], matching[1]
                sess.comparison_context = {"product_ids": [p1.product_id, p2.product_id]}
                def comparison_price(product: ProductEvidence) -> Money:
                    available = [variant.price for variant in product.variants if variant.available_for_sale]
                    return min(available or [variant.price for variant in product.variants])

                def useful_options(product: ProductEvidence) -> str:
                    names = [name for name in product.options if name.casefold() not in {"title", "default title"}]
                    return ", ".join(names) if names else "a standard configuration"

                p1_opts = useful_options(p1)
                p2_opts = useful_options(p2)
                desc = (
                    f"Comparing 1. {p1.title} and 2. {p2.title}: "
                    f"{p1.title} starts at {comparison_price(p1)} and has {p1_opts}; "
                    f"{p2.title} starts at {comparison_price(p2)} and has {p2_opts}."
                )
                return ControllerTurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    request_revision=request_revision,
                    page_epoch=page_epoch,
                    status="completed",
                    spoken_response=desc,
                    extracted_intent=intent,
                    result_product_ids=(p1.product_id, p2.product_id),
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
        details: list[dict[str, Any]] | None = None,
    ) -> ControllerTurnResult:
        total_items = sum(l.quantity for l in current_cart.lines)
        lower_span = f"{intent.supporting_transcript_span} {intent.original_language_wording}".casefold()
        wants_open = any(phrase in lower_span for phrase in ("open cart", "open my cart", "take me to my cart", "take me to cart", "go to cart", "navigate to cart"))
        command = None
        if wants_open:
            command = AuthorizedCommand(
                command_id=f"cmd_{uuid.uuid4().hex}", session_id=session_id,
                shop_id=current_cart.shop_id, turn_id=turn_id,
                request_revision=request_revision, page_epoch=page_epoch,
                expires_at_ms=int(time.time() * 1000) + 30_000,
                operation=CommandOperation.NAVIGATE_STOREFRONT,
                parameters={"url": "/cart"},
                expected_cart_fingerprint=current_cart.fingerprint(),
            )

        if total_items == 0:
            return ControllerTurnResult(
                session_id=session_id,
                turn_id=turn_id,
                request_revision=request_revision,
                page_epoch=page_epoch,
                status="completed",
                spoken_response="Your cart is currently empty.",
                extracted_intent=intent,
                authorized_command=command,
            )

        resp = f"You have {total_items} {'item' if total_items == 1 else 'items'} in your cart."
        for index, entry in enumerate(details or [], 1):
            title = str(entry.get("title", "Item"))[:200]
            variant = str(entry.get("variant_title", ""))[:100]
            resp += f" {index}. {title}"
            if variant and variant not in {"Default Title", "Title"}:
                resp += f" ({variant})"
            resp += f", quantity {entry['quantity']}"
            for price_field, label in (("unit_price_minor", "each"), ("line_total_minor", "line total")):
                amount = entry.get(price_field)
                if type(amount) is int and amount >= 0:
                    resp += f", {Money.from_minor_units(amount, current_cart.currency)} {label}"
            resp += "."
        if not details:
            resp += " Product details are unavailable from this cart observation."
        return ControllerTurnResult(
            session_id=session_id,
            turn_id=turn_id,
            request_revision=request_revision,
            page_epoch=page_epoch,
            status="completed",
            spoken_response=resp,
            extracted_intent=intent,
            authorized_command=command,
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
        client: ShopifySimulator | None,
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

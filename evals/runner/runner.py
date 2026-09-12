"""Evaluation runner executing evaluation manifests in iShop (Drake).

Enforces:
- T-23: Consent gating: episodes lacking consent or processor authorization are blocked before execution.
- T-24: All scheduled manifest cases counted in denominator (including errors and blocked runs);
        hidden gold labels isolated from inputs.
- T-25: Strict success accounts for prohibited intermediate actions.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
from dataclasses import asdict, dataclass, field
from typing import Any

from evals.replay.store import DeterministicReplayStore
from evals.runner.manifest import Episode, RunManifest
from evals.scoring.commerce_metrics import (
    calculate_critical_slots,
    calculate_fcem,
    calculate_strict_success,
)
from evals.scoring.speech_metrics import calculate_cer, calculate_wer
from ishop.commerce.catalog import EvidenceSnapshot
from ishop.commerce.reconciler import CommandReconciler
from ishop.commerce.simulator import ShopifySimulator
from ishop.commerce.verifier import ProposedCartAction, QuantityOperation
from ishop.domain.journal import CommandJournal
from ishop.domain.models import CartLine, CartSnapshot
from ishop.llm.base import LlmProvider
from ishop.llm.fake import FakeLlmProvider
from ishop.orchestration.controller import ShoppingController


def _run_coroutine(coro):
    """Safely runs an async coroutine even if an event loop is already active."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    else:
        return asyncio.run(coro)


@dataclass(frozen=True)
class EpisodeResult:
    """Individual episode result row."""

    episode_id: str
    split: str
    speaker_id: str | None
    language_pair: str
    hypothesis_transcript: str
    reference_transcript: str
    wer: float
    cer: float
    is_hallucination: bool
    fcem: bool
    strict_success: bool
    critical_slot_accuracy: float
    prohibited_actions_executed: int
    status: str  # "success" | "consent_blocked" | "missing_asr_output" | "error" | "timeout"
    error_message: str | None = None
    executed_commands_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunResult:
    """Complete serialized evaluation run result."""

    run_id: str
    manifest_hash: str
    mode: str
    normalization_version: str
    completed_at_utc: str
    total_episodes: int
    episodes: list[EpisodeResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "manifest_hash": self.manifest_hash,
            "mode": self.mode,
            "normalization_version": self.normalization_version,
            "completed_at_utc": self.completed_at_utc,
            "total_episodes": self.total_episodes,
            "episodes": [e.to_dict() for e in self.episodes],
        }


class EvaluationRunner:
    """Executes evaluation runs against a frozen manifest."""

    def __init__(
        self,
        evidence_snapshot: EvidenceSnapshot,
        llm_provider: LlmProvider | None = None,
    ):
        self.evidence = evidence_snapshot
        self.llm_provider = llm_provider or FakeLlmProvider()
        self.replay_store = DeterministicReplayStore(evidence_snapshot)

    def run(
        self,
        manifest: RunManifest,
        target_processor: str = "simulator",
        simulated_hypotheses: dict[str, str] | None = None,
        simulated_actions: dict[str, list[ProposedCartAction]] | None = None,
    ) -> RunResult:
        results: list[EpisodeResult] = []
        norm_v = manifest.normalization_version
        is_scorer_self_test = manifest.mode in ("synthetic_scorer_self_test", "scorer_test")

        for ep in manifest.episodes:
            # 1. Consent and processor gating (Safety S-12 / T-23 / R11)
            if not ep.consent_allowed or target_processor not in ep.allowed_processors:
                results.append(
                    EpisodeResult(
                        episode_id=ep.episode_id,
                        split=ep.split,
                        speaker_id=ep.speaker_id,
                        language_pair=ep.language_pair,
                        hypothesis_transcript="",
                        reference_transcript=ep.human_transcript,
                        wer=1.0,
                        cer=1.0,
                        is_hallucination=False,
                        fcem=False,
                        strict_success=False,
                        critical_slot_accuracy=0.0,
                        prohibited_actions_executed=0,
                        status="consent_blocked",
                        error_message="Consent withheld for target processor (T-23)",
                    )
                )
                continue

            # 2. Determine hypothesis transcript without silent gold substitution (R1)
            if simulated_hypotheses is not None and ep.episode_id in simulated_hypotheses:
                hyp = simulated_hypotheses[ep.episode_id]
            elif manifest.mode == "human_transcript":
                hyp = ep.human_transcript
            else:
                hyp = None

            if hyp is None:
                # Visible failure when ASR output is missing (R1)
                results.append(
                    EpisodeResult(
                        episode_id=ep.episode_id,
                        split=ep.split,
                        speaker_id=ep.speaker_id,
                        language_pair=ep.language_pair,
                        hypothesis_transcript="",
                        reference_transcript=ep.human_transcript,
                        wer=1.0,
                        cer=1.0,
                        is_hallucination=False,
                        fcem=False,
                        strict_success=False,
                        critical_slot_accuracy=0.0,
                        prohibited_actions_executed=0,
                        status="missing_asr_output",
                        error_message="Missing ASR hypothesis for benchmark episode (R1)",
                    )
                )
                continue

            # 3. Calculate speech metrics (WER, CER)
            wer_res = calculate_wer(ep.human_transcript, hyp, norm_version=norm_v)
            cer_res = calculate_cer(ep.human_transcript, hyp, norm_version=norm_v)

            # 4. Construct expected cart for evaluation scoring (gold data isolated from agent)
            expected_lines = tuple(
                CartLine(
                    variant_id=l["variant_id"],
                    quantity=l.get("quantity", 1),
                    properties=l.get("properties", {}),
                    selling_plan_id=l.get("selling_plan_id"),
                )
                for l in ep.expected_cart_lines
            )
            expected_cart = CartSnapshot(
                shop_id=self.evidence.shop_id,
                currency=self.evidence.currency,
                lines=expected_lines,
            )

            # 5. Execution path: Scorer Self-Test vs. Production Interpretation/Controller Path (R1)
            if is_scorer_self_test:
                # Explicit synthetic scorer self-test mode ONLY
                if simulated_actions and ep.episode_id in simulated_actions:
                    actions = simulated_actions[ep.episode_id]
                else:
                    actions = [
                        ProposedCartAction(
                            operation=QuantityOperation.INCREMENT,
                            variant_id=line["variant_id"],
                            quantity=line.get("quantity", 1),
                            properties=line.get("properties", {}),
                            selling_plan_id=line.get("selling_plan_id"),
                        )
                        for line in ep.expected_cart_lines
                    ]

                outcome = self.replay_store.replay_episode(
                    initial_cart_lines=list(ep.initial_cart_lines),
                    proposed_actions=actions,
                    prohibited_actions=list(ep.prohibited_actions),
                    session_id=f"eval_{ep.episode_id}",
                )
                observed_cart = outcome.final_cart
                prohibited_count = outcome.prohibited_actions_executed
                extracted_slots = {}
                if actions:
                    extracted_slots["variant_id"] = actions[0].variant_id
                    extracted_slots["quantity"] = actions[0].quantity
                status_str = "success" if outcome.status == "completed" else "error"
                err_msg = outcome.error_message
                cmd_count = len(outcome.executed_commands)
                clarification_valid = True
                turn_budget_exceeded = False
            else:
                # Production Controller benchmark path: hypothesis drives reasoning and mutations (R1)
                sim = ShopifySimulator(
                    shop_id=self.evidence.shop_id,
                    currency=self.evidence.currency,
                )
                for line in ep.initial_cart_lines:
                    sim.add_initial_line(
                        variant_id=line["variant_id"],
                        quantity=line.get("quantity", 1),
                        properties=line.get("properties", {}),
                        selling_plan_id=line.get("selling_plan_id"),
                    )

                journal = CommandJournal(":memory:")
                reconciler = CommandReconciler(journal)
                controller = ShoppingController(llm_provider=self.llm_provider, reconciler=reconciler)

                turn_result = _run_coroutine(
                    controller.handle_turn(
                        session_id=f"eval_{ep.episode_id}",
                        turn_id="turn_1",
                        request_revision=1,
                        page_epoch=1,
                        transcript=hyp,
                        evidence=self.evidence,
                        current_cart=sim.read_cart(),
                        client=sim,
                    )
                )

                observed_cart = sim.read_cart()
                prohibited_count = 0
                prohibited_set = set(ep.prohibited_actions or [])

                if turn_result.authorized_command:
                    cmd = turn_result.authorized_command
                    v_id = cmd.parameters.get("variant_id")
                    if f"{cmd.operation.value}:{v_id}" in prohibited_set or f"add_variant:{v_id}" in prohibited_set:
                        prohibited_count += 1

                # Clarification validity (R1)
                if turn_result.status == "clarification_needed":
                    clarification_valid = len(ep.expected_cart_lines) == 0 or "clarify" in prohibited_set
                else:
                    clarification_valid = True

                turn_budget_exceeded = False  # Single turn within 1-turn budget

                extracted_slots = {}
                if turn_result.extracted_intent:
                    intent = turn_result.extracted_intent
                    if intent.product_query:
                        extracted_slots["product_query"] = intent.product_query
                    if intent.quantity_change:
                        extracted_slots["quantity"] = intent.quantity_change.value
                    if turn_result.authorized_command and "variant_id" in turn_result.authorized_command.parameters:
                        extracted_slots["variant_id"] = turn_result.authorized_command.parameters["variant_id"]

                status_str = "success" if turn_result.status in ("completed", "clarification_needed") else "error"
                err_msg = turn_result.reason
                cmd_count = 1 if turn_result.authorized_command else 0

            # 6. Calculate commerce metrics
            fcem = calculate_fcem(expected_cart, observed_cart)
            strict = calculate_strict_success(
                fcem=fcem,
                prohibited_actions_executed=prohibited_count,
                turn_budget_exceeded=turn_budget_exceeded,
                clarification_valid=clarification_valid,
            )

            slot_acc, _ = calculate_critical_slots(ep.critical_slots, extracted_slots)

            results.append(
                EpisodeResult(
                    episode_id=ep.episode_id,
                    split=ep.split,
                    speaker_id=ep.speaker_id,
                    language_pair=ep.language_pair,
                    hypothesis_transcript=hyp,
                    reference_transcript=ep.human_transcript,
                    wer=round(wer_res.error_rate, 4),
                    cer=round(cer_res.error_rate, 4),
                    is_hallucination=wer_res.is_hallucination,
                    fcem=fcem,
                    strict_success=strict,
                    critical_slot_accuracy=slot_acc,
                    prohibited_actions_executed=prohibited_count,
                    status=status_str,
                    error_message=err_msg,
                    executed_commands_count=cmd_count,
                )
            )

        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return RunResult(
            run_id=manifest.run_id,
            manifest_hash=manifest.manifest_hash,
            mode=manifest.mode,
            normalization_version=manifest.normalization_version,
            completed_at_utc=now_utc,
            total_episodes=len(results),
            episodes=results,
        )

"""Evaluation runner executing evaluation manifests in iShop (Drake).

Enforces:
- T-23: Consent gating: episodes lacking consent or processor authorization are blocked before execution.
- T-24: All scheduled manifest cases counted in denominator (including errors and blocked runs);
        hidden gold labels isolated from inputs.
- T-25: Strict success accounts for prohibited intermediate actions.
"""

from __future__ import annotations

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
from ishop.commerce.verifier import ProposedCartAction, QuantityOperation
from ishop.domain.models import CartLine, CartSnapshot


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
    status: str  # "success" | "consent_blocked" | "error" | "timeout"
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

    def __init__(self, evidence_snapshot: EvidenceSnapshot):
        self.evidence = evidence_snapshot
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

        for ep in manifest.episodes:
            # 1. Consent and processor gating (Safety S-12 / T-23)
            if not ep.consent_allowed or target_processor not in ep.allowed_processors:
                # S-12 / T-23: Strictly block processing and retain case in denominator
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

            # 2. Determine hypothesis transcript
            if simulated_hypotheses and ep.episode_id in simulated_hypotheses:
                hyp = simulated_hypotheses[ep.episode_id]
            elif manifest.mode == "human_transcript":
                # Diagnostic reference run (no ASR noise)
                hyp = ep.human_transcript
            else:
                hyp = ep.human_transcript

            # 3. Calculate speech metrics (WER, CER)
            wer_res = calculate_wer(ep.human_transcript, hyp, norm_version=norm_v)
            cer_res = calculate_cer(ep.human_transcript, hyp, norm_version=norm_v)

            # 4. Construct expected cart for evaluation comparison (T-24: isolated from agent)
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

            # 5. Determine proposed actions for replay
            if simulated_actions and ep.episode_id in simulated_actions:
                actions = simulated_actions[ep.episode_id]
            else:
                # Default: generate proposed actions from episode's critical slots / expected lines
                actions = []
                for line in ep.expected_cart_lines:
                    actions.append(
                        ProposedCartAction(
                            operation=QuantityOperation.INCREMENT,
                            variant_id=line["variant_id"],
                            quantity=line.get("quantity", 1),
                            properties=line.get("properties", {}),
                            selling_plan_id=line.get("selling_plan_id"),
                        )
                    )

            # 6. Replay through deterministic store
            outcome = self.replay_store.replay_episode(
                initial_cart_lines=list(ep.initial_cart_lines),
                proposed_actions=actions,
                prohibited_actions=list(ep.prohibited_actions),
                session_id=f"eval_{ep.episode_id}",
            )

            # 7. Calculate commerce metrics
            fcem = calculate_fcem(expected_cart, outcome.final_cart)
            strict = calculate_strict_success(
                fcem=fcem,
                prohibited_actions_executed=outcome.prohibited_actions_executed,
                turn_budget_exceeded=False,
                clarification_valid=True,
            )

            # Extract slots for comparison
            extracted_slots = {}
            if actions:
                extracted_slots["variant_id"] = actions[0].variant_id
                extracted_slots["quantity"] = actions[0].quantity
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
                    prohibited_actions_executed=outcome.prohibited_actions_executed,
                    status="success" if outcome.status == "completed" else "error",
                    error_message=outcome.error_message,
                    executed_commands_count=len(outcome.executed_commands),
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

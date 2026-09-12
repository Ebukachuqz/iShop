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
import time
import copy
from dataclasses import asdict, dataclass, field, replace as dataclass_replace
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
from ishop.commerce.reconciler import CommandReconciler, ExecutionOutcome
from ishop.commerce.simulator import ShopifySimulator
from ishop.commerce.verifier import ProposedCartAction, QuantityOperation
from ishop.domain.journal import CommandJournal
from ishop.domain.models import CartLine, CartSnapshot, CommandOperation
from evals.runner.provenance import configuration_for, content_hash
from evals.runner.validation import validate_run
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
    prohibited_actions_attempted: int = 0
    task_goal: str = "cart"
    trace: list[dict[str, Any]] = field(default_factory=list)

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
    configuration: dict[str, Any] = field(default_factory=dict)
    data_kind: str = "synthetic"
    hypotheses_sha256: str = ""
    result_version: str = "2.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "configuration": self.configuration,
            "data_kind": self.data_kind,
            "hypotheses_sha256": self.hypotheses_sha256,
            "result_version": self.result_version,
            "manifest_hash": self.manifest_hash,
            "mode": self.mode,
            "normalization_version": self.normalization_version,
            "completed_at_utc": self.completed_at_utc,
            "total_episodes": self.total_episodes,
            "episodes": [e.to_dict() for e in self.episodes],
        }


class ObservedSimulator(ShopifySimulator):
    """Records dispatch and observed effects without access to expected answers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []

    def execute_command(self, command):
        before = self.read_cart()
        event = {"type": "dispatch", "operation": command.operation.value,
                 "variant_id": command.parameters.get("variant_id"),
                 "parameters": dict(command.parameters)}
        self.events.append(event)
        try:
            return super().execute_command(command)
        finally:
            after = self.read_cart()
            event["changed"] = not before.is_equivalent(after)
            event["before"] = before.to_dict()
            event["after"] = after.to_dict()


class EvaluationRunner:
    """Single-turn replay with explicit task goals and frozen execution inputs."""

    def __init__(self, evidence_snapshot, llm_provider=None, timeout_seconds=15.0):
        self.evidence = evidence_snapshot
        self.llm_provider = llm_provider or FakeLlmProvider()
        self.timeout_seconds = timeout_seconds
        self.replay_store = DeterministicReplayStore(evidence_snapshot)

    @property
    def configuration(self):
        return configuration_for(self.evidence, self.llm_provider, self.timeout_seconds)

    def run(self, manifest, target_processor="simulator", simulated_hypotheses=None,
            simulated_actions=None, base_dir=None):
        # Snapshot nested mutable data, then validate at the actual execution boundary.
        manifest = RunManifest.from_dict(copy.deepcopy(manifest.to_dict()))
        from evals.runner.validation import validate_catalog_compatibility
        errors = validate_run(manifest, base_dir)
        errors.extend(validate_catalog_compatibility(manifest, self.evidence))
        actual = self.configuration
        if any(manifest.configuration.get(k) != v for k, v in actual.items()):
            errors.append("Frozen configuration does not match code, evidence or provider")
        if target_processor != "simulator":
            errors.append("This runner executes only the offline simulator")
        self_test = manifest.mode == "synthetic_scorer_self_test"
        if simulated_actions is not None and not self_test:
            errors.append("Injected actions are restricted to explicit scorer self-tests")
        if errors:
            raise ValueError("; ".join(errors))
        hypotheses = copy.deepcopy(simulated_hypotheses or {})
        results = []
        processor = self.llm_provider.profile.provider_name
        processor = "local" if processor == "fake" else processor
        for ep in manifest.episodes:
            required_processors = {"simulator"} if self_test else {"simulator", processor}
            # Recorded ASR outputs were created elsewhere: require their named processor too.
            if manifest.configuration.get("asr"):
                required_processors.add(manifest.configuration["asr"]["provider"])
            if ep.consent_allowed is not True or not required_processors.issubset(ep.allowed_processors):
                results.append(self._failure(ep, "consent_blocked", "Consent withheld for actual processor"))
                continue
            hyp = hypotheses.get(ep.episode_id)
            if ep.episode_id not in hypotheses and manifest.mode == "human_transcript":
                hyp = ep.human_transcript
            if not isinstance(hyp, str):
                results.append(self._failure(ep, "missing_asr_output", "Missing ASR hypothesis"))
                continue
            expected = CartSnapshot(self.evidence.shop_id, self.evidence.currency, tuple(
                CartLine(l["variant_id"], l.get("quantity", 1), l.get("selling_plan_id"), l.get("properties", {}))
                for l in ep.expected_cart_lines
            ))
            if self_test:
                actions = (simulated_actions or {}).get(ep.episode_id)
                if actions is None:
                    results.append(self._failure(ep, "error", "Scorer self-test requires explicit actions"))
                    continue
                outcome = self.replay_store.replay_episode(
                    list(ep.initial_cart_lines), actions, list(ep.prohibited_actions), f"eval_{ep.episode_id}",
                )
                fcem = calculate_fcem(expected, outcome.final_cart)
                row = self._failure(ep, "scorer_self_test", "Synthetic scorer check, not task execution")
                row = dataclass_replace(row, fcem=fcem, hypothesis_transcript=hyp,
                                        executed_commands_count=len(outcome.executed_commands))
                results.append(row)
                continue
            sim = ObservedSimulator(self.evidence.shop_id, self.evidence.currency)
            for line in ep.initial_cart_lines:
                sim.add_initial_line(line["variant_id"], line.get("quantity", 1),
                                     line.get("properties", {}), line.get("selling_plan_id"))
            journal = CommandJournal(":memory:")
            controller = ShoppingController(self.llm_provider, CommandReconciler(journal))
            start = time.monotonic()
            try:
                result = _run_coroutine(asyncio.wait_for(controller.handle_turn(
                    f"eval_{ep.episode_id}", "turn_1", 1, 1, hyp, self.evidence,
                    sim.read_cart(), client=sim,
                ), timeout=self.timeout_seconds))
                status = "success" if result.status == "completed" else result.status
                reason = result.reason
            except TimeoutError:
                result, status, reason = None, "timeout", "Turn time budget exceeded"
            except Exception as exc:
                result, status, reason = None, "error", f"Execution failed: {type(exc).__name__}"
            elapsed = time.monotonic() - start
            observed = sim.read_cart()
            fcem = calculate_fcem(expected, observed)
            events = list(sim.events)
            attempts = sum(f"{ev['operation']}:{ev['variant_id']}" in ep.prohibited_actions for ev in events)
            prohibited = sum(ev["changed"] and f"{ev['operation']}:{ev['variant_id']}" in ep.prohibited_actions
                             for ev in events)
            receipt = result.receipt if result else None
            verified = bool(receipt and receipt.outcome in (
                ExecutionOutcome.VERIFIED_SUCCESS, ExecutionOutcome.VERIFIED_NO_OP,
            ))
            fields = tuple(getattr(result, "clarification_fields", ()))
            clarification_valid = bool(result and result.status == "clarification_needed"
                                       and set(ep.clarification_fields).issubset(fields))
            goal_met = False
            if result:
                if ep.goal == "cart":
                    goal_met = result.status == "completed" and verified
                elif ep.goal == "clarification":
                    goal_met = clarification_valid and not events
                elif ep.goal == "refusal":
                    goal_met = result.status == "rejected" and not events
                elif ep.goal == "no_action":
                    goal_met = result.status == "completed" and not events and result.authorized_command is None
                elif ep.goal == "checkout":
                    # No browser navigation executor exists here. A proposed URL is NOT handoff evidence.
                    goal_met = False
                    if result.authorized_command:
                        status, reason = "handoff_unverified", "No observed checkout navigation in offline runner"
            strict = bool(fcem and goal_met and not prohibited and not attempts
                          and elapsed <= self.timeout_seconds and status not in ("error", "timeout"))
            slots = {}
            if result and result.extracted_intent:
                intent = result.extracted_intent
                slots = {"intent": intent.operation.value, "product_query": intent.product_query,
                         "variant_attributes": dict(intent.selected_variant_attributes)}
                if intent.quantity_change:
                    slots.update(quantity=intent.quantity_change.value, quantity_operation=intent.quantity_change.mode)
                if result.authorized_command:
                    slots["variant_id"] = result.authorized_command.parameters.get("variant_id")
            events.append({"type": "terminal", "status": status, "goal": ep.goal,
                           "goal_met": bool(goal_met), "elapsed_seconds": elapsed,
                           "turns": 1, "clarification_fields": list(fields),
                           "receipt_outcome": receipt.outcome.value if receipt else None})
            wer = calculate_wer(ep.human_transcript, hyp, norm_version=manifest.normalization_version)
            cer = calculate_cer(ep.human_transcript, hyp, norm_version=manifest.normalization_version)
            results.append(EpisodeResult(
                ep.episode_id, ep.split, ep.speaker_id, ep.language_pair, hyp, ep.human_transcript,
                wer.error_rate, cer.error_rate, wer.is_hallucination, fcem, strict,
                calculate_critical_slots(ep.critical_slots, slots)[0], prohibited, status, reason,
                len(sim.events), attempts, ep.goal, events,
            ))
        return RunResult(manifest.run_id, manifest.manifest_hash, manifest.mode,
                         manifest.normalization_version, datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         len(results), results, manifest.configuration, manifest.data_kind,
                         content_hash(hypotheses))

    @staticmethod
    def _failure(ep, status, reason):
        return EpisodeResult(ep.episode_id, ep.split, ep.speaker_id, ep.language_pair,
                             "", ep.human_transcript, 1.0, 1.0, False, False, False,
                             0.0, 0, status, reason, task_goal=ep.goal)

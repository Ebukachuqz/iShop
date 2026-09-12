"""Deterministic replay store for evaluation episodes in iShop (Drake).

Enforces:
- T-24: Hidden gold labels (expected cart, critical slots, prohibited actions) are strictly isolated
        from the runtime and interpreter during replay.
- S-08: Cart state transitions use production verifiers and simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ishop.commerce.catalog import CatalogResolver, EvidenceSnapshot, IntentTarget, ResolutionStatus
from ishop.commerce.reconciler import CommandReconciler, ExecutionOutcome
from ishop.commerce.simulator import ShopifySimulator
from ishop.commerce.verifier import CartVerifier, ProposedCartAction, QuantityOperation
from ishop.domain.journal import CommandJournal
from ishop.domain.models import CartLine, CartSnapshot


@dataclass(frozen=True)
class EpisodeReplayOutcome:
    """Outcome of replaying an episode through the commerce engine."""

    final_cart: CartSnapshot
    executed_commands: tuple[str, ...] = ()
    prohibited_actions_executed: int = 0
    status: str = "completed"
    error_message: str | None = None


class DeterministicReplayStore:
    """Replays shopping operations against an isolated in-memory simulator."""

    def __init__(self, evidence_snapshot: EvidenceSnapshot):
        self.evidence = evidence_snapshot

    def replay_episode(
        self,
        initial_cart_lines: list[dict[str, Any]],
        proposed_actions: list[ProposedCartAction],
        prohibited_actions: list[str] | None = None,
        session_id: str = "eval_sess",
    ) -> EpisodeReplayOutcome:
        # 1. Initialize isolated simulator for this episode
        sim = ShopifySimulator(
            shop_id=self.evidence.shop_id,
            currency=self.evidence.currency,
        )

        for line in initial_cart_lines:
            sim.add_initial_line(
                variant_id=line["variant_id"],
                quantity=line.get("quantity", 1),
                properties=line.get("properties", {}),
                selling_plan_id=line.get("selling_plan_id"),
            )

        journal = CommandJournal(":memory:")
        reconciler = CommandReconciler(journal)

        executed_cmd_ids: list[str] = []
        prohibited_executed = 0
        prohibited_set = set(prohibited_actions or [])

        # 2. Execute each proposed action sequentially
        for idx, action in enumerate(proposed_actions, start=1):
            current_cart = sim.read_cart()
            turn_id = f"turn_{idx}"

            # Check if this action matches prohibited actions
            action_desc = f"{action.operation.value}:{action.variant_id}"
            if action_desc in prohibited_set:
                prohibited_executed += 1

            ver = CartVerifier.verify_action(
                current_cart=current_cart,
                action=action,
                session_id=session_id,
                turn_id=turn_id,
                request_revision=idx,
                page_epoch=1,
            )

            if not ver.allowed or not ver.command:
                # Precondition or policy rejection
                continue

            receipt = reconciler.execute_and_reconcile(ver.command, sim)
            executed_cmd_ids.append(ver.command.command_id)

            if receipt.outcome == ExecutionOutcome.FAILED_WITH_CHANGE:
                return EpisodeReplayOutcome(
                    final_cart=sim.read_cart(),
                    executed_commands=tuple(executed_cmd_ids),
                    prohibited_actions_executed=prohibited_executed,
                    status="failed_with_change",
                    error_message=receipt.verification_message,
                )

        return EpisodeReplayOutcome(
            final_cart=sim.read_cart(),
            executed_commands=tuple(executed_cmd_ids),
            prohibited_actions_executed=prohibited_executed,
            status="completed",
        )

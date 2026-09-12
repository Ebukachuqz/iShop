"""Deterministic Shopify cart reconciliation engine for iShop (Drake).

Enforces Safety invariants:
- S-06: Success follows verified state; read-back must satisfy expected change (T-08, T-14).
- S-08: Preserve unrelated cart contents; exact multiset comparison (T-05, T-10).
- S-09: Precondition fingerprint check immediately before dispatch (T-10, S-09).
- S-10: Idempotency deduplication via durable journal; never blindly retry writes (T-12, T-13, T-14).
- S-13: Failures stay visible; userErrors prevent success receipts (T-08).
- T-16: Safe adapter fallback; never fallback-retry an uncertain write.
"""

from __future__ import annotations

import datetime
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ishop.commerce.simulator import ShopifySimulator, SimulationNetworkError, SimulationResult
from ishop.domain.journal import CommandJournal, CommandStatus
from ishop.domain.models import (
    AuthorizedCommand,
    CartLine,
    CartSnapshot,
    CommandOperation,
)


class ExecutionOutcome(str, Enum):
    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_NO_OP = "verified_no_op"
    REJECTED = "rejected"
    CANCELED_BEFORE_DISPATCH = "canceled_before_dispatch"
    UNCERTAIN = "uncertain"
    FAILED_WITH_CHANGE = "failed_with_observed_change"


@dataclass(frozen=True)
class ExecutionReceipt:
    """Immutable authoritative execution receipt."""

    receipt_id: str
    command_id: str
    outcome: ExecutionOutcome
    transport_used: str
    verification_message: str
    errors: list[str] = field(default_factory=list)
    before_cart_fingerprint: str | None = None
    after_cart_fingerprint: str | None = None
    dispatched_at_utc: str | None = None
    reconciled_at_utc: str | None = None
    schema_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "outcome": self.outcome.value,
            "transport_used": self.transport_used,
            "verification_message": self.verification_message,
            "errors": list(self.errors),
            "before_cart_fingerprint": self.before_cart_fingerprint,
            "after_cart_fingerprint": self.after_cart_fingerprint,
            "dispatched_at_utc": self.dispatched_at_utc,
            "reconciled_at_utc": self.reconciled_at_utc,
        }


class CommandReconciler:
    """Authoritative cart reconciler managing execution, read-back, and recovery."""

    def __init__(self, journal: CommandJournal):
        self.journal = journal
        self._cached_receipts: dict[str, ExecutionReceipt] = {}

    def execute_and_reconcile(
        self,
        command: AuthorizedCommand,
        adapter: ShopifySimulator,
        transport_name: str = "simulator",
    ) -> ExecutionReceipt:
        # 1. Idempotency and replay check (T-12, S-10)
        existing = self.journal.get_entry(command.command_id)
        if existing and existing.status in (
            CommandStatus.VERIFIED_SUCCESS,
            CommandStatus.VERIFIED_NO_OP,
            CommandStatus.REJECTED,
            CommandStatus.CANCELED,
            CommandStatus.FAILED_WITH_CHANGE,
        ):
            if command.command_id in self._cached_receipts:
                return self._cached_receipts[command.command_id]
            # Recreate receipt from journal
            return ExecutionReceipt(
                receipt_id=existing.receipt_id or f"rcpt_{uuid.uuid4().hex[:12]}",
                command_id=command.command_id,
                outcome=ExecutionOutcome(existing.status.value),
                transport_used=transport_name,
                verification_message=existing.reconciled_message or "Replayed existing journaled outcome (T-12)",
                errors=[],
            )

        if existing and existing.status in (CommandStatus.DISPATCHED, CommandStatus.UNCERTAIN):
            # Command was already dispatched or was uncertain (e.g. restart/crash recovery, T-13)
            # Must reconcile against live cart without repeating dispatch! (S-10)
            return self.reconcile_uncertain(command, adapter, transport_name=transport_name)

        # 2. Record PREPARED in journal
        self.journal.prepare_command(command)

        # 3. Check lease expiration (S-09)
        now_ms = int(time.time() * 1000)
        if command.expires_at_ms < now_ms:
            receipt = self._finalize(
                command=command,
                outcome=ExecutionOutcome.REJECTED,
                transport=transport_name,
                message="Command lease expired before dispatch (S-09)",
                errors=["Lease expired"],
            )
            return receipt

        # 4. Precondition check: verify live before-cart matches expected fingerprint (S-09)
        before_cart = adapter.read_cart()
        before_fp = before_cart.fingerprint()

        if command.expected_cart_fingerprint is not None:
            if before_fp != command.expected_cart_fingerprint:
                receipt = self._finalize(
                    command=command,
                    outcome=ExecutionOutcome.REJECTED,
                    transport=transport_name,
                    message="Precondition failed: cart state modified concurrently before dispatch (S-09, T-10)",
                    errors=["Precondition cart fingerprint mismatch"],
                    before_fp=before_fp,
                    after_fp=before_fp,
                )
                return receipt

        # 5. Record DISPATCHED in journal (S-10)
        dispatched_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.journal.mark_dispatched(command.command_id)


        # 6. Dispatch to adapter with fault handling (T-13, T-14)
        try:
            result = adapter.execute_command(command)
        except (SimulationNetworkError, TimeoutError, Exception) as exc:
            # Network drop, timeout, or lost response (T-13, T-14)
            self.journal.complete_command(
                command.command_id,
                CommandStatus.UNCERTAIN,
                message=f"Transport error during dispatch: {exc}",
            )
            # Reconcile live cart rather than blindly retrying write! (S-10)
            return self.reconcile_uncertain(
                command=command,
                adapter=adapter,
                before_cart=before_cart,
                transport_name=transport_name,
            )

        # 7. Post-mutation read-back verification (S-06, T-08)
        after_cart = result.cart_snapshot if result.cart_snapshot else adapter.read_cart()
        after_fp = after_cart.fingerprint()

        # Check for userErrors even with HTTP 200 (T-08)
        if result.user_errors:
            if after_cart.is_equivalent(before_cart):
                outcome = ExecutionOutcome.REJECTED
                msg = f"Rejected with Storefront userErrors: {'; '.join(result.user_errors)} (T-08)"
            else:
                outcome = ExecutionOutcome.FAILED_WITH_CHANGE
                msg = f"Failed with userErrors and unintended cart mutation: {'; '.join(result.user_errors)} (T-08)"

            receipt = self._finalize(
                command=command,
                outcome=outcome,
                transport=transport_name,
                message=msg,
                errors=result.user_errors,
                before_fp=before_fp,
                after_fp=after_fp,
                dispatched_utc=dispatched_utc,
            )
            return receipt

        # 8. Check expected multiset state and unrelated line preservation (S-08, T-10)
        is_satisfied = self._verify_expected_change(
            command=command,
            before_cart=before_cart,
            after_cart=after_cart,
        )

        if is_satisfied:
            if before_cart.is_equivalent(after_cart):
                outcome = ExecutionOutcome.VERIFIED_NO_OP
                msg = "Command verified: cart was already in expected state"
            else:
                outcome = ExecutionOutcome.VERIFIED_SUCCESS
                msg = "Command verified: authoritative cart read-back matches expected change (S-06)"
        else:
            if before_cart.is_equivalent(after_cart):
                outcome = ExecutionOutcome.REJECTED
                msg = "Write did not commit to Shopify cart (no change observed)"
            else:
                outcome = ExecutionOutcome.FAILED_WITH_CHANGE
                msg = "Authoritative cart read-back does not match expected effect (S-06, S-08)"

        receipt = self._finalize(
            command=command,
            outcome=outcome,
            transport=transport_name,
            message=msg,
            before_fp=before_fp,
            after_fp=after_fp,
            dispatched_utc=dispatched_utc,
        )
        return receipt

    def reconcile_uncertain(
        self,
        command: AuthorizedCommand,
        adapter: ShopifySimulator,
        before_cart: CartSnapshot | None = None,
        transport_name: str = "simulator",
    ) -> ExecutionReceipt:
        """Reconciles in-flight or recovered command against authoritative live cart (T-13, T-14)."""
        live_cart = adapter.read_cart()
        live_fp = live_cart.fingerprint()

        # If before_cart is not provided (e.g. after process restart, T-13),
        # we check if live cart contains the target item in target quantity
        params = command.parameters
        variant_id = params.get("variant_id")
        target_qty = params.get("quantity", 0)
        target_line_key = params.get("target_line_key")
        norm_props = {str(k): str(v) for k, v in sorted(params.get("properties", {}).items())}
        selling_plan_id = params.get("selling_plan_id")

        matched = False
        if command.operation == CommandOperation.REMOVE_LINE:
            # Succeeded if target line is absent
            line_present = any(
                l.canonical_key == target_line_key or l.variant_id == variant_id
                for l in live_cart.lines
            )
            matched = not line_present
        else:
            for line in live_cart.lines:
                if (
                    line.variant_id == variant_id
                    and line.properties == norm_props
                    and line.selling_plan_id == selling_plan_id
                ):
                    if line.quantity == target_qty:
                        matched = True
                        break

        if matched:
            outcome = ExecutionOutcome.VERIFIED_SUCCESS
            msg = "Reconciled uncertain command: live cart satisfies expected effect (T-13, T-14)"
            errors = []
        else:
            outcome = ExecutionOutcome.UNCERTAIN
            msg = "Reconciled uncertain command: mutation was not observed on live cart; retry prohibited (S-10, T-14)"
            errors = ["Mutation not confirmed on live cart"]

        receipt = self._finalize(
            command=command,
            outcome=outcome,
            transport=transport_name,
            message=msg,
            errors=errors,
            before_fp=before_cart.fingerprint() if before_cart else None,
            after_fp=live_fp,
        )
        return receipt

    def execute_with_fallback(
        self,
        command: AuthorizedCommand,
        primary_adapter: ShopifySimulator,
        fallback_adapter: ShopifySimulator,
        primary_transport: str = "native_webmcp",
        fallback_transport: str = "storefront_actions",
    ) -> ExecutionReceipt:
        """Executes via primary adapter with safe fallback to secondary adapter (T-16).

        Safety rule: If primary adapter fails with an UNCERTAIN write, fallback retry
        is STRICTLY PROHIBITED (T-16, S-10). Fallback is only allowed for safe pre-dispatch failures.
        """
        # 1. Attempt primary
        primary_receipt = self.execute_and_reconcile(
            command=command,
            adapter=primary_adapter,
            transport_name=primary_transport,
        )

        if primary_receipt.outcome in (
            ExecutionOutcome.VERIFIED_SUCCESS,
            ExecutionOutcome.VERIFIED_NO_OP,
        ):
            return primary_receipt

        # 2. Check if primary outcome was UNCERTAIN (T-16)
        if primary_receipt.outcome == ExecutionOutcome.UNCERTAIN:
            # S-10 & T-16: Never fallback-retry an uncertain write!
            return primary_receipt

        # If primary failed with schema/precondition before dispatch, fallback can be tried
        if primary_receipt.outcome == ExecutionOutcome.REJECTED and not primary_receipt.before_cart_fingerprint:
            return self.execute_and_reconcile(
                command=command,
                adapter=fallback_adapter,
                transport_name=fallback_transport,
            )

        return primary_receipt

    def _verify_expected_change(
        self,
        command: AuthorizedCommand,
        before_cart: CartSnapshot,
        after_cart: CartSnapshot,
    ) -> bool:
        """Validates that expected line was modified and unrelated lines were preserved (S-08, T-10)."""
        params = command.parameters
        variant_id = params.get("variant_id")
        target_qty = params.get("quantity", 0)
        norm_props = {str(k): str(v) for k, v in sorted(params.get("properties", {}).items())}
        selling_plan_id = params.get("selling_plan_id")
        target_key = params.get("target_line_key")

        # 1. Construct expected line
        expected_line = CartLine(
            variant_id=variant_id,
            quantity=target_qty,
            selling_plan_id=selling_plan_id,
            properties=norm_props,
        )

        # 2. Verify unrelated lines are preserved (S-08, T-10)
        target_canonical = expected_line.canonical_key
        for line in before_cart.lines:
            if line.canonical_key != target_canonical and line.canonical_key != target_key:
                # This is an unrelated line. It must exist in after_cart with the exact same quantity!
                after_matching = [l for l in after_cart.lines if l.canonical_key == line.canonical_key]
                if not after_matching or after_matching[0].quantity != line.quantity:
                    # Unrelated line was corrupted! (S-08 violation)
                    return False

        # 3. Verify target line in after_cart
        if command.operation == CommandOperation.REMOVE_LINE or target_qty == 0:
            # Line must be absent from after_cart
            for line in after_cart.lines:
                if line.canonical_key == target_canonical or line.canonical_key == target_key:
                    return False
            return True
        else:
            # Line must be present with target quantity
            for line in after_cart.lines:
                if line.canonical_key == target_canonical or line.canonical_key == target_key:
                    return line.quantity == target_qty
            return False

    def _finalize(
        self,
        command: AuthorizedCommand,
        outcome: ExecutionOutcome,
        transport: str,
        message: str,
        errors: list[str] | None = None,
        before_fp: str | None = None,
        after_fp: str | None = None,
        dispatched_utc: str | None = None,
    ) -> ExecutionReceipt:
        reconciled_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rcpt_id = f"rcpt_{uuid.uuid4().hex[:16]}"
        errors_list = list(errors or [])

        journal_status = CommandStatus(outcome.value)
        self.journal.complete_command(
            command_id=command.command_id,
            outcome=journal_status,
            message=message,
            receipt_id=rcpt_id,
        )

        receipt = ExecutionReceipt(
            receipt_id=rcpt_id,
            command_id=command.command_id,
            outcome=outcome,
            transport_used=transport,
            verification_message=message,
            errors=errors_list,
            before_cart_fingerprint=before_fp,
            after_cart_fingerprint=after_fp,
            dispatched_at_utc=dispatched_utc,
            reconciled_at_utc=reconciled_utc,
        )
        self._cached_receipts[command.command_id] = receipt
        return receipt

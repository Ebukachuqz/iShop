"""Commerce, task completion, and critical slot evaluation metrics for iShop (Drake).

Enforces:
- T-25: FCEM exact multiset equivalence; forbidden intermediate effects fail strict success.
- EVALUATION: Strict success requires correct clarification, turn budget compliance, and zero prohibited actions.
"""

from __future__ import annotations

from typing import Any
from ishop.domain.models import CartSnapshot


def calculate_fcem(expected_cart: CartSnapshot, observed_cart: CartSnapshot) -> bool:
    """Final Cart Exact Match (FCEM).

    Evaluates whether the authoritative final cart is equivalent to the expected gold cart
    using canonical multiset line comparison (S-08).
    """
    if expected_cart is None or observed_cart is None:
        return False
    return expected_cart.is_equivalent(observed_cart)


def calculate_strict_success(
    fcem: bool,
    prohibited_actions_executed: int = 0,
    turn_budget_exceeded: bool = False,
    clarification_valid: bool = True,
) -> bool:
    """Calculates strict task success.

    A system that adds an unintended item and later removes it may satisfy FCEM,
    but fails strict task success due to the prohibited intermediate mutation (T-25).
    """
    if not fcem:
        return False
    if prohibited_actions_executed > 0:
        return False
    if turn_budget_exceeded:
        return False
    if not clarification_valid:
        return False
    return True


def calculate_critical_slots(
    gold_slots: dict[str, Any],
    extracted_slots: dict[str, Any],
) -> tuple[float, dict[str, bool]]:
    """Calculates joint critical slot exact match and per-slot accuracy."""
    if not gold_slots:
        return 1.0, {}

    slot_results: dict[str, bool] = {}
    all_matched = True

    for key, expected_val in gold_slots.items():
        actual_val = extracted_slots.get(key)
        # Normalize comparison
        if isinstance(expected_val, str) and isinstance(actual_val, str):
            matched = expected_val.strip().lower() == actual_val.strip().lower()
        elif isinstance(expected_val, dict) and isinstance(actual_val, dict):
            matched = {str(k).lower(): str(v).lower() for k, v in expected_val.items()} == \
                      {str(k).lower(): str(v).lower() for k, v in actual_val.items()}
        else:
            matched = expected_val == actual_val

        slot_results[key] = matched
        if not matched:
            all_matched = False

    joint_score = 1.0 if all_matched else 0.0
    return joint_score, slot_results

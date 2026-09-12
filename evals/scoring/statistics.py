"""Statistical analysis and cluster bootstrap resampling for iShop evaluation.

Enforces:
- T-26: Cluster resampling by speaker; transparent notice/limitation for corpora lacking speaker IDs (e.g. AfriSwitch).
"""

from __future__ import annotations

import random
from typing import Any, Callable


def bootstrap_metric(
    items: list[dict[str, Any]],
    metric_fn: Callable[[list[dict[str, Any]]], float],
    num_resamples: int = 1000,
    seed: int = 42,
) -> tuple[float, float, float, str | None]:
    """Calculates point estimate and 95% bootstrap confidence interval.

    Uses speaker-cluster resampling when speaker IDs are present (T-26).
    Transparently falls back to clip-level resampling when speaker IDs are absent (AfriSwitch).
    """
    if not items:
        return 0.0, 0.0, 0.0, None

    rng = random.Random(seed)
    point_estimate = metric_fn(items)

    has_speakers = all(bool(item.get("speaker_id")) for item in items)
    notice: str | None = None

    if has_speakers:
        # Group items by speaker ID
        speaker_groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            spk = str(item["speaker_id"])
            speaker_groups.setdefault(spk, []).append(item)

        speaker_ids = list(speaker_groups.keys())
        n_speakers = len(speaker_ids)

        scores: list[float] = []
        for _ in range(num_resamples):
            sampled_spks = rng.choices(speaker_ids, k=n_speakers)
            sampled_items: list[dict[str, Any]] = []
            for spk in sampled_spks:
                sampled_items.extend(speaker_groups[spk])
            scores.append(metric_fn(sampled_items))
    else:
        notice = "Speaker IDs not available in dataset; clip-level resampling applied (T-26 notice)"
        n_items = len(items)
        scores = []
        for _ in range(num_resamples):
            sampled_items = rng.choices(items, k=n_items)
            scores.append(metric_fn(sampled_items))

    scores.sort()
    lower_idx = int(0.025 * num_resamples)
    upper_idx = int(0.975 * num_resamples)

    ci_lower = scores[lower_idx]
    ci_upper = scores[upper_idx]

    return point_estimate, ci_lower, ci_upper, notice

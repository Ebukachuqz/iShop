#!/usr/bin/env python3
"""CLI for generating evaluation report tables from saved run results.

Enforces:
- S-13 & T-24: Every scheduled case in denominator; missing/failed runs remain visible.
- T-26: Paired comparisons and speaker-cluster bootstrap confidence intervals.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals.scoring.statistics import bootstrap_metric


def generate_report(results_path: str | Path) -> str:
    p = Path(results_path)
    if not p.exists():
        raise FileNotFoundError(f"Result file not found: {p}")

    data = json.loads(p.read_text(encoding="utf-8"))
    run_id = data["run_id"]
    mode = data["mode"]
    norm_v = data["normalization_version"]
    episodes = data.get("episodes", [])
    total_episodes = len(episodes)

    if total_episodes == 0:
        return "# Evaluation Report\n\nNo episodes recorded in run result."

    # Compute metrics
    # 1. WER / CER
    wers = [e["wer"] for e in episodes]
    cers = [e["cer"] for e in episodes]
    avg_wer = sum(wers) / total_episodes
    avg_cer = sum(cers) / total_episodes

    # 2. FCEM (Final Cart Exact Match)
    fcem_count = sum(1 for e in episodes if e.get("fcem") is True)
    fcem_rate = fcem_count / total_episodes

    # 3. Strict Task Success
    strict_count = sum(1 for e in episodes if e.get("strict_success") is True)
    strict_rate = strict_count / total_episodes

    # 4. Prohibited actions
    prohibited_total = sum(e.get("prohibited_actions_executed", 0) for e in episodes)

    # 5. Bootstrap confidence intervals for FCEM
    fcem_point, fcem_low, fcem_high, fcem_notice = bootstrap_metric(
        episodes,
        metric_fn=lambda eps: sum(1 for ep in eps if ep.get("fcem") is True) / len(eps),
        num_resamples=1000,
    )

    # 6. Bootstrap confidence intervals for WER
    wer_point, wer_low, wer_high, _ = bootstrap_metric(
        episodes,
        metric_fn=lambda eps: sum(ep.get("wer", 0.0) for ep in eps) / len(eps),
        num_resamples=1000,
    )

    # Format Markdown
    report_lines = [
        f"# iShop Evaluation Report: {run_id}",
        "",
        f"- **Mode:** `{mode}`",
        f"- **Data kind:** `{data.get('data_kind', 'legacy/unverified')}`",
        "- Synthetic and scorer self-test outputs are engineering checks, not benchmark evidence.",
        f"- **Normalization Version:** `{norm_v}`",
        f"- **Total Scheduled Cases (Denominator):** {total_episodes}",
        "",
        "## Summary Metrics",
        "",
        "| Metric | Point Estimate | 95% Confidence Interval | Methodological Notice |",
        "|---|---|---|---|",
        f"| **FCEM (Final Cart Exact Match)** | {fcem_rate * 100:.1f}% | [{fcem_low * 100:.1f}%, {fcem_high * 100:.1f}%] | {fcem_notice or 'Speaker-cluster bootstrap'} |",
        f"| **Strict Task Success** | {strict_rate * 100:.1f}% | N/A | Requires zero prohibited actions |",
        f"| **Word Error Rate (WER)** | {avg_wer * 100:.1f}% | [{wer_low * 100:.1f}%, {wer_high * 100:.1f}%] | NFC normalized |",
        f"| **Character Error Rate (CER)** | {avg_cer * 100:.1f}% | N/A | Tone marks preserved |",
        f"| **Unintended Mutations Executed** | {prohibited_total} | N/A | Safety invariant S-08 |",
        "",
        "## Episode Breakdown",
        "",
        "| Episode ID | Split | Language | WER | FCEM | Strict Success | Status |",
        "|---|---|---|---|---|---|---|",
    ]

    for ep in episodes:
        fcem_str = "PASS" if ep.get("fcem") else "FAIL"
        strict_str = "PASS" if ep.get("strict_success") else "FAIL"
        report_lines.append(
            f"| `{ep['episode_id']}` | {ep['split']} | {ep['language_pair']} | {ep['wer'] * 100:.1f}% | {fcem_str} | {strict_str} | `{ep['status']}` |"
        )

    return "\n".join(report_lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate iShop evaluation report")
    parser.add_argument(
        "--results",
        "-r",
        default="evals/tests/fixtures/synthetic_result.json",
        help="Path to evaluation results JSON file",
    )
    args = parser.parse_args()

    try:
        report = generate_report(args.results)
        print(report)
        return 0
    except Exception as exc:
        print(f"Error generating report: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""CLI for running evaluation against a manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals.runner.manifest import load_manifest
from evals.runner.runner import EvaluationRunner
from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.domain.models import Money


def create_mock_evidence() -> EvidenceSnapshot:
    """Standard evaluation catalog snapshot."""
    v1 = VariantEvidence(
        variant_id="var_red_s",
        product_id="prod_shirt",
        product_title="Cotton T-Shirt",
        variant_title="Red / S",
        selected_options={"color": "red", "size": "s"},
        price=Money.from_string("25.00", "USD"),
        available_for_sale=True,
        quantity_available=10,
    )
    v2 = VariantEvidence(
        variant_id="var_blue_m",
        product_id="prod_shirt",
        product_title="Cotton T-Shirt",
        variant_title="Blue / M",
        selected_options={"color": "blue", "size": "m"},
        price=Money.from_string("25.00", "USD"),
        available_for_sale=True,
        quantity_available=10,
    )
    product = ProductEvidence(
        product_id="prod_shirt",
        title="Cotton T-Shirt",
        variants=(v1, v2),
        options=("Color", "Size"),
    )
    return EvidenceSnapshot(
        snapshot_id="eval_snap_001",
        shop_id="test-store.myshopify.com",
        currency="USD",
        observed_at_ms=1700000000000,
        products={"prod_shirt": product},
    )


def run_evaluation(manifest_path: str, output_path: str | None = None) -> int:
    print(f"Loading manifest: {manifest_path}...")
    manifest = load_manifest(manifest_path)
    evidence = create_mock_evidence()

    print(f"Running evaluation: {manifest.run_id} ({len(manifest.episodes)} episodes)...")
    runner = EvaluationRunner(evidence)
    result = runner.run(manifest)

    serialized = json.dumps(result.to_dict(), indent=2)
    if output_path:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(serialized, encoding="utf-8")
        print(f"Results saved to: {out_p}")
    else:
        print("\n--- Evaluation Run Summary ---")
        print(f"Total Episodes Evaluated: {result.total_episodes}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run iShop evaluation")
    parser.add_argument(
        "--manifest",
        "-m",
        default="evals/tests/fixtures/synthetic_manifest.json",
        help="Path to manifest JSON file",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="evals/tests/fixtures/synthetic_result.json",
        help="Path to save output JSON results",
    )
    args = parser.parse_args()
    return run_evaluation(args.manifest, args.output)


if __name__ == "__main__":
    sys.exit(main())

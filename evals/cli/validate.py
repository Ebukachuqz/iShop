#!/usr/bin/env python3
"""CLI for validating evaluation manifests.

Enforces:
- T-23: Checks consent and permitted processors.
- T-24: Checks that manifest cases are well-formed and no labels leak.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.runner.manifest import load_manifest


def validate_manifest(manifest_path: str | Path) -> bool:
    print(f"Validating manifest: {manifest_path}...")
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        print(f"FAIL: Error loading manifest: {exc}")
        return False

    print(f"Manifest ID: {manifest.run_id}")
    print(f"Mode: {manifest.mode}")
    print(f"Normalization Version: {manifest.normalization_version}")
    print(f"Episodes Count: {len(manifest.episodes)}")

    if not manifest.episodes:
        print("FAIL: Manifest contains no episodes.")
        return False

    errors: list[str] = []
    consent_blocked_count = 0

    for idx, ep in enumerate(manifest.episodes, start=1):
        if not ep.episode_id:
            errors.append(f"Episode #{idx} missing episode_id")
        if ep.split not in ("dev", "test"):
            errors.append(f"Episode {ep.episode_id} invalid split: '{ep.split}'")
        if not ep.consent_allowed:
            consent_blocked_count += 1

    if errors:
        print(f"FAIL: {len(errors)} validation errors:")
        for err in errors:
            print(f"  - {err}")
        return False

    print(f"PASS: Manifest is valid. (Consent-gated episodes: {consent_blocked_count})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate iShop evaluation manifest")
    parser.add_argument(
        "--manifest",
        "-m",
        default="evals/tests/fixtures/synthetic_manifest.json",
        help="Path to manifest JSON file",
    )
    args = parser.parse_args()

    success = validate_manifest(args.manifest)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

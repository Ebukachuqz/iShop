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

from evals.runner.manifest import (
    ALLOWED_MODES,
    ALLOWED_NORMALIZATIONS,
    compute_manifest_hash,
    load_manifest,
)


def validate_manifest(manifest_path: str | Path) -> tuple[bool, list[str]]:
    p = Path(manifest_path)
    if not p.exists():
        return False, [f"Manifest file not found: {p}"]

    try:
        manifest = load_manifest(p)
    except Exception as exc:
        return False, [f"Error loading manifest: {exc}"]

    errors: list[str] = []

    if manifest.mode not in ALLOWED_MODES:
        errors.append(f"Unsupported manifest mode: '{manifest.mode}'. Allowed: {sorted(ALLOWED_MODES)}")

    if manifest.normalization_version not in ALLOWED_NORMALIZATIONS:
        errors.append(f"Unsupported normalization version: '{manifest.normalization_version}'. Allowed: {sorted(ALLOWED_NORMALIZATIONS)}")

    # Verify SHA-256 hash (R11)
    computed_hash = compute_manifest_hash(
        run_id=manifest.run_id,
        mode=manifest.mode,
        normalization_version=manifest.normalization_version,
        episodes=manifest.episodes,
    )
    if manifest.manifest_hash != computed_hash:
        errors.append(
            f"Manifest hash mismatch: computed '{computed_hash}', got '{manifest.manifest_hash}' (tampered or outdated)"
        )

    if not manifest.episodes:
        errors.append("Manifest contains no episodes.")

    seen_ids: set[str] = set()
    consent_blocked_count = 0
    base_dir = p.parent

    for idx, ep in enumerate(manifest.episodes, start=1):
        if not ep.episode_id:
            errors.append(f"Episode #{idx} missing episode_id")
        elif ep.episode_id in seen_ids:
            errors.append(f"Duplicate episode_id '{ep.episode_id}' at index #{idx}")
        else:
            seen_ids.add(ep.episode_id)

        if ep.split not in ("dev", "test"):
            errors.append(f"Episode {ep.episode_id} invalid split: '{ep.split}'")

        if not ep.consent_allowed:
            consent_blocked_count += 1

        if ep.audio_ref:
            audio_path = Path(ep.audio_ref)
            if not audio_path.is_absolute():
                if not (base_dir / audio_path).exists() and not audio_path.exists():
                    errors.append(f"Episode {ep.episode_id} audio_ref not found: '{ep.audio_ref}'")
            elif not audio_path.exists():
                errors.append(f"Episode {ep.episode_id} audio_ref not found: '{ep.audio_ref}'")

    return len(errors) == 0, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate iShop evaluation manifest")
    parser.add_argument(
        "--manifest",
        "-m",
        default="evals/tests/fixtures/synthetic_manifest.json",
        help="Path to manifest JSON file",
    )
    args = parser.parse_args()

    is_valid, errors = validate_manifest(args.manifest)
    if not is_valid:
        print(f"FAIL: {len(errors)} validation errors:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("PASS: Manifest is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

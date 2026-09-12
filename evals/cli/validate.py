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
    from evals.runner.validation import validate_run
    try:
        manifest = load_manifest(manifest_path)
        errors = validate_run(manifest, Path(manifest_path).resolve().parent)
        return not errors, errors
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return False, [str(exc)]


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

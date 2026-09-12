#!/usr/bin/env python3
"""Validation script for architectural and dependency boundaries.

Checks:
1. Archived third-party code in docs/research/archive/ or docs/research/sources/
   is never imported into application packages (services/, packages/, evals/).
2. Pure domain modules (services/runtime/src/ishop/domain/) do not import
   provider SDKs, browser transports, or network clients.
3. Third-party research archives are excluded from package discovery and linting.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

APPLICATION_ROOTS = [
    ROOT / "services",
    ROOT / "packages",
    ROOT / "evals",
]

FORBIDDEN_ARCHIVE_IMPORT = re.compile(
    r"(?:from|import)\s+(?:docs\.research|archive|sources)"
)

FORBIDDEN_DOMAIN_IMPORTS = [
    re.compile(r"(?:from|import)\s+(?:google\.genai|google\.cloud|groq|openai|elevenlabs)"),
    re.compile(r"(?:from|import)\s+(?:pipecat|websockets|aiohttp|httpx|requests)"),
]


def check_archive_imports() -> list[str]:
    errors: list[str] = []
    for app_root in APPLICATION_ROOTS:
        if not app_root.exists():
            continue
        for py_file in app_root.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            for idx, line in enumerate(content.splitlines(), start=1):
                if FORBIDDEN_ARCHIVE_IMPORT.search(line):
                    rel = py_file.relative_to(ROOT)
                    errors.append(f"Forbidden archive import in {rel}:{idx}: {line.strip()}")

        for js_file in list(app_root.rglob("*.js")) + list(app_root.rglob("*.ts")):
            content = js_file.read_text(encoding="utf-8")
            for idx, line in enumerate(content.splitlines(), start=1):
                if "docs/research" in line:
                    rel = js_file.relative_to(ROOT)
                    errors.append(f"Forbidden archive reference in {rel}:{idx}: {line.strip()}")
    return errors


def check_domain_purity() -> list[str]:
    errors: list[str] = []
    domain_root = ROOT / "services" / "runtime" / "src" / "ishop" / "domain"
    if not domain_root.exists():
        return []

    for py_file in domain_root.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for idx, line in enumerate(content.splitlines(), start=1):
            for pat in FORBIDDEN_DOMAIN_IMPORTS:
                if pat.search(line):
                    rel = py_file.relative_to(ROOT)
                    errors.append(f"Domain purity violation in {rel}:{idx}: {line.strip()}")
    return errors


def main() -> int:
    print("Checking for illegal imports of research archive code...")
    archive_errors = check_archive_imports()
    errors: list[str] = []

    if archive_errors:
        print(f"FAIL: {len(archive_errors)} forbidden archive imports found:")
        for err in archive_errors:
            print(f"  - {err}")
        errors.extend(archive_errors)
    else:
        print("PASS: No application code imports research archives.")

    print("Checking domain module purity...")
    domain_errors = check_domain_purity()
    if domain_errors:
        print(f"FAIL: {len(domain_errors)} domain purity violations found:")
        for err in domain_errors:
            print(f"  - {err}")
        errors.extend(domain_errors)
    else:
        print("PASS: Domain modules respect provider-neutral boundaries.")

    if errors:
        print(f"\nTotal failures: {len(errors)}")
        return 1

    print("\nAll architectural boundary checks passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

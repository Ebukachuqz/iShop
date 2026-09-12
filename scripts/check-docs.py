#!/usr/bin/env python3
"""Validation script for iShop documentation integrity.

Checks:
1. Markdown local links resolve to existing files (excluding third-party archive sources).
2. All 15 work packages are present in docs/plans/STATUS.md.
3. Safety invariants (S-01 to S-14) map to defined test IDs (T-01 to T-30) in docs/TESTING.md.
4. Absence of machine-specific absolute paths in active documentation.
5. In CI mode (--ci or CI=true): if docs/ is not present (due to gitignore),
   gracefully skips local-only doc checks.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOC_ROOTS = [
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "evals" / "AGENTS.md",
    ROOT / "docs",
]

EXCLUDED_FROM_LINK_CHECK = {
    ROOT / "docs" / "research" / "sources",
    ROOT / "docs" / "research" / "archive",
}


def is_excluded(path: Path) -> bool:
    for exc in EXCLUDED_FROM_LINK_CHECK:
        if exc in path.parents or path == exc:
            return True
    return False


def check_markdown_links(doc_roots: list[Path] | None = None) -> list[str]:
    errors: list[str] = []
    roots = doc_roots or DOC_ROOTS
    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    for doc_root in roots:
        if not doc_root.exists():
            continue
        if doc_root.is_file():
            md_files = [doc_root]
        elif doc_root.is_dir():
            md_files = list(doc_root.rglob("*.md"))
        else:
            continue

        for md_file in md_files:
            if is_excluded(md_file):
                continue

            content = md_file.read_text(encoding="utf-8")
            for match in link_pattern.finditer(content):
                target = match.group(2).strip()

                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue

                target_path_str = target.split("#")[0]
                if not target_path_str:
                    continue

                if target_path_str.startswith("file:///"):
                    target_path_str = target_path_str[8:]
                    resolved_path = Path(target_path_str)
                else:
                    resolved_path = (md_file.parent / target_path_str).resolve()

                if not resolved_path.exists():
                    rel_source = md_file.relative_to(ROOT) if md_file.is_relative_to(ROOT) else md_file
                    errors.append(f"Broken link in {rel_source}: [{match.group(1)}]({target}) -> {resolved_path}")

    return errors


def check_status_work_packages(status_content: str | None = None) -> list[str]:
    errors: list[str] = []
    if status_content is None:
        status_file = ROOT / "docs" / "plans" / "STATUS.md"
        if not status_file.exists():
            return ["docs/plans/STATUS.md does not exist"]
        status_content = status_file.read_text(encoding="utf-8")

    expected_packages = [
        "WP-00",
        "WP-01",
        "WP-02",
        "WP-03",
        "WP-04",
        "WP-05",
        "WP-06",
        "WP-07",
        "WP-08",
        "WP-09",
        "WP-09B",
        "WP-10",
        "WP-11",
        "WP-12",
        "WP-13",
    ]

    for wp in expected_packages:
        if wp not in status_content:
            errors.append(f"Package {wp} missing from docs/plans/STATUS.md")

    return errors


def parse_safety_invariants(safety_markdown: str) -> dict[str, list[str]]:
    """Parses safety invariants and their mapped test IDs from SAFETY.md markdown table."""
    invariants: dict[str, list[str]] = {}
    row_pattern = re.compile(r"^\s*\|\s*(S-\d{2})\s*\|([^|]+)\|([^|]+)\|\s*([^|]+)\|\s*$", re.MULTILINE)
    test_id_pattern = re.compile(r"T-\d{2}")

    for match in row_pattern.finditer(safety_markdown):
        s_id = match.group(1).strip()
        evidence_col = match.group(4).strip()
        mapped_tests = test_id_pattern.findall(evidence_col)
        invariants[s_id] = mapped_tests

    return invariants


def parse_test_cases(testing_markdown: str) -> set[str]:
    """Parses defined test IDs from TESTING.md markdown table."""
    test_ids: set[str] = set()
    row_pattern = re.compile(r"^\s*\|\s*(T-\d{2})\s*\|", re.MULTILINE)
    for match in row_pattern.finditer(testing_markdown):
        test_ids.add(match.group(1).strip())
    return test_ids


def validate_invariant_mappings(safety_markdown: str, testing_markdown: str) -> list[str]:
    """Validates that all S-01..S-14 invariants are defined, have mapped tests, and tests exist in TESTING.md."""
    errors: list[str] = []
    invariants = parse_safety_invariants(safety_markdown)
    defined_tests = parse_test_cases(testing_markdown)

    # Check S-01 through S-14
    for i in range(1, 15):
        s_id = f"S-{i:02d}"
        if s_id not in invariants:
            errors.append(f"Invariant {s_id} is missing from SAFETY.md invariant table")
        else:
            mapped = invariants[s_id]
            if not mapped:
                errors.append(f"Invariant {s_id} has no mapped test cases in Evidence column")
            else:
                for test_id in mapped:
                    if test_id not in defined_tests:
                        errors.append(
                            f"Invariant {s_id} maps to undefined test case {test_id} (not found in TESTING.md)"
                        )

    return errors


def check_safety_invariants() -> list[str]:
    safety_file = ROOT / "docs" / "SAFETY.md"
    testing_file = ROOT / "docs" / "TESTING.md"

    if not safety_file.exists() or not testing_file.exists():
        return ["docs/SAFETY.md or docs/TESTING.md missing"]

    safety_content = safety_file.read_text(encoding="utf-8")
    testing_content = testing_file.read_text(encoding="utf-8")
    return validate_invariant_mappings(safety_content, testing_content)


def check_machine_specific_paths(doc_roots: list[Path] | None = None) -> list[str]:
    errors: list[str] = []
    roots = doc_roots or DOC_ROOTS
    forbidden_patterns = [
        re.compile(r"[a-zA-Z]:\\(?:Users|home|tmp)"),
        re.compile(r"/(?:Users|home)/[a-zA-Z0-9_-]+"),
    ]

    for doc_root in roots:
        if not doc_root.exists():
            continue
        if doc_root.is_file():
            md_files = [doc_root]
        elif doc_root.is_dir():
            md_files = list(doc_root.rglob("*.md"))
        else:
            continue

        for md_file in md_files:
            if is_excluded(md_file):
                continue

            content = md_file.read_text(encoding="utf-8")
            for line_idx, line in enumerate(content.splitlines(), start=1):
                for pattern in forbidden_patterns:
                    if pattern.search(line):
                        rel_source = md_file.relative_to(ROOT) if md_file.is_relative_to(ROOT) else md_file
                        errors.append(f"Machine-specific path in {rel_source}:{line_idx}: {line.strip()}")

    return errors


def main() -> int:
    is_ci = "--ci" in sys.argv or os.environ.get("CI") == "true"
    docs_dir = ROOT / "docs"

    if is_ci and not docs_dir.exists():
        print("[check-docs] Running in CI mode and docs/ directory is not present (excluded by .gitignore).")
        print("[check-docs] Skipping local-only documentation validation in CI.")
        return 0

    all_errors: list[str] = []

    print("Checking markdown links...")
    link_errors = check_markdown_links()
    if link_errors:
        print(f"FAIL: {len(link_errors)} broken links found:")
        for err in link_errors:
            print(f"  - {err}")
        all_errors.extend(link_errors)
    else:
        print("PASS: All markdown links resolve.")

    print("Checking STATUS work packages...")
    wp_errors = check_status_work_packages()
    if wp_errors:
        print(f"FAIL: {len(wp_errors)} missing packages:")
        for err in wp_errors:
            print(f"  - {err}")
        all_errors.extend(wp_errors)
    else:
        print("PASS: All 15 work packages present in STATUS.md.")

    print("Checking safety invariant to test case mappings...")
    safety_errors = check_safety_invariants()
    if safety_errors:
        print(f"FAIL: {len(safety_errors)} safety mapping errors:")
        for err in safety_errors:
            print(f"  - {err}")
        all_errors.extend(safety_errors)
    else:
        print("PASS: All 14 safety invariants map to valid defined test cases in TESTING.md.")

    print("Checking for machine-specific paths...")
    path_errors = check_machine_specific_paths()
    if path_errors:
        print(f"FAIL: {len(path_errors)} machine-specific paths found:")
        for err in path_errors:
            print(f"  - {err}")
        all_errors.extend(path_errors)
    else:
        print("PASS: No machine-specific paths found in active documentation.")

    if all_errors:
        print(f"\nTotal failures: {len(all_errors)}")
        return 1

    print("\nAll documentation integrity checks passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

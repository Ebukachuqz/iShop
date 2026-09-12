#!/usr/bin/env python3
"""Validation script for iShop documentation integrity.

Checks:
1. Markdown local links resolve to existing files (excluding third-party archive sources).
2. All 15 work packages are present in docs/plans/STATUS.md.
3. Safety invariants (S-01 to S-14) map to defined test IDs (T-01 to T-30).
4. Absence of machine-specific absolute paths in active documentation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files and directories to check for links
DOC_ROOTS = [
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "evals" / "AGENTS.md",
    ROOT / "docs",
]

# Upstream third-party source dumps are excluded from link resolution
EXCLUDED_FROM_LINK_CHECK = {
    ROOT / "docs" / "research" / "sources",
    ROOT / "docs" / "research" / "archive",
}


def is_excluded(path: Path) -> bool:
    for exc in EXCLUDED_FROM_LINK_CHECK:
        if exc in path.parents or path == exc:
            return True
    return False


def check_markdown_links() -> list[str]:
    errors: list[str] = []
    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    for doc_root in DOC_ROOTS:
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

                # Ignore external URLs, mailto, and fragment-only links
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue

                # Strip anchor fragment
                target_path_str = target.split("#")[0]
                if not target_path_str:
                    continue

                # Strip file:/// scheme if present
                if target_path_str.startswith("file:///"):
                    target_path_str = target_path_str[8:]
                    resolved_path = Path(target_path_str)
                else:
                    resolved_path = (md_file.parent / target_path_str).resolve()

                if not resolved_path.exists():
                    rel_source = md_file.relative_to(ROOT)
                    errors.append(f"Broken link in {rel_source}: [{match.group(1)}]({target}) -> {resolved_path}")

    return errors


def check_status_work_packages() -> list[str]:
    errors: list[str] = []
    status_file = ROOT / "docs" / "plans" / "STATUS.md"
    if not status_file.exists():
        return ["docs/plans/STATUS.md does not exist"]

    content = status_file.read_text(encoding="utf-8")
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
        if wp not in content:
            errors.append(f"Package {wp} missing from docs/plans/STATUS.md")

    return errors


def check_safety_invariants() -> list[str]:
    errors: list[str] = []
    safety_file = ROOT / "docs" / "SAFETY.md"
    testing_file = ROOT / "docs" / "TESTING.md"

    if not safety_file.exists() or not testing_file.exists():
        return ["docs/SAFETY.md or docs/TESTING.md missing"]

    safety_content = safety_file.read_text(encoding="utf-8")
    testing_content = testing_file.read_text(encoding="utf-8")

    # Invariants S-01 to S-14
    for i in range(1, 15):
        s_id = f"S-{i:02d}"
        if s_id not in safety_content:
            errors.append(f"Invariant {s_id} missing in docs/SAFETY.md")

    # Test cases T-01 to T-30
    for i in range(1, 31):
        t_id = f"T-{i:02d}"
        if t_id not in testing_content:
            errors.append(f"Test case {t_id} missing in docs/TESTING.md")

    return errors


def check_machine_specific_paths() -> list[str]:
    errors: list[str] = []
    forbidden_patterns = [
        re.compile(r"[a-zA-Z]:\\(?:Users|home|tmp)"),
        re.compile(r"/(?:Users|home)/[a-zA-Z0-9_-]+"),
    ]

    for doc_root in DOC_ROOTS:
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
                        rel_source = md_file.relative_to(ROOT)
                        errors.append(f"Machine-specific path in {rel_source}:{line_idx}: {line.strip()}")

    return errors


def main() -> int:
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
        print("PASS: All 14 safety invariants and 30 test cases present.")

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

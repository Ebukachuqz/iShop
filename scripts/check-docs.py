#!/usr/bin/env python3
"""Validate public Markdown shipped with iShop."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ROOTS = [ROOT / "README.md", ROOT / "evals" / "reports"]
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
FORBIDDEN_PATTERNS = {
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\"),
    "Unix user path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+"),
    "internal agent instruction": re.compile(r"\b(?:AGENTS\.md|docs/plans|docs/research)\b", re.IGNORECASE),
    "competition-specific wording": re.compile(r"\bhackathon\b", re.IGNORECASE),
}


def markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in PUBLIC_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.md")))
    return files


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []
    content = path.read_text(encoding="utf-8")
    source = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path

    for match in LINK_PATTERN.finditer(content):
        target = match.group(2).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target_path = target.split("#", 1)[0]
        if target_path and not (path.parent / target_path).resolve().exists():
            errors.append(f"Broken link in {source}: [{match.group(1)}]({target})")

    for label, pattern in FORBIDDEN_PATTERNS.items():
        for line_number, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                errors.append(f"{label} in {source}:{line_number}")

    return errors


def main() -> int:
    files = markdown_files()
    if not files:
        print("FAIL: no public Markdown files were found")
        return 1

    errors = [error for path in files for error in validate_file(path)]
    if errors:
        print(f"FAIL: {len(errors)} public documentation issue(s):")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"PASS: validated {len(files)} public Markdown file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

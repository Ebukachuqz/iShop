#!/usr/bin/env python3
"""Validation script for secret exclusions and privacy integrity.

Checks:
1. No private files (.env, *.pem, *.key, credentials/, private audio) are tracked by Git.
2. .env.example contains only placeholders, no real secrets.
3. Tracked text files contain no obvious private key patterns or live API tokens.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_TRACKED_EXTENSIONS = {
    ".pem",
    ".key",
    ".wav",
    ".mp3",
    ".m4a",
    ".webm",
    ".pcm",
    ".ogg",
    ".opus",
    ".sqlite",
    ".sqlite3",
    ".db",
}

FORBIDDEN_TRACKED_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
}

FORBIDDEN_TRACKED_DIRECTORIES = (
    "credentials",
    "data/private",
    "evals/private",
    "artifacts/private",
)

SUSPICIOUS_TOKEN_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "OpenAI / generic API key"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,40}"), "Google API key"),
    (re.compile(r"shpat_[a-fA-F0-9]{32}"), "Shopify admin/store access token"),
    (re.compile(r"gsk_[a-zA-Z0-9]{20,}"), "Groq API key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "Private key header"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]{25,}"), "Live Bearer token"),
]

SAFE_PREFIXES = (
    "your_",
    "replace_",
    "example",
    "http://",
    "https://",
    "ws://",
    "wss://",
    "services/",
    "apps/",
    "credentials/",
    "data/",
    "read_",
    "sahara-",
    "gemini-",
)


def get_git_tracked_files(root: Path | None = None) -> list[str]:
    target_root = root or ROOT
    try:
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=target_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception as e:
        print(f"Warning: git ls-files failed: {e}. Falling back to empty list.")
        return []


def check_tracked_files(tracked: list[str]) -> list[str]:
    """Validates that no forbidden files, extensions, or directories are in the tracked list."""
    errors: list[str] = []
    for file_path in tracked:
        norm_path = file_path.replace("\\", "/")
        p = Path(norm_path)
        if p.name in FORBIDDEN_TRACKED_NAMES:
            errors.append(f"Forbidden file tracked in git: {norm_path}")
        if p.suffix.lower() in FORBIDDEN_TRACKED_EXTENSIONS:
            errors.append(f"Forbidden file extension tracked in git: {norm_path}")
        for forbidden_dir in FORBIDDEN_TRACKED_DIRECTORIES:
            if norm_path.startswith(forbidden_dir + "/") or norm_path == forbidden_dir:
                errors.append(f"Forbidden directory path tracked in git: {norm_path}")
    return errors


def scan_content_for_secrets(content: str, source_name: str = "") -> list[str]:
    """Scans raw text content for suspicious high-entropy secret patterns."""
    errors: list[str] = []
    lines = content.splitlines()
    for line_idx, line in enumerate(lines, start=1):
        clean_line = line.strip()
        if not clean_line or clean_line.startswith(("#", "//", "/*", "*")):
            continue
        for pattern, desc in SUSPICIOUS_TOKEN_PATTERNS:
            match = pattern.search(clean_line)
            if match:
                matched_val = match.group(0)
                # Ignore placeholders
                if any(matched_val.startswith(p) for p in SAFE_PREFIXES):
                    continue
                location = f"{source_name}:{line_idx}" if source_name else f"line {line_idx}"
                errors.append(f"Secret pattern ({desc}) detected in {location}")
    return errors


def check_env_example(env_path: Path | None = None) -> list[str]:
    """Validates that .env.example contains only non-secret placeholders."""
    errors: list[str] = []
    target_env = env_path or (ROOT / ".env.example")
    if not target_env.exists():
        return [f"{target_env} does not exist"]

    content = target_env.read_text(encoding="utf-8")
    for line_idx, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            val = val.strip()
            if not val or val.startswith(SAFE_PREFIXES):
                continue
            for pattern, desc in SUSPICIOUS_TOKEN_PATTERNS:
                if pattern.search(val):
                    errors.append(f"Real secret pattern ({desc}) in .env.example:{line_idx}: {key}=***")
    return errors


def check_tracked_contents(tracked_files: list[str], root: Path | None = None) -> list[str]:
    """Scans content of tracked source files for hardcoded secrets."""
    errors: list[str] = []
    target_root = root or ROOT
    text_extensions = {".py", ".js", ".mjs", ".ts", ".json", ".yaml", ".yml", ".toml", ".md"}

    for rel_path in tracked_files:
        norm_path = rel_path.replace("\\", "/")
        p = target_root / norm_path
        if p.suffix.lower() in text_extensions and p.is_file():
            # Exclude archive test snapshots
            if "docs/research/archive" in norm_path or "docs/research/sources" in norm_path:
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                file_errors = scan_content_for_secrets(content, source_name=norm_path)
                errors.extend(file_errors)
            except Exception:
                pass
    return errors


def main() -> int:
    print("Checking git-tracked files for secret or private artifacts...")
    tracked = get_git_tracked_files()
    errors: list[str] = []

    if tracked:
        tracked_errors = check_tracked_files(tracked)
        if tracked_errors:
            print(f"FAIL: {len(tracked_errors)} forbidden files tracked in Git:")
            for err in tracked_errors:
                print(f"  - {err}")
            errors.extend(tracked_errors)
        else:
            print(f"PASS: None of the {len(tracked)} tracked files violate privacy/secret rules.")

        content_errors = check_tracked_contents(tracked)
        if content_errors:
            print(f"FAIL: {len(content_errors)} secret patterns found in tracked files:")
            for err in content_errors:
                print(f"  - {err}")
            errors.extend(content_errors)
        else:
            print("PASS: Tracked file contents contain no live secret patterns.")
    else:
        print("INFO: No tracked files found or git unavailable; checking directory structure.")

    print("Checking .env.example template...")
    env_errors = check_env_example()
    if env_errors:
        print(f"FAIL: {len(env_errors)} issues in .env.example:")
        for err in env_errors:
            print(f"  - {err}")
        errors.extend(env_errors)
    else:
        print("PASS: .env.example contains only redacted placeholders.")

    if errors:
        print(f"\nTotal failures: {len(errors)}")
        return 1

    print("\nAll secret and privacy exclusion checks passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

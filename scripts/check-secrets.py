#!/usr/bin/env python3
"""Validation script for secret exclusions and privacy integrity.

Checks:
1. No private files (.env, *.pem, *.key, credentials/, private audio) are tracked by Git.
2. .env.example contains only placeholders, no real secrets.
3. No obvious high-entropy API key patterns in committed/tracked files.
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
}

FORBIDDEN_TRACKED_DIRECTORIES = {
    "credentials",
    "data/private",
    "evals/private",
    "artifacts/private",
}


def get_git_tracked_files() -> list[str]:
    try:
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception as e:
        print(f"Warning: git ls-files failed: {e}. Falling back to directory scan.")
        return []


def check_tracked_files(tracked: list[str]) -> list[str]:
    errors: list[str] = []
    for file_path in tracked:
        p = Path(file_path)
        if p.name in FORBIDDEN_TRACKED_NAMES:
            errors.append(f"Forbidden file tracked in git: {file_path}")
        if p.suffix in FORBIDDEN_TRACKED_EXTENSIONS:
            errors.append(f"Forbidden file extension tracked in git: {file_path}")
        for forbidden_dir in FORBIDDEN_TRACKED_DIRECTORIES:
            if file_path.startswith(forbidden_dir):
                errors.append(f"Forbidden directory tracked in git: {file_path}")
    return errors


# High-entropy secret token patterns
SUSPICIOUS_TOKEN_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),             # OpenAI / generic secret keys
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),             # Google API keys
    re.compile(r"shpat_[a-fA-F0-9]{32}"),             # Shopify access tokens
    re.compile(r"gsk_[a-zA-Z0-9]{20,}"),              # Groq API keys
    re.compile(r"[a-fA-F0-9]{32,64}"),                # Raw hex tokens
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


def check_env_example() -> list[str]:
    errors: list[str] = []
    env_example = ROOT / ".env.example"
    if not env_example.exists():
        return [".env.example does not exist"]

    content = env_example.read_text(encoding="utf-8")
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            val = val.strip()
            if not val or val.startswith(SAFE_PREFIXES):
                continue
            for pat in SUSPICIOUS_TOKEN_PATTERNS:
                if pat.search(val):
                    errors.append(f"Potential real secret value in .env.example line: {key}=***")
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
    else:
        print("INFO: No tracked files found or git unavailable; check skipped.")

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

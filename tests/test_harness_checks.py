"""Automated tests for validation harness scripts.

Includes negative test cases for secret scanning and safety invariant mappings
to ensure false passes are impossible under Safety S-13.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_script_module(module_name: str, script_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_docs = load_script_module("check_docs", ROOT / "scripts" / "check-docs.py")
check_secrets = load_script_module("check_secrets", ROOT / "scripts" / "check-secrets.py")

check_status_work_packages = check_docs.check_status_work_packages
parse_safety_invariants = check_docs.parse_safety_invariants
parse_test_cases = check_docs.parse_test_cases
validate_invariant_mappings = check_docs.validate_invariant_mappings

check_tracked_files = check_secrets.check_tracked_files
scan_content_for_secrets = check_secrets.scan_content_for_secrets
check_env_example = check_secrets.check_env_example


class TestSecretScanning:
    def test_negative_forbidden_file_names(self):
        forbidden_samples = [
            ".env",
            "subdir/.env",
            ".env.local",
            ".env.production",
            ".env.staging",
        ]
        errors = check_tracked_files(forbidden_samples)
        assert len(errors) == len(forbidden_samples)
        for err in errors:
            assert "Forbidden file tracked in git" in err

    def test_negative_forbidden_extensions(self):
        forbidden_extensions = [
            "keys/server.pem",
            "server.key",
            "shopper.wav",
            "recording.mp3",
            "speech.m4a",
            "audio.webm",
            "raw.pcm",
            "prompt.ogg",
            "voice.opus",
            "ishop_runtime.sqlite",
            "store.sqlite3",
            "cache.db",
        ]
        errors = check_tracked_files(forbidden_extensions)
        assert len(errors) == len(forbidden_extensions)
        for err in errors:
            assert "Forbidden file extension tracked in git" in err

    def test_negative_forbidden_directories(self):
        forbidden_dirs = [
            "credentials/google_cloud.json",
            "data/private/audio_001.wav",
            "evals/private/manifest.json",
            "artifacts/private/trace.json",
        ]
        errors = check_tracked_files(forbidden_dirs)
        assert len(errors) >= len(forbidden_dirs)
        for err in errors:
            assert "Forbidden directory path tracked in git" in err or "Forbidden file extension" in err

    def test_positive_permitted_files(self):
        permitted = [
            "package.json",
            "pyproject.toml",
            ".env.example",
            "README.md",
            "services/runtime/src/ishop/__init__.py",
            "packages/contracts/package.json",
            "scripts/command-dispatcher.mjs",
        ]
        errors = check_tracked_files(permitted)
        assert errors == []

    def test_negative_content_token_detection(self):
        leaks = [
            ("sk-" + "proj-abc123456789012345678901234567890", "OpenAI"),
            ("AIza" + "SyB1234567890123456789012345678901", "Google"),
            ("shpat_" + "0123456789abcdef0123456789abcdef", "Shopify"),
            ("gsk_" + "123456789012345678901234567890", "Groq"),
            ("-----BEGIN " + "RSA PRIVATE KEY-----", "Private key"),
            ("Bearer " + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummysecret1234567890", "Bearer"),
        ]
        for token, desc in leaks:
            content = f"API_KEY={token}\n"
            errors = scan_content_for_secrets(content, "test.env")
            assert len(errors) >= 1, f"Failed to detect leak: {desc}"

    def test_positive_safe_placeholders(self):
        safe_content = """
        OPENAI_API_KEY=your_openai_api_key
        GEMINI_API_KEY=your_gemini_api_key
        GROQ_API_KEY=your_groq_api_key
        SAHARA_TOKEN=replace_with_token
        SHOPIFY_API_KEY=example_api_key
        """
        errors = scan_content_for_secrets(safe_content, "safe.env")
        assert errors == []


class TestInvariantMappings:
    def test_parse_safety_invariants(self):
        sample_safety = """
| ID | Invariant | Required enforcement | Evidence |
|---|---|---|---|
| S-01 | No payment | Deny | T-01, T-20 |
| S-02 | Checkout handoff | Terminate | T-20, T-21 |
"""
        invariants = parse_safety_invariants(sample_safety)
        assert "S-01" in invariants
        assert invariants["S-01"] == ["T-01", "T-20"]
        assert "S-02" in invariants
        assert invariants["S-02"] == ["T-20", "T-21"]

    def test_negative_missing_invariant(self):
        sample_safety = "\n".join(
            f"| S-{i:02d} | Invariant {i} | Enforcement | T-01 |" for i in range(1, 14)
        )
        sample_testing = "| T-01 | Test 1 | WP-01 |"

        errors = validate_invariant_mappings(sample_safety, sample_testing)
        assert any("Invariant S-14 is missing" in err for err in errors)

    def test_negative_unmapped_invariant(self):
        sample_safety = "\n".join(
            f"| S-{i:02d} | Invariant {i} | Enforcement | {'T-01' if i != 3 else ''} |"
            for i in range(1, 15)
        )
        sample_testing = "| T-01 | Test 1 | WP-01 |"

        errors = validate_invariant_mappings(sample_safety, sample_testing)
        assert any("Invariant S-03 has no mapped test cases" in err for err in errors)

    def test_negative_undefined_test_case_in_mapping(self):
        sample_safety = "\n".join(
            f"| S-{i:02d} | Invariant {i} | Enforcement | {'T-01' if i != 2 else 'T-99'} |"
            for i in range(1, 15)
        )
        sample_testing = "| T-01 | Test 1 | WP-01 |"

        errors = validate_invariant_mappings(sample_safety, sample_testing)
        assert any("maps to undefined test case T-99" in err for err in errors)

    def test_negative_status_work_packages(self):
        status_missing_wp = """
| Package | State | Evidence |
| [WP-00](work-packages/WP-00.md) | complete | done |
| [WP-01](work-packages/WP-01.md) | queued | next |
"""
        errors = check_status_work_packages(status_missing_wp)
        assert len(errors) == 13

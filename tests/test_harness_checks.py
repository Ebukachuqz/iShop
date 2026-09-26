"""Automated tests for public-documentation and secret validation scripts."""

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

validate_public_doc = check_docs.validate_file

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
        SHOPIFY_APP_CLIENT_ID=example_client_id
        """
        errors = scan_content_for_secrets(safe_content, "safe.env")
        assert errors == []


class TestPublicDocumentation:
    def test_valid_local_and_external_links(self, tmp_path: Path):
        target = tmp_path / "target.md"
        target.write_text("# Target\n", encoding="utf-8")
        source = tmp_path / "source.md"
        source.write_text("[Local](target.md) and [Web](https://example.com)\n", encoding="utf-8")

        assert validate_public_doc(source) == []

    def test_broken_local_link_fails(self, tmp_path: Path):
        source = tmp_path / "source.md"
        source.write_text("[Missing](missing.md)\n", encoding="utf-8")

        errors = validate_public_doc(source)
        assert len(errors) == 1
        assert "Broken link" in errors[0]

    def test_internal_or_machine_specific_content_fails(self, tmp_path: Path):
        source = tmp_path / "source.md"
        source.write_text(
            "Read AGENTS.md and C:\\Users\\example\\private.txt for the hackathon.\n",
            encoding="utf-8",
        )

        errors = validate_public_doc(source)
        assert any("internal agent instruction" in error for error in errors)
        assert any("Windows user path" in error for error in errors)
        assert any("competition-specific wording" in error for error in errors)

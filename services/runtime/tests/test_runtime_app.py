from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from ishop.app import _verified_command_response, create_runtime_app
from ishop.config import RuntimeSettings


def settings() -> RuntimeSettings:
    return RuntimeSettings(
        host="127.0.0.1",
        port=8000,
        signing_secret="runtime_test_signing_secret_123456789",
        allowed_origins=frozenset({"https://shop.myshopify.com"}),
        sahara_api_key="test-key",
        record_audio=False,
    )


def test_runtime_accepts_configured_gemini_merchant_profile() -> None:
    configured = RuntimeSettings(
        host="127.0.0.1",
        port=8000,
        signing_secret="runtime_test_signing_secret_123456789",
        allowed_origins=frozenset({"https://shop.myshopify.com"}),
        sahara_api_key="test-key",
        record_audio=False,
        gemini_api_key="configured-gemini-key",
    )
    # App construction exercises the production registry and allowlist wiring.
    with TestClient(create_runtime_app(configured)) as client:
        assert client.get("/health").status_code == 200


def test_health_reports_safe_runtime_configuration() -> None:
    with TestClient(create_runtime_app(settings())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ishop-runtime",
        "speech_profile": "sahara-stream-pcm",
        "audio_recording": False,
    }


def test_verified_add_receipt_names_quantity_and_hides_default_variant() -> None:
    command = {
        "operation": "add_variant",
        "parameters": {"variant_id": "variant_1", "quantity": 1},
    }
    result = {
        "before_cart": {"lines": [{"variant_id": "variant_1", "quantity": 4}]},
        "after_cart": {
            "lines": [{"variant_id": "variant_1", "quantity": 5}],
            "display_lines": [{
                "variant_id": "variant_1", "title": "The Collection Snowboard: Hydrogen",
                "variant_title": "Default Title", "quantity": 5,
            }],
        },
    }
    fallback, facts, operation = _verified_command_response(command, result, True)
    assert operation.value == "add_to_cart"
    assert "added 1 The Collection Snowboard: Hydrogen" in fallback
    assert "now have 5" in fallback
    assert "Default Title" not in fallback
    assert "total cart quantity 5" in facts


def test_runtime_settings_require_secret_origin_and_sahara_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "SESSION_SIGNING_SECRET",
        "DEV_ALLOWED_ORIGINS",
        "SAHARA_API_KEY",
        "INTRON_API_KEY",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="SESSION_SIGNING_SECRET"):
        RuntimeSettings.from_environment()


def test_runtime_settings_reject_wildcard_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SIGNING_SECRET", "runtime_test_signing_secret_123456789")
    monkeypatch.setenv("DEV_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("SAHARA_API_KEY", "test-key")
    with pytest.raises(ValueError, match="Wildcard"):
        RuntimeSettings.from_environment()


def test_runtime_settings_reject_short_control_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_SIGNING_SECRET", "runtime_test_signing_secret_123456789")
    monkeypatch.setenv("DEV_ALLOWED_ORIGINS", "https://shop.myshopify.com")
    monkeypatch.setenv("SAHARA_API_KEY", "test-key")
    monkeypatch.setenv("RUNTIME_CONTROL_SECRET", "short")
    with pytest.raises(ValueError, match="RUNTIME_CONTROL_SECRET"):
        RuntimeSettings.from_environment()

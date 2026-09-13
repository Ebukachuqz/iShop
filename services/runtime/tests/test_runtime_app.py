from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from ishop.app import create_runtime_app
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

"""Unit tests for speech recognition providers and registry (WP-08, T-14, T-30)."""

import pytest
import asyncio
from ishop.speech.factory import create_default_speech_registry
from ishop.speech.fake import FakeSpeechProvider
from ishop.speech.sahara import SaharaSpeechProvider
from ishop.speech.groq_whisper import GroqWhisperSpeechProvider
from ishop.speech.assemblyai import AssemblyAiSpeechProvider
from ishop.speech.gemini_transcribe import GeminiSpeechProvider
from ishop.speech.elevenlabs_scribe import ElevenLabsScribeSpeechProvider
from ishop.speech.base import SpeechProviderError, SpeechTranscriptionResult


def test_speech_registry_registration_and_listing():
    """T-30: Speech registry lists all active speech routes."""
    registry = create_default_speech_registry()
    profiles = registry.list_profiles()
    profile_ids = {p.profile_id for p in profiles}

    assert "sahara-intron-asr" in profile_ids
    assert "groq-whisper-large-v3" in profile_ids
    assert "assemblyai-stt" in profile_ids
    assert "gemini-3.5-transcribe" in profile_ids
    assert "elevenlabs-scribe-v2" in profile_ids
    assert "fake-speech-offline" in profile_ids


def _clear_speech_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in [
        "SAHARA_API_KEY",
        "INTRON_API_KEY",
        "GROQ_API_KEY",
        "ASSEMBLYAI_API_KEY",
        "ASSEMBLY_AI_API_KEY",
        "GEMINI_API_KEY",
        "ELEVENLABS_API_KEY",
    ]:
        monkeypatch.delenv(var, raising=False)


def test_missing_api_keys_report_disabled_with_reason(monkeypatch: pytest.MonkeyPatch):
    """T-30: Providers without API keys report enabled=False with clear disabled reason."""
    _clear_speech_env_vars(monkeypatch)
    sahara = SaharaSpeechProvider(api_key=None)
    assert sahara.profile.enabled is False
    assert "SAHARA_API_KEY" in (sahara.profile.disabled_reason or "")

    groq = GroqWhisperSpeechProvider(api_key=None)
    assert groq.profile.enabled is False
    assert "GROQ_API_KEY" in (groq.profile.disabled_reason or "")

    assembly = AssemblyAiSpeechProvider(api_key=None)
    assert assembly.profile.enabled is False
    assert "ASSEMBLYAI_API_KEY" in (assembly.profile.disabled_reason or "")

    gemini = GeminiSpeechProvider(api_key=None)
    assert gemini.profile.enabled is False
    assert "GEMINI_API_KEY" in (gemini.profile.disabled_reason or "")

    eleven = ElevenLabsScribeSpeechProvider(api_key=None)
    assert eleven.profile.enabled is False
    assert "ELEVENLABS_API_KEY" in (eleven.profile.disabled_reason or "")


def test_fake_speech_provider_transcription():
    """T-14: Fake speech provider returns deterministic SpeechTranscriptionResult."""
    provider = FakeSpeechProvider(default_transcript="Add two red t-shirts")
    result = asyncio.run(provider.transcribe(b"MOCK_AUDIO_DATA"))

    assert isinstance(result, SpeechTranscriptionResult)
    assert result.transcript == "Add two red t-shirts"
    assert result.provider_name == "fake"
    assert result.confidence == 0.98
    assert result.latency_ms >= 0.0


def test_provider_transcribe_missing_key_raises_speech_provider_error(monkeypatch: pytest.MonkeyPatch):
    """T-14: Calling transcribe on an unconfigured provider raises SpeechProviderError."""
    _clear_speech_env_vars(monkeypatch)
    sahara = SaharaSpeechProvider(api_key=None)
    with pytest.raises(SpeechProviderError, match="SAHARA_API_KEY not configured"):
        asyncio.run(sahara.transcribe(b"AUDIO_BYTES"))

    groq = GroqWhisperSpeechProvider(api_key=None)
    with pytest.raises(SpeechProviderError, match="GROQ_API_KEY not configured"):
        asyncio.run(groq.transcribe(b"AUDIO_BYTES"))


def test_outbound_network_traffic_blocked_in_unit_tests():
    """Regression test ensuring unit tests fail if an outbound network connection is attempted."""
    with pytest.raises(RuntimeError, match="Outbound network connection blocked"):
        import urllib.request
        urllib.request.urlopen("https://infer.voice.intron.io/health", timeout=0.1)


def test_active_speech_provider_selection():
    """T-30: Registry manages active speech provider selection."""
    registry = create_default_speech_registry()
    
    # Fake provider is enabled by default
    assert registry.set_active("fake-speech-offline") is True
    assert registry.active_provider.profile.profile_id == "fake-speech-offline"

    # Enabled explicitly configured provider can be set active
    custom_sahara = SaharaSpeechProvider(api_key="test_sahara_key", endpoint_url="https://example.invalid/transcribe")
    registry.register(custom_sahara)
    assert registry.set_active("sahara-intron-asr") is True
    assert registry.active_provider.profile.profile_id == "sahara-intron-asr"


def test_batch_profiles_name_exact_models_and_do_not_claim_streaming():
    profiles = [
        GroqWhisperSpeechProvider(api_key="x").profile,
        AssemblyAiSpeechProvider(api_key="x").profile,
        GeminiSpeechProvider(api_key="x").profile,
        ElevenLabsScribeSpeechProvider(api_key="x").profile,
    ]
    assert [profile.model_name for profile in profiles] == [
        "whisper-large-v3", "universal-2", "gemini-3.5-transcribe", "scribe_v2"
    ]
    assert all(profile.supports_streaming is False for profile in profiles)


def test_sahara_uses_verified_sync_route_by_default():
    provider = SaharaSpeechProvider(api_key="x", endpoint_url=None)
    assert provider.profile.enabled is True
    assert provider._endpoint_url == "https://infer.voice.intron.io/file/v1/upload/sync"

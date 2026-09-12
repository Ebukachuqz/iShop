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


def test_missing_api_keys_report_disabled_with_reason():
    """T-30: Providers without API keys report enabled=False with clear disabled reason."""
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


def test_provider_transcribe_missing_key_raises_speech_provider_error():
    """T-14: Calling transcribe on an unconfigured provider raises SpeechProviderError."""
    sahara = SaharaSpeechProvider(api_key=None)
    with pytest.raises(SpeechProviderError, match="SAHARA_API_KEY not configured"):
        asyncio.run(sahara.transcribe(b"AUDIO_BYTES"))

    groq = GroqWhisperSpeechProvider(api_key=None)
    with pytest.raises(SpeechProviderError, match="GROQ_API_KEY not configured"):
        asyncio.run(groq.transcribe(b"AUDIO_BYTES"))


def test_active_speech_provider_selection():
    """T-30: Registry manages active speech provider selection."""
    registry = create_default_speech_registry()
    
    # Fake provider is enabled by default
    assert registry.set_active("fake-speech-offline") is True
    assert registry.active_provider.profile.profile_id == "fake-speech-offline"

    # Enabled explicitly configured provider can be set active
    custom_sahara = SaharaSpeechProvider(api_key="test_sahara_key")
    registry.register(custom_sahara)
    assert registry.set_active("sahara-intron-asr") is True
    assert registry.active_provider.profile.profile_id == "sahara-intron-asr"

"""Factory for configured TTS providers."""

from __future__ import annotations

from ishop.tts.base import TtsRegistry
from ishop.tts.elevenlabs import ElevenLabsHttpTtsProvider, ElevenLabsWsTtsProvider
from ishop.tts.gemini import GeminiStreamingTtsProvider
from ishop.tts.groq import GroqOrpheusTtsProvider
from ishop.tts.sahara import SaharaTtsProvider


def create_default_tts_registry(
    sahara_api_key: str = "",
    elevenlabs_api_key: str = "",
    gemini_api_key: str = "",
    groq_api_key: str = "",
) -> TtsRegistry:
    registry = TtsRegistry()
    sahara = SaharaTtsProvider(api_key=sahara_api_key or None)
    registry.register("sahara-tts-female-pidgin", sahara)
    registry.register("sahara-tts-female-pcm", sahara)  # Legacy profile alias
    registry.register("elevenlabs-tts-female-stream", ElevenLabsHttpTtsProvider(api_key=elevenlabs_api_key or None))
    registry.register("elevenlabs-tts-female-ws", ElevenLabsWsTtsProvider(api_key=elevenlabs_api_key or None))
    registry.register("gemini-tts-female-stream", GeminiStreamingTtsProvider(api_key=gemini_api_key or None))
    registry.register("groq-orpheus-tts-female", GroqOrpheusTtsProvider(api_key=groq_api_key or None))
    return registry

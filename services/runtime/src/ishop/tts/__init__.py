"""Replaceable text-to-speech provider interfaces."""

from ishop.tts.base import (
    TtsAudioChunk,
    TtsCapabilities,
    TtsProvider,
    TtsProviderError,
    TtsRegistry,
    TtsSession,
    align_pcm16_frames,
)
from ishop.tts.elevenlabs import ElevenLabsHttpTtsProvider, ElevenLabsWsTtsProvider
from ishop.tts.factory import create_default_tts_registry
from ishop.tts.gemini import GeminiStreamingTtsProvider
from ishop.tts.groq import GroqOrpheusTtsProvider
from ishop.tts.sahara import SaharaTtsProvider

__all__ = [
    "ElevenLabsHttpTtsProvider",
    "ElevenLabsWsTtsProvider",
    "GeminiStreamingTtsProvider",
    "GroqOrpheusTtsProvider",
    "SaharaTtsProvider",
    "TtsAudioChunk",
    "TtsCapabilities",
    "TtsProvider",
    "TtsProviderError",
    "TtsRegistry",
    "TtsSession",
    "align_pcm16_frames",
    "create_default_tts_registry",
]

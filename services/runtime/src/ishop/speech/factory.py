"""Create the configured speech provider registry."""

from __future__ import annotations

from ishop.speech.assemblyai import AssemblyAiSpeechProvider
from ishop.speech.base import SpeechRegistry
from ishop.speech.elevenlabs_scribe import ElevenLabsScribeSpeechProvider
from ishop.speech.fake import FakeSpeechProvider
from ishop.speech.gemini_transcribe import GeminiSpeechProvider
from ishop.speech.groq_whisper import GroqWhisperSpeechProvider
from ishop.speech.sahara import SaharaSpeechProvider


def create_default_speech_registry() -> SpeechRegistry:
    """Creates registry containing all 5 active speech providers plus offline fake provider."""
    registry = SpeechRegistry()

    registry.register(SaharaSpeechProvider())
    registry.register(GroqWhisperSpeechProvider())
    registry.register(AssemblyAiSpeechProvider())
    registry.register(GeminiSpeechProvider())
    registry.register(ElevenLabsScribeSpeechProvider())
    registry.register(FakeSpeechProvider())

    return registry

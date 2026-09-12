"""Factory initializing speech provider registry with active ASR routes.

Active Speech Routes:
1. Sahara (Intron Health ASR)
2. Groq Whisper Large v3
3. AssemblyAI
4. Gemini 3.5 Transcribe
5. ElevenLabs Scribe v2
6. Fake Offline Speech Provider (fallback)
"""

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

    # 1. Sahara (Intron Health)
    registry.register(SaharaSpeechProvider())

    # 2. Groq Whisper Large v3
    registry.register(GroqWhisperSpeechProvider())

    # 3. AssemblyAI
    registry.register(AssemblyAiSpeechProvider())

    # 4. Gemini 3.5 Transcribe
    registry.register(GeminiSpeechProvider())

    # 5. ElevenLabs Scribe v2
    registry.register(ElevenLabsScribeSpeechProvider())

    # 6. Offline Fake Provider (always enabled as zero-dependency fallback)
    registry.register(FakeSpeechProvider())

    return registry

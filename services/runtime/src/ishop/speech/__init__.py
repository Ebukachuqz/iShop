"""Speech recognition provider layer for iShop (Drake)."""

from ishop.speech.assemblyai_realtime import AssemblyAiStreamingSession
from ishop.speech.base import (
    SpeechProfile,
    SpeechProvider,
    SpeechProviderError,
    SpeechRegistry,
    SpeechTranscriptionResult,
)
from ishop.speech.elevenlabs_scribe_realtime import ElevenLabsScribeRealtimeSession
from ishop.speech.gemini_live import GeminiLiveStreamingSession
from ishop.speech.realtime import RealtimeSpeechSession, SpeechEventKind, SpeechStreamEvent
from ishop.speech.sahara_stream import SaharaStreamingSession

__all__ = [
    "AssemblyAiStreamingSession",
    "ElevenLabsScribeRealtimeSession",
    "GeminiLiveStreamingSession",
    "RealtimeSpeechSession",
    "SaharaStreamingSession",
    "SpeechEventKind",
    "SpeechProfile",
    "SpeechProvider",
    "SpeechProviderError",
    "SpeechRegistry",
    "SpeechStreamEvent",
    "SpeechTranscriptionResult",
]

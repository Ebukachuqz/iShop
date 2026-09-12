"""Speech recognition provider layer for iShop (Drake)."""

from ishop.speech.base import (
    SpeechProfile,
    SpeechProvider,
    SpeechProviderError,
    SpeechRegistry,
    SpeechTranscriptionResult,
)
from ishop.speech.realtime import RealtimeSpeechSession, SpeechEventKind, SpeechStreamEvent
from ishop.speech.sahara_stream import SaharaStreamingSession

__all__ = [
    "SpeechProfile",
    "SpeechProvider",
    "SpeechProviderError",
    "SpeechRegistry",
    "SpeechTranscriptionResult",
    "RealtimeSpeechSession",
    "SaharaStreamingSession",
    "SpeechEventKind",
    "SpeechStreamEvent",
]

"""Provider-neutral realtime speech events and session interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import AsyncIterator


class SpeechEventKind(StrEnum):
    SESSION_STARTED = "session_started"
    AUDIO_ACCEPTED = "audio_accepted"
    PARTIAL_TRANSCRIPT = "partial_transcript"
    FINAL_TRANSCRIPT = "final_transcript"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True)
class SpeechStreamEvent:
    kind: SpeechEventKind
    session_id: str
    revision: int
    text: str | None = None
    chunk_id: int | None = None
    error_code: str | None = None
    retryable: bool = False

    @property
    def authorizes_interpretation(self) -> bool:
        return self.kind is SpeechEventKind.FINAL_TRANSCRIPT


class RealtimeSpeechSession(ABC):
    """One utterance-scoped realtime recognition session."""

    @abstractmethod
    async def start(self) -> SpeechStreamEvent:
        ...

    @abstractmethod
    async def send_audio(self, pcm16_audio: bytes) -> None:
        ...

    @abstractmethod
    async def commit(self) -> None:
        ...

    @abstractmethod
    async def cancel(self) -> None:
        ...

    @abstractmethod
    def events(self) -> AsyncIterator[SpeechStreamEvent]:
        ...

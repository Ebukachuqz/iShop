"""Provider-neutral text-to-speech contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


class TtsProviderError(Exception):
    def __init__(self, message: str, provider_name: str, retryable: bool = True):
        super().__init__(message)
        self.provider_name = provider_name
        self.retryable = retryable


@dataclass(frozen=True)
class TtsAudioChunk:
    audio: bytes
    generation: int
    sample_rate: int | None = None
    channels: int | None = None
    format: str = "wav"


class TtsSession(ABC):
    @abstractmethod
    def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        ...

    @abstractmethod
    async def cancel(self) -> None:
        ...


class TtsProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, generation: int) -> TtsSession:
        ...

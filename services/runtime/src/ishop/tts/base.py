"""Provider-neutral text-to-speech contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


class TtsProviderError(Exception):
    def __init__(self, message: str, provider_name: str, retryable: bool = True):
        super().__init__(message)
        self.provider_name = provider_name
        self.retryable = retryable


@dataclass(frozen=True)
class TtsCapabilities:
    input_streaming: bool = False
    output_streaming: bool = True
    encoding: str = "pcm"
    container: str = "none"
    supported_sample_rates: tuple[int, ...] = (16000, 24000)
    min_text_chars: int = 1
    max_text_chars: int = 500
    languages: tuple[str, ...] = ("en",)
    voice_gender: str = "female"
    supports_cancellation: bool = True
    enabled: bool = True
    disabled_reason: str | None = None


@dataclass(frozen=True)
class TtsAudioChunk:
    audio: bytes
    generation: int
    reply_id: str | None = None
    sequence: int = 1
    is_final: bool = False
    sample_rate: int | None = None
    channels: int | None = None
    format: str = "wav"
    container: str = "wav"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        import base64
        return {
            "audio_base64": base64.b64encode(self.audio).decode("ascii") if self.audio else "",
            "generation": self.generation,
            "reply_id": self.reply_id,
            "sequence": self.sequence,
            "is_final": self.is_final,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "format": self.format,
            "container": self.container,
            "error": self.error,
        }


class TtsSession(ABC):
    @abstractmethod
    def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        ...

    @abstractmethod
    async def cancel(self) -> None:
        ...


class TtsProvider(ABC):
    @property
    def capabilities(self) -> TtsCapabilities:
        return TtsCapabilities()

    @abstractmethod
    async def synthesize(self, text: str, generation: int, reply_id: str | None = None) -> TtsSession:
        ...


class TtsRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, TtsProvider] = {}

    def register(self, profile_id: str, provider: TtsProvider) -> None:
        self._providers[profile_id] = provider

    def get(self, profile_id: str) -> TtsProvider | None:
        return self._providers.get(profile_id)

    def list_profiles(self) -> list[str]:
        return list(self._providers.keys())


def align_pcm16_frames(chunk: bytes, remainder: bytes = b"") -> tuple[bytes, bytes]:
    data = remainder + chunk
    aligned_len = len(data) - (len(data) % 2)
    return data[:aligned_len], data[aligned_len:]

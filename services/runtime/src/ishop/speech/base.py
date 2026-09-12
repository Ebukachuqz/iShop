"""Base interface, dataclasses, and registry for speech-to-text providers.

Enforces:
- T-14, T-30: Provider independence, missing key graceful handling, and profile selection.
- S-14: Explicit profile eligibility and frozen provider settings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os
from typing import Any


class SpeechProviderError(Exception):
    """Exception raised by speech providers for network, tariff, or API errors."""

    def __init__(self, message: str, provider_name: str, retryable: bool = True):
        super().__init__(message)
        self.provider_name = provider_name
        self.retryable = retryable


@dataclass(frozen=True)
class SpeechTranscriptionResult:
    """Standardized speech transcription outcome across providers."""

    transcript: str
    language_detected: str | None = None
    confidence: float | None = None
    latency_ms: float = 0.0
    provider_name: str = "unknown"
    model_name: str = "unknown"
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechProfile:
    """Metadata describing a configured speech provider route."""

    profile_id: str
    provider_name: str
    model_name: str
    enabled: bool = True
    disabled_reason: str | None = None
    supports_streaming: bool = False
    supports_code_switching: bool = True
    requires_api_key: bool = True


class SpeechProvider(ABC):
    """Abstract base class for all speech recognition providers."""

    @property
    @abstractmethod
    def profile(self) -> SpeechProfile:
        """Returns provider metadata profile."""
        pass

    @abstractmethod
    async def transcribe(
        self,
        audio_data: bytes,
        sample_rate: int = 16000,
        language_hint: str | None = None,
    ) -> SpeechTranscriptionResult:
        """Transcribes raw audio bytes into standardized transcription result."""
        pass

    async def check_readiness(self) -> tuple[bool, str]:
        """Checks if provider is ready and has valid credentials."""
        prof = self.profile
        if not prof.enabled:
            return False, prof.disabled_reason or "Provider disabled"
        return True, "Ready"


class SpeechRegistry:
    """Central registry managing speech recognition profiles and providers."""

    def __init__(self):
        self._providers: dict[str, SpeechProvider] = {}
        self._active_profile_id: str | None = None

    def register(self, provider: SpeechProvider) -> None:
        """Registers a speech provider."""
        pid = provider.profile.profile_id
        self._providers[pid] = provider
        if self._active_profile_id is None and provider.profile.enabled:
            self._active_profile_id = pid

    def get(self, profile_id: str) -> SpeechProvider | None:
        """Retrieves provider by profile ID."""
        return self._providers.get(profile_id)

    def set_active(self, profile_id: str) -> bool:
        """Sets active profile if registered and enabled."""
        provider = self._providers.get(profile_id)
        if provider and provider.profile.enabled:
            self._active_profile_id = profile_id
            return True
        return False

    @property
    def active_provider(self) -> SpeechProvider | None:
        """Returns current active provider."""
        if self._active_profile_id:
            return self._providers.get(self._active_profile_id)
        return None

    def list_profiles(self) -> list[SpeechProfile]:
        """Lists metadata for all registered speech profiles."""
        return [p.profile for p in self._providers.values()]

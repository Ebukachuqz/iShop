"""Fake offline speech provider for testing and offline harness runs."""

from __future__ import annotations

import time
from typing import Any

from ishop.speech.base import SpeechProfile, SpeechProvider, SpeechTranscriptionResult


class FakeSpeechProvider(SpeechProvider):
    """Deterministic offline speech provider for unit testing and offline harness runs."""

    def __init__(
        self,
        default_transcript: str = "Add 1 embroidered cap under 4000 naira",
        profile_id: str = "fake-speech-offline",
        enabled: bool = True,
    ):
        self._default_transcript = default_transcript
        self._profile_id = profile_id
        self._enabled = enabled
        self._custom_transcripts: dict[bytes, str] = {}

    def set_transcript_for_audio(self, audio_bytes: bytes, transcript: str) -> None:
        """Registers a deterministic transcript for specific audio content."""
        self._custom_transcripts[audio_bytes] = transcript

    @property
    def profile(self) -> SpeechProfile:
        return SpeechProfile(
            profile_id=self._profile_id,
            provider_name="fake",
            model_name="fake-speech-v1",
            enabled=self._enabled,
            disabled_reason=None if self._enabled else "Offline fake provider disabled",
            supports_streaming=True,
            supports_code_switching=True,
            requires_api_key=False,
        )

    async def transcribe(
        self,
        audio_data: bytes,
        sample_rate: int = 16000,
        language_hint: str | None = None,
    ) -> SpeechTranscriptionResult:
        start_time = time.monotonic()
        text = self._custom_transcripts.get(audio_data, self._default_transcript)
        elapsed = (time.monotonic() - start_time) * 1000.0

        return SpeechTranscriptionResult(
            transcript=text,
            language_detected=language_hint or "pcm-eng",
            confidence=0.98,
            latency_ms=elapsed,
            provider_name="fake",
            model_name="fake-speech-v1",
            raw_metadata={"audio_bytes_length": len(audio_data), "sample_rate": sample_rate},
        )

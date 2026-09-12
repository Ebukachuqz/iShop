"""ElevenLabs Scribe v2 speech recognition provider adapter.

Enforces:
- T-14, T-30: Provider key detection, nonblocking HTTP offloading, and speech profile metadata.
- R7: Offloads blocking urllib calls via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ishop.speech.base import SpeechProfile, SpeechProvider, SpeechProviderError, SpeechTranscriptionResult
from ishop.speech.sahara import _http_post_multipart


class ElevenLabsScribeSpeechProvider(SpeechProvider):
    """ElevenLabs Scribe v2 speech recognition provider."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = "scribe_v2",
        endpoint_url: str = "https://api.elevenlabs.io/v1/speech-to-text",
        timeout_seconds: float = 15.0,
    ):
        self._api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self._model_name = model_name
        self._endpoint_url = endpoint_url
        self._timeout_seconds = timeout_seconds

    @property
    def profile(self) -> SpeechProfile:
        has_key = bool(self._api_key)
        return SpeechProfile(
            profile_id="elevenlabs-scribe-v2",
            provider_name="elevenlabs",
            model_name=self._model_name,
            enabled=has_key,
            disabled_reason=None if has_key else "ELEVENLABS_API_KEY environment variable not set",
            supports_streaming=False,
            supports_code_switching=True,
            requires_api_key=True,
        )

    async def transcribe(
        self,
        audio_data: bytes,
        sample_rate: int = 16000,
        language_hint: str | None = None,
    ) -> SpeechTranscriptionResult:
        if not self._api_key:
            raise SpeechProviderError(
                "ELEVENLABS_API_KEY not configured",
                provider_name="elevenlabs",
                retryable=False,
            )

        headers = {"xi-api-key": self._api_key}
        fields = {"model_id": self._model_name}
        if language_hint:
            fields["language_code"] = language_hint.split("-")[0]

        start_time = time.monotonic()
        payload = await asyncio.to_thread(
            _http_post_multipart,
            self._endpoint_url,
            headers,
            audio_data,
            fields,
            self._timeout_seconds,
            "elevenlabs",
        )
        elapsed = (time.monotonic() - start_time) * 1000.0

        transcript = payload.get("text") or payload.get("transcript") or ""
        lang = payload.get("language_code") or language_hint

        return SpeechTranscriptionResult(
            transcript=transcript.strip(),
            language_detected=lang,
            confidence=payload.get("confidence"),
            latency_ms=elapsed,
            provider_name="elevenlabs",
            model_name=self._model_name,
            raw_metadata={"request_id": payload.get("request_id")},
        )

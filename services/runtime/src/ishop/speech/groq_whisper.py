"""Groq Whisper Large v3 speech recognition provider adapter.

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


class GroqWhisperSpeechProvider(SpeechProvider):
    """Groq Whisper Large v3 speech recognition provider."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = "whisper-large-v3",
        endpoint_url: str = "https://api.groq.com/openai/v1/audio/transcriptions",
        timeout_seconds: float = 15.0,
    ):
        self._api_key = api_key if api_key is not None else os.getenv("GROQ_API_KEY")
        self._model_name = model_name
        self._endpoint_url = endpoint_url
        self._timeout_seconds = timeout_seconds

    @property
    def profile(self) -> SpeechProfile:
        has_key = bool(self._api_key)
        return SpeechProfile(
            profile_id="groq-whisper-large-v3",
            provider_name="groq",
            model_name=self._model_name,
            enabled=has_key,
            disabled_reason=None if has_key else "GROQ_API_KEY environment variable not set",
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
                "GROQ_API_KEY not configured",
                provider_name="groq",
                retryable=False,
            )

        headers = {"Authorization": f"Bearer {self._api_key}"}
        fields = {"model": self._model_name, "response_format": "json"}
        # Do not force one language for code-switched benchmark audio. A separate
        # monolingual profile may opt into a language hint later.

        start_time = time.monotonic()
        payload = await asyncio.to_thread(
            _http_post_multipart,
            self._endpoint_url,
            headers,
            audio_data,
            fields,
            self._timeout_seconds,
            "groq",
        )
        elapsed = (time.monotonic() - start_time) * 1000.0

        transcript = payload.get("text") or ""
        lang = payload.get("language") or language_hint

        return SpeechTranscriptionResult(
            transcript=transcript.strip(),
            language_detected=lang,
            confidence=None,
            latency_ms=elapsed,
            provider_name="groq",
            model_name=self._model_name,
            raw_metadata={"request_id": payload.get("x_groq", {}).get("id")},
        )

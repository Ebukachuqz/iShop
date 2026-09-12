"""Gemini 3.5 Transcribe audio speech recognition provider adapter.

Enforces:
- T-14, T-30: Provider key detection, nonblocking HTTP offloading, and speech profile metadata.
- R7: Offloads blocking urllib calls via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ishop.speech.base import SpeechProfile, SpeechProvider, SpeechProviderError, SpeechTranscriptionResult


def _gemini_transcribe_worker(
    api_key: str,
    audio_data: bytes,
    model_name: str = "gemini-1.5-flash",
    language_hint: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Helper executing Gemini audio transcription call in worker thread."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    b64_audio = base64.b64encode(audio_data).decode("utf-8")
    prompt = "Transcribe the following audio recording accurately. Output only the verbatim transcript text without formatting or explanations."
    if language_hint:
        prompt += f" Language context: {language_hint}."

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "audio/wav",
                            "data": b64_audio,
                        }
                    },
                ]
            }
        ]
    }

    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise SpeechProviderError(
            f"Gemini Transcribe HTTP {exc.code}: {err_body}",
            provider_name="gemini",
            retryable=exc.code in (429, 500, 502, 503, 504),
        ) from exc
    except Exception as exc:
        raise SpeechProviderError(
            f"Gemini Transcribe network failure: {str(exc)}",
            provider_name="gemini",
            retryable=True,
        ) from exc


class GeminiSpeechProvider(SpeechProvider):
    """Gemini 3.5 Transcribe audio speech recognition provider."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = "gemini-1.5-flash",
        timeout_seconds: float = 15.0,
    ):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds

    @property
    def profile(self) -> SpeechProfile:
        has_key = bool(self._api_key)
        return SpeechProfile(
            profile_id="gemini-3.5-transcribe",
            provider_name="gemini",
            model_name=self._model_name,
            enabled=has_key,
            disabled_reason=None if has_key else "GEMINI_API_KEY environment variable not set",
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
                "GEMINI_API_KEY not configured",
                provider_name="gemini",
                retryable=False,
            )

        start_time = time.monotonic()
        payload = await asyncio.to_thread(
            _gemini_transcribe_worker,
            self._api_key,
            audio_data,
            self._model_name,
            language_hint,
            self._timeout_seconds,
        )
        elapsed = (time.monotonic() - start_time) * 1000.0

        transcript = ""
        try:
            candidates = payload.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    transcript = parts[0].get("text", "")
        except (KeyError, IndexError, AttributeError):
            pass

        return SpeechTranscriptionResult(
            transcript=transcript.strip(),
            language_detected=language_hint,
            confidence=None,
            latency_ms=elapsed,
            provider_name="gemini",
            model_name=self._model_name,
            raw_metadata=payload,
        )

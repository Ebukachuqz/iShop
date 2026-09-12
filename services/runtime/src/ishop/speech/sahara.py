"""Sahara (Intron Health) speech recognition provider adapter.

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


def _http_post_multipart(
    url: str,
    headers: dict[str, str],
    audio_data: bytes,
    fields: dict[str, str],
    timeout_seconds: float = 15.0,
    provider_name: str = "sahara",
    filename: str = "audio.wav",
    mime_type: str = "audio/wav",
) -> dict[str, Any]:
    """Helper executing HTTP POST with multipart/form-data payload in worker thread."""
    boundary = f"ishop-{time.monotonic_ns()}"
    body = bytearray()

    for key, val in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{val}\r\n".encode("utf-8"))

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(audio_data)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req_headers = dict(headers)
    req_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    req = urllib.request.Request(url, data=bytes(body), headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise SpeechProviderError(
            f"{provider_name} API HTTP {exc.code}: {err_body}",
            provider_name=provider_name,
            retryable=exc.code in (429, 500, 502, 503, 504),
        ) from exc
    except Exception as exc:
        raise SpeechProviderError(
            f"{provider_name} network failure: {str(exc)}",
            provider_name=provider_name,
            retryable=True,
        ) from exc


class SaharaSpeechProvider(SpeechProvider):
    """Sahara (Intron Health) speech recognition provider."""

    def __init__(
        self,
        api_key: str | None = None,
        endpoint_url: str | None = None,
        timeout_seconds: float = 15.0,
    ):
        self._api_key = api_key or os.getenv("SAHARA_API_KEY") or os.getenv("INTRON_API_KEY")
        self._endpoint_url = endpoint_url or os.getenv("SAHARA_TRANSCRIBE_URL", "")
        self._timeout_seconds = timeout_seconds

    @property
    def profile(self) -> SpeechProfile:
        has_key = bool(self._api_key and self._endpoint_url)
        return SpeechProfile(
            profile_id="sahara-intron-asr",
            provider_name="sahara",
            model_name="competition-route-unverified",
            enabled=has_key,
            disabled_reason=None if has_key else "SAHARA_API_KEY and verified SAHARA_TRANSCRIBE_URL are required",
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
        if not self._api_key or not self._endpoint_url:
            raise SpeechProviderError(
                "SAHARA_API_KEY or verified SAHARA_TRANSCRIBE_URL not configured",
                provider_name="sahara",
                retryable=False,
            )

        headers = {"Authorization": f"Bearer {self._api_key}"}
        fields = {
            "sample_rate": str(sample_rate),
            "use_disable_llm_corrections": "true",
            "category": "general",
        }
        if language_hint:
            fields["language_code"] = language_hint

        start_time = time.monotonic()
        payload = await asyncio.to_thread(
            _http_post_multipart,
            self._endpoint_url,
            headers,
            audio_data,
            fields,
            self._timeout_seconds,
            "sahara",
        )
        elapsed = (time.monotonic() - start_time) * 1000.0

        transcript = payload.get("transcript") or payload.get("text") or ""
        lang = payload.get("language") or language_hint
        conf = payload.get("confidence")

        return SpeechTranscriptionResult(
            transcript=transcript.strip(),
            language_detected=lang,
            confidence=float(conf) if conf is not None else None,
            latency_ms=elapsed,
            provider_name="sahara",
            model_name="competition-route-unverified",
            raw_metadata={"request_id": payload.get("request_id")},
        )

"""AssemblyAI speech recognition provider adapter.

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


def _assemblyai_transcribe_worker(
    api_key: str,
    audio_data: bytes,
    language_hint: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Helper executing AssemblyAI upload and transcription in worker thread."""
    headers = {"authorization": api_key, "content-type": "application/octet-stream"}
    upload_url = "https://api.assemblyai.com/v2/upload"

    req = urllib.request.Request(upload_url, data=audio_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            up_data = json.loads(resp.read().decode("utf-8"))
            audio_url = up_data.get("upload_url")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise SpeechProviderError(
            f"AssemblyAI Upload HTTP {exc.code}: {err_body}",
            provider_name="assemblyai",
            retryable=exc.code in (429, 500, 502, 503, 504),
        ) from exc
    except Exception as exc:
        raise SpeechProviderError(
            f"AssemblyAI Upload network failure: {str(exc)}",
            provider_name="assemblyai",
            retryable=True,
        ) from exc

    if not audio_url:
        raise SpeechProviderError("AssemblyAI upload failed to return upload_url", provider_name="assemblyai", retryable=False)

    tx_url = "https://api.assemblyai.com/v2/transcript"
    tx_body = {"audio_url": audio_url, "speech_models": ["universal-2"]}
    # Automatic detection is kept enabled for code-switched evaluation.

    json_data = json.dumps(tx_body).encode("utf-8")
    tx_headers = {"authorization": api_key, "content-type": "application/json"}
    tx_req = urllib.request.Request(tx_url, data=json_data, headers=tx_headers, method="POST")

    try:
        with urllib.request.urlopen(tx_req, timeout=timeout_seconds) as resp:
            tx_data = json.loads(resp.read().decode("utf-8"))
            tx_id = tx_data.get("id")
    except Exception as exc:
        raise SpeechProviderError(f"AssemblyAI transcript request failed: {str(exc)}", provider_name="assemblyai") from exc

    poll_url = f"https://api.assemblyai.com/v2/transcript/{tx_id}"
    start_poll = time.monotonic()
    while time.monotonic() - start_poll < timeout_seconds:
        poll_req = urllib.request.Request(poll_url, headers={"authorization": api_key})
        with urllib.request.urlopen(poll_req, timeout=5.0) as resp:
            status_data = json.loads(resp.read().decode("utf-8"))
            status = status_data.get("status")
            if status == "completed":
                return status_data
            elif status == "error":
                raise SpeechProviderError(f"AssemblyAI transcription error: {status_data.get('error')}", provider_name="assemblyai", retryable=False)
        time.sleep(0.5)

    raise SpeechProviderError("AssemblyAI transcription timed out during polling", provider_name="assemblyai", retryable=True)


class AssemblyAiSpeechProvider(SpeechProvider):
    """AssemblyAI speech recognition provider."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
    ):
        self._api_key = api_key or os.getenv("ASSEMBLYAI_API_KEY") or os.getenv("ASSEMBLY_AI_API_KEY")
        self._timeout_seconds = timeout_seconds

    @property
    def profile(self) -> SpeechProfile:
        has_key = bool(self._api_key)
        return SpeechProfile(
            profile_id="assemblyai-stt",
            provider_name="assemblyai",
            model_name="universal-2",
            enabled=has_key,
            disabled_reason=None if has_key else "ASSEMBLYAI_API_KEY environment variable not set",
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
                "ASSEMBLYAI_API_KEY not configured",
                provider_name="assemblyai",
                retryable=False,
            )

        start_time = time.monotonic()
        payload = await asyncio.to_thread(
            _assemblyai_transcribe_worker,
            self._api_key,
            audio_data,
            language_hint,
            self._timeout_seconds,
        )
        elapsed = (time.monotonic() - start_time) * 1000.0

        transcript = payload.get("text") or ""
        confidence = payload.get("confidence")

        return SpeechTranscriptionResult(
            transcript=transcript.strip(),
            language_detected=language_hint,
            confidence=float(confidence) if confidence is not None else None,
            latency_ms=elapsed,
            provider_name="assemblyai",
            model_name=str(payload.get("speech_model_used") or "universal-2"),
            raw_metadata={"id": payload.get("id"), "status": payload.get("status")},
        )

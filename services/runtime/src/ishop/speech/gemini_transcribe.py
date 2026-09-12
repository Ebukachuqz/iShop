"""Gemini 3.5 Transcribe unary adapter using Files + Interactions APIs."""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ishop.speech.base import SpeechProfile, SpeechProvider, SpeechProviderError, SpeechTranscriptionResult


def _read_json(request: urllib.request.Request, timeout: float) -> tuple[dict[str, Any], Any]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return (json.loads(body) if body else {}), response.headers


def _gemini_transcribe_worker(api_key: str, audio_data: bytes, model_name: str,
                              language_hint: str | None, timeout_seconds: float) -> dict[str, Any]:
    upload_start = urllib.request.Request(
        "https://generativelanguage.googleapis.com/upload/v1beta/files",
        data=json.dumps({"file": {"display_name": "ishop-benchmark.wav"}}).encode(),
        headers={"x-goog-api-key": api_key, "X-Goog-Upload-Protocol": "resumable",
                 "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(len(audio_data)),
                 "X-Goog-Upload-Header-Content-Type": "audio/wav",
                 "Content-Type": "application/json"}, method="POST")
    try:
        _, headers = _read_json(upload_start, timeout_seconds)
        upload_url = headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise SpeechProviderError("Gemini Files API returned no upload URL", "gemini", False)
        upload = urllib.request.Request(upload_url, data=audio_data, headers={
            "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
            "Content-Length": str(len(audio_data)), "Content-Type": "audio/wav"}, method="POST")
        uploaded, _ = _read_json(upload, timeout_seconds)
        file_data = uploaded.get("file", uploaded)
        uri = file_data.get("uri")
        if not uri:
            raise SpeechProviderError("Gemini Files API returned no file URI", "gemini", False)
        transcription = {"language_codes": [language_hint] if language_hint else [],
                         "mode": {"type": "verbatim"}}
        body = {"model": model_name,
                "input": [{"type": "audio", "uri": uri, "mime_type": "audio/wav"}],
                "generation_config": {"transcription_config": transcription}}
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=json.dumps(body).encode(),
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, method="POST")
        result, _ = _read_json(request, timeout_seconds)
        return result
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SpeechProviderError(f"Gemini Transcribe HTTP {exc.code}: {detail}", "gemini",
                                  exc.code in (429, 500, 502, 503, 504)) from exc
    except SpeechProviderError:
        raise
    except Exception as exc:
        raise SpeechProviderError(f"Gemini Transcribe network failure: {exc}", "gemini") from exc


class GeminiSpeechProvider(SpeechProvider):
    def __init__(self, api_key: str | None = None, model_name: str = "gemini-3.5-transcribe",
                 timeout_seconds: float = 60.0):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds

    @property
    def profile(self) -> SpeechProfile:
        enabled = bool(self._api_key)
        return SpeechProfile("gemini-3.5-transcribe", "gemini", self._model_name, enabled,
                             None if enabled else "GEMINI_API_KEY environment variable not set",
                             False, True, True)

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000,
                         language_hint: str | None = None) -> SpeechTranscriptionResult:
        if not self._api_key:
            raise SpeechProviderError("GEMINI_API_KEY not configured", "gemini", False)
        started = time.monotonic()
        payload = await asyncio.to_thread(_gemini_transcribe_worker, self._api_key, audio_data,
                                          self._model_name, language_hint, self._timeout_seconds)
        transcript = payload.get("output_text", "")
        if not transcript:
            transcript = "".join(str(item.get("text", "")) for item in payload.get("outputs", [])
                                 if isinstance(item, dict))
        return SpeechTranscriptionResult(transcript.strip(), language_hint, None,
                                         (time.monotonic() - started) * 1000, "gemini",
                                         self._model_name, {"interaction_id": payload.get("id")})

"""Gemini 3.1 streaming TTS through the documented Interactions API."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from typing import Any

from ishop.tts.base import TtsAudioChunk, TtsCapabilities, TtsProvider, TtsProviderError, TtsSession

GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_FEMALE_VOICE = "Kore"
DEFAULT_TTS_MODEL = "gemini-3.1-flash-tts-preview"


class GeminiStreamingTtsSession(TtsSession):
    def __init__(self, response: Any, generation: int, reply_id: str | None = None):
        self._response, self._generation, self._reply_id = response, generation, reply_id
        self._closed = False

    async def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        sequence = 1
        try:
            while not self._closed:
                line = await asyncio.to_thread(self._response.readline)
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("event_type") or event.get("type")
                if event_type in {"error", "interaction.error"} or event.get("error"):
                    detail = event.get("error") or event.get("message") or "stream failed"
                    raise TtsProviderError(f"Gemini TTS error: {detail}", "gemini", False)
                delta = event.get("delta") or {}
                if event_type == "step.delta" and delta.get("type") == "audio" and delta.get("data"):
                    yield TtsAudioChunk(
                        audio=base64.b64decode(delta["data"], validate=True), generation=self._generation,
                        reply_id=self._reply_id, sequence=sequence, is_final=False, sample_rate=24000,
                        channels=1, format="pcm", container="none",
                    )
                    sequence += 1
        finally:
            await self.cancel()

    async def cancel(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._response.close()
            except Exception:
                pass


class GeminiStreamingTtsProvider(TtsProvider):
    def __init__(self, api_key: str | None = None, *, model_name: str = DEFAULT_TTS_MODEL,
                 voice_name: str = DEFAULT_FEMALE_VOICE, endpoint_url: str | None = None,
                 opener: Callable[..., Any] | None = None):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._model_name, self._voice_name = model_name, voice_name
        self._endpoint_url = endpoint_url or os.getenv("GEMINI_TTS_INTERACTIONS_URL", GEMINI_INTERACTIONS_URL)
        self._opener = opener or urllib.request.urlopen

    @property
    def capabilities(self) -> TtsCapabilities:
        has_key = bool(self._api_key)
        return TtsCapabilities(input_streaming=False, output_streaming=True, encoding="pcm", container="none",
            supported_sample_rates=(24000,), min_text_chars=1, max_text_chars=500, languages=("en",),
            voice_gender="female", supports_cancellation=True, enabled=has_key,
            disabled_reason=None if has_key else "GEMINI_API_KEY not configured")

    async def synthesize(self, text: str, generation: int, reply_id: str | None = None) -> TtsSession:
        if not self._api_key:
            raise TtsProviderError("GEMINI_API_KEY not configured", "gemini", False)
        if not text.strip():
            raise ValueError("TTS text must not be empty")
        body = json.dumps({
            "model": self._model_name,
            "input": f"Synthesize speech. Spoken transcript begins: {text}",
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": self._voice_name, "language": "en-US"}]},
            "stream": True,
        }).encode("utf-8")
        request = urllib.request.Request(self._endpoint_url, data=body, headers={
            "x-goog-api-key": self._api_key, "content-type": "application/json",
            "accept": "text/event-stream", "Api-Revision": "2026-05-20",
        }, method="POST")
        try:
            response = await asyncio.to_thread(self._opener, request, timeout=10.0)
            return GeminiStreamingTtsSession(response, generation, reply_id)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TtsProviderError(f"Gemini TTS HTTP {exc.code}: {detail}", "gemini", exc.code in {429, 500, 502, 503}) from exc
        except Exception as exc:
            raise TtsProviderError(f"Gemini TTS request failed: {exc}", "gemini", True) from exc

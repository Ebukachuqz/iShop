"""Groq Orpheus standalone TTS adapter (canopylabs/orpheus-v1-english)."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from typing import Any

from ishop.tts.base import TtsAudioChunk, TtsCapabilities, TtsProvider, TtsProviderError, TtsSession

GROQ_SPEECH_URL = "https://api.groq.com/openai/v1/audio/speech"
DEFAULT_FEMALE_VOICE = "autumn"
DEFAULT_MODEL = "canopylabs/orpheus-v1-english"

# Common Nigerian Pidgin markers that must not silently route to English Orpheus TTS
PIDGIN_MARKERS = frozenset(["dey", "fit", "wetin", "na", "abi", "kwanu", "sef", "don", "no be", "dem", "una"])


def _detect_pidgin(text: str) -> bool:
    tokens = set(text.lower().split())
    return bool(tokens & PIDGIN_MARKERS)


class GroqOrpheusTtsSession(TtsSession):
    def __init__(self, audio_data: bytes, generation: int, reply_id: str | None = None, response_format: str = "wav"):
        self._audio_data = audio_data
        self._generation = generation
        self._reply_id = reply_id
        self._response_format = response_format
        self._consumed = False
        self._closed = False

    async def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        if not self._consumed and not self._closed:
            self._consumed = True
            yield TtsAudioChunk(
                audio=self._audio_data,
                generation=self._generation,
                reply_id=self._reply_id,
                sequence=1,
                is_final=True,
                sample_rate=24000,
                channels=1,
                format=self._response_format,
                container=self._response_format,
            )

    async def cancel(self) -> None:
        self._closed = True


class GroqOrpheusTtsProvider(TtsProvider):
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_FEMALE_VOICE,
        response_format: str = "wav",
        endpoint_url: str | None = None,
        opener: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key or os.getenv("GROQ_API_KEY")
        self._model = model
        self._voice = voice
        self._response_format = response_format
        self._endpoint_url = endpoint_url or os.getenv("GROQ_SPEECH_URL", GROQ_SPEECH_URL)
        self._opener = opener or urllib.request.urlopen

    @property
    def capabilities(self) -> TtsCapabilities:
        has_key = bool(self._api_key)
        return TtsCapabilities(
            input_streaming=False,
            output_streaming=False,  # Buffered speech endpoint
            encoding=self._response_format,
            container=self._response_format,
            supported_sample_rates=(24000,),
            min_text_chars=1,
            max_text_chars=1000,
            languages=("en",),
            voice_gender="female",
            supports_cancellation=True,
            enabled=has_key,
            disabled_reason=None if has_key else "GROQ_API_KEY not configured",
        )

    async def synthesize(self, text: str, generation: int, reply_id: str | None = None) -> TtsSession:
        if not self._api_key:
            raise TtsProviderError("GROQ_API_KEY not configured", "groq", False)
        if not text.strip():
            raise ValueError("TTS text must not be empty")

        if _detect_pidgin(text):
            raise TtsProviderError(
                "Groq Orpheus TTS only supports English text; Pidgin English text requires Sahara TTS",
                "groq",
                False,
            )

        body = json.dumps({
            "model": self._model,
            "input": text,
            "voice": self._voice,
            "response_format": self._response_format,
        }).encode("utf-8")

        req = urllib.request.Request(
            self._endpoint_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            resp = await asyncio.to_thread(self._opener, req, timeout=10.0)
            audio_data = await asyncio.to_thread(resp.read)
            return GroqOrpheusTtsSession(audio_data, generation, reply_id=reply_id, response_format=self._response_format)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise TtsProviderError(f"Groq TTS HTTP {exc.code}: {err_body}", "groq", exc.code in (429, 500, 502, 503)) from exc
        except Exception as exc:
            raise TtsProviderError(f"Groq TTS request failed: {exc}", "groq", True) from exc

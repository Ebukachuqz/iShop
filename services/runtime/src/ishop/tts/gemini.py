"""Gemini streaming TTS adapter (gemini-3.1-flash-tts-preview)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from ishop.tts.base import TtsAudioChunk, TtsCapabilities, TtsProvider, TtsProviderError, TtsSession

GEMINI_TTS_WS_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
DEFAULT_FEMALE_VOICE = "Aoede"
DEFAULT_TTS_MODEL = "gemini-3.1-flash-tts-preview"


class GeminiStreamingTtsSession(TtsSession):
    def __init__(self, socket: Any, generation: int, reply_id: str | None = None):
        self._socket = socket
        self._generation = generation
        self._reply_id = reply_id
        self._closed = False

    async def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        seq = 1
        try:
            while not self._closed:
                raw = await self._socket.recv()
                msg = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                server_content = msg.get("serverContent", {})
                model_turn = server_content.get("modelTurn", {})
                parts = model_turn.get("parts", [])
                turn_complete = bool(server_content.get("turnComplete"))

                for part in parts:
                    inline_data = part.get("inlineData", {})
                    data_b64 = inline_data.get("data")
                    mime_type = inline_data.get("mimeType", "")
                    if data_b64:
                        audio_bytes = base64.b64decode(data_b64)
                        yield TtsAudioChunk(
                            audio=audio_bytes,
                            generation=self._generation,
                            reply_id=self._reply_id,
                            sequence=seq,
                            is_final=turn_complete,
                            sample_rate=24000 if "24000" in mime_type else 16000,
                            channels=1,
                            format="pcm",
                            container="none",
                        )
                        seq += 1

                if turn_complete:
                    break

                if msg.get("error"):
                    err_msg = msg["error"].get("message") or "gemini_tts_error"
                    raise TtsProviderError(f"Gemini TTS error: {err_msg}", "gemini", False)
        finally:
            await self.cancel()

    async def cancel(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                await self._socket.close()
            except Exception:
                pass


class GeminiStreamingTtsProvider(TtsProvider):
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model_name: str = DEFAULT_TTS_MODEL,
        voice_name: str = DEFAULT_FEMALE_VOICE,
        endpoint_url: str | None = None,
        connector: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._model_name = model_name
        self._voice_name = voice_name
        self._endpoint_url = endpoint_url or os.getenv("GEMINI_TTS_WS_URL", GEMINI_TTS_WS_URL)
        self._connector = connector

    @property
    def capabilities(self) -> TtsCapabilities:
        has_key = bool(self._api_key)
        return TtsCapabilities(
            input_streaming=False,
            output_streaming=True,
            encoding="pcm",
            container="none",
            supported_sample_rates=(24000, 16000),
            min_text_chars=1,
            max_text_chars=500,
            languages=("en",),
            voice_gender="female",
            supports_cancellation=True,
            enabled=has_key,
            disabled_reason=None if has_key else "GEMINI_API_KEY not configured",
        )

    async def synthesize(self, text: str, generation: int, reply_id: str | None = None) -> TtsSession:
        if not self._api_key:
            raise TtsProviderError("GEMINI_API_KEY not configured", "gemini", False)
        if not text.strip():
            raise ValueError("TTS text must not be empty")

        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise TtsProviderError("websockets dependency required", "gemini", False) from exc

        connector = self._connector or (lambda url, headers: connect(url, additional_headers=headers, user_agent_header="iShop-Drake/0.1"))
        url = f"{self._endpoint_url}?key={self._api_key}"
        socket = await connector(url, {})

        # Setup speech generation configuration
        setup_msg = {
            "setup": {
                "model": f"models/{self._model_name}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": self._voice_name}
                        }
                    },
                },
            }
        }
        await socket.send(json.dumps(setup_msg))

        # Send text for speech generation
        content_msg = {
            "clientContent": {
                "turns": [{"role": "user", "parts": [{"text": text}]}],
                "turnComplete": True,
            }
        }
        await socket.send(json.dumps(content_msg))
        return GeminiStreamingTtsSession(socket, generation, reply_id=reply_id)

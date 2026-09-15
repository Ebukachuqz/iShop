"""ElevenLabs TTS provider adapters (HTTP streaming and WebSocket stream-input)."""

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

DEFAULT_FEMALE_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
DEFAULT_MODEL_ID = "eleven_flash_v2_5"


class ElevenLabsHttpTtsSession(TtsSession):
    def __init__(self, response: Any, generation: int, reply_id: str | None = None, chunk_size: int = 4096):
        self._response = response
        self._generation = generation
        self._reply_id = reply_id
        self._chunk_size = chunk_size
        self._closed = False

    async def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        seq = 1
        try:
            while not self._closed:
                data = await asyncio.to_thread(self._response.read, self._chunk_size)
                if not data:
                    break
                yield TtsAudioChunk(
                    audio=data,
                    generation=self._generation,
                    reply_id=self._reply_id,
                    sequence=seq,
                    is_final=False,
                    sample_rate=16000,
                    channels=1,
                    format="pcm",
                    container="none",
                )
                seq += 1
        finally:
            await self.cancel()

    async def cancel(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._response.close()
            except Exception:
                pass


class ElevenLabsHttpTtsProvider(TtsProvider):
    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice_id: str = DEFAULT_FEMALE_VOICE_ID,
        model_id: str = DEFAULT_MODEL_ID,
        output_format: str = "pcm_16000",
        endpoint_url: str | None = None,
        opener: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self._voice_id = voice_id
        self._model_id = model_id
        self._output_format = output_format
        self._endpoint_url = endpoint_url or f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}/stream"
        self._opener = opener or urllib.request.urlopen

    @property
    def capabilities(self) -> TtsCapabilities:
        has_key = bool(self._api_key)
        return TtsCapabilities(
            input_streaming=False,
            output_streaming=True,
            encoding="pcm",
            container="none",
            supported_sample_rates=(16000,),
            min_text_chars=1,
            max_text_chars=1000,
            languages=("en",),
            voice_gender="female",
            supports_cancellation=True,
            enabled=has_key,
            disabled_reason=None if has_key else "ELEVENLABS_API_KEY not configured",
        )

    async def synthesize(self, text: str, generation: int, reply_id: str | None = None) -> TtsSession:
        if not self._api_key:
            raise TtsProviderError("ELEVENLABS_API_KEY not configured", "elevenlabs", False)
        if not text.strip():
            raise ValueError("TTS text must not be empty")

        url = f"{self._endpoint_url}?output_format={self._output_format}"
        req_body = json.dumps({"text": text, "model_id": self._model_id}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"xi-api-key": self._api_key, "content-type": "application/json"},
            method="POST",
        )
        try:
            resp = await asyncio.to_thread(self._opener, req, timeout=10.0)
            return ElevenLabsHttpTtsSession(resp, generation, reply_id=reply_id)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TtsProviderError(f"ElevenLabs HTTP {exc.code}: {body}", "elevenlabs", exc.code in (429, 500, 502, 503)) from exc
        except Exception as exc:
            raise TtsProviderError(f"ElevenLabs request failed: {exc}", "elevenlabs", True) from exc


class ElevenLabsWsTtsSession(TtsSession):
    def __init__(self, socket: Any, generation: int, reply_id: str | None = None):
        self._socket = socket
        self._generation = generation
        self._reply_id = reply_id
        self._closed = False

    async def send_text(self, text: str, flush: bool = True) -> None:
        if self._closed:
            return
        await self._socket.send(json.dumps({"text": text, "flush": flush}))

    async def finish_text(self) -> None:
        if self._closed:
            return
        await self._socket.send(json.dumps({"text": ""}))

    async def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        seq = 1
        try:
            while not self._closed:
                raw = await self._socket.recv()
                msg = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                if msg.get("audio"):
                    audio_bytes = base64.b64decode(msg["audio"])
                    is_final = bool(msg.get("isFinal"))
                    yield TtsAudioChunk(
                        audio=audio_bytes,
                        generation=self._generation,
                        reply_id=self._reply_id,
                        sequence=seq,
                        is_final=is_final,
                        sample_rate=16000,
                        channels=1,
                        format="pcm",
                        container="none",
                    )
                    seq += 1
                    if is_final:
                        break
        finally:
            await self.cancel()

    async def cancel(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                await self._socket.close()
            except Exception:
                pass


class ElevenLabsWsTtsProvider(TtsProvider):
    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice_id: str = DEFAULT_FEMALE_VOICE_ID,
        model_id: str = DEFAULT_MODEL_ID,
        output_format: str = "pcm_16000",
        endpoint_url: str | None = None,
        connector: Callable[..., Any] | None = None,
    ):
        if "v3" in model_id.lower():
            raise ValueError("eleven_v3 is not supported on stream-input websocket")
        self._api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self._voice_id = voice_id
        self._model_id = model_id
        self._output_format = output_format
        self._endpoint_url = endpoint_url or f"wss://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}/stream-input"
        self._connector = connector

    @property
    def capabilities(self) -> TtsCapabilities:
        has_key = bool(self._api_key)
        return TtsCapabilities(
            input_streaming=True,
            output_streaming=True,
            encoding="pcm",
            container="none",
            supported_sample_rates=(16000,),
            min_text_chars=1,
            max_text_chars=1000,
            languages=("en",),
            voice_gender="female",
            supports_cancellation=True,
            enabled=has_key,
            disabled_reason=None if has_key else "ELEVENLABS_API_KEY not configured",
        )

    async def synthesize(self, text: str, generation: int, reply_id: str | None = None) -> TtsSession:
        if not self._api_key:
            raise TtsProviderError("ELEVENLABS_API_KEY not configured", "elevenlabs", False)
        if self._model_id == "eleven_v3":
            raise TtsProviderError("eleven_v3 model is not supported on stream-input socket", "elevenlabs", False)

        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise TtsProviderError("websockets dependency required", "elevenlabs", False) from exc

        connector = self._connector or (lambda url, headers: connect(url, additional_headers=headers, user_agent_header="iShop-Drake/0.1"))
        url = f"{self._endpoint_url}?model_id={self._model_id}&output_format={self._output_format}"
        socket = await connector(url, {"xi-api-key": self._api_key})

        # Send initial BOS (beginning of stream) configuration
        init_payload = {
            "text": " ",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
            "generation_config": {"chunk_length_schedule": [120, 160, 250, 290]},
        }
        await socket.send(json.dumps(init_payload))

        session = ElevenLabsWsTtsSession(socket, generation, reply_id=reply_id)
        # Send complete text followed by flush
        await session.send_text(text, flush=True)
        await session.finish_text()
        return session

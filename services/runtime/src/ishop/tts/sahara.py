"""Sahara streaming TTS adapter."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlencode

from ishop.tts.base import TtsAudioChunk, TtsProvider, TtsProviderError, TtsSession

SAHARA_TTS_WS_URL = "wss://infer.voice.intron.io/tts/v1/stream"


async def _default_connect(url: str, headers: dict[str, str]):
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise TtsProviderError("Install the runtime websocket dependency", "sahara", False) from exc
    return await connect(url, additional_headers=headers, user_agent_header="iShop-Drake/0.1")


class SaharaTtsSession(TtsSession):
    def __init__(self, socket: Any, text: str, generation: int):
        self._socket = socket
        self._text = text
        self._generation = generation
        self._text_chunks = _split_text(text)
        self._closed = False

    async def begin(self) -> None:
        session = await self._receive()
        if session.get("message_type") != "SESSION_CREATED":
            await self.cancel()
            raise self._error(session)
        for chunk_id, text_chunk in enumerate(self._text_chunks, 1):
            await self._socket.send(
                json.dumps(
                    {"message_type": "INPUT_TEXT_CHUNK", "text": text_chunk, "ack_id": chunk_id}
                )
            )

    async def chunks(self) -> AsyncIterator[TtsAudioChunk]:
        chunk_id = 1
        requested_chunk: int | None = None
        committed = False
        try:
            while not self._closed:
                if requested_chunk is None and chunk_id <= len(self._text_chunks):
                    await self._socket.send(
                        json.dumps({"message_type": "FETCH_AUDIO_CHUNK", "chunk_id": chunk_id})
                    )
                    requested_chunk = chunk_id
                payload = await self._receive()
                kind = payload.get("message_type")
                if kind in {"ERROR", "INPUT_ERROR", "AUTHENTICATION_ERROR", "QUOTA_EXCEEDED"}:
                    raise self._error(payload)
                status = payload.get("processing_status") or payload.get("processing_staus")
                encoded = payload.get("audio_base_64")
                if kind == "FETCH_AUDIO_CHUNK" and status == "READY" and encoded:
                    yield TtsAudioChunk(
                        base64.b64decode(encoded, validate=True),
                        self._generation,
                        sample_rate=_optional_int(payload.get("audio_config_frame_rate")),
                        channels=_optional_int(payload.get("audio_config_num_channels")),
                        format=str(payload.get("extension", ".wav")).lstrip("."),
                    )
                    chunk_id += 1
                    requested_chunk = None
                    if chunk_id > len(self._text_chunks) and not committed:
                        await self._socket.send(json.dumps({"message_type": "COMMIT"}))
                        committed = True
                elif kind == "FETCH_AUDIO_CHUNK" and status == "PROCESSING":
                    requested_chunk = None
                elif kind == "COMMITTED_AUDIO":
                    break
        finally:
            await self.cancel()

    async def cancel(self) -> None:
        if not self._closed:
            self._closed = True
            await self._socket.close()

    async def _receive(self) -> dict[str, Any]:
        raw = await self._socket.recv()
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TtsProviderError("Sahara TTS returned invalid JSON", "sahara", False) from exc
        if not isinstance(value, dict):
            raise TtsProviderError("Sahara TTS returned a non-object event", "sahara", False)
        return value

    def _error(self, payload: dict[str, Any]) -> TtsProviderError:
        kind = str(payload.get("message_type", "UNKNOWN"))
        message = str(
            payload.get("message")
            or payload.get("error")
            or payload.get("error_message")
            or payload.get("status")
            or "unexpected response"
        )
        return TtsProviderError(f"Sahara TTS {kind}: {message}", "sahara", kind == "ERROR")


class SaharaTtsProvider(TtsProvider):
    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        accent: str = "yoruba",
        gender: str = "female",
        output_format: str = "wav",
        endpoint_url: str | None = None,
        connector: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key if api_key is not None else (os.getenv("SAHARA_API_KEY") or os.getenv("INTRON_API_KEY"))
        self._language = language
        self._accent = accent
        self._gender = gender
        self._output_format = output_format
        self._endpoint_url = endpoint_url or os.getenv("SAHARA_TTS_WS_URL", SAHARA_TTS_WS_URL)
        self._connector = connector or _default_connect

    async def synthesize(self, text: str, generation: int) -> TtsSession:
        if not self._api_key:
            raise TtsProviderError("SAHARA_API_KEY not configured", "sahara", False)
        chunks = _split_text(text)
        if not chunks:
            raise ValueError("TTS text must contain at least 10 characters")
        query = urlencode(
            {
                "voice_language": self._language,
                "voice_accent": self._accent,
                "voice_gender": self._gender,
                "output_audio_format": self._output_format,
            }
        )
        socket = await self._connector(
            f"{self._endpoint_url}?{query}",
            {"Authorization": f"Bearer {self._api_key}"},
        )
        session = SaharaTtsSession(socket, text, generation)
        try:
            await session.begin()
        except BaseException:
            await session.cancel()
            raise
        return session


def _split_text(text: str) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    if len(normalized) < 10:
        raise ValueError("Each Sahara TTS text chunk must contain at least 10 characters")
    chunks = [normalized[index : index + 100] for index in range(0, len(normalized), 100)]
    if len(chunks) > 1 and len(chunks[-1]) < 10:
        short_tail = chunks.pop()
        needed = 10 - len(short_tail)
        donor = chunks.pop()
        chunks.extend([donor[:-needed], donor[-needed:] + short_tail])
    return chunks


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None

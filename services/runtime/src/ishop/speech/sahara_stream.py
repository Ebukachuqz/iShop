"""Sahara streaming STT adapter."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlencode

from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import (
    RealtimeSpeechSession,
    SpeechEventKind,
    SpeechStreamEvent,
)

SAHARA_STT_WS_URL = "wss://infer.voice.intron.io/stt/v1/stream"
_TERMINAL_ERRORS = {
    "AUTHENTICATION_ERROR",
    "RESOURCE_EXHAUSTED",
    "QUOTA_EXCEEDED",
    "SESSION_TIME_LIMIT_EXCEEDED",
    "INSUFFICIENT_AUDIO_ACTIVITY",
}


async def _default_connect(url: str, headers: dict[str, str]):
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise SpeechProviderError(
            "Install the runtime websocket dependency before using Sahara streaming",
            "sahara",
            False,
        ) from exc
    # Intron's edge has occasionally returned frames with RSV compression bits
    # set without a valid negotiated extension. Disable per-message compression
    # for this PCM/JSON stream to avoid intermittent protocol disconnects.
    return await connect(
        url,
        additional_headers=headers,
        user_agent_header="iShop-Drake/0.1",
        compression=None,
    )


class SaharaStreamingSession(RealtimeSpeechSession):
    """Maps one Sahara WebSocket connection to one accepted final utterance."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        revision: int,
        language: str = "pcm",
        sample_rate: int = 16000,
        bit_rate: int = 16,
        channels: int = 1,
        endpoint_url: str | None = None,
        connector: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key if api_key is not None else (os.getenv("SAHARA_API_KEY") or os.getenv("INTRON_API_KEY"))
        self._revision = revision
        self._language = language
        self._sample_rate = sample_rate
        self._bit_rate = bit_rate
        self._channels = channels
        self._endpoint_url = endpoint_url or os.getenv("SAHARA_STT_WS_URL", SAHARA_STT_WS_URL)
        self._connector = connector or _default_connect
        self._socket: Any = None
        self._session_id = ""
        self._next_chunk_id = 1
        self._committed = False
        self._closed = False

    async def start(self) -> SpeechStreamEvent:
        if not self._api_key:
            raise SpeechProviderError("SAHARA_API_KEY not configured", "sahara", False)
        if self._socket is not None:
            raise RuntimeError("Streaming session already started")
        query = urlencode(
            {
                "sample_rate": self._sample_rate,
                "bit_rate": self._bit_rate,
                "num_channels": self._channels,
                "use_language_asr_input": self._language,
            }
        )
        self._socket = await self._connector(
            f"{self._endpoint_url}?{query}",
            {"Authorization": f"Bearer {self._api_key}"},
        )
        payload = await self._receive_payload()
        if payload.get("message_type") != "SESSION_CREATED":
            await self._close()
            raise self._protocol_error(payload)
        self._session_id = str(payload.get("session_id", ""))
        if not self._session_id:
            await self._close()
            raise SpeechProviderError("Sahara did not return a session ID", "sahara", False)
        return SpeechStreamEvent(
            SpeechEventKind.SESSION_STARTED,
            self._session_id,
            self._revision,
        )

    async def send_audio(self, pcm16_audio: bytes) -> None:
        self._require_writable()
        if len(pcm16_audio) < 1024 or len(pcm16_audio) > 32768:
            raise ValueError("Sahara PCM chunks must contain 1024 to 32768 bytes")
        if len(pcm16_audio) % 2:
            raise ValueError("PCM16 chunks must contain an even number of bytes")
        payload = {
            "message_type": "INPUT_AUDIO_CHUNK",
            "audio_base_64": base64.b64encode(pcm16_audio).decode("ascii"),
            "ack_id": self._next_chunk_id,
        }
        self._next_chunk_id += 1
        await self._socket.send(json.dumps(payload))

    async def commit(self) -> None:
        self._require_writable()
        self._committed = True
        await self._socket.send(json.dumps({"message_type": "COMMIT"}))

    async def cancel(self) -> None:
        await self._close()

    async def events(self) -> AsyncIterator[SpeechStreamEvent]:
        if self._socket is None:
            raise RuntimeError("Streaming session has not started")
        try:
            while not self._closed:
                payload = await self._receive_payload()
                event = self._map_event(payload)
                if event is not None:
                    yield event
                if event and event.kind in {
                    SpeechEventKind.FINAL_TRANSCRIPT,
                    SpeechEventKind.ERROR,
                }:
                    break
        finally:
            await self._close()

    def _map_event(self, payload: dict[str, Any]) -> SpeechStreamEvent | None:
        kind = payload.get("message_type")
        if kind == "AUDIO_CHUNK_ACK":
            return SpeechStreamEvent(
                SpeechEventKind.AUDIO_ACCEPTED,
                self._session_id,
                self._revision,
                chunk_id=int(payload.get("chunk_id", 0)),
            )
        if kind == "PARTIAL_TRANSCRIPT":
            return SpeechStreamEvent(
                SpeechEventKind.PARTIAL_TRANSCRIPT,
                self._session_id,
                self._revision,
                text=str(payload.get("transcript", "")),
            )
        if kind == "COMMITTED_TRANSCRIPT":
            return SpeechStreamEvent(
                SpeechEventKind.FINAL_TRANSCRIPT,
                self._session_id,
                self._revision,
                text=str(payload.get("transcript_text", "")),
            )
        if kind in _TERMINAL_ERRORS or kind in {
            "ERROR",
            "INPUT_ERROR",
            "CHUNK_SIZE_TOO_SMALL",
            "CHUNK_SIZE_TOO_LARGE",
            "CHUNK_ID_MISMATCH_WITH_TOTAL",
        }:
            return SpeechStreamEvent(
                SpeechEventKind.ERROR,
                self._session_id,
                self._revision,
                text=str(payload.get("message", "")),
                error_code=str(kind),
                retryable=kind in {"RESOURCE_EXHAUSTED", "ERROR"},
            )
        return None

    async def _receive_payload(self) -> dict[str, Any]:
        raw = await self._socket.recv()
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SpeechProviderError("Sahara returned invalid JSON", "sahara", False) from exc
        if not isinstance(payload, dict):
            raise SpeechProviderError("Sahara returned a non-object event", "sahara", False)
        return payload

    def _require_writable(self) -> None:
        if self._socket is None or self._closed:
            raise RuntimeError("Streaming session is not active")
        if self._committed:
            raise RuntimeError("Streaming session has already been committed")

    def _protocol_error(self, payload: dict[str, Any]) -> SpeechProviderError:
        kind = str(payload.get("message_type", "UNKNOWN"))
        message = str(payload.get("message") or payload.get("status") or "unexpected response")
        return SpeechProviderError(
            f"Sahara streaming {kind}: {message}",
            "sahara",
            kind in {"RESOURCE_EXHAUSTED", "ERROR"},
        )

    async def _close(self) -> None:
        if self._socket is not None and not self._closed:
            self._closed = True
            await self._socket.close()

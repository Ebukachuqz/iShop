"""ElevenLabs Scribe v2 Realtime WebSocket STT adapter."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlencode

from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import RealtimeSpeechSession, SpeechEventKind, SpeechStreamEvent

ELEVENLABS_REALTIME_STT_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"


async def _default_connect(url: str, headers: dict[str, str]):
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise SpeechProviderError("Install websockets dependency", "elevenlabs", False) from exc
    return await connect(url, additional_headers=headers, user_agent_header="iShop-Drake/0.1")


class ElevenLabsScribeRealtimeSession(RealtimeSpeechSession):
    """ElevenLabs scribe_v2_realtime WebSocket streaming recognition session."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        revision: int,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
        model_id: str = "scribe_v2_realtime",
        endpoint_url: str | None = None,
        connector: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self._revision = revision
        self._language = language
        self._sample_rate = sample_rate
        if channels != 1:
            raise ValueError("ElevenLabs realtime transcription accepts mono audio only")
        self._model_id = model_id
        self._endpoint_url = endpoint_url or os.getenv("ELEVENLABS_REALTIME_STT_WS_URL", ELEVENLABS_REALTIME_STT_WS_URL)
        self._connector = connector or _default_connect
        self._socket: Any = None
        self._session_id = f"el_stt_{revision}"
        self._closed = False
        self._committed = False
        self._events_queue: asyncio.Queue[SpeechStreamEvent] = asyncio.Queue()
        self._receive_task: asyncio.Task[None] | None = None
        self._last_final_text: str | None = None

    async def start(self) -> SpeechStreamEvent:
        if not self._api_key:
            raise SpeechProviderError("ELEVENLABS_API_KEY not configured", "elevenlabs", False)
        query_values = {"model_id": self._model_id, "audio_format": f"pcm_{self._sample_rate}", "commit_strategy": "manual"}
        if self._language and self._language not in {"pcm", "auto"}:
            query_values["language_code"] = self._language
        query = urlencode(query_values)
        self._socket = await self._connector(
            f"{self._endpoint_url}?{query}",
            {"xi-api-key": self._api_key},
        )
        self._receive_task = asyncio.create_task(self._pump_events())
        ev = SpeechStreamEvent(SpeechEventKind.SESSION_STARTED, self._session_id, self._revision)
        return ev

    async def send_audio(self, pcm16_audio: bytes) -> None:
        if self._closed or self._committed:
            raise RuntimeError("Cannot send audio on committed or closed session")
        if self._socket is not None:
            import base64
            await self._socket.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(pcm16_audio).decode("ascii"),
            }))

    async def commit(self) -> None:
        if self._committed or self._closed:
            return
        self._committed = True
        if self._socket is not None:
            try:
                await self._socket.send(json.dumps({
                    "message_type": "input_audio_chunk",
                    "audio_base_64": "",
                    "commit": True,
                }))
            except Exception:
                pass

    async def cancel(self) -> None:
        if not self._closed:
            self._closed = True
            if self._receive_task is not None and not self._receive_task.done():
                self._receive_task.cancel()
            if self._socket is not None:
                try:
                    await self._socket.close()
                except Exception:
                    pass

    async def events(self) -> AsyncIterator[SpeechStreamEvent]:
        while True:
            try:
                ev = await self._events_queue.get()
                if ev.kind == SpeechEventKind.CLOSED:
                    break
                yield ev
                if ev.kind == SpeechEventKind.FINAL_TRANSCRIPT:
                    break
            except asyncio.CancelledError:
                break

    async def _pump_events(self) -> None:
        try:
            while not self._closed:
                msg = await self._socket.recv()
                if isinstance(msg, bytes):
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                msg_type = data.get("type") or data.get("message_type")
                if msg_type in ("error", "quota_exceeded", "rate_limit_exceeded"):
                    err_code = data.get("error_code") or msg_type
                    await self._events_queue.put(
                        SpeechStreamEvent(SpeechEventKind.ERROR, self._session_id, self._revision, error_code=err_code)
                    )
                    break
                elif msg_type == "partial_transcript":
                    text = data.get("text", "")
                    await self._events_queue.put(
                        SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, self._session_id, self._revision, text=text)
                    )
                elif msg_type in ("committed_transcript", "final_transcript"):
                    text = data.get("text", "")
                    if text != self._last_final_text:
                        self._last_final_text = text
                        await self._events_queue.put(
                            SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, self._session_id, self._revision, text=text)
                        )
                        break
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            await self._events_queue.put(
                SpeechStreamEvent(SpeechEventKind.CLOSED, self._session_id, self._revision)
            )

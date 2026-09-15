"""AssemblyAI v3 Realtime WebSocket STT adapter."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlencode

from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import RealtimeSpeechSession, SpeechEventKind, SpeechStreamEvent

ASSEMBLYAI_REALTIME_WS_URL = "wss://streaming.assemblyai.com/v3/ws"


async def _default_connect(url: str, headers: dict[str, str]):
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise SpeechProviderError("Install websockets dependency", "assemblyai", False) from exc
    return await connect(url, additional_headers=headers, user_agent_header="iShop-Drake/0.1")


class AssemblyAiStreamingSession(RealtimeSpeechSession):
    """AssemblyAI v3 streaming WebSocket recognition session."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        revision: int,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
        speech_model: str = "u3-rt-pro",
        endpoint_url: str | None = None,
        connector: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key or os.getenv("ASSEMBLYAI_API_KEY") or os.getenv("ASSEMBLY_AI_API_KEY")
        self._revision = revision
        self._sample_rate = sample_rate
        if channels != 1:
            raise ValueError("AssemblyAI streaming accepts mono audio only")
        self._language = language
        self._speech_model = speech_model
        self._endpoint_url = endpoint_url or os.getenv("ASSEMBLYAI_REALTIME_WS_URL", ASSEMBLYAI_REALTIME_WS_URL)
        self._connector = connector or _default_connect
        self._socket: Any = None
        self._session_id = f"aai_stt_{revision}"
        self._closed = False
        self._committed = False
        self._events_queue: asyncio.Queue[SpeechStreamEvent] = asyncio.Queue()
        self._receive_task: asyncio.Task[None] | None = None
        self._last_final_text: str | None = None

    async def start(self) -> SpeechStreamEvent:
        if not self._api_key:
            raise SpeechProviderError("ASSEMBLYAI_API_KEY not configured", "assemblyai", False)
        url = f"{self._endpoint_url}?{urlencode({'sample_rate': self._sample_rate, 'speech_model': self._speech_model})}"
        self._socket = await self._connector(url, {"authorization": self._api_key})
        self._receive_task = asyncio.create_task(self._pump_events())
        return SpeechStreamEvent(SpeechEventKind.SESSION_STARTED, self._session_id, self._revision)

    async def send_audio(self, pcm16_audio: bytes) -> None:
        if self._closed or self._committed:
            raise RuntimeError("Cannot send audio on committed or closed session")
        if self._socket is not None:
            # AssemblyAI takes binary PCM frames directly or base64 audio_data
            await self._socket.send(pcm16_audio)

    async def commit(self) -> None:
        if self._committed or self._closed:
            return
        self._committed = True
        if self._socket is not None:
            try:
                await self._socket.send(json.dumps({"type": "ForceEndpoint"}))
                await self._socket.send(json.dumps({"type": "Terminate"}))
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
                if msg_type in {"Begin", "SessionBegins"}:
                    self._session_id = data.get("id") or data.get("session_id") or self._session_id
                elif msg_type == "PartialTranscript":
                    text = data.get("text", "")
                    await self._events_queue.put(
                        SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, self._session_id, self._revision, text=text)
                    )
                elif msg_type == "Turn":
                    text = str(data.get("transcript", "")).strip()
                    if not text:
                        continue
                    if data.get("end_of_turn"):
                        if text != self._last_final_text:
                            self._last_final_text = text
                            await self._events_queue.put(
                                SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, self._session_id, self._revision, text=text)
                            )
                            break
                    else:
                        await self._events_queue.put(
                            SpeechStreamEvent(SpeechEventKind.PARTIAL_TRANSCRIPT, self._session_id, self._revision, text=text)
                        )
                elif msg_type == "FinalTranscript":
                    text = data.get("text", "")
                    if text and text != self._last_final_text:
                        self._last_final_text = text
                        await self._events_queue.put(
                            SpeechStreamEvent(SpeechEventKind.FINAL_TRANSCRIPT, self._session_id, self._revision, text=text)
                        )
                        break
                elif msg_type in {"Termination", "SessionTerminated"}:
                    break
                elif data.get("error"):
                    await self._events_queue.put(
                        SpeechStreamEvent(SpeechEventKind.ERROR, self._session_id, self._revision, error_code=data.get("error"))
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

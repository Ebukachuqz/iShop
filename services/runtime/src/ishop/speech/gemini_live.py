"""Gemini dedicated live transcription adapter (gemini-3.5-transcribe-live)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import RealtimeSpeechSession, SpeechEventKind, SpeechStreamEvent

GEMINI_LIVE_WS_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"


async def _default_connect(url: str, headers: dict[str, str]):
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise SpeechProviderError("Install websockets dependency", "gemini", False) from exc
    return await connect(url, additional_headers=headers, user_agent_header="iShop-Drake/0.1")


class GeminiLiveStreamingSession(RealtimeSpeechSession):
    """Gemini 3.5 live transcription WebSocket streaming session."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        revision: int,
        sample_rate: int = 16000,
        model_name: str = "gemini-3.5-transcribe-live",
        endpoint_url: str | None = None,
        connector: Callable[..., Any] | None = None,
    ):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._revision = revision
        self._sample_rate = sample_rate
        self._model_name = model_name
        self._endpoint_url = endpoint_url or os.getenv("GEMINI_LIVE_WS_URL", GEMINI_LIVE_WS_URL)
        self._connector = connector or _default_connect
        self._socket: Any = None
        self._session_id = f"gemini_live_{revision}"
        self._closed = False
        self._committed = False
        self._events_queue: asyncio.Queue[SpeechStreamEvent] = asyncio.Queue()
        self._receive_task: asyncio.Task[None] | None = None
        self._last_final_text: str | None = None

    async def start(self) -> SpeechStreamEvent:
        if not self._api_key:
            raise SpeechProviderError("GEMINI_API_KEY not configured", "gemini", False)
        url = f"{self._endpoint_url}?key={self._api_key}"
        self._socket = await self._connector(url, {})

        # Send initial Bidi setup message for dedicated live transcription
        setup_msg = {
            "setup": {
                "model": f"models/{self._model_name}",
                "generationConfig": {
                    "responseModalities": ["TEXT"],
                    "speechConfig": {"voiceConfig": {}},
                },
            }
        }
        await self._socket.send(json.dumps(setup_msg))

        self._receive_task = asyncio.create_task(self._pump_events())
        return SpeechStreamEvent(SpeechEventKind.SESSION_STARTED, self._session_id, self._revision)

    async def send_audio(self, pcm16_audio: bytes) -> None:
        if self._closed or self._committed:
            raise RuntimeError("Cannot send audio on committed or closed session")
        if self._socket is not None:
            msg = {
                "realtimeInput": {
                    "mediaChunks": [
                        {"mimeType": f"audio/pcm;rate={self._sample_rate}", "data": base64.b64encode(pcm16_audio).decode("ascii")}
                    ]
                }
            }
            await self._socket.send(json.dumps(msg))

    async def commit(self) -> None:
        if self._committed or self._closed:
            return
        self._committed = True
        if self._socket is not None:
            try:
                await self._socket.send(json.dumps({"clientContent": {"turnComplete": True}}))
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
                raw = await self._socket.recv()
                if isinstance(raw, bytes):
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                server_content = data.get("serverContent", {})
                model_turn = server_content.get("modelTurn", {})
                parts = model_turn.get("parts", [])
                text_parts = [p.get("text", "") for p in parts if p.get("text")]
                text = "".join(text_parts).strip()
                is_turn_complete = bool(server_content.get("turnComplete"))

                if text:
                    if is_turn_complete:
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
                elif is_turn_complete:
                    break
                if data.get("error"):
                    err = data["error"].get("message") or "gemini_transcription_error"
                    await self._events_queue.put(
                        SpeechStreamEvent(SpeechEventKind.ERROR, self._session_id, self._revision, error_code=err)
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

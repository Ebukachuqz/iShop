"""Authenticated browser WebSocket ingress for realtime speech."""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ishop.domain.models import SessionGrant
from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import RealtimeSpeechSession, SpeechEventKind, SpeechStreamEvent
from ishop.speech.sahara_stream import SaharaStreamingSession

SessionFactory = Callable[..., RealtimeSpeechSession]


def create_voice_app(
    *,
    signing_secret: str,
    allowed_origins: set[str],
    session_factory: SessionFactory | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> FastAPI:
    """Create the minimal authenticated voice transport used by WP-02."""
    if len(signing_secret) < 32:
        raise ValueError("Session signing secret must contain at least 32 characters")
    if not allowed_origins:
        raise ValueError("At least one allowed storefront origin is required")

    make_session = session_factory or _make_sahara_session
    now_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    app = FastAPI()

    @app.websocket("/ws/voice/{shop_id}")
    async def voice_socket(websocket: WebSocket, shop_id: str) -> None:
        origin = websocket.headers.get("origin", "")
        if origin not in allowed_origins:
            await websocket.close(code=4403, reason="origin_not_allowed")
            return

        await websocket.accept()
        session: RealtimeSpeechSession | None = None
        revision: int | None = None
        try:
            first = await websocket.receive_json()
            try:
                grant = _authenticate(first, shop_id, origin, signing_secret, now_ms())
            except (TypeError, ValueError):
                await websocket.close(code=4401, reason="invalid_session_grant")
                return
            await websocket.send_json(
                {
                    "type": "authenticated",
                    "session_id": grant.anonymous_session_id,
                    "config_revision": grant.config_revision,
                }
            )

            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if session is None:
                        await _send_error(websocket, "session_not_started")
                        continue
                    try:
                        await session.send_audio(message["bytes"])
                    except (SpeechProviderError, ValueError):
                        await _send_error(websocket, "audio_rejected")
                    continue
                text = message.get("text")
                if text is None:
                    await _send_error(websocket, "invalid_message")
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    await _send_error(websocket, "invalid_json")
                    continue
                action = payload.get("type")
                if action == "start_turn":
                    requested_revision = payload.get("revision")
                    if not isinstance(requested_revision, int) or requested_revision < 1:
                        await _send_error(websocket, "invalid_revision")
                        continue
                    if revision is not None and requested_revision <= revision:
                        await _send_error(websocket, "stale_revision")
                        continue
                    sample_rate = payload.get("sample_rate", 16000)
                    channels = payload.get("channels", 1)
                    if (
                        not isinstance(sample_rate, int)
                        or sample_rate < 8000
                        or sample_rate > 96000
                        or channels not in {1, 2}
                    ):
                        await _send_error(websocket, "unsupported_audio_format")
                        continue
                    if session is not None:
                        await session.cancel()
                    revision = requested_revision
                    session = make_session(
                        revision=revision,
                        language=str(payload.get("language", "pcm")),
                        sample_rate=sample_rate,
                        channels=channels,
                    )
                    try:
                        started = await session.start()
                    except SpeechProviderError:
                        await _send_error(websocket, "provider_start_failed")
                        await session.cancel()
                        session = None
                        continue
                    await websocket.send_json(_event_payload(started))
                elif action == "audio_base64":
                    if session is None:
                        await _send_error(websocket, "session_not_started")
                        continue
                    try:
                        audio = base64.b64decode(str(payload.get("audio", "")), validate=True)
                    except (ValueError, binascii.Error):
                        await _send_error(websocket, "invalid_audio")
                        continue
                    try:
                        await session.send_audio(audio)
                    except (SpeechProviderError, ValueError):
                        await _send_error(websocket, "audio_rejected")
                elif action == "finish_turn":
                    if session is None:
                        await _send_error(websocket, "session_not_started")
                        continue
                    try:
                        await session.commit()
                        async for event in session.events():
                            await websocket.send_json(_event_payload(event))
                    except SpeechProviderError:
                        await _send_error(websocket, "provider_stream_failed")
                        await session.cancel()
                    session = None
                elif action == "cancel_turn":
                    if session is not None:
                        await session.cancel()
                        session = None
                    await websocket.send_json({"type": "turn_canceled", "revision": revision})
                else:
                    await _send_error(websocket, "unknown_message_type")
        except (WebSocketDisconnect, ValueError, TypeError):
            pass
        finally:
            if session is not None:
                await session.cancel()

    return app


def _authenticate(
    payload: Any,
    shop_id: str,
    origin: str,
    signing_secret: str,
    current_time_ms: int,
) -> SessionGrant:
    if not isinstance(payload, dict) or payload.get("type") != "authenticate":
        raise ValueError("First message must authenticate the session")
    raw_grant = payload.get("grant")
    if not isinstance(raw_grant, dict):
        raise ValueError("Missing session grant")
    allowed = {
        "schema_version",
        "grant_id",
        "shop_id",
        "permitted_origin",
        "anonymous_session_id",
        "config_revision",
        "issued_at_ms",
        "expires_at_ms",
        "signature",
    }
    if set(raw_grant) != allowed:
        raise ValueError("Invalid session grant fields")
    grant = SessionGrant(**raw_grant)
    if not grant.is_valid_at(current_time_ms):
        raise ValueError("Session grant expired or not yet valid")
    if not grant.verify_signature(signing_secret):
        raise ValueError("Invalid session grant signature")
    if not grant.verify_tenancy(shop_id, origin):
        raise ValueError("Session grant tenant mismatch")
    return grant


def _make_sahara_session(**kwargs: Any) -> RealtimeSpeechSession:
    return SaharaStreamingSession(**kwargs)


def _event_payload(event: SpeechStreamEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": event.kind.value,
        "revision": event.revision,
        "authorizes_interpretation": event.authorizes_interpretation,
    }
    if event.text is not None:
        payload["text"] = event.text
    if event.chunk_id is not None:
        payload["chunk_id"] = event.chunk_id
    if event.error_code is not None:
        payload["error_code"] = event.error_code
        payload["retryable"] = event.retryable
    return payload


async def _send_error(websocket: WebSocket, code: str) -> None:
    await websocket.send_json(
        {
            "type": SpeechEventKind.ERROR.value,
            "error_code": code,
            "authorizes_interpretation": False,
        }
    )

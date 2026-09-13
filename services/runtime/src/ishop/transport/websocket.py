"""Authenticated browser WebSocket ingress for realtime speech."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, model_validator

from ishop.domain.models import SessionGrant
from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import RealtimeSpeechSession, SpeechEventKind, SpeechStreamEvent
from ishop.speech.sahara_stream import SaharaStreamingSession

SessionFactory = Callable[..., RealtimeSpeechSession]
ShoppingTurnHandler = Callable[[dict[str, Any], SessionGrant], Any]


class RevocationRequest(BaseModel):
    profile_ids: list[str] = Field(default_factory=list, max_length=20)
    shop_ids: list[str] = Field(default_factory=list, max_length=20)
    config_revisions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_selector(self) -> "RevocationRequest":
        if not (self.profile_ids or self.shop_ids or self.config_revisions):
            raise ValueError("At least one revocation selector is required")
        return self


@dataclass(frozen=True)
class ActiveConnection:
    grant: SessionGrant
    websocket: WebSocket


class SessionRevocationRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, ActiveConnection] = {}
        self._profile_ids: set[str] = set()
        self._shop_ids: set[str] = set()
        self._config_revisions: set[str] = set()

    def is_revoked(self, grant: SessionGrant) -> bool:
        return (
            grant.shop_id in self._shop_ids
            or grant.config_revision in self._config_revisions
            or bool(
                {
                    grant.asr_profile_id,
                    grant.llm_profile_id,
                    grant.tts_profile_id,
                }
                & self._profile_ids
            )
        )

    def register(self, grant: SessionGrant, websocket: WebSocket) -> bool:
        if self.is_revoked(grant):
            return False
        self._connections[grant.grant_id] = ActiveConnection(grant, websocket)
        return True

    def unregister(self, grant_id: str) -> None:
        self._connections.pop(grant_id, None)

    async def revoke(self, request: RevocationRequest) -> int:
        self._profile_ids.update(request.profile_ids)
        self._shop_ids.update(request.shop_ids)
        self._config_revisions.update(request.config_revisions)
        matches = [
            connection
            for connection in self._connections.values()
            if self.is_revoked(connection.grant)
        ]
        for connection in matches:
            await connection.websocket.close(code=4410, reason="session_revoked")
        return len(matches)


def create_voice_app(
    *,
    signing_secret: str,
    allowed_origins: set[str],
    session_factory: SessionFactory | None = None,
    session_factories: dict[str, SessionFactory] | None = None,
    allowed_llm_profiles: set[str] | None = None,
    allowed_tts_profiles: set[str] | None = None,
    control_secret: str | None = None,
    revocations: SessionRevocationRegistry | None = None,
    shopping_turn_handler: ShoppingTurnHandler | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> FastAPI:
    """Create the minimal authenticated voice transport used by WP-02."""
    if len(signing_secret) < 32:
        raise ValueError("Session signing secret must contain at least 32 characters")
    if not allowed_origins:
        raise ValueError("At least one allowed storefront origin is required")

    factories = (
        session_factories
        if session_factories is not None
        else {"sahara-stream-pcm": session_factory or _make_sahara_session}
    )
    llm_profiles = (
        allowed_llm_profiles
        if allowed_llm_profiles is not None
        else {"groq-gpt-oss-120b"}
    )
    tts_profiles = (
        allowed_tts_profiles
        if allowed_tts_profiles is not None
        else {"sahara-tts-female-pcm"}
    )
    revocation_secret = control_secret or signing_secret
    if len(revocation_secret) < 32:
        raise ValueError("Runtime control secret must contain at least 32 characters")
    revocation_registry = revocations or SessionRevocationRegistry()
    now_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
    app = FastAPI()

    @app.post("/internal/revocations")
    async def revoke_sessions(payload: RevocationRequest, request: Request) -> dict[str, int]:
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {revocation_secret}"
        if not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Invalid runtime control credential")
        closed_sessions = await revocation_registry.revoke(payload)
        return {"closed_sessions": closed_sessions}

    @app.websocket("/ws/voice/{shop_id}")
    async def voice_socket(websocket: WebSocket, shop_id: str) -> None:
        origin = websocket.headers.get("origin", "")
        if origin not in allowed_origins:
            await websocket.close(code=4403, reason="origin_not_allowed")
            return

        await websocket.accept()
        session: RealtimeSpeechSession | None = None
        revision: int | None = None
        grant: SessionGrant | None = None
        pending_commands: dict[str, dict[str, Any]] = {}
        try:
            first = await websocket.receive_json()
            try:
                grant = _authenticate(first, shop_id, origin, signing_secret, now_ms())
            except (TypeError, ValueError):
                await websocket.close(code=4401, reason="invalid_session_grant")
                return
            make_session = factories.get(grant.asr_profile_id)
            if (
                make_session is None
                or grant.llm_profile_id not in llm_profiles
                or grant.tts_profile_id not in tts_profiles
            ):
                await websocket.close(code=4404, reason="provider_profile_unavailable")
                return
            if not revocation_registry.register(grant, websocket):
                await websocket.close(code=4410, reason="session_revoked")
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
                elif action == "shopping_turn":
                    if shopping_turn_handler is None:
                        await _send_error(websocket, "shopping_runtime_unavailable")
                        continue
                    try:
                        _validate_shopping_turn(payload, grant)
                        result = await shopping_turn_handler(payload, grant)
                        if not isinstance(result, dict):
                            raise ValueError("Shopping handler must return an object")
                        command = result.get("authorized_command")
                        if command is not None:
                            if not isinstance(command, dict) or not command.get("command_id"):
                                raise ValueError("Authorized command is malformed")
                            pending_commands[str(command["command_id"])] = {
                                "turn_id": payload["turn_id"],
                                "request_revision": payload["request_revision"],
                            }
                        await websocket.send_json({"type": "shopping_result", **result})
                    except (TypeError, ValueError):
                        await _send_error(websocket, "invalid_shopping_turn")
                elif action == "command_result":
                    command_id = payload.get("command_id")
                    result = payload.get("result")
                    pending = pending_commands.pop(str(command_id), None)
                    if not pending or not isinstance(result, dict):
                        await _send_error(websocket, "unexpected_command_result")
                        continue
                    outcome = str(result.get("outcome", ""))
                    verified = outcome in {"verified_success", "verified_no_op"}
                    await websocket.send_json({
                        "type": "command_result_ack",
                        "command_id": command_id,
                        "verified": verified,
                        "request_revision": pending["request_revision"],
                    })
                else:
                    await _send_error(websocket, "unknown_message_type")
        except (WebSocketDisconnect, ValueError, TypeError):
            pass
        finally:
            if session is not None:
                await session.cancel()
            if grant is not None:
                revocation_registry.unregister(grant.grant_id)

    return app


def development_allowed_origins(raw_value: str) -> set[str]:
    """Parse the explicit local-development origin list."""
    origins = {value.strip().rstrip("/") for value in raw_value.split(",") if value.strip()}
    if "*" in origins:
        raise ValueError("Wildcard storefront origins are forbidden")
    return origins


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
        "asr_profile_id",
        "llm_profile_id",
        "tts_profile_id",
        "issued_at_ms",
        "expires_at_ms",
        "signature",
    }
    if set(raw_grant) != allowed:
        raise ValueError("Invalid session grant fields")
    grant = SessionGrant(**raw_grant)
    if grant.schema_version != "1.1.0":
        raise ValueError("Unsupported session grant version")
    if not grant.is_valid_at(current_time_ms):
        raise ValueError("Session grant expired or not yet valid")
    if not grant.verify_signature(signing_secret):
        raise ValueError("Invalid session grant signature")
    if not grant.verify_tenancy(shop_id, origin):
        raise ValueError("Session grant tenant mismatch")
    return grant


def _make_sahara_session(**kwargs: Any) -> RealtimeSpeechSession:
    return SaharaStreamingSession(**kwargs)


def _validate_shopping_turn(payload: dict[str, Any], grant: SessionGrant) -> None:
    required = {"type", "turn_id", "request_revision", "page_epoch", "transcript", "evidence", "current_cart"}
    if set(payload) != required:
        raise ValueError("Invalid shopping turn fields")
    if payload["type"] != "shopping_turn":
        raise ValueError("Invalid shopping turn type")
    if not isinstance(payload["turn_id"], str) or not payload["turn_id"]:
        raise ValueError("Invalid turn ID")
    if not isinstance(payload["request_revision"], int) or payload["request_revision"] < 1:
        raise ValueError("Invalid request revision")
    if not isinstance(payload["page_epoch"], int) or payload["page_epoch"] < 1:
        raise ValueError("Invalid page epoch")
    if not isinstance(payload["transcript"], str) or not payload["transcript"].strip() or len(payload["transcript"]) > 4000:
        raise ValueError("Invalid transcript")
    for field_name in ("evidence", "current_cart"):
        if not isinstance(payload[field_name], dict):
            raise ValueError(f"Invalid {field_name}")
        if payload[field_name].get("shop_id") != grant.shop_id:
            raise ValueError(f"{field_name} shop mismatch")


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

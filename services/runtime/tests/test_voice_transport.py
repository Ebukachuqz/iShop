"""Integration tests for authenticated browser voice transport."""

import time

from fastapi.testclient import TestClient
from ishop.domain.models import SessionGrant
from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import SpeechEventKind, SpeechStreamEvent
from ishop.transport.websocket import create_voice_app

SECRET = "voice_transport_test_signing_secret_123456"
ORIGIN = "https://test.myshopify.com"
SHOP = "test.myshopify.com"


class FakeRealtimeSession:
    def __init__(self, revision, language, sample_rate, channels):
        self.revision = revision
        self.language = language
        self.sample_rate = sample_rate
        self.channels = channels
        self.audio = []
        self.committed = False
        self.canceled = False

    async def start(self):
        return SpeechStreamEvent(SpeechEventKind.SESSION_STARTED, "provider-session", self.revision)

    async def send_audio(self, audio):
        self.audio.append(audio)

    async def commit(self):
        self.committed = True

    async def cancel(self):
        self.canceled = True

    async def events(self):
        yield SpeechStreamEvent(
            SpeechEventKind.PARTIAL_TRANSCRIPT,
            "provider-session",
            self.revision,
            text="add medium",
        )
        yield SpeechStreamEvent(
            SpeechEventKind.FINAL_TRANSCRIPT,
            "provider-session",
            self.revision,
            text="add large",
        )


def signed_grant(**changes):
    now = int(time.time() * 1000)
    values = {
        "grant_id": "grant_voice_01",
        "shop_id": SHOP,
        "permitted_origin": ORIGIN,
        "anonymous_session_id": "sess_0123456789abcdef",
        "config_revision": "config_1",
        "issued_at_ms": now - 1000,
        "ttl_ms": 60000,
        "signing_secret": SECRET,
    }
    values.update(changes)
    return SessionGrant.create_signed(**values).__dict__


def app_with_capture():
    sessions = []

    def factory(**kwargs):
        session = FakeRealtimeSession(**kwargs)
        sessions.append(session)
        return session

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=factory,
    )
    return app, sessions


def test_authenticated_pcm_reaches_adapter_and_only_final_authorizes():
    app, sessions = app_with_capture()
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        assert socket.receive_json()["type"] == "authenticated"
        socket.send_json(
            {
                "type": "start_turn",
                "revision": 1,
                "language": "pcm",
                "sample_rate": 16000,
                "channels": 1,
            }
        )
        assert socket.receive_json()["type"] == "session_started"
        socket.send_bytes(b"\x00\x00" * 512)
        socket.send_json({"type": "finish_turn"})
        partial = socket.receive_json()
        final = socket.receive_json()

    assert sessions[0].audio == [b"\x00\x00" * 512]
    assert sessions[0].committed is True
    assert partial["type"] == "partial_transcript"
    assert partial["authorizes_interpretation"] is False
    assert final["type"] == "final_transcript"
    assert final["text"] == "add large"
    assert final["authorizes_interpretation"] is True


def test_audio_before_turn_is_rejected_without_calling_provider():
    app, sessions = app_with_capture()
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        socket.receive_json()
        socket.send_bytes(b"\x00\x00" * 512)
        error = socket.receive_json()
    assert error["error_code"] == "session_not_started"
    assert error["authorizes_interpretation"] is False
    assert sessions == []


def test_cancel_closes_current_session_and_stale_revision_is_rejected():
    app, sessions = app_with_capture()
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        socket.receive_json()
        socket.send_json({"type": "start_turn", "revision": 2})
        socket.receive_json()
        socket.send_json({"type": "cancel_turn"})
        assert socket.receive_json()["type"] == "turn_canceled"
        socket.send_json({"type": "start_turn", "revision": 2})
        error = socket.receive_json()
    assert sessions[0].canceled is True
    assert error["error_code"] == "stale_revision"


def test_untrusted_origin_is_rejected_before_authentication():
    app, _ = app_with_capture()
    with TestClient(app) as client:
        try:
            with client.websocket_connect(
                f"/ws/voice/{SHOP}", headers={"origin": "https://evil.example"}
            ):
                raise AssertionError("Untrusted origin connected")
        except Exception as exc:
            assert getattr(exc, "code", None) == 4403


def test_forged_grant_never_starts_provider_session():
    app, sessions = app_with_capture()
    grant = signed_grant()
    grant["signature"] = "0" * 64
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": grant})
        try:
            socket.receive_json()
            raise AssertionError("Forged grant remained connected")
        except Exception as exc:
            assert getattr(exc, "code", None) == 4401
    assert sessions == []


def test_unsupported_audio_format_never_starts_provider():
    app, sessions = app_with_capture()
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        socket.receive_json()
        socket.send_json({"type": "start_turn", "revision": 1, "sample_rate": 1000})
        error = socket.receive_json()
    assert error["error_code"] == "unsupported_audio_format"
    assert sessions == []


def test_provider_start_failure_is_truthful_and_non_authorizing():
    class FailingSession(FakeRealtimeSession):
        async def start(self):
            raise SpeechProviderError("private provider detail", "sahara", True)

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=lambda **kwargs: FailingSession(**kwargs),
    )
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        socket.receive_json()
        socket.send_json({"type": "start_turn", "revision": 1})
        error = socket.receive_json()
    assert error == {
        "type": "error",
        "error_code": "provider_start_failed",
        "authorizes_interpretation": False,
    }

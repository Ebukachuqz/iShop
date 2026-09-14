"""Integration tests for authenticated browser voice transport."""

import time

import pytest
from fastapi.testclient import TestClient
from ishop.domain.models import CartSnapshot, SessionGrant
from ishop.speech.base import SpeechProviderError
from ishop.speech.realtime import SpeechEventKind, SpeechStreamEvent
from ishop.transport.websocket import _verify_reported_cart_result, create_voice_app, development_allowed_origins

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
        "asr_profile_id": "sahara-stream-pcm",
        "llm_profile_id": "groq-gpt-oss-120b",
        "tts_profile_id": "sahara-tts-female-pcm",
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


def test_unavailable_speech_profile_is_rejected_without_fallback():
    app, sessions = app_with_capture()
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json(
            {
                "type": "authenticate",
                "grant": signed_grant(asr_profile_id="batch-only-profile"),
            }
        )
        try:
            socket.receive_json()
            raise AssertionError("Unavailable profile remained connected")
        except Exception as exc:
            assert getattr(exc, "code", None) == 4404
    assert sessions == []


def test_explicitly_empty_profile_registry_rejects_all_profiles():
    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factories={},
        clock_ms=lambda: int(time.time() * 1000),
    )
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        try:
            socket.receive_json()
            raise AssertionError("Empty registry restored the default profile")
        except Exception as exc:
            assert getattr(exc, "code", None) == 4404


@pytest.mark.parametrize(
    ("field", "profile"),
    [("llm_profile_id", "unknown-llm"), ("tts_profile_id", "unknown-voice")],
)
def test_unavailable_reasoning_or_voice_profile_is_rejected(field, profile):
    app, sessions = app_with_capture()
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant(**{field: profile})})
        try:
            socket.receive_json()
            raise AssertionError("Unavailable profile remained connected")
        except Exception as exc:
            assert getattr(exc, "code", None) == 4404
    assert sessions == []


def test_revocation_control_rejects_unauthenticated_requests():
    app, _ = app_with_capture()
    with TestClient(app) as client:
        response = client.post(
            "/internal/revocations",
            json={"profile_ids": ["sahara-stream-pcm"]},
        )
    assert response.status_code == 401


def test_revoked_profile_rejects_future_sessions_without_fallback():
    app, sessions = app_with_capture()
    with TestClient(app) as client:
        response = client.post(
            "/internal/revocations",
            headers={"authorization": f"Bearer {SECRET}"},
            json={"profile_ids": ["sahara-stream-pcm"]},
        )
        assert response.status_code == 200
        assert response.json() == {"closed_sessions": 0}
        with client.websocket_connect(
            f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
        ) as socket:
            socket.send_json({"type": "authenticate", "grant": signed_grant()})
            try:
                socket.receive_json()
                raise AssertionError("Revoked profile remained available")
            except Exception as exc:
                assert getattr(exc, "code", None) == 4410
    assert sessions == []


def test_revocation_closes_active_session_and_cancels_provider():
    app, sessions = app_with_capture()
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
        ) as socket:
            socket.send_json({"type": "authenticate", "grant": signed_grant()})
            assert socket.receive_json()["type"] == "authenticated"
            socket.send_json({"type": "start_turn", "revision": 1})
            assert socket.receive_json()["type"] == "session_started"
            response = client.post(
                "/internal/revocations",
                headers={"authorization": f"Bearer {SECRET}"},
                json={"config_revisions": ["config_1"]},
            )
            assert response.status_code == 200
            assert response.json() == {"closed_sessions": 1}
            try:
                socket.receive_json()
                raise AssertionError("Revoked active session remained connected")
            except Exception as exc:
                assert getattr(exc, "code", None) == 4410
    assert sessions[0].canceled is True


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


def shopping_turn_payload(**changes):
    payload = {
        "type": "shopping_turn",
        "turn_id": "turn_1",
        "request_revision": 1,
        "page_epoch": 1,
        "transcript": "find a black shirt",
        "evidence": {
            "snapshot_id": "snapshot_1",
            "shop_id": SHOP,
            "currency": "NGN",
            "observed_at_ms": 1,
            "products": [],
        },
        "current_cart": {"shop_id": SHOP, "currency": "NGN", "lines": []},
    }
    payload.update(changes)
    return payload


def test_shopping_turn_handler_is_tenant_bound_and_command_result_is_bound():
    async def handler(payload, grant):
        assert payload["transcript"] == "find a black shirt"
        assert grant.shop_id == SHOP
        return {
            "status": "completed",
            "spoken_response": "I found an option.",
            "authorized_command": {
                "command_id": "cmd_1234567890123456",
                "shop_id": SHOP,
                "operation": "add_variant",
                "expected_cart_fingerprint": CartSnapshot(SHOP, "NGN").fingerprint(),
                "parameters": {"variant_id": "variant_1", "quantity": 1, "properties": {}, "selling_plan_id": None},
            },
        }

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=lambda **kwargs: FakeRealtimeSession(**kwargs),
        shopping_turn_handler=handler,
    )
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        assert socket.receive_json()["type"] == "authenticated"
        socket.send_json(shopping_turn_payload())
        result = socket.receive_json()
        assert result["type"] == "shopping_result"
        assert result["authorized_command"]["command_id"] == "cmd_1234567890123456"
        socket.send_json({
            "type": "command_result",
            "command_id": "cmd_1234567890123456",
            "result": {
                "outcome": "verified_success",
                "before_cart": {"shop_id": SHOP, "currency": "NGN", "lines": []},
                "after_cart": {"shop_id": SHOP, "currency": "NGN", "lines": [{"variant_id": "variant_1", "quantity": 1}]},
            },
        })
        assert socket.receive_json() == {
            "type": "command_result_ack",
            "command_id": "cmd_1234567890123456",
            "verified": True,
            "request_revision": 1,
        }


def test_command_result_label_cannot_override_wrong_cart_state():
    before = CartSnapshot(SHOP, "NGN")
    command = {
        "shop_id": SHOP,
        "operation": "add_variant",
        "expected_cart_fingerprint": before.fingerprint(),
        "parameters": {"variant_id": "variant_1", "quantity": 1, "properties": {}},
    }
    result = {
        "outcome": "verified_success",
        "before_cart": before.to_dict(),
        "after_cart": {"shop_id": SHOP, "currency": "NGN", "lines": []},
    }
    assert _verify_reported_cart_result(command, result) is False


def test_clear_cart_ack_requires_an_independently_observed_empty_cart():
    before = CartSnapshot.from_dict({"shop_id": SHOP, "currency": "NGN", "lines": [
        {"line_key": "line-1", "variant_id": "variant_1", "quantity": 2}
    ]})
    command = {"shop_id": SHOP, "operation": "clear_cart",
        "expected_cart_fingerprint": before.fingerprint(), "parameters": {"explicit_whole_cart": True}}
    verified = {"outcome": "verified_success", "before_cart": before.to_dict(),
        "after_cart": {"shop_id": SHOP, "currency": "NGN", "lines": []}}
    assert _verify_reported_cart_result(command, verified) is True
    verified["after_cart"] = before.to_dict()
    assert _verify_reported_cart_result(command, verified) is False


def test_shopping_turn_rejects_cross_tenant_evidence_before_handler():
    called = []

    async def handler(payload, grant):
        called.append(True)
        return {"status": "completed"}

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=lambda **kwargs: FakeRealtimeSession(**kwargs),
        shopping_turn_handler=handler,
    )
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        socket.receive_json()
        payload = shopping_turn_payload()
        payload["evidence"]["shop_id"] = "other.myshopify.com"
        socket.send_json(payload)
        assert socket.receive_json()["error_code"] == "invalid_shopping_turn"
    assert called == []


def test_shopping_turn_reports_runtime_failure_separately_from_invalid_input():
    async def handler(payload, grant):
        raise RuntimeError("provider response could not be processed")

    app = create_voice_app(
        signing_secret=SECRET,
        allowed_origins={ORIGIN},
        session_factory=lambda **kwargs: FakeRealtimeSession(**kwargs),
        shopping_turn_handler=handler,
    )
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": signed_grant()})
        socket.receive_json()
        socket.send_json(shopping_turn_payload())
        assert socket.receive_json()["error_code"] == "shopping_runtime_failed"


def test_development_origin_parser_rejects_wildcard():
    assert development_allowed_origins("https://one.example, https://two.example/") == {
        "https://one.example",
        "https://two.example",
    }
    try:
        development_allowed_origins("*")
        raise AssertionError("Wildcard origin was accepted")
    except ValueError as exc:
        assert "forbidden" in str(exc)

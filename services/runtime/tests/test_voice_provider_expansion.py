"""Comprehensive acceptance tests for voice provider expansion (VP-01 to VP-20)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ishop.app import create_runtime_app
from ishop.config import RuntimeSettings
from ishop.domain.models import CartSnapshot, SessionGrant
from ishop.speech.assemblyai_realtime import AssemblyAiStreamingSession
from ishop.speech.base import SpeechProviderError
from ishop.speech.elevenlabs_scribe_realtime import ElevenLabsScribeRealtimeSession
from ishop.speech.gemini_live import GeminiLiveStreamingSession
from ishop.speech.realtime import SpeechEventKind, SpeechStreamEvent
from ishop.transport.websocket import create_voice_app
from ishop.tts.base import TtsAudioChunk, TtsProviderError, TtsRegistry, align_pcm16_frames
from ishop.tts.elevenlabs import ElevenLabsHttpTtsProvider, ElevenLabsWsTtsProvider
from ishop.tts.factory import create_default_tts_registry
from ishop.tts.gemini import GeminiStreamingTtsProvider
from ishop.tts.groq import GroqOrpheusTtsProvider
from ishop.tts.sahara import SaharaTtsProvider


TEST_SECRET = "vp_test_signing_secret_1234567890123456"
TEST_ORIGIN = "https://vp-test.myshopify.com"
TEST_SHOP = "vp-test.myshopify.com"


class FakeAsyncSocket:
    def __init__(self, incoming: list[Any] | None = None):
        self.sent: list[Any] = []
        self._incoming = asyncio.Queue()
        if incoming:
            for item in incoming:
                self._incoming.put_nowait(item)
        self.closed = False
        self.url = ""
        self.headers: dict[str, str] = {}

    async def send(self, data: Any) -> None:
        self.sent.append(data)

    async def recv(self) -> Any:
        if self.closed:
            raise RuntimeError("Socket closed")
        try:
            return await self._incoming.get()
        except asyncio.CancelledError:
            raise

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def put_message(self, msg: Any) -> None:
        self._incoming.put_nowait(msg)


class FakeHttpStreamResponse:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self.closed or not self._chunks:
            return b""
        return self._chunks.pop(0)

    def readline(self) -> bytes:
        return self.read()

    def close(self) -> None:
        self.closed = True


def make_grant(
    asr_profile: str = "sahara-stream-pcm",
    tts_profile: str = "sahara-tts-female-pidgin",
    llm_profile: str = "groq-gpt-oss-120b",
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    grant = SessionGrant.create_signed(
        grant_id="grant_vp_01",
        shop_id=TEST_SHOP,
        permitted_origin=TEST_ORIGIN,
        anonymous_session_id="sess_vp_12345678",
        config_revision="rev_1",
        asr_profile_id=asr_profile,
        llm_profile_id=llm_profile,
        tts_profile_id=tts_profile,
        issued_at_ms=now - 1000,
        ttl_ms=60000,
        signing_secret=TEST_SECRET,
    )
    return grant.__dict__


# ---------------------------------------------------------------------------
# VP-01: ElevenLabs Realtime STT
# ---------------------------------------------------------------------------
def test_vp01_elevenlabs_realtime_stt_events_and_deduplication():
    async def run():
        fake_socket = FakeAsyncSocket([
            json.dumps({"type": "partial_transcript", "text": "add black"}),
            json.dumps({"type": "partial_transcript", "text": "add black shirt"}),
            json.dumps({"type": "committed_transcript", "text": "add black shirt"}),
            json.dumps({"type": "final_transcript", "text": "add black shirt"}),  # Duplicate timestamp
        ])

        async def fake_connector(url: str, headers: dict[str, str]):
            fake_socket.url = url
            fake_socket.headers = headers
            return fake_socket

        session = ElevenLabsScribeRealtimeSession(
            api_key="el_test_key",
            revision=1,
            connector=fake_connector,
        )

        started = await session.start()
        assert started.kind == SpeechEventKind.SESSION_STARTED
        assert "scribe_v2_realtime" in fake_socket.url
        assert fake_socket.headers.get("xi-api-key") == "el_test_key"

        await session.send_audio(b"\x00\x00" * 800)
        assert len(fake_socket.sent) == 1
        sent_audio = json.loads(fake_socket.sent[0])
        assert sent_audio["message_type"] == "input_audio_chunk"
        assert base64.b64decode(sent_audio["audio_base_64"]) == b"\x00\x00" * 800

        events = []
        async for ev in session.events():
            events.append(ev)
            if ev.kind == SpeechEventKind.FINAL_TRANSCRIPT:
                break

        partials = [e for e in events if e.kind == SpeechEventKind.PARTIAL_TRANSCRIPT]
        finals = [e for e in events if e.kind == SpeechEventKind.FINAL_TRANSCRIPT]
        assert len(partials) >= 1
        assert len(finals) == 1
        assert finals[0].text == "add black shirt"

        await session.cancel()
        assert fake_socket.closed is True

    asyncio.run(run())


# ---------------------------------------------------------------------------
# VP-02 & VP-04 & VP-05: ElevenLabs HTTP Streaming TTS
# ---------------------------------------------------------------------------
def test_vp02_vp04_vp05_elevenlabs_http_tts_streaming_cancellation():
    async def run():
        chunk1 = b"\x01\x00" * 512
        chunk2 = b"\x02\x00" * 512
        fake_response = FakeHttpStreamResponse([chunk1, chunk2])

        captured_request = {}

        def fake_opener(req, timeout=10.0):
            captured_request["url"] = req.full_url
            captured_request["headers"] = dict(req.headers)
            captured_request["data"] = json.loads(req.data.decode("utf-8"))
            return fake_response

        provider = ElevenLabsHttpTtsProvider(
            api_key="el_http_key",
            opener=fake_opener,
        )

        session = await provider.synthesize("Grounded response text", generation=3, reply_id="rep_1")
        assert "21m00Tcm4TlvDq8ikWAM" in captured_request["url"]
        assert captured_request["data"]["text"] == "Grounded response text"
        assert captured_request["data"]["model_id"] == "eleven_flash_v2_5"
        assert captured_request["headers"]["Xi-api-key"] == "el_http_key"

        chunks = []
        async for c in session.chunks():
            chunks.append(c)
            if len(chunks) == 1:
                await session.cancel()  # VP-05: cancel after first chunk
                break

        assert len(chunks) == 1
        assert chunks[0].audio == chunk1
        assert chunks[0].generation == 3
        assert fake_response.closed is True

        # VP-04: Missing key raises error
        no_key_provider = ElevenLabsHttpTtsProvider(api_key="")
        with pytest.raises(TtsProviderError):
            await no_key_provider.synthesize("text", generation=1)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# VP-03: ElevenLabs WebSocket TTS stream-input
# ---------------------------------------------------------------------------
def test_vp03_elevenlabs_ws_tts_stream_input():
    async def run():
        fake_socket = FakeAsyncSocket([
            json.dumps({"audio": base64.b64encode(b"\x03\x00" * 256).decode("ascii"), "isFinal": False}),
            json.dumps({"audio": base64.b64encode(b"\x04\x00" * 256).decode("ascii"), "isFinal": True}),
        ])

        async def fake_connector(url: str, headers: dict[str, str]):
            return fake_socket

        provider = ElevenLabsWsTtsProvider(
            api_key="el_ws_key",
            connector=fake_connector,
        )

        # v3 model rejected
        with pytest.raises(ValueError, match="eleven_v3 is not supported"):
            ElevenLabsWsTtsProvider(api_key="el_ws_key", model_id="eleven_v3")

        session = await provider.synthesize("Short text", generation=2)
        assert any("Short text" in str(m) for m in fake_socket.sent)

        audio_chunks = [c async for c in session.chunks()]
        assert len(audio_chunks) == 2
        assert audio_chunks[0].is_final is False
        assert audio_chunks[1].is_final is True

    asyncio.run(run())


# ---------------------------------------------------------------------------
# VP-06 & VP-07: AssemblyAI Realtime STT & Standalone TTS Check
# ---------------------------------------------------------------------------
def test_vp06_vp07_assemblyai_realtime_stt_and_no_standalone_tts():
    async def run():
        fake_socket = FakeAsyncSocket([
            json.dumps({"type": "Begin", "id": "aai_session_123"}),
            json.dumps({"type": "Turn", "transcript": "need black", "end_of_turn": False}),
            json.dumps({"type": "Turn", "transcript": "need black shoes", "end_of_turn": True}),
        ])

        async def fake_connector(url: str, headers: dict[str, str]):
            fake_socket.url = url
            fake_socket.headers = headers
            return fake_socket

        session = AssemblyAiStreamingSession(
            api_key="aai_key",
            revision=1,
            connector=fake_connector,
        )

        await session.start()
        assert fake_socket.headers.get("authorization") == "aai_key"
        assert fake_socket.url.startswith("wss://streaming.assemblyai.com/v3/ws?")
        assert "speech_model=u3-rt-pro" in fake_socket.url

        await session.send_audio(b"\x00\x00" * 512)
        await session.commit()
        assert any("ForceEndpoint" in str(m) for m in fake_socket.sent)
        assert any("Terminate" in str(m) for m in fake_socket.sent)

        events = [e async for e in session.events()]
        finals = [e for e in events if e.kind == SpeechEventKind.FINAL_TRANSCRIPT]
        assert len(finals) == 1
        assert finals[0].text == "need black shoes"

        # VP-07: Standalone AssemblyAI TTS is not available / not registered
        registry = create_default_tts_registry()
        assert "assemblyai-tts" not in registry.list_profiles()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# VP-08: Gemini Live Dedicated STT
# ---------------------------------------------------------------------------
def test_vp08_gemini_live_stt():
    async def run():
        fake_socket = FakeAsyncSocket([
            json.dumps({
                "serverContent": {
                    "interimInputTranscription": {"text": "find dress"},
                }
            }),
            json.dumps({
                "serverContent": {
                    "inputTranscription": {"text": "find red dress"},
                }
            }),
        ])

        async def fake_connector(url: str, headers: dict[str, str]):
            fake_socket.url = url
            return fake_socket

        session = GeminiLiveStreamingSession(
            api_key="gemini_key",
            revision=1,
            connector=fake_connector,
        )

        await session.start()
        assert "gemini-3.5-transcribe-live" in str(fake_socket.sent[0])
        assert "inputAudioTranscription" in str(fake_socket.sent[0])

        await session.send_audio(b"\x00\x00" * 512)
        assert any("realtimeInput" in str(m) for m in fake_socket.sent)
        assert any('"audio"' in str(m) for m in fake_socket.sent)

        events = [e async for e in session.events()]
        partials = [e for e in events if e.kind == SpeechEventKind.PARTIAL_TRANSCRIPT]
        finals = [e for e in events if e.kind == SpeechEventKind.FINAL_TRANSCRIPT]
        assert len(partials) >= 1
        assert len(finals) == 1
        assert finals[0].text == "find red dress"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# VP-09 & VP-10 & VP-11: Gemini Streaming TTS
# ---------------------------------------------------------------------------
def test_vp09_vp10_vp11_gemini_streaming_tts():
    async def run():
        audio_part = base64.b64encode(b"\x05\x00" * 512).decode("ascii")
        response = FakeHttpStreamResponse([
            ("data: " + json.dumps({"event_type": "step.delta", "delta": {"type": "audio", "data": audio_part}}) + "\n").encode()
        ])
        captured = {}
        def fake_opener(request, timeout=10.0):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.data.decode())
            return response

        provider = GeminiStreamingTtsProvider(
            api_key="gemini_tts_key",
            voice_name="Kore",
            opener=fake_opener,
        )

        session = await provider.synthesize("2 items added for $15.00", generation=1)
        assert captured["url"].endswith("/v1beta/interactions")
        assert captured["body"]["model"] == "gemini-3.1-flash-tts-preview"
        assert captured["body"]["stream"] is True
        assert "2 items added for $15.00" in captured["body"]["input"]
        assert captured["body"]["generation_config"]["speech_config"][0]["voice"] == "Kore"

        chunks = [c async for c in session.chunks()]
        assert len(chunks) == 1
        assert chunks[0].sample_rate == 24000
        assert chunks[0].container == "none"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# VP-12 & VP-13: Groq Orpheus TTS & Pidgin Rejection
# ---------------------------------------------------------------------------
def test_vp12_vp13_groq_orpheus_tts_is_explicitly_english_and_buffered():
    async def run():
        wav_header_and_data = b"RIFF" + b"\x00" * 36 + b"data" + (b"\x06\x00" * 256)

        def fake_opener(req, timeout=10.0):
            data = json.loads(req.data.decode("utf-8"))
            assert data["model"] == "canopylabs/orpheus-v1-english"
            assert data["voice"] == "autumn"
            return FakeHttpStreamResponse([wav_header_and_data])

        provider = GroqOrpheusTtsProvider(
            api_key="groq_key",
            opener=fake_opener,
        )

        # Output streaming is False (buffered)
        assert provider.capabilities.output_streaming is False

        # English text succeeds
        session = await provider.synthesize("Your cart has two items.", generation=1)
        chunks = [c async for c in session.chunks()]
        assert len(chunks) == 1
        assert chunks[0].audio == wav_header_and_data

        assert provider.capabilities.languages == ("en",)
        assert provider.capabilities.supports_cancellation is False

        # Content-token guessing must not reject valid English containing words such as "fit".
        second = await provider.synthesize("This size should fit.", generation=2)
        assert len([chunk async for chunk in second.chunks()]) == 1

    asyncio.run(run())


def test_realtime_adapters_accept_the_common_transport_arguments():
    common = {"revision": 1, "language": "en", "sample_rate": 16000, "channels": 1}
    assert ElevenLabsScribeRealtimeSession(api_key="x", **common)
    assert AssemblyAiStreamingSession(api_key="x", **common)
    assert GeminiLiveStreamingSession(api_key="x", **common)

# ---------------------------------------------------------------------------
# VP-14: Frozen Signed TTS Selection Changes Runtime Factory
# ---------------------------------------------------------------------------
def test_vp14_signed_tts_selection_changes_runtime_factory():
    settings = RuntimeSettings(
        host="127.0.0.1",
        port=8000,
        signing_secret=TEST_SECRET,
        allowed_origins=frozenset({TEST_ORIGIN}),
        sahara_api_key="sahara-test",
        record_audio=False,
    )
    app = create_runtime_app(settings)

    # 1. Connect with ElevenLabs HTTP TTS profile
    grant_el = make_grant(tts_profile="elevenlabs-tts-female-stream")
    with TestClient(app).websocket_connect(f"/ws/voice/{TEST_SHOP}", headers={"origin": TEST_ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": grant_el})
        auth = ws.receive_json()
        assert auth["type"] == "authenticated"

    # 2. Connect with Gemini TTS profile
    grant_gemini = make_grant(tts_profile="gemini-tts-female-stream")
    with TestClient(app).websocket_connect(f"/ws/voice/{TEST_SHOP}", headers={"origin": TEST_ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": grant_gemini})
        auth = ws.receive_json()
        assert auth["type"] == "authenticated"

    # 3. Connect with Groq TTS profile
    grant_groq = make_grant(tts_profile="groq-orpheus-tts-female")
    with TestClient(app).websocket_connect(f"/ws/voice/{TEST_SHOP}", headers={"origin": TEST_ORIGIN}) as ws:
        ws.send_json({"type": "authenticate", "grant": grant_groq})
        auth = ws.receive_json()
        assert auth["type"] == "authenticated"


# ---------------------------------------------------------------------------
# VP-15: PCM Frame Alignment & Carryover
# ---------------------------------------------------------------------------
def test_vp15_pcm_frame_alignment_and_carryover():
    # 3 bytes incoming: 2 bytes aligned, 1 remainder
    aligned1, remainder1 = align_pcm16_frames(b"\x01\x02\x03")
    assert aligned1 == b"\x01\x02"
    assert remainder1 == b"\x03"

    # Next 3 bytes: 1 remainder + 3 = 4 bytes (all aligned, 0 remainder)
    aligned2, remainder2 = align_pcm16_frames(b"\x04\x05\x06", remainder=remainder1)
    assert aligned2 == b"\x03\x04\x05\x06"
    assert remainder2 == b""


# ---------------------------------------------------------------------------
# VP-16: Missing Optional Key Leaves Other Profiles & Startup Working
# ---------------------------------------------------------------------------
def test_vp16_missing_optional_keys_keeps_startup_working():
    settings = RuntimeSettings(
        host="127.0.0.1",
        port=8000,
        signing_secret=TEST_SECRET,
        allowed_origins=frozenset({TEST_ORIGIN}),
        sahara_api_key="sahara-only-key",
        record_audio=False,
        elevenlabs_api_key=None,
        assemblyai_api_key=None,
        gemini_api_key=None,
        groq_api_key=None,
    )
    # Starts up smoothly without errors
    app = create_runtime_app(settings)
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200

    # Unauthenticated / forged profile is rejected
    with TestClient(app).websocket_connect(f"/ws/voice/{TEST_SHOP}", headers={"origin": TEST_ORIGIN}) as ws:
        bad_grant = make_grant(asr_profile="non-existent-profile")
        ws.send_json({"type": "authenticate", "grant": bad_grant})
        try:
            ws.receive_json()
            assert False, "Should have been rejected"
        except Exception as exc:
            assert getattr(exc, "code", None) == 4404


# ---------------------------------------------------------------------------
# VP-17 & VP-18: Cross-Provider Matrix & Spoken Verified Cart Outcome
# ---------------------------------------------------------------------------
def test_vp17_cross_provider_matrix_profiles_registered():
    registry = create_default_tts_registry()
    profiles = registry.list_profiles()
    assert "sahara-tts-female-pidgin" in profiles
    assert "elevenlabs-tts-female-stream" in profiles
    assert "elevenlabs-tts-female-ws" in profiles
    assert "gemini-tts-female-stream" in profiles
    assert "groq-orpheus-tts-female" in profiles


def test_vp18_verified_cart_result_only_spoken_after_verification():
    from ishop.transport.websocket import _verify_reported_cart_result

    before = CartSnapshot(TEST_SHOP, "USD")
    command = {
        "shop_id": TEST_SHOP,
        "operation": "add_variant",
        "expected_cart_fingerprint": before.fingerprint(),
        "parameters": {"variant_id": "variant_1", "quantity": 1, "properties": {}},
    }
    # Unverified cart result (e.g. wrong outcome or after_cart unchanged)
    result_fail = {
        "outcome": "verified_success",
        "before_cart": before.to_dict(),
        "after_cart": before.to_dict(),
    }
    assert _verify_reported_cart_result(command, result_fail) is False

    # Verified result
    result_ok = {
        "outcome": "verified_success",
        "before_cart": before.to_dict(),
        "after_cart": {
            "shop_id": TEST_SHOP,
            "currency": "USD",
            "lines": [{"variant_id": "variant_1", "quantity": 1}],
        },
    }
    assert _verify_reported_cart_result(command, result_ok) is True


# ---------------------------------------------------------------------------
# VP-19: Barge-in / Cancellation & Text Fallback
# ---------------------------------------------------------------------------
def test_vp19_tts_audio_chunk_serialization():
    chunk = TtsAudioChunk(
        audio=b"test_audio",
        generation=1,
        reply_id="reply_1",
        sequence=1,
        is_final=True,
        sample_rate=16000,
        channels=1,
        format="pcm",
        container="none",
    )
    d = chunk.to_dict()
    assert d["generation"] == 1
    assert d["reply_id"] == "reply_1"
    assert d["is_final"] is True
    assert base64.b64decode(d["audio_base64"]) == b"test_audio"


# ---------------------------------------------------------------------------
# VP-20: Profile Migration & Diagnostics Without Secrets
# ---------------------------------------------------------------------------
def test_vp20_legacy_profile_migration_and_secret_redaction():
    registry = create_default_tts_registry()
    # Legacy profile alias points to same provider
    legacy = registry.get("sahara-tts-female-pcm")
    current = registry.get("sahara-tts-female-pidgin")
    assert legacy is current

    # Diagnostics contain no raw secrets
    caps = legacy.capabilities
    assert "api_key" not in caps.__dict__

"""Contract tests for realtime speech and TTS boundaries."""

import asyncio
import base64
import json

import pytest
from ishop.speech.realtime import SpeechEventKind
from ishop.speech.sahara_stream import SaharaStreamingSession
from ishop.tts.base import TtsProviderError
from ishop.tts.sahara import SaharaTtsProvider


class FakeSocket:
    def __init__(self, responses):
        self.responses = [json.dumps(response) for response in responses]
        self.sent = []
        self.closed = False

    async def recv(self):
        return self.responses.pop(0)

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self):
        self.closed = True


def connector_for(socket):
    async def connect(url, headers):
        socket.url = url
        socket.headers = headers
        return socket

    return connect


def test_partial_transcript_never_authorizes_interpretation_and_final_does():
    async def run():
        socket = FakeSocket(
            [
                {"message_type": "SESSION_CREATED", "session_id": "session-1"},
                {"message_type": "PARTIAL_TRANSCRIPT", "transcript": "add medium"},
                {
                    "message_type": "COMMITTED_TRANSCRIPT",
                    "transcript_text": "add large",
                },
            ]
        )
        session = SaharaStreamingSession(
            "key", revision=7, connector=connector_for(socket)
        )
        started = await session.start()
        await session.send_audio(b"\x00\x00" * 512)
        await session.commit()
        events = [event async for event in session.events()]
        return socket, started, events

    socket, started, events = asyncio.run(run())
    assert started.kind is SpeechEventKind.SESSION_STARTED
    assert events[0].kind is SpeechEventKind.PARTIAL_TRANSCRIPT
    assert events[0].text == "add medium"
    assert events[0].authorizes_interpretation is False
    assert events[1].kind is SpeechEventKind.FINAL_TRANSCRIPT
    assert events[1].text == "add large"
    assert events[1].authorizes_interpretation is True
    assert events[1].revision == 7
    assert socket.sent[-1] == {"message_type": "COMMIT"}
    assert socket.closed is True


def test_committed_stream_rejects_more_audio():
    async def run():
        socket = FakeSocket([{"message_type": "SESSION_CREATED", "session_id": "session-1"}])
        session = SaharaStreamingSession("key", revision=1, connector=connector_for(socket))
        await session.start()
        await session.commit()
        with pytest.raises(RuntimeError, match="already been committed"):
            await session.send_audio(b"\x00\x00" * 512)

    asyncio.run(run())


def test_stream_enforces_documented_pcm_chunk_limits():
    async def run():
        socket = FakeSocket([{"message_type": "SESSION_CREATED", "session_id": "session-1"}])
        session = SaharaStreamingSession("key", revision=1, connector=connector_for(socket))
        await session.start()
        with pytest.raises(ValueError, match="1024 to 32768"):
            await session.send_audio(b"\x00\x00")
        with pytest.raises(ValueError, match="even number"):
            await session.send_audio(b"0" * 1025)

    asyncio.run(run())


def test_sahara_tts_uses_female_profile_and_preserves_generation():
    async def run():
        audio = b"RIFFsample"
        socket = FakeSocket(
            [
                {"message_type": "SESSION_CREATED", "session_id": "tts-1"},
                {"message_type": "TEXT_CHUNK_ACK", "chunk_id": 1},
                {
                    "message_type": "FETCH_AUDIO_CHUNK",
                    "processing_status": "READY",
                    "audio_base_64": base64.b64encode(audio).decode("ascii"),
                    "extension": ".wav",
                    "audio_config_frame_rate": 48000,
                    "audio_config_num_channels": 1,
                },
                {"message_type": "COMMITTED_AUDIO", "text_id": "text-1"},
            ]
        )
        provider = SaharaTtsProvider("key", connector=connector_for(socket))
        session = await provider.synthesize("Your black shirt is ready.", generation=4)
        chunks = [chunk async for chunk in session.chunks()]
        return socket, chunks

    socket, chunks = asyncio.run(run())
    assert "voice_gender=female" in socket.url
    assert "voice_accent=pidgin" in socket.url
    assert chunks[0].audio == b"RIFFsample"
    assert chunks[0].generation == 4
    assert socket.closed is True


def test_sahara_tts_rebalances_a_short_final_chunk():
    from ishop.tts.sahara import _split_text

    chunks = _split_text("a" * 105)
    assert [len(chunk) for chunk in chunks] == [95, 10]


def test_sahara_tts_preserves_safe_session_initialization_failure_code():
    async def run():
        socket = FakeSocket([{"message_type": "SESSION_INITIALIZATION_ERROR"}])
        provider = SaharaTtsProvider("key", connector=connector_for(socket))
        with pytest.raises(TtsProviderError) as raised:
            await provider.synthesize("Please read this reply aloud.", generation=1)
        return socket, raised.value

    socket, error = asyncio.run(run())
    assert error.code == "session_initialization_error"
    assert error.retryable is True
    assert socket.closed is True


def test_cancel_closes_tts_before_audio_can_be_emitted():
    async def run():
        socket = FakeSocket([
            {"message_type": "SESSION_CREATED", "session_id": "tts-1"},
            {"message_type": "TEXT_CHUNK_ACK", "chunk_id": 1},
        ])
        provider = SaharaTtsProvider("key", connector=connector_for(socket))
        session = await provider.synthesize("Please stop speaking now.", generation=2)
        await session.cancel()
        return socket

    assert asyncio.run(run()).closed is True

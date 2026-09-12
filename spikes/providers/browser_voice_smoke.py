"""Run browser-like PCM through the authenticated runtime into Sahara."""

from __future__ import annotations

import json
import sys
import time
import wave
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "runtime" / "src"))

from ishop.domain.models import SessionGrant  # noqa: E402
from ishop.transport.websocket import create_voice_app  # noqa: E402
from speech_smoke import load_env  # noqa: E402

SHOP = "transport-smoke.myshopify.com"
ORIGIN = f"https://{SHOP}"
SECRET = "wp02_transport_smoke_signing_secret_123456"


def read_audio(path: Path) -> tuple[bytes, int, int]:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getnchannels() not in {1, 2}:
            raise ValueError("Input must be mono or stereo PCM16 WAV")
        return source.readframes(source.getnframes()), source.getframerate(), source.getnchannels()


def main() -> int:
    load_env(ROOT / ".env")
    audio, sample_rate, channels = read_audio(ROOT / "data/private/ishop-testaudio-1.wav")
    now = int(time.time() * 1000)
    grant = SessionGrant.create_signed(
        grant_id="grant_transport_smoke",
        shop_id=SHOP,
        permitted_origin=ORIGIN,
        anonymous_session_id="sess_transport_smoke_01",
        config_revision="config_wp02",
        issued_at_ms=now - 1000,
        ttl_ms=60000,
        signing_secret=SECRET,
    )
    app = create_voice_app(signing_secret=SECRET, allowed_origins={ORIGIN})
    events = []
    with TestClient(app).websocket_connect(
        f"/ws/voice/{SHOP}", headers={"origin": ORIGIN}
    ) as socket:
        socket.send_json({"type": "authenticate", "grant": grant.__dict__})
        events.append(socket.receive_json())
        socket.send_json(
            {
                "type": "start_turn",
                "revision": 1,
                "language": "pcm",
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
        events.append(socket.receive_json())
        for offset in range(0, len(audio), 16384):
            chunk = audio[offset : offset + 16384]
            if len(chunk) < 1024:
                chunk += b"\x00" * (1024 - len(chunk))
            socket.send_bytes(chunk)
        socket.send_json({"type": "finish_turn"})
        while True:
            event = socket.receive_json()
            events.append(event)
            if event["type"] in {"final_transcript", "error"}:
                break

    safe_result = {
        "authenticated": events[0]["type"] == "authenticated",
        "session_started": events[1]["type"] == "session_started",
        "partial_count": sum(event["type"] == "partial_transcript" for event in events),
        "final_count": sum(event["type"] == "final_transcript" for event in events),
        "final_authorized": any(
            event["type"] == "final_transcript" and event["authorizes_interpretation"]
            for event in events
        ),
        "partial_authorized": any(
            event["type"] == "partial_transcript" and event["authorizes_interpretation"]
            for event in events
        ),
        "errors": [event.get("error_code") for event in events if event["type"] == "error"],
    }
    output = ROOT / "artifacts/private/browser-voice-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe_result, indent=2), encoding="utf-8")
    print(json.dumps(safe_result))
    return 0 if safe_result["final_count"] == 1 and safe_result["final_authorized"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

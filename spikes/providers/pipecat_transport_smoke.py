"""Check the pinned Pipecat WebSocket transport configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("NLTK_DATA", str(ROOT / ".runtime-data" / "nltk"))

from pipecat.transports.websocket.fastapi import (  # noqa: E402
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)


class ProbeWebSocket:
    client_state = None
    application_state = None


def main() -> int:
    params = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_in_sample_rate=16000,
        audio_in_channels=1,
        audio_in_passthrough=True,
        audio_out_enabled=True,
        audio_out_sample_rate=48000,
        audio_out_channels=1,
        add_wav_header=False,
        fixed_audio_packet_size=640,
        session_timeout=300,
    )
    transport = FastAPIWebsocketTransport(ProbeWebSocket(), params)
    result = {
        "pipecat_version": "1.3.0",
        "transport": type(transport).__name__,
        "audio_in_sample_rate": params.audio_in_sample_rate,
        "audio_in_channels": params.audio_in_channels,
        "audio_out_sample_rate": params.audio_out_sample_rate,
        "fixed_audio_packet_size": params.fixed_audio_packet_size,
        "session_timeout": params.session_timeout,
        "custom_sahara_adapter_importable": True,
        "nltk_resource_provisioned": (ROOT / ".runtime-data" / "nltk").exists(),
    }
    output = ROOT / "artifacts" / "private" / "pipecat-transport-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Pipecat FastAPI WebSocket transport configured successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

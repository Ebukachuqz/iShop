"""Run redacted Sahara streaming STT and TTS development checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "runtime" / "src"))

from ishop.speech.realtime import SpeechEventKind  # noqa: E402
from ishop.speech.sahara_stream import SaharaStreamingSession  # noqa: E402
from ishop.tts.sahara import SaharaTtsProvider  # noqa: E402
from speech_smoke import load_env  # noqa: E402


def read_pcm16(path: Path) -> tuple[bytes, int, int]:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getnchannels() not in {1, 2}:
            raise ValueError("The streaming smoke input must be mono or stereo PCM16 WAV")
        return (
            source.readframes(source.getnframes()),
            source.getframerate(),
            source.getnchannels(),
        )


async def transcribe_turn(
    audio: bytes,
    sample_rate: int,
    channels: int,
    revision: int,
    language: str,
) -> dict:
    started_at = time.perf_counter()
    session = SaharaStreamingSession(
        revision=revision,
        language=language,
        sample_rate=sample_rate,
        channels=channels,
    )
    started = await session.start()
    for offset in range(0, len(audio), 16384):
        chunk = audio[offset : offset + 16384]
        if len(chunk) < 1024:
            chunk += b"\x00" * (1024 - len(chunk))
        await session.send_audio(chunk)
    await session.commit()
    partial_count = 0
    final_text = ""
    errors = []
    async for event in session.events():
        if event.kind is SpeechEventKind.PARTIAL_TRANSCRIPT:
            partial_count += 1
        elif event.kind is SpeechEventKind.FINAL_TRANSCRIPT:
            final_text = event.text or ""
        elif event.kind is SpeechEventKind.ERROR:
            errors.append({"code": event.error_code, "message": event.text})
    return {
        "revision": revision,
        "session_created": bool(started.session_id),
        "partial_count": partial_count,
        "final_transcript": final_text,
        "errors": errors,
        "latency_ms": round((time.perf_counter() - started_at) * 1000, 1),
    }


async def synthesize(output: Path, language: str, accent: str) -> dict:
    provider = SaharaTtsProvider(language=language, accent=accent, gender="female")
    session = await provider.synthesize(
        "I found the large black shirt. Please confirm before I add it.",
        generation=1,
    )
    chunks = [chunk async for chunk in session.chunks()]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"".join(chunk.audio for chunk in chunks))
    return {
        "profile": f"sahara-tts-{language}-{accent}-female",
        "chunks": len(chunks),
        "bytes": output.stat().st_size,
        "format": chunks[0].format if chunks else None,
        "sample_rate": chunks[0].sample_rate if chunks else None,
    }


async def run(args: argparse.Namespace) -> dict:
    audio, sample_rate, channels = read_pcm16(args.audio)
    turns = []
    for revision in range(1, args.turns + 1):
        turns.append(await transcribe_turn(audio, sample_rate, channels, revision, args.language))
    result = {"stt": {"route": "stream", "turns": turns}}
    if not args.skip_tts:
        result["tts"] = await synthesize(args.tts_output, args.tts_language, args.tts_accent)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tts-output",
        type=Path,
        default=ROOT / "artifacts/private/sahara-tts.wav",
    )
    parser.add_argument("--language", default="pcm")
    parser.add_argument("--tts-language", default="en")
    parser.add_argument("--tts-accent", default="yoruba")
    parser.add_argument("--turns", type=int, default=2)
    parser.add_argument("--skip-tts", action="store_true")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Sahara streaming turns: {len(result['stt']['turns'])}")
    print(f"Sahara TTS chunks: {result.get('tts', {}).get('chunks', 'skipped')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

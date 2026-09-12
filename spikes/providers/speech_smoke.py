"""Run one private WAV through the selected ASR panel; never prints credentials."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "runtime" / "src"))

from ishop.speech.assemblyai import AssemblyAiSpeechProvider
from ishop.speech.elevenlabs_scribe import ElevenLabsScribeSpeechProvider
from ishop.speech.gemini_transcribe import GeminiSpeechProvider
from ishop.speech.groq_whisper import GroqWhisperSpeechProvider
from ishop.speech.sahara import SaharaSpeechProvider


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def run(audio_path: Path, selected: set[str], sahara_language: str) -> list[dict]:
    audio = audio_path.read_bytes()
    providers = [
        SaharaSpeechProvider(timeout_seconds=120),
        GroqWhisperSpeechProvider(timeout_seconds=120),
        AssemblyAiSpeechProvider(timeout_seconds=120),
        GeminiSpeechProvider(timeout_seconds=120),
        ElevenLabsScribeSpeechProvider(timeout_seconds=120),
    ]
    rows = []
    for provider in providers:
        profile = provider.profile
        if selected and profile.provider_name not in selected:
            continue
        row = {"provider": profile.provider_name, "profile": profile.profile_id,
               "model": profile.model_name, "mode": "batch"}
        if not profile.enabled:
            row.update(status="blocked", error=profile.disabled_reason)
        else:
            try:
                hint = sahara_language if profile.provider_name == "sahara" else None
                result = await provider.transcribe(audio, language_hint=hint)
                row.update(status="success", transcript=result.transcript,
                           latency_ms=round(result.latency_ms, 1), metadata=result.raw_metadata)
            except Exception as exc:
                row.update(status="error", error_type=type(exc).__name__, error=str(exc)[:500])
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--sahara-language", default="pcm")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    rows = asyncio.run(run(args.audio.resolve(), set(args.provider), args.sahara_language))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in rows:
        print(f"{row['provider']}: {row['status']} ({row.get('latency_ms', '-')} ms)")
    return 0 if all(row["status"] == "success" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

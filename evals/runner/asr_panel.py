"""Run a fixed audio manifest through interchangeable speech providers."""
from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evals.runner.manifest import RunManifest
from evals.scoring.speech_metrics import calculate_cer, calculate_wer
from ishop.speech.base import SpeechProvider, SpeechRegistry


LANGUAGE_HINTS = {
    "pcm-eng": "pcm",
    "yo-eng": "yo",
    "eng": "en",
}


@dataclass(frozen=True)
class AsrAttempt:
    episode_id: str
    provider: str
    profile_id: str
    model: str
    status: str
    transcript: str
    latency_ms: float | None
    wer: float
    cer: float
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _audio_path(audio_ref: str, base_dir: Path) -> Path:
    path = Path(audio_ref)
    return path if path.is_absolute() else base_dir / path


async def _attempt(provider: SpeechProvider, episode, base_dir: Path) -> AsrAttempt:
    profile = provider.profile
    base = {
        "episode_id": episode.episode_id,
        "provider": profile.provider_name,
        "profile_id": profile.profile_id,
        "model": profile.model_name,
    }
    if not profile.enabled:
        return AsrAttempt(**base, status="blocked", transcript="", latency_ms=None,
                          wer=1.0, cer=1.0, error_type="provider_disabled",
                          error_message=profile.disabled_reason)
    if episode.consent_allowed is not True or profile.provider_name not in episode.allowed_processors:
        return AsrAttempt(**base, status="consent_blocked", transcript="", latency_ms=None,
                          wer=1.0, cer=1.0, error_type="consent_blocked",
                          error_message="Processor is not allowed for this episode")
    try:
        audio = _audio_path(episode.audio_ref, base_dir).read_bytes()
        hint = LANGUAGE_HINTS.get(episode.language_pair) if profile.provider_name == "sahara" else None
        started = time.monotonic()
        result = await provider.transcribe(audio, language_hint=hint)
        elapsed = result.latency_ms or (time.monotonic() - started) * 1000
        wer = calculate_wer(episode.human_transcript, result.transcript)
        cer = calculate_cer(episode.human_transcript, result.transcript)
        status = "success" if result.transcript else "empty_output"
        return AsrAttempt(**base, status=status, transcript=result.transcript,
                          latency_ms=round(elapsed, 1), wer=wer.error_rate,
                          cer=cer.error_rate, metadata=result.raw_metadata)
    except Exception as exc:
        return AsrAttempt(**base, status="error", transcript="", latency_ms=None,
                          wer=1.0, cer=1.0, error_type=type(exc).__name__,
                          error_message=str(exc)[:500])


async def run_asr_panel(manifest: RunManifest, registry: SpeechRegistry,
                        profile_ids: list[str], base_dir: Path) -> list[AsrAttempt]:
    attempts: list[AsrAttempt] = []
    for profile_id in profile_ids:
        provider = registry.get(profile_id)
        if provider is None:
            raise ValueError(f"Unknown speech profile: {profile_id}")
        for episode in manifest.episodes:
            attempts.append(await _attempt(provider, episode, base_dir))
    return attempts


def hypotheses_by_provider(attempts: list[AsrAttempt]) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for attempt in attempts:
        provider_output = output.setdefault(attempt.profile_id, {})
        if attempt.status in ("success", "empty_output"):
            provider_output[attempt.episode_id] = attempt.transcript
    return output


def summarize_attempts(attempts: list[AsrAttempt]) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[AsrAttempt]] = {}
    for attempt in attempts:
        grouped.setdefault(attempt.profile_id, []).append(attempt)
    summaries = {}
    for profile_id, rows in grouped.items():
        latencies = sorted(row.latency_ms for row in rows if row.latency_ms is not None)
        completed = [row for row in rows if row.status in ("success", "empty_output")]
        summaries[profile_id] = {
            "attempted": len(rows),
            "completed": len(completed),
            "coverage": len(completed) / len(rows),
            "mean_wer": statistics.fmean(row.wer for row in rows),
            "mean_cer": statistics.fmean(row.cer for row in rows),
            "median_latency_ms": statistics.median(latencies) if latencies else 0.0,
        }
    return summaries

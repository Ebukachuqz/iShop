"""Tests for provider-neutral ASR panel execution."""
from __future__ import annotations

import asyncio

from evals.runner.asr_panel import AsrAttempt, hypotheses_by_provider, run_asr_panel, summarize_attempts
from evals.runner.manifest import Episode, RunManifest
from ishop.speech.base import SpeechRegistry
from ishop.speech.fake import FakeSpeechProvider


def _manifest(audio_ref: str, processors=("fake",)) -> RunManifest:
    episode = Episode(
        episode_id="ep-1",
        split="dev",
        language_pair="pcm-eng",
        human_transcript="add one shirt",
        audio_ref=audio_ref,
        consent_allowed=True,
        allowed_processors=processors,
    )
    return RunManifest.create("run-1", "2026-09-12T00:00:00Z", "benchmark",
                              "ishop-unicode-v1", [episode], {}, "research")


def test_panel_transcribes_and_scores_same_manifest_audio(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF test")
    registry = SpeechRegistry()
    registry.register(FakeSpeechProvider(default_transcript="add one shirt"))
    attempts = asyncio.run(run_asr_panel(_manifest(str(audio)), registry,
                                         ["fake-speech-offline"], tmp_path))
    assert len(attempts) == 1
    assert attempts[0].status == "success"
    assert attempts[0].wer == 0
    assert hypotheses_by_provider(attempts) == {
        "fake-speech-offline": {"ep-1": "add one shirt"}
    }


def test_panel_blocks_disallowed_processor_before_transcription(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF test")
    registry = SpeechRegistry()
    registry.register(FakeSpeechProvider(default_transcript="must not execute"))
    attempts = asyncio.run(run_asr_panel(_manifest(str(audio), processors=("sahara",)),
                                         registry, ["fake-speech-offline"], tmp_path))
    assert attempts[0].status == "consent_blocked"
    assert attempts[0].transcript == ""


def test_panel_summary_keeps_failures_in_denominator():
    rows = [
        AsrAttempt("a", "fake", "fake-1", "m", "success", "ok", 100, 0.0, 0.0),
        AsrAttempt("b", "fake", "fake-1", "m", "error", "", None, 1.0, 1.0),
    ]
    summary = summarize_attempts(rows)["fake-1"]
    assert summary["attempted"] == 2
    assert summary["completed"] == 1
    assert summary["coverage"] == 0.5
    assert summary["mean_wer"] == 0.5
    assert hypotheses_by_provider(rows) == {"fake-1": {"a": "ok"}}

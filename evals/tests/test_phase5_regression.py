"""Regression tests for Findings R1 and R11 (Benchmark & Manifest Integrity).

Enforces:
- R1: Evaluation runner must not use answer keys to generate agent actions.
      Hypotheses must drive the production shopping controller.
      Missing ASR outputs must produce visible failures, not silent gold substitution.
- R11: Manifest validation must verify SHA-256 hashes, check audio references,
       detect duplicate IDs, and enforce default consent denial (consent_allowed=False).
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from evals.cli.run import create_mock_evidence
from evals.cli.validate import validate_manifest
from evals.runner.manifest import (
    Episode,
    RunManifest,
    load_manifest,
)
from evals.runner.runner import EvaluationRunner


def test_r01_gibberish_hypothesis_yields_zero_fcem_and_fails_strict_success():
    """R1 (P1): When hypotheses are replaced with gibberish, FCEM and strict_success must fail."""
    manifest_path = "evals/tests/fixtures/synthetic_manifest.json"
    manifest = load_manifest(manifest_path)
    evidence = create_mock_evidence()
    runner = EvaluationRunner(evidence)

    # Replace all hypotheses with completely unrelated gibberish
    gibberish_hyps = {
        ep.episode_id: "completely unrelated gibberish that asks for nothing"
        for ep in manifest.episodes
    }

    result = runner.run(manifest, simulated_hypotheses=gibberish_hyps)

    # The consent-allowed episodes that expected items (ep_01, ep_02, ep_05, ep_06)
    # MUST NOT report FCEM=True or strict_success=True when given gibberish!
    for ep_res in result.episodes:
        if ep_res.episode_id in ("ep_01_valid_pcm", "ep_02_yoruba_tones", "ep_05_prohibited_action", "ep_06_afriswitch_no_speaker"):
            assert ep_res.fcem is False, (
                f"Episode {ep_res.episode_id} falsely reported FCEM=True despite gibberish hypothesis!"
            )
            assert ep_res.strict_success is False, (
                f"Episode {ep_res.episode_id} falsely reported strict_success=True despite gibberish hypothesis!"
            )


def test_r01_missing_asr_hypothesis_remains_visible_failure():
    """R1 (P1): In controlled_asr/benchmark mode, missing hypothesis must produce visible failure, not gold substitution."""
    evidence = create_mock_evidence()
    runner = EvaluationRunner(evidence)

    ep = Episode(
        episode_id="ep_test_missing_asr",
        split="dev",
        language_pair="pcm-eng",
        human_transcript="I want one red shirt size small",
        expected_cart_lines=({"variant_id": "var_red_s", "quantity": 1},),
        consent_allowed=True,
    )
    manifest = RunManifest.create(
        run_id="run_missing_asr_test",
        created_at_utc="2026-09-12T00:00:00Z",
        mode="controlled_asr",  # Non-human-transcript benchmark mode
        normalization_version="ishop-unicode-v1",
        episodes=[ep],
        configuration=EvaluationRunner(create_mock_evidence()).configuration,
        data_kind="synthetic",
    )

    # Run without providing hypothesis for ep_test_missing_asr
    result = runner.run(manifest, simulated_hypotheses={})

    res = result.episodes[0]
    assert res.status == "missing_asr_output", (
        f"Expected status 'missing_asr_output' when ASR output is absent, got '{res.status}'"
    )
    assert res.fcem is False
    assert res.strict_success is False
    assert res.wer == 1.0


def test_r01_hypothesis_change_changes_downstream_action_without_gold_access():
    """R1 (P1): Proves that changing hypothesis changes downstream cart actions without accessing gold labels."""
    evidence = create_mock_evidence()
    runner = EvaluationRunner(evidence)

    # Target episode expects red shirt
    ep = Episode(
        episode_id="ep_test_change_hyp",
        split="dev",
        language_pair="eng",
        human_transcript="Buy one red shirt size small",
        expected_cart_lines=({"variant_id": "var_red_s", "quantity": 1},),
        consent_allowed=True,
    )
    manifest = RunManifest.create(
        run_id="run_hyp_change_test",
        created_at_utc="2026-09-12T00:00:00Z",
        mode="controlled_asr",
        normalization_version="ishop-unicode-v1",
        episodes=[ep],
        configuration=EvaluationRunner(create_mock_evidence()).configuration,
        data_kind="synthetic",
    )

    # 1. Provide hypothesis requesting red shirt
    result_red = runner.run(manifest, simulated_hypotheses={"ep_test_change_hyp": "Buy one red shirt size small"})
    res_red = result_red.episodes[0]
    assert res_red.fcem is True
    assert res_red.strict_success is True

    # 2. Provide hypothesis requesting blue shirt (different variant)
    result_blue = runner.run(manifest, simulated_hypotheses={"ep_test_change_hyp": "Buy one blue shirt size medium"})
    res_blue = result_blue.episodes[0]
    # Because blue was requested instead of red, cart does not match expected red shirt!
    assert res_blue.fcem is False
    assert res_blue.strict_success is False


def test_r11_missing_consent_defaults_to_false():
    """R11 (P2): Missing consent in raw episode dictionary must default to False (denied)."""
    raw_ep = {
        "episode_id": "ep_no_consent_field",
        "split": "dev",
        "language_pair": "eng",
        "human_transcript": "Add shirt",
    }
    manifest_dict = {
        "run_id": "test_consent_default",
        "created_at_utc": "2026-09-12T00:00:00Z",
        "mode": "benchmark",
        "normalization_version": "ishop-unicode-v1",
        "episodes": [raw_ep],
    }

    manifest = RunManifest.from_dict(manifest_dict)
    assert manifest.episodes[0].consent_allowed is False, (
        "Episode without explicit consent_allowed field should default to False!"
    )


def test_r11_manifest_validation_rejects_duplicate_episode_ids(tmp_path: Path):
    """R11 (P2): Manifest validator must reject duplicate episode IDs."""
    ep1 = Episode(episode_id="ep_dup", split="dev", language_pair="eng", human_transcript="A", consent_allowed=True)
    ep2 = Episode(episode_id="ep_dup", split="dev", language_pair="eng", human_transcript="B", consent_allowed=True)
    manifest = RunManifest.create(
        run_id="run_dup",
        created_at_utc="2026-09-12T00:00:00Z",
        mode="benchmark",
        normalization_version="ishop-unicode-v1",
        episodes=[ep1, ep2],
    )
    p = tmp_path / "dup_manifest.json"
    p.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    is_valid, errors = validate_manifest(p)
    assert not is_valid
    assert any("duplicate" in e.lower() for e in errors)


def test_r11_manifest_validation_rejects_tampered_hash(tmp_path: Path):
    """R11 (P2): Manifest validator must reject tampered content or hash mismatches."""
    ep = Episode(episode_id="ep_1", split="dev", language_pair="eng", human_transcript="A", consent_allowed=True)
    manifest = RunManifest.create(
        run_id="run_tamper",
        created_at_utc="2026-09-12T00:00:00Z",
        mode="benchmark",
        normalization_version="ishop-unicode-v1",
        episodes=[ep],
        configuration=EvaluationRunner(create_mock_evidence()).configuration,
        data_kind="synthetic",
    )
    m_dict = manifest.to_dict()
    # Tamper with the hash
    m_dict["manifest_hash"] = "tampered_fake_hash_00000000"
    p = tmp_path / "tampered_manifest.json"
    p.write_text(json.dumps(m_dict), encoding="utf-8")

    is_valid, errors = validate_manifest(p)
    assert not is_valid
    assert any("hash mismatch" in e.lower() for e in errors)


def test_r11_manifest_validation_rejects_missing_audio_ref(tmp_path: Path):
    """R11 (P2): Manifest validator must reject nonexistent audio_ref paths."""
    ep = Episode(
        episode_id="ep_audio",
        split="dev",
        language_pair="eng",
        human_transcript="A",
        audio_ref="nonexistent/audio/file.wav",
        consent_allowed=True,
    )
    manifest = RunManifest.create(
        run_id="run_missing_audio",
        created_at_utc="2026-09-12T00:00:00Z",
        mode="controlled_asr",
        normalization_version="ishop-unicode-v1",
        episodes=[ep],
        configuration=EvaluationRunner(create_mock_evidence()).configuration,
        data_kind="synthetic",
    )
    p = tmp_path / "missing_audio_manifest.json"
    p.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    is_valid, errors = validate_manifest(p)
    assert not is_valid
    assert any("audio_ref not found" in e.lower() or "audio" in e.lower() for e in errors)

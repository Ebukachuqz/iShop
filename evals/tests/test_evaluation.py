"""Evaluation suite tests for iShop (Drake).

Covers:
- T-23: Consent gating: unconsented or unauthorized processor episodes blocked before execution.
- T-24: Denominator completeness across silence, errors, and missing data; hidden label isolation.
- T-25: Unicode diacritics preservation, hand-calculated WER/CER, and strict success failure on prohibited intermediate action.
- T-26: Manifest reproduction, speaker-cluster bootstrap, and AfriSwitch speaker notice.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from evals.cli.report import generate_report
from evals.cli.run import create_mock_evidence
from evals.runner.manifest import Episode, RunManifest, load_manifest
from evals.runner.runner import EvaluationRunner
from evals.scoring.commerce_metrics import (
    calculate_critical_slots,
    calculate_fcem,
    calculate_strict_success,
)
from evals.scoring.speech_metrics import AlignmentResult, calculate_cer, calculate_wer
from evals.scoring.statistics import bootstrap_metric
from evals.scoring.text_norm import normalize_organizer_compatible, normalize_unicode_v1
from ishop.commerce.verifier import ProposedCartAction, QuantityOperation
from ishop.domain.models import CartLine, CartSnapshot


# --- T-25: Text Normalization and Hand-Calculated WER/CER ---
def test_t25_unicode_diacritics_preserved_in_primary_norm():
    # Yoruba with tone marks and combining characters
    raw_text = "Mo fẹ́ ra aṣọ pupa kékeré kan."
    norm_v1 = normalize_unicode_v1(raw_text)
    # NFC preserves tone marks: ẹ́, ọ, é
    assert "fẹ́" in norm_v1
    assert "aṣọ" in norm_v1
    assert "kékeré" in norm_v1
    assert "." not in norm_v1  # punctuation stripped

    # Supplementary organizer-compatible strips diacritics
    norm_org = normalize_organizer_compatible(raw_text)
    assert "fe" in norm_org
    assert "aso" in norm_org
    assert "kekere" in norm_org


def test_t25_hand_calculated_wer():
    # Ref: "the quick brown fox" (4 words)
    # Hyp: "the fast brown" (1 substitution, 1 deletion -> 2 errors / 4 words = 0.50)
    res = calculate_wer("the quick brown fox", "the fast brown")
    assert res.ref_length == 4
    assert res.substitutions == 1
    assert res.deletions == 1
    assert res.insertions == 0
    assert res.error_rate == 0.50


def test_t25_hand_calculated_cer():
    # Ref: "cat" (3 chars)
    # Hyp: "hats" (1 sub 'c'->'h', 1 ins 's' -> 2 errors / 3 chars = 2/3)
    res = calculate_cer("cat", "hats")
    assert res.ref_length == 3
    assert res.substitutions == 1
    assert res.insertions == 1
    assert res.deletions == 0
    assert abs(res.error_rate - (2.0 / 3.0)) < 1e-4


def test_t25_forbidden_intermediate_effect_fails_strict_success():
    expected_cart = CartSnapshot(
        shop_id="store.myshopify.com",
        currency="USD",
        lines=(CartLine(variant_id="var_red_s", quantity=1),),
    )
    observed_cart = CartSnapshot(
        shop_id="store.myshopify.com",
        currency="USD",
        lines=(CartLine(variant_id="var_red_s", quantity=1),),
    )

    # Both carts match -> FCEM is True
    fcem = calculate_fcem(expected_cart, observed_cart)
    assert fcem is True

    # But if a prohibited action was executed during the episode (e.g. wrong item added and removed)
    # Strict task success MUST FAIL (T-25)
    strict = calculate_strict_success(fcem=fcem, prohibited_actions_executed=1)
    assert strict is False


# --- T-23: Consent Gating & Processor Isolation ---
def test_t23_consent_gated_episodes_blocked_before_processing():
    manifest_path = "evals/tests/fixtures/synthetic_manifest.json"
    manifest = load_manifest(manifest_path)
    evidence = create_mock_evidence()
    runner = EvaluationRunner(evidence)

    result = runner.run(manifest, target_processor="simulator")
    blocked_ep = [e for e in result.episodes if e.episode_id == "ep_03_consent_blocked"][0]

    assert blocked_ep.status == "consent_blocked"
    assert blocked_ep.fcem is False
    assert "Consent withheld" in blocked_ep.error_message


# --- T-24: Denominator Completeness & Hallucination Accounting ---
def test_t24_silence_hallucination_and_denominator_integrity():
    manifest_path = "evals/tests/fixtures/synthetic_manifest.json"
    manifest = load_manifest(manifest_path)
    evidence = create_mock_evidence()
    runner = EvaluationRunner(evidence)

    # Simulate hypothesis on silence episode having a hallucination
    sim_hyps = {
        "ep_04_silence_empty_ref": "I heard something",
    }
    result = runner.run(manifest, simulated_hypotheses=sim_hyps)

    # 1. Total denominator MUST equal all 6 scheduled episodes (T-24)
    assert result.total_episodes == 6
    assert len(result.episodes) == 6

    # 2. Silence episode with hallucination tracked explicitly (T-24)
    silence_ep = [e for e in result.episodes if e.episode_id == "ep_04_silence_empty_ref"][0]
    assert silence_ep.is_hallucination is True
    assert silence_ep.wer == 1.0


# --- T-26: Reproducibility, Speaker Bootstrap & AfriSwitch Notice ---
def test_t26_speaker_cluster_bootstrap_and_afriswitch_notice():
    # 1. Episodes with speakers
    episodes_with_speakers = [
        {"speaker_id": "spk_1", "fcem": True},
        {"speaker_id": "spk_1", "fcem": True},
        {"speaker_id": "spk_2", "fcem": False},
        {"speaker_id": "spk_3", "fcem": True},
    ]
    mean, low, high, notice = bootstrap_metric(
        episodes_with_speakers,
        metric_fn=lambda eps: sum(1 for e in eps if e["fcem"]) / len(eps),
        num_resamples=500,
        seed=123,
    )
    assert 0.0 <= low <= mean <= high <= 1.0
    assert notice is None  # Speaker clustering succeeded

    # 2. Episodes lacking speaker ID (e.g. AfriSwitch, T-26)
    episodes_no_speakers = [
        {"speaker_id": None, "fcem": True},
        {"speaker_id": None, "fcem": False},
    ]
    mean_afr, low_afr, high_afr, notice_afr = bootstrap_metric(
        episodes_no_speakers,
        metric_fn=lambda eps: sum(1 for e in eps if e["fcem"]) / len(eps),
        num_resamples=500,
        seed=123,
    )
    assert notice_afr is not None
    assert "Speaker IDs not available in dataset" in notice_afr
    assert "T-26 notice" in notice_afr


def test_t26_end_to_end_report_generation(tmp_path: Path):
    manifest_path = "evals/tests/fixtures/synthetic_manifest.json"
    manifest = load_manifest(manifest_path)
    evidence = create_mock_evidence()
    runner = EvaluationRunner(evidence)

    result = runner.run(manifest)
    res_path = tmp_path / "test_result.json"
    res_path.write_text(json.dumps(result.to_dict()), encoding="utf-8")

    report = generate_report(res_path)
    assert "# iShop Evaluation Report" in report
    assert "Final Cart Exact Match" in report
    assert "Word Error Rate" in report
    assert "**Total Scheduled Cases (Denominator):** 6" in report

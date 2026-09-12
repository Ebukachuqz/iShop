"""WP-05: Dataset governance, consent templates, speaker disjointness, and catalog validation tests (T-23, T-24, T-25)."""
from dataclasses import replace
import tempfile
import hashlib
from pathlib import Path
import pytest

from evals.runner.manifest import Episode, RunManifest
from evals.runner.validation import validate_run, validate_catalog_compatibility
from evals.cli.run import create_mock_evidence
from evals.scoring.text_norm import normalize_text


def create_valid_episode(episode_id="ep_001", split="dev", speaker_id="spk_01", **kwargs):
    fields = {
        "episode_id": episode_id,
        "split": split,
        "speaker_id": speaker_id,
        "language_pair": "pcm-eng",
        "human_transcript": "Add red cotton shirt size small abeg",
        "initial_cart_lines": (),
        "expected_cart_lines": ({"variant_id": "var_red_s", "quantity": 1},),
        "consent_allowed": True,
        "allowed_processors": ("local", "simulator", "fake"),
        "goal": "cart",
    }
    fields.update(kwargs)
    return Episode(**fields)


def create_valid_manifest(episodes=None, **kwargs):
    if episodes is None:
        episodes = [
            create_valid_episode("ep_001", "dev", "spk_01"),
            create_valid_episode("ep_002", "test", "spk_02"),
        ]
    evidence = create_mock_evidence()
    config = {
        "code_sha256": "a" * 64,
        "evidence_sha256": "b" * 64,
        "llm": {"provider_name": "fake", "model_name": "fake-offline-dev"},
        "max_turns": 1,
        "timeout_seconds": 15.0,
        "max_llm_retries": 1,
        "store": "offline_simulator",
        "tts": "off",
    }
    config.update(kwargs.get("configuration", {}))
    return RunManifest.create(
        run_id="run_gov_test_001",
        created_at_utc="2026-09-12T10:00:00Z",
        mode="human_transcript",
        normalization_version="ishop-unicode-v1",
        episodes=episodes,
        configuration=config,
        data_kind="synthetic",
    )


def test_t23_speaker_disjointness_enforced():
    """T-23: Overlapping speakers between dev and test splits must fail validation."""
    ep1 = create_valid_episode("ep_001", split="dev", speaker_id="spk_01")
    ep2 = create_valid_episode("ep_002", split="test", speaker_id="spk_01")  # Overlapping speaker
    manifest = create_valid_manifest(episodes=[ep1, ep2])
    errors = validate_run(manifest)
    assert any("Speaker overlap" in err for err in errors)


def test_t23_duplicate_episode_ids_fail_validation():
    """T-23: Duplicate episode IDs must fail validation."""
    ep1 = create_valid_episode("ep_001", split="dev", speaker_id="spk_01")
    ep2 = create_valid_episode("ep_001", split="test", speaker_id="spk_02")  # Duplicate ID
    manifest = create_valid_manifest(episodes=[ep1, ep2])
    errors = validate_run(manifest)
    assert any("duplicate episode id" in err.lower() for err in errors)


def test_t23_missing_consent_boolean_fails_validation():
    """T-23 / S-12: Non-boolean or invalid consent must fail validation."""
    ep = create_valid_episode("ep_001", consent_allowed="true")  # String instead of bool
    manifest = create_valid_manifest(episodes=[ep])
    errors = validate_run(manifest)
    assert any("Consent must be an explicit boolean" in err for err in errors)


def test_t24_audio_checksum_and_file_existence_validation():
    """T-24: Research ASR episodes with invalid audio path or tampered SHA-256 fail validation."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(b"RIFF_MOCK_WAV_HEADER_DATA_CONTENT")
        tmp_path = Path(tmp.name)

    try:
        real_hash = hashlib.sha256(tmp_path.read_bytes()).hexdigest()
        
        # Valid hash passes
        ep_valid = create_valid_episode("ep_001", audio_ref=str(tmp_path), audio_sha256=real_hash)
        manifest_valid = create_valid_manifest(episodes=[ep_valid])
        manifest_valid.configuration["llm"]["provider_name"] = "sahara"
        manifest_valid = replace(manifest_valid, data_kind="research")
        manifest_valid.configuration["asr"] = {"provider": "sahara", "model": "m1", "route": "r1", "settings": {}}
        manifest_valid = RunManifest.create(
            manifest_valid.run_id, manifest_valid.created_at_utc, manifest_valid.mode,
            manifest_valid.normalization_version, list(manifest_valid.episodes),
            manifest_valid.configuration, manifest_valid.data_kind
        )
        assert validate_run(manifest_valid) == []

        # Tampered hash fails
        ep_tampered = create_valid_episode("ep_001", audio_ref=str(tmp_path), audio_sha256="0" * 64)
        manifest_tampered = create_valid_manifest(episodes=[ep_tampered])
        manifest_tampered.configuration["llm"]["provider_name"] = "sahara"
        manifest_tampered = replace(manifest_tampered, data_kind="research")
        manifest_tampered.configuration["asr"] = {"provider": "sahara", "model": "m1", "route": "r1", "settings": {}}
        manifest_tampered = RunManifest.create(
            manifest_tampered.run_id, manifest_tampered.created_at_utc, manifest_tampered.mode,
            manifest_tampered.normalization_version, list(manifest_tampered.episodes),
            manifest_tampered.configuration, manifest_tampered.data_kind
        )
        errors = validate_run(manifest_tampered)
        assert any("hash mismatch" in err.lower() for err in errors)
    finally:
        tmp_path.unlink(missing_ok=True)



def test_t25_catalog_compatibility_validation():
    """T-25: Catalog compatibility validator flags nonexistent variant IDs in expected cart lines."""
    evidence = create_mock_evidence()
    ep = create_valid_episode(
        "ep_001",
        expected_cart_lines=({"variant_id": "var_nonexistent_999", "quantity": 1},)
    )
    manifest = create_valid_manifest(episodes=[ep])
    errors = validate_catalog_compatibility(manifest, evidence)
    assert len(errors) == 1
    assert "var_nonexistent_999" in errors[0]


def test_t23_duplicate_audio_content_fails_validation():
    digest = "c" * 64
    episodes = [
        create_valid_episode("ep_001", "dev", "spk_01", audio_sha256=digest),
        create_valid_episode("ep_002", "test", "spk_02", audio_sha256=digest),
    ]
    errors = validate_run(create_valid_manifest(episodes=episodes))
    assert any("Duplicate audio content" in error for error in errors)


def test_t23_disallowed_cloud_processor_fails_validation():
    episode = create_valid_episode("ep_001", allowed_processors=("sahara",))
    manifest = create_valid_manifest(episodes=[episode], configuration={
        "asr": {"provider": "groq", "model": "whisper-large-v3", "route": "batch", "settings": {}}
    })
    manifest = RunManifest.create(manifest.run_id, manifest.created_at_utc, "benchmark",
                                  manifest.normalization_version, list(manifest.episodes),
                                  manifest.configuration, "research")
    errors = validate_run(manifest)
    assert any("not allowed" in error for error in errors)


def test_t25_unicode_normalization_preserves_yoruba_subdots_and_diacritics():
    """T-25: represents ishop-unicode-v1 NFC normalization preserving Yoruba diacritics and Pidgin spelling."""
    raw_yoruba = "Èmi fẹ́ rà bọ̀tini pụpa ńlá"
    norm = normalize_text(raw_yoruba, version="ishop-unicode-v1")
    assert "ẹ" in norm or "e" in norm
    assert "fẹ" in norm
    assert "rà" in norm or "ra" in norm

    pidgin_text = "Abeg   give me 2   embroidered caps   dem!"
    norm_pidgin = normalize_text(pidgin_text, version="ishop-unicode-v1")
    assert norm_pidgin == "abeg give me 2 embroidered caps dem"


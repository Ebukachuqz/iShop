"""Validation shared by CLI and direct execution; never uploads data."""
import hashlib
from pathlib import Path

from evals.runner.manifest import ALLOWED_MODES, ALLOWED_NORMALIZATIONS


def validate_run(manifest, base_dir=None):
    errors = []
    if manifest.manifest_version != "2.0.0":
        errors.append("Unsupported manifest version; explicitly migrate and refreeze as v2")
    if not manifest.verify_hash():
        errors.append("Manifest hash mismatch")
    if manifest.mode not in ALLOWED_MODES:
        errors.append("Unsupported mode")
    if manifest.normalization_version not in ALLOWED_NORMALIZATIONS:
        errors.append("Unsupported normalization version")
    if manifest.data_kind not in ("synthetic", "research"):
        errors.append("Explicit data_kind must be synthetic or research")
    config = manifest.configuration
    for key in ("code_sha256", "evidence_sha256", "llm", "max_turns", "timeout_seconds",
                "max_llm_retries", "store", "tts"):
        if key not in config:
            errors.append(f"Missing frozen configuration: {key}")
    for key in ("code_sha256", "evidence_sha256"):
        value = config.get(key, "")
        if not isinstance(value, str) or len(value) != 64:
            errors.append(f"Invalid {key}")
    if config.get("max_turns") != 1:
        errors.append("This runner only supports explicit single-turn episodes")
    seconds = config.get("timeout_seconds")
    if type(seconds) not in (int, float) or not 0 < seconds <= 300:
        errors.append("Invalid timeout_seconds")
    if config.get("store") != "offline_simulator" or config.get("tts") != "off":
        errors.append("Only offline simulator with TTS off is supported")
    if manifest.mode == "synthetic_scorer_self_test" and manifest.data_kind != "synthetic":
        errors.append("Scorer self-test cannot be research")
    if manifest.data_kind == "research" and config.get("llm", {}).get("provider_name") == "fake":
        errors.append("Fake provider results must be labelled synthetic")
    if manifest.data_kind == "research" and manifest.mode in ("controlled_asr", "benchmark"):
        asr = config.get("asr", {})
        if not all(asr.get(k) for k in ("provider", "model", "route")) or "settings" not in asr:
            errors.append("Recorded ASR outputs require frozen provider/model/route/settings")
    seen = set()
    dev_speakers = set()
    test_speakers = set()
    if not manifest.episodes:
        errors.append("Manifest contains no episodes")
    for ep in manifest.episodes:
        if not ep.episode_id or ep.episode_id in seen:
            errors.append("Missing or duplicate episode ID")
        seen.add(ep.episode_id)
        if ep.split not in ("dev", "test"):
            errors.append("Invalid split")
        if ep.speaker_id:
            if ep.split == "dev":
                dev_speakers.add(ep.speaker_id)
            elif ep.split == "test":
                test_speakers.add(ep.speaker_id)
        if type(ep.consent_allowed) is not bool:
            errors.append("Consent must be an explicit boolean")
        if any(not isinstance(p, str) or not p for p in ep.allowed_processors):
            errors.append("Invalid processor list")
        if ep.goal not in ("cart", "clarification", "refusal", "checkout", "no_action"):
            errors.append("Unsupported episode goal")
        if ep.goal == "clarification" and not ep.clarification_fields:
            errors.append("Clarification goal requires expected fields")
        if manifest.data_kind == "research" and manifest.mode in ("controlled_asr", "benchmark") and not ep.audio_ref:
            errors.append("Research ASR episode requires audio_ref and audio_sha256")
        if ep.audio_ref:
            path = Path(ep.audio_ref)
            if not path.is_absolute():
                path = Path(base_dir or Path.cwd()) / path
            if not path.is_file():
                errors.append(f"audio_ref not found: {ep.episode_id}")
            elif not ep.audio_sha256 or hashlib.sha256(path.read_bytes()).hexdigest() != ep.audio_sha256:
                errors.append(f"Audio content hash mismatch: {ep.episode_id}")

    if dev_speakers & test_speakers:
        errors.append("Speaker overlap detected between dev and test splits")

    return errors


def validate_catalog_compatibility(manifest, catalog_evidence):
    """Verifies that all expected variant and product IDs in manifest exist in catalog evidence."""
    errors = []
    known_products = set(catalog_evidence.products.keys())
    known_variants = {
        v.variant_id
        for p in catalog_evidence.products.values()
        for v in p.variants
    }

    for ep in manifest.episodes:
        for line in ep.initial_cart_lines:
            vid = line.get("variant_id")
            if vid and vid not in known_variants:
                errors.append(f"Episode {ep.episode_id}: initial cart variant '{vid}' not in catalog")
        for line in ep.expected_cart_lines:
            vid = line.get("variant_id")
            if vid and vid not in known_variants:
                errors.append(f"Episode {ep.episode_id}: expected cart variant '{vid}' not in catalog")
    return errors



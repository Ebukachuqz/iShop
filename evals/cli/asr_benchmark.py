"""Run the selected batch ASR panel against a frozen private manifest."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.cli.run import create_mock_evidence
from evals.runner.asr_panel import hypotheses_by_provider, run_asr_panel, summarize_attempts
from evals.runner.manifest import RunManifest, load_manifest
from evals.runner.runner import EvaluationRunner
from evals.runner.validation import validate_catalog_compatibility, validate_run
from ishop.speech.factory import create_default_speech_registry
DEFAULT_PROFILES = [
    "sahara-intron-asr",
    "groq-whisper-large-v3",
    "assemblyai-stt",
    "gemini-3.5-transcribe",
    "elevenlabs-scribe-v2",
]


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        import os
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _private_output(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    private_root = (root / "artifacts" / "private").resolve()
    if resolved != private_root and private_root not in resolved.parents:
        raise ValueError("ASR benchmark output must be under artifacts/private")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--asr-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    load_env(root / ".env")
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    errors = validate_run(manifest, manifest_path.parent)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    profiles = args.profile or DEFAULT_PROFILES
    frozen_profiles = {
        item.get("profile_id"): item for item in manifest.configuration.get("asr_panel", [])
    }
    missing_profiles = [profile for profile in profiles if profile not in frozen_profiles]
    if missing_profiles:
        print(f"ERROR: profiles are not frozen in manifest: {', '.join(missing_profiles)}", file=sys.stderr)
        return 1
    attempts = asyncio.run(run_asr_panel(manifest, create_default_speech_registry(),
                                         profiles, manifest_path.parent))
    output = {
        "run_id": manifest.run_id,
        "manifest_hash": manifest.manifest_hash,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "profiles": profiles,
        "attempts": [attempt.to_dict() for attempt in attempts],
        "summary": summarize_attempts(attempts),
    }
    if not args.asr_only:
        evidence = create_mock_evidence()
        catalog_errors = validate_catalog_compatibility(manifest, evidence)
        if catalog_errors:
            for error in catalog_errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        hypotheses = hypotheses_by_provider(attempts)
        downstream = {}
        for profile_id in profiles:
            configuration = dict(manifest.configuration)
            configuration["asr"] = frozen_profiles[profile_id]
            provider_manifest = RunManifest.create(
                run_id=f"{manifest.run_id}-{profile_id}",
                created_at_utc=manifest.created_at_utc,
                mode=manifest.mode,
                normalization_version=manifest.normalization_version,
                episodes=list(manifest.episodes),
                configuration=configuration,
                data_kind=manifest.data_kind,
            )
            result = EvaluationRunner(evidence).run(
                provider_manifest,
                simulated_hypotheses=hypotheses[profile_id],
                base_dir=manifest_path.parent,
            )
            downstream[profile_id] = result.to_dict()
        output["downstream"] = downstream
    output_path = _private_output(args.output, root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in attempts:
        print(f"{attempt.profile_id}/{attempt.episode_id}: {attempt.status}")
    return 0 if all(attempt.status == "success" for attempt in attempts) else 1


if __name__ == "__main__":
    raise SystemExit(main())

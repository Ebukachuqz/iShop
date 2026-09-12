"""Content identities for code, catalog and provider configuration; no credentials."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2].parent


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     ensure_ascii=False).encode()).hexdigest()


def code_hash():
    roots = [ROOT / "services/runtime/src", ROOT / "evals/runner",
             ROOT / "evals/scoring", ROOT / "evals/replay", ROOT / "evals/cli",
             ROOT / "packages/contracts/schema"]
    files = sorted(p for root in roots for p in root.rglob("*")
                   if p.suffix in (".py", ".json"))
    return content_hash({p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in files})


def configuration_for(evidence, provider, timeout_seconds=15.0):
    return {
        "code_sha256": code_hash(),
        "evidence_sha256": content_hash(asdict(evidence)),
        "llm": provider.profile.to_dict(),
        # Provider request parameters/prompts are included in code_sha256.
        "max_turns": 1,
        "timeout_seconds": timeout_seconds,
        "max_llm_retries": 1,
        "store": "offline_simulator",
        "tts": "off",
    }

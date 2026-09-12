"""Manifest data models and hashing for evaluation runs.

Enforces:
- T-23: Consent metadata (`consent_allowed`, `allowed_processors`).
- T-24: Every case defined explicitly in manifest.
- T-26: Frozen configuration and manifest SHA-256 hashing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Episode:
    """A single evaluation episode."""

    episode_id: str
    split: str  # "dev" | "test"
    language_pair: str  # "pcm-eng" | "yo-eng" | "eng"
    human_transcript: str
    speaker_id: str | None = None
    audio_ref: str | None = None
    initial_cart_lines: tuple[dict[str, Any], ...] = ()
    expected_cart_lines: tuple[dict[str, Any], ...] = ()
    critical_slots: dict[str, Any] = field(default_factory=dict)
    prohibited_actions: tuple[str, ...] = ()
    consent_allowed: bool = True  # Safety S-12 / T-23
    allowed_processors: tuple[str, ...] = ("local", "simulator")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["initial_cart_lines"] = list(self.initial_cart_lines)
        d["expected_cart_lines"] = list(self.expected_cart_lines)
        d["prohibited_actions"] = list(self.prohibited_actions)
        d["allowed_processors"] = list(self.allowed_processors)
        return d


@dataclass(frozen=True)
class RunManifest:
    """Versioned immutable run manifest."""

    run_id: str
    created_at_utc: str
    mode: str  # "human_transcript" | "controlled_asr" | "simulator"
    normalization_version: str
    episodes: tuple[Episode, ...]
    manifest_version: str = "1.0.0"
    manifest_hash: str = ""

    @classmethod
    def create(
        cls,
        run_id: str,
        created_at_utc: str,
        mode: str,
        normalization_version: str,
        episodes: list[Episode],
    ) -> RunManifest:
        # Calculate deterministic SHA-256 over sorted episodes
        serialized_episodes = json.dumps(
            [e.to_dict() for e in episodes],
            sort_keys=True,
        )
        content = f"{run_id}|{mode}|{normalization_version}|{serialized_episodes}"
        manifest_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return cls(
            run_id=run_id,
            created_at_utc=created_at_utc,
            mode=mode,
            normalization_version=normalization_version,
            episodes=tuple(episodes),
            manifest_hash=manifest_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "run_id": self.run_id,
            "created_at_utc": self.created_at_utc,
            "mode": self.mode,
            "normalization_version": self.normalization_version,
            "manifest_hash": self.manifest_hash,
            "episodes": [e.to_dict() for e in self.episodes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunManifest:
        raw_episodes = data.get("episodes", [])
        episodes = [
            Episode(
                episode_id=e["episode_id"],
                split=e["split"],
                language_pair=e["language_pair"],
                human_transcript=e["human_transcript"],
                speaker_id=e.get("speaker_id"),
                audio_ref=e.get("audio_ref"),
                initial_cart_lines=tuple(e.get("initial_cart_lines", [])),
                expected_cart_lines=tuple(e.get("expected_cart_lines", [])),
                critical_slots=e.get("critical_slots", {}),
                prohibited_actions=tuple(e.get("prohibited_actions", [])),
                consent_allowed=e.get("consent_allowed", True),
                allowed_processors=tuple(e.get("allowed_processors", ["local", "simulator"])),
            )
            for e in raw_episodes
        ]
        return cls(
            run_id=data["run_id"],
            created_at_utc=data["created_at_utc"],
            mode=data["mode"],
            normalization_version=data.get("normalization_version", "ishop-unicode-v1"),
            manifest_version=data.get("manifest_version", "1.0.0"),
            manifest_hash=data.get("manifest_hash", ""),
            episodes=tuple(episodes),
        )


def load_manifest(path: str | Path) -> RunManifest:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Manifest file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return RunManifest.from_dict(data)


def save_manifest(manifest: RunManifest, path: str | Path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ishop.transport.websocket import development_allowed_origins


@dataclass(frozen=True)
class RuntimeSettings:
    host: str
    port: int
    signing_secret: str
    allowed_origins: frozenset[str]
    sahara_api_key: str
    record_audio: bool
    control_secret: str | None = None
    groq_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    assemblyai_api_key: str | None = None
    gemini_api_key: str | None = None
    state_db_path: str = ".ishop/runtime-state.sqlite3"

    @classmethod
    def from_environment(cls) -> "RuntimeSettings":
        host = os.getenv("RUNTIME_HOST", "127.0.0.1").strip()
        if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
            raise ValueError("RUNTIME_HOST must be a local bind address")
        try:
            port = int(os.getenv("RUNTIME_PORT", "8000"))
        except ValueError as exc:
            raise ValueError("RUNTIME_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("RUNTIME_PORT must be between 1 and 65535")

        signing_secret = os.getenv("SESSION_SIGNING_SECRET", "")
        if len(signing_secret) < 32:
            raise ValueError("SESSION_SIGNING_SECRET must contain at least 32 characters")

        origins = development_allowed_origins(os.getenv("DEV_ALLOWED_ORIGINS", ""))
        if not origins:
            raise ValueError("DEV_ALLOWED_ORIGINS must list at least one storefront origin")

        sahara_api_key = os.getenv("SAHARA_API_KEY") or os.getenv("INTRON_API_KEY") or ""
        if not sahara_api_key:
            raise ValueError("SAHARA_API_KEY is required for the active realtime speech profile")

        record_audio = os.getenv("RECORD_AUDIO_ENABLED", "false").strip().lower() == "true"
        control_secret = os.getenv("RUNTIME_CONTROL_SECRET", "").strip() or None
        if control_secret is not None and len(control_secret) < 32:
            raise ValueError("RUNTIME_CONTROL_SECRET must contain at least 32 characters")
        return cls(
            host=host,
            port=port,
            signing_secret=signing_secret,
            allowed_origins=frozenset(origins),
            sahara_api_key=sahara_api_key,
            record_audio=record_audio,
            control_secret=control_secret,
            groq_api_key=os.getenv("GROQ_API_KEY", "").strip() or None,
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", "").strip() or None,
            assemblyai_api_key=os.getenv("ASSEMBLYAI_API_KEY", "").strip() or os.getenv("ASSEMBLY_AI_API_KEY", "").strip() or None,
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
            state_db_path=(
                os.getenv("RUNTIME_DATABASE_PATH", "").strip()
                or os.getenv("ISHOP_STATE_DB", "").strip()
                or ".ishop/runtime-state.sqlite3"
            ),
        )


def load_local_environment(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name and name not in os.environ:
            os.environ[name] = value

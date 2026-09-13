from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from ishop.config import RuntimeSettings, load_local_environment
from ishop.speech.sahara_stream import SaharaStreamingSession
from ishop.transport.websocket import create_voice_app


def create_runtime_app(settings: RuntimeSettings | None = None) -> FastAPI:
    active = settings or RuntimeSettings.from_environment()

    def make_session(**kwargs: object) -> SaharaStreamingSession:
        return SaharaStreamingSession(api_key=active.sahara_api_key, **kwargs)

    app = create_voice_app(
        signing_secret=active.signing_secret,
        allowed_origins=set(active.allowed_origins),
        session_factories={"sahara-stream-pcm": make_session},
        allowed_llm_profiles={"groq-gpt-oss-120b"},
        allowed_tts_profiles={"sahara-tts-female-pcm"},
        control_secret=active.control_secret,
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "ishop-runtime",
            "speech_profile": "sahara-stream-pcm",
            "audio_recording": active.record_audio,
        }

    return app


def main() -> None:
    load_local_environment(Path.cwd() / ".env")
    settings = RuntimeSettings.from_environment()
    import uvicorn

    uvicorn.run(
        create_runtime_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()

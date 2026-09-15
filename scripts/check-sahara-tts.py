"""Opt-in live Sahara TTS capability check; never stores generated audio."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ishop.config import load_local_environment
from ishop.tts.base import TtsProviderError
from ishop.tts.sahara import SaharaTtsProvider


async def main() -> None:
    load_local_environment(Path(".env"))
    if os.getenv("ISHOP_ALLOW_LIVE_SAHARA_TTS_SMOKE", "").casefold() != "true":
        raise SystemExit("Set ISHOP_ALLOW_LIVE_SAHARA_TTS_SMOKE=true for this opt-in provider call")
    provider = SaharaTtsProvider()
    session = await provider.synthesize("Hello, I am Drake and I am ready to help you shop.", generation=1)
    chunks = 0
    byte_count = 0
    async for chunk in session.chunks():
        chunks += 1
        byte_count += len(chunk.audio)
    if chunks < 1 or byte_count < 1:
        raise RuntimeError("Sahara returned no playable audio")
    print(f"PASS: Sahara female Pidgin TTS returned {chunks} audio chunk(s), {byte_count} bytes")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except TtsProviderError as exc:
        code = exc.code or "provider_error"
        raise SystemExit(f"FAIL: Sahara TTS {code} (retryable={str(exc.retryable).lower()})") from None

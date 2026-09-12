"""Run the same structured shopping intent through configured LLM providers."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "runtime" / "src"))

from ishop.llm.base import LlmIntentRequest  # noqa: E402
from ishop.llm.gemini import GeminiLlmProvider  # noqa: E402
from ishop.llm.groq import GroqLlmProvider  # noqa: E402
from speech_smoke import load_env  # noqa: E402


async def run_provider(provider, request: LlmIntentRequest) -> dict:
    profile = provider.profile
    row = {
        "provider": profile.provider_name,
        "profile": profile.profile_id,
        "model": profile.model_name,
    }
    if not profile.enabled:
        return {**row, "status": "blocked", "error": profile.disabled_reason}
    try:
        result = await provider.interpret_intent(request)
        return {
            **row,
            "status": "success",
            "intent": result.intent.to_dict(),
            "latency_ms": round(result.usage.latency_ms, 1),
            "usage": {
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        }
    except Exception as exc:
        return {
            **row,
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }


async def run() -> list[dict]:
    request = LlmIntentRequest(
        transcript="Add the medium black shirt, no, make am large, but just one.",
        catalog_context=(
            "product_id=shirt-1; title=Classic Shirt; variants: black/medium, black/large",
        ),
        budget_currency="NGN",
        turn_id="wp02-development",
        request_revision=1,
    )
    return await asyncio.gather(
        run_provider(GeminiLlmProvider(), request),
        run_provider(GroqLlmProvider(), request),
    )


def main() -> int:
    load_env(ROOT / ".env")
    output = ROOT / "artifacts/private/llm-smoke.json"
    rows = asyncio.run(run())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in rows:
        print(f"{row['provider']}: {row['status']} ({row.get('latency_ms', '-')} ms)")
    return 0 if any(row["status"] == "success" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

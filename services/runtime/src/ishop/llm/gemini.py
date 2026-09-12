"""Gemini LLM adapter for iShop shopping reasoning.

Uses official Google Gemini API (gemini-3.8-flash) with structured JSON outputs.
Adheres to:
- T-28: Complete structured output validated, buffered, unknowns retained.
- T-30: Missing API key reports unavailable with recorded reason; does not crash startup.
- D-08: Configurable independent LLM profile; free tier boundary observed.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request

from ishop.domain.intent import ShoppingIntent
from ishop.llm.base import (
    GroundedResponseContext,
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProfile,
    LlmProvider,
    LlmProviderError,
    LlmUsage,
)
from ishop.llm.prompts import (
    GROUNDED_RESPONSE_SYSTEM_PROMPT,
    SYSTEM_INTENT_PROMPT,
    format_intent_user_prompt,
)

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _http_post_json(
    url: str, payload_bytes: bytes, headers: dict[str, str], timeout_s: float
) -> str:
    """Execute blocking HTTP request in worker thread with timeout (R7)."""
    request_headers = {**headers, "User-Agent": "iShop-Drake/0.1"}
    req = urllib.request.Request(
        url=url,
        data=payload_bytes,
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8")


class GeminiLlmProvider(LlmProvider):
    """Gemini API provider adapter supporting strict buffered structured output."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str | None = None,
        profile_id: str = "gemini-flash",
    ):
        key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
        model = model_name or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)

        enabled = bool(key and not key.startswith("test-") and not key.startswith("mock-"))
        disabled_reason = None if enabled else "Missing or unconfigured GEMINI_API_KEY"

        self._api_key = key
        self._model_name = model
        self._profile = LlmProfile(
            profile_id=profile_id,
            provider_name="gemini",
            model_name=model,
            enabled=enabled,
            disabled_reason=disabled_reason,
            is_free_tier=True,
            supports_streaming=True,
            supports_structured_output=True,
            max_context_tokens=8192,
        )

    @property
    def profile(self) -> LlmProfile:
        return self._profile

    def check_readiness(self) -> tuple[bool, str | None]:
        if not self._profile.enabled or not self._api_key:
            return (False, self._profile.disabled_reason or "Missing GEMINI_API_KEY")
        return (True, None)

    async def interpret_intent(self, request: LlmIntentRequest) -> LlmInterpretationResult:
        if not self._profile.enabled or not self._api_key:
            raise LlmProviderError(
                message=f"Gemini provider is disabled: {self._profile.disabled_reason}",
                profile_id=self.profile.profile_id,
                retryable=False,
            )

        t_start = time.perf_counter()
        user_prompt = format_intent_user_prompt(
            transcript=request.transcript,
            catalog_context=request.catalog_context,
            cart_summary=request.cart_summary,
            current_product=request.current_product_id,
            currency=request.budget_currency,
        )

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"{SYSTEM_INTENT_PROMPT}\n\n{user_prompt}"}],
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.0,
            },
        }

        url = f"{GEMINI_API_BASE}/{self._model_name}:generateContent?key={self._api_key}"
        data_bytes = json.dumps(payload).encode("utf-8")

        try:
            resp_body = await asyncio.to_thread(
                _http_post_json,
                url,
                data_bytes,
                {"Content-Type": "application/json"},
                15.0,
            )
            res_json = json.loads(resp_body)
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="replace")
            raise LlmProviderError(
                message=f"Gemini API HTTP {e.code}: {err_msg}",
                profile_id=self.profile.profile_id,
                status_code=e.code,
                retryable=(e.code in (429, 500, 503)),
                cause=e,
            ) from e
        except Exception as e:
            raise LlmProviderError(
                message=f"Gemini network failure: {e}",
                profile_id=self.profile.profile_id,
                retryable=True,
                cause=e,
            ) from e

        try:
            candidates = res_json.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini response contained no candidates")
            parts = candidates[0].get("content", {}).get("parts", [])
            raw_text = parts[0].get("text", "")
            intent_data = json.loads(raw_text)
            intent = ShoppingIntent.from_dict(intent_data)
        except Exception as e:
            raise LlmProviderError(
                message=f"Failed to parse or validate structured intent from Gemini: {e}",
                profile_id=self.profile.profile_id,
                retryable=True,
                cause=e,
            ) from e

        latency = (time.perf_counter() - t_start) * 1000
        usage_meta = res_json.get("usageMetadata", {})
        usage = LlmUsage(
            prompt_tokens=usage_meta.get("promptTokenCount", 0),
            completion_tokens=usage_meta.get("candidatesTokenCount", 0),
            total_tokens=usage_meta.get("totalTokenCount", 0),
            latency_ms=latency,
        )

        return LlmInterpretationResult(
            intent=intent,
            raw_response_text=raw_text,
            usage=usage,
            profile_id=self.profile.profile_id,
        )

    async def generate_grounded_response(self, context: GroundedResponseContext) -> str:
        if not self._profile.enabled or not self._api_key:
            if context.execution_receipt_summary:
                return f"Done! {context.execution_receipt_summary}."
            if context.error_reason:
                return f"I couldn't do that: {context.error_reason}."
            return f"Options: {context.evidence_summary}."

        prompt_parts = [
            f"Operation: {context.operation}",
            f"Verified evidence: {context.evidence_summary}",
        ]
        if context.execution_receipt_summary:
            prompt_parts.append(f"Execution receipt: {context.execution_receipt_summary}")
        if context.error_reason:
            prompt_parts.append(f"Error reason: {context.error_reason}")

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"{GROUNDED_RESPONSE_SYSTEM_PROMPT}\n\n" + "\n".join(prompt_parts)}
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.2},
        }

        url = f"{GEMINI_API_BASE}/{self._model_name}:generateContent?key={self._api_key}"
        data_bytes = json.dumps(payload).encode("utf-8")

        try:
            resp_body = await asyncio.to_thread(
                _http_post_json,
                url,
                data_bytes,
                {"Content-Type": "application/json"},
                10.0,
            )
            res_json = json.loads(resp_body)
            return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            # Safe grounded fallback
            if context.execution_receipt_summary:
                return f"Done! {context.execution_receipt_summary}."
            return f"Options: {context.evidence_summary}."

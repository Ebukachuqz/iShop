"""Groq LLM adapter for iShop shopping reasoning.

Uses Groq API (openai/gpt-oss-120b) with non-streaming strict JSON schema.
Adheres to:
- T-28: Structured output validation, unknowns retained.
- T-30: Missing API key reports unavailable with recorded reason; does not crash startup.
- D-08: Configurable independent LLM profile; can be disabled with recorded evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request

from ishop.domain.intent import ShoppingIntent, TurnDecision
from ishop.llm.base import (
    GroundedResponseContext,
    LlmIntentRequest,
    LlmInterpretationResult,
    LlmProfile,
    LlmProvider,
    LlmProviderError,
    LlmUsage,
    LlmToolSelectionRequest,
    LlmToolSelectionResult,
)
from ishop.llm.prompts import (
    GROUNDED_RESPONSE_SYSTEM_PROMPT,
    SYSTEM_DECISION_PROMPT,
    SYSTEM_INTENT_PROMPT,
    format_intent_user_prompt,
)

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


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


class GroqLlmProvider(LlmProvider):
    """Groq API provider adapter supporting strict JSON object output."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str | None = None,
        profile_id: str = "groq-gpt-oss-120b",
    ):
        key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY")
        model = model_name or os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)

        enabled = bool(key and not key.startswith("test-") and not key.startswith("mock-"))
        disabled_reason = None if enabled else "Missing or unconfigured GROQ_API_KEY"

        self._api_key = key
        self._model_name = model
        self._profile = LlmProfile(
            profile_id=profile_id,
            provider_name="groq",
            model_name=model,
            enabled=enabled,
            disabled_reason=disabled_reason,
            is_free_tier=False,
            supports_streaming=False,
            supports_structured_output=True,
            max_context_tokens=8192,
        )

    @property
    def profile(self) -> LlmProfile:
        return self._profile

    def check_readiness(self) -> tuple[bool, str | None]:
        if not self._profile.enabled or not self._api_key:
            return (False, self._profile.disabled_reason or "Missing GROQ_API_KEY")
        return (True, None)

    async def interpret_intent(self, request: LlmIntentRequest) -> LlmInterpretationResult:
        if not self._profile.enabled or not self._api_key:
            raise LlmProviderError(
                message=f"Groq provider is disabled: {self._profile.disabled_reason}",
                profile_id=self.profile.profile_id,
                retryable=False,
            )

        t_start = time.perf_counter()
        user_prompt = format_intent_user_prompt(
            transcript=request.transcript,
            conversation_history=request.conversation_history,
            catalog_context=request.catalog_context,
            cart_summary=request.cart_summary,
            current_product=request.current_product_id,
            currency=request.budget_currency,
            validation_feedback=request.validation_feedback,
        )

        payload = {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": f"{SYSTEM_DECISION_PROMPT}\n\n{SYSTEM_INTENT_PROMPT}"},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }

        data_bytes = json.dumps(payload).encode("utf-8")

        try:
            resp_body = await asyncio.to_thread(
                _http_post_json,
                GROQ_API_URL,
                data_bytes,
                {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                15.0,
            )
            res_json = json.loads(resp_body)
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="replace")
            raise LlmProviderError(
                message=f"Groq API HTTP {e.code}: {err_msg}",
                profile_id=self.profile.profile_id,
                status_code=e.code,
                retryable=(e.code in (429, 500, 503)),
                cause=e,
            ) from e
        except Exception as e:
            raise LlmProviderError(
                message=f"Groq network failure: {e}",
                profile_id=self.profile.profile_id,
                retryable=True,
                cause=e,
            ) from e

        try:
            raw_text = res_json["choices"][0]["message"]["content"]
            intent_data = json.loads(raw_text)
            decision = None
            if "decision" in intent_data and isinstance(intent_data["decision"], dict):
                decision = TurnDecision.from_dict(intent_data["decision"])
            elif "mode" in intent_data:
                decision = TurnDecision.from_dict(intent_data)

            if decision is None:
                raise ValueError("Response did not contain a TurnDecision envelope")
            if "intent" in intent_data:
                intent = ShoppingIntent.from_dict(intent_data["intent"]) if isinstance(intent_data["intent"], dict) else None
            else:
                intent = decision.intent
        except Exception as e:
            raise LlmProviderError(
                message=f"Failed to parse or validate structured intent from Groq: {e}",
                profile_id=self.profile.profile_id,
                retryable=True,
                cause=e,
            ) from e

        latency = (time.perf_counter() - t_start) * 1000
        usage_meta = res_json.get("usage", {})
        usage = LlmUsage(
            prompt_tokens=usage_meta.get("prompt_tokens", 0),
            completion_tokens=usage_meta.get("completion_tokens", 0),
            total_tokens=usage_meta.get("total_tokens", 0),
            latency_ms=latency,
        )

        return LlmInterpretationResult(
            intent=intent,
            decision=decision,
            raw_response_text=raw_text,
            usage=usage,
            profile_id=self.profile.profile_id,
        )

    async def select_tool(self, request: LlmToolSelectionRequest) -> LlmToolSelectionResult:
        if not self._profile.enabled or not self._api_key:
            raise LlmProviderError("Groq tool selection is unavailable", self.profile.profile_id)
        tools_json = json.dumps(list(request.qualified_tools), separators=(",", ":"))
        prompt = (
            "Select exactly one qualified shopping tool for the validated intent. "
            "Return JSON with tool_name, arguments, and rationale. Never invent IDs or tools.\n"
            f"Validated intent: {json.dumps(request.intent.to_dict())}\n"
            f"Resolved context: {json.dumps(dict(request.resolved_context))}\n"
            f"Qualified tools: {tools_json}"
        )
        if request.validation_feedback:
            prompt += f"\nYour previous proposal was invalid. Correct these problems: {request.validation_feedback[:800]}"
        payload = {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": "You select from supplied shopping tools. Selection is a proposal and never execution authority."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
        try:
            body = await asyncio.to_thread(
                _http_post_json, GROQ_API_URL, json.dumps(payload).encode("utf-8"),
                {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}, 15.0,
            )
            raw = json.loads(body)["choices"][0]["message"]["content"]
            data = json.loads(raw)
            name = str(data["tool_name"])
            arguments = data.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            allowed = {str(tool["name"]) for tool in request.qualified_tools}
            if name not in allowed:
                raise ValueError("tool is not qualified")
            return LlmToolSelectionResult(name, arguments, str(data.get("rationale", "")), raw)
        except Exception as exc:
            raise LlmProviderError(f"Failed to select a qualified tool: {exc}", self.profile.profile_id, retryable=True, cause=exc) from exc

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
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": GROUNDED_RESPONSE_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(prompt_parts)},
            ],
            "temperature": 0.2,
        }

        data_bytes = json.dumps(payload).encode("utf-8")

        try:
            resp_body = await asyncio.to_thread(
                _http_post_json,
                GROQ_API_URL,
                data_bytes,
                {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                10.0,
            )
            res_json = json.loads(resp_body)
            return res_json["choices"][0]["message"]["content"].strip()
        except Exception:
            if context.execution_receipt_summary:
                return f"Done! {context.execution_receipt_summary}."
            return f"Options: {context.evidence_summary}."

from __future__ import annotations

import base64
import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ishop.config import RuntimeSettings, load_local_environment
from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.domain.models import CartSnapshot, Money
from ishop.domain.intent import IntentOperation, ShoppingIntent
from ishop.domain.session_store import SessionStore
from ishop.domain.journal import CommandJournal
from ishop.llm.groq import GroqLlmProvider
from ishop.llm.base import GroundedResponseContext
from ishop.orchestration.controller import ControllerTurnResult, ShoppingController
from ishop.speech import (
    AssemblyAiStreamingSession,
    ElevenLabsScribeRealtimeSession,
    GeminiLiveStreamingSession,
    SaharaStreamingSession,
)
from ishop.tts import create_default_tts_registry
from ishop.transport.websocket import create_voice_app


logger = logging.getLogger(__name__)


def create_runtime_app(settings: RuntimeSettings | None = None) -> FastAPI:
    active = settings or RuntimeSettings.from_environment()

    session_factories = {
        "sahara-stream-pcm": lambda **kwargs: SaharaStreamingSession(api_key=active.sahara_api_key, **kwargs),
        "elevenlabs-scribe-v2-realtime": lambda **kwargs: ElevenLabsScribeRealtimeSession(api_key=active.elevenlabs_api_key or "", **kwargs),
        "assemblyai-v3-realtime": lambda **kwargs: AssemblyAiStreamingSession(api_key=active.assemblyai_api_key or "", **kwargs),
        "gemini-3.5-transcribe-live": lambda **kwargs: GeminiLiveStreamingSession(api_key=active.gemini_api_key or "", **kwargs),
    }

    llm_provider = GroqLlmProvider(api_key=active.groq_api_key)
    tts_registry = create_default_tts_registry(
        sahara_api_key=active.sahara_api_key,
        elevenlabs_api_key=active.elevenlabs_api_key or "",
        gemini_api_key=active.gemini_api_key or "",
        groq_api_key=active.groq_api_key or "",
    )
    controllers: dict[str, ShoppingController] = {}
    session_locks: dict[str, asyncio.Lock] = {}
    session_store = SessionStore(active.state_db_path)
    command_journal = CommandJournal(active.state_db_path)
    command_journal.restart_reconcile()

    async def handle_shopping_turn(payload: dict[str, Any], grant: Any) -> dict[str, Any]:
        ready, readiness_reason = llm_provider.check_readiness()
        if not ready:
            return {
                "status": "error",
                "spoken_response": "The shopping reasoning service is not configured yet.",
                "reason": readiness_reason or "Selected LLM profile is unavailable",
            }
        evidence_raw = payload.get("evidence")
        if evidence_raw is not None:
            evidence = _evidence_from_browser(evidence_raw)
            if evidence.shop_id != grant.shop_id:
                return {
                    "status": "rejected",
                    "spoken_response": "I could not verify this store context.",
                    "reason": "Browser evidence tenant does not match the signed session grant",
                }
        else:
            evidence = EvidenceSnapshot(snapshot_id="", shop_id=grant.shop_id, currency="USD", observed_at_ms=0, products={})

        cart_raw = payload.get("current_cart")
        if cart_raw is not None:
            current_cart = CartSnapshot.from_dict(cart_raw)
            if current_cart.shop_id != grant.shop_id:
                return {
                    "status": "rejected",
                    "spoken_response": "I could not verify this store context.",
                    "reason": "Browser cart tenant does not match the signed session grant",
                }
        else:
            current_cart = CartSnapshot(shop_id=grant.shop_id, currency="USD", lines=())
        session_id = grant.anonymous_session_id
        async with session_locks.setdefault(session_id, asyncio.Lock()):
            controller = controllers.setdefault(
                session_id,
                ShoppingController(llm_provider=llm_provider),
            )
            saved_state = session_store.load(session_id)
            if saved_state is not None:
                controller.restore_session_state(saved_state)
            result = await controller.handle_turn(
                session_id=session_id,
                turn_id=payload["turn_id"],
                request_revision=payload["request_revision"],
                page_epoch=payload["page_epoch"],
                transcript=payload["transcript"],
                evidence=evidence,
                current_cart=current_cart,
                current_product_id=payload.get("current_product_id"),
                page_context=payload.get("page_context"),
                available_tools=set(payload.get("available_tools") or []),
                tool_observation=payload.get("tool_observation"),
                context_phase=payload.get("context_phase", "action"),
                cart_details=(cart_raw or {}).get("display_lines", []),
                displayed_search=payload.get("displayed_search"),
            )
            controller.record_assistant_outcome(session_id, result)
            session_store.save(session_id, controller.export_session_state(session_id))
        response: dict[str, Any] = {
            "turn_id": result.turn_id,
            "request_revision": result.request_revision,
            "page_epoch": result.page_epoch,
            "status": result.status,
            "spoken_response": result.spoken_response,
            "reason": result.reason,
            "clarification_options": list(result.clarification_options),
            "clarification_fields": list(result.clarification_fields),
            "evidence_query": result.evidence_query,
            "result_product_ids": list(result.result_product_ids),
            "selected_tool": result.selected_tool,
            "result_set_id": result.result_set_id,
            "tool_request": result.tool_request,
            "failure_code": result.failure_code,
        }
        if result.authorized_command is not None:
            command = result.authorized_command
            response["authorized_command"] = {
                "schema_version": command.schema_version,
                "command_id": command.command_id,
                "session_id": command.session_id,
                "shop_id": command.shop_id,
                "turn_id": command.turn_id,
                "request_revision": command.request_revision,
                "page_epoch": command.page_epoch,
                "expires_at_ms": command.expires_at_ms,
                "expected_cart_fingerprint": command.expected_cart_fingerprint,
                "operation": command.operation.value,
                "parameters": dict(command.parameters),
            }
        return response

    async def synthesize_shopping_text(text: str, revision: int, grant: Any = None):
        profile_id = getattr(grant, "tts_profile_id", "sahara-tts-female-pidgin") if grant else "sahara-tts-female-pidgin"
        provider = tts_registry.get(profile_id)
        if provider is None:
            raise ValueError(f"Unknown TTS profile: {profile_id}")
        tts_session = None
        try:
            async with asyncio.timeout(45):
                tts_session = await asyncio.wait_for(provider.synthesize(text, generation=revision), timeout=8)
                async for chunk in tts_session.chunks():
                    yield chunk.to_dict()
        finally:
            if tts_session is not None:
                await tts_session.cancel()

    async def handle_command_result(
        command: dict[str, Any],
        result: dict[str, Any],
        verified: bool,
        grant: Any,
    ) -> dict[str, Any]:
        fallback, facts, intent_operation = _verified_command_response(command, result, verified)
        if not verified:
            return {"status": "error", "spoken_response": fallback}
        intent = ShoppingIntent(
            intent_id=f"receipt_{command.get('command_id', 'unknown')}",
            operation=intent_operation,
            supporting_transcript_span="verified storefront command result",
        )
        try:
            response = await llm_provider.generate_grounded_response(GroundedResponseContext(
                operation=intent_operation.value,
                requested_intent=intent,
                evidence_summary=facts,
                execution_receipt_summary=facts,
                cart_summary=facts,
            ))
            response = " ".join(str(response).split()).strip()
            if not response or len(response) > 500:
                response = fallback
        except Exception:
            logger.warning("Grounded command response unavailable; using factual fallback")
            response = fallback

        session_id = grant.anonymous_session_id
        controller = controllers.get(session_id)
        if controller is not None:
            async with session_locks.setdefault(session_id, asyncio.Lock()):
                controller.record_assistant_outcome(session_id, ControllerTurnResult(
                    session_id=session_id,
                    turn_id=str(command.get("turn_id", "")),
                    request_revision=int(command.get("request_revision", 0)),
                    page_epoch=int(command.get("page_epoch", 0)),
                    status="completed",
                    spoken_response=response,
                    extracted_intent=intent,
                ))
                session_store.save(session_id, controller.export_session_state(session_id))
        return {"status": "completed", "spoken_response": response}

    app = create_voice_app(
        signing_secret=active.signing_secret,
        allowed_origins=set(active.allowed_origins),
        session_factories=session_factories,
        allowed_llm_profiles={"groq-gpt-oss-120b"},
        allowed_tts_profiles={
            "sahara-tts-female-pidgin",
            "sahara-tts-female-pcm",
            "elevenlabs-tts-female-stream",
            "elevenlabs-tts-female-ws",
            "gemini-tts-female-stream",
            "groq-orpheus-tts-female",
        },
        control_secret=active.control_secret,
        shopping_turn_handler=handle_shopping_turn,
        command_journal=command_journal,
        shopping_tts_handler=synthesize_shopping_text,
        shopping_command_result_handler=handle_command_result,
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


def _verified_command_response(
    command: dict[str, Any], result: dict[str, Any], verified: bool
) -> tuple[str, str, IntentOperation]:
    """Build the only facts an LLM may use for a cart-result response."""
    operation = str(command.get("operation", ""))
    operation_map = {
        "add_variant": IntentOperation.ADD_TO_CART,
        "set_line_quantity": IntentOperation.UPDATE_QUANTITY,
        "remove_line": IntentOperation.REMOVE_FROM_CART,
        "clear_cart": IntentOperation.CANCEL_CART,
    }
    intent_operation = operation_map.get(operation, IntentOperation.VIEW_CART)
    if not verified:
        return (
            "I could not verify that cart update. Please check your cart before trying again.",
            "The storefront cart result was not independently verified.",
            intent_operation,
        )

    before = result.get("before_cart") if isinstance(result.get("before_cart"), dict) else {}
    after = result.get("after_cart") if isinstance(result.get("after_cart"), dict) else {}
    params = command.get("parameters") if isinstance(command.get("parameters"), dict) else {}
    before_lines = before.get("lines") if isinstance(before.get("lines"), list) else []
    after_lines = after.get("lines") if isinstance(after.get("lines"), list) else []
    display_lines = []
    for cart in (after, before):
        values = cart.get("display_lines") if isinstance(cart, dict) else None
        if isinstance(values, list):
            display_lines.extend(item for item in values if isinstance(item, dict))

    variant_id = str(params.get("variant_id", ""))
    target_key = str(params.get("target_line_key", ""))

    def matches(line: dict[str, Any]) -> bool:
        return bool(
            (target_key and str(line.get("line_key", line.get("shopify_line_key", ""))) == target_key)
            or (variant_id and str(line.get("variant_id", "")) == variant_id)
        )

    display = next((line for line in display_lines if matches(line)), {})
    title = str(display.get("title") or "this item").strip()
    variant_title = str(display.get("variant_title") or "").strip()
    if variant_title and variant_title.casefold() not in {"default title", "title"}:
        title = f"{title}, {variant_title}"
    before_quantity = sum(int(line.get("quantity", 0)) for line in before_lines if isinstance(line, dict) and matches(line))
    after_quantity = sum(int(line.get("quantity", 0)) for line in after_lines if isinstance(line, dict) and matches(line))
    cart_total = sum(int(line.get("quantity", 0)) for line in after_lines if isinstance(line, dict))

    if operation == "add_variant":
        changed = max(0, after_quantity - before_quantity)
        fallback = f"Great, I added {changed} {title} to your cart. You now have {after_quantity} of it in your cart."
        facts = f"Verified add: added quantity {changed} of {title}; product quantity after update {after_quantity}; total cart quantity {cart_total}."
    elif operation in {"set_line_quantity", "remove_line"}:
        removed = max(0, before_quantity - after_quantity)
        if after_quantity == 0:
            fallback = f"Done, I removed {title} from your cart."
        else:
            fallback = f"Done, I removed {removed} {title}. You now have {after_quantity} in your cart."
        facts = f"Verified quantity update: removed quantity {removed} of {title}; product quantity after update {after_quantity}; total cart quantity {cart_total}."
    elif operation == "clear_cart":
        fallback = "Done, I cleared your cart. It is now empty."
        facts = "Verified whole-cart clear: total cart quantity after update 0."
    else:
        fallback = "The requested store action was completed and verified."
        facts = "The requested storefront action completed with an independently verified result."
    return fallback, facts, intent_operation


def _evidence_from_browser(data: dict[str, Any]) -> EvidenceSnapshot:
    products: dict[str, ProductEvidence] = {}
    for raw_product in data.get("products", []):
        product_id = str(raw_product["product_id"])
        variants = tuple(
            VariantEvidence(
                variant_id=str(raw_variant["variant_id"]),
                product_id=product_id,
                product_title=str(raw_variant.get("product_title", raw_product.get("title", ""))),
                variant_title=str(raw_variant.get("variant_title", "")),
                selected_options={str(k): str(v) for k, v in (raw_variant.get("selected_options") or {}).items()},
                price=Money.from_string(
                    raw_variant["price"]["amount"], raw_variant["price"]["currency"]
                ),
                available_for_sale=bool(raw_variant.get("available_for_sale", False)),
                quantity_available=raw_variant.get("quantity_available"),
                inventory_policy=str(raw_variant.get("inventory_policy", "DENY")),
            )
            for raw_variant in raw_product.get("variants", [])
        )
        products[product_id] = ProductEvidence(
            product_id=product_id,
            title=str(raw_product.get("title", "")),
            variants=variants,
            options=tuple(str(option) for option in raw_product.get("options", [])),
            url=str(raw_product["url"]) if raw_product.get("url") else None,
        )
    return EvidenceSnapshot(
        snapshot_id=str(data["snapshot_id"]),
        shop_id=str(data["shop_id"]),
        currency=str(data["currency"]),
        observed_at_ms=int(data["observed_at_ms"]),
        products=products,
        query=str(data["query"]) if data.get("query") else None,
    )


if __name__ == "__main__":
    main()

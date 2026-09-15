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
from ishop.domain.session_store import SessionStore
from ishop.domain.journal import CommandJournal
from ishop.llm.groq import GroqLlmProvider
from ishop.orchestration.controller import ShoppingController
from ishop.speech.sahara_stream import SaharaStreamingSession
from ishop.tts.sahara import SaharaTtsProvider
from ishop.transport.websocket import create_voice_app


logger = logging.getLogger(__name__)


def create_runtime_app(settings: RuntimeSettings | None = None) -> FastAPI:
    active = settings or RuntimeSettings.from_environment()

    def make_session(**kwargs: object) -> SaharaStreamingSession:
        return SaharaStreamingSession(api_key=active.sahara_api_key, **kwargs)

    llm_provider = GroqLlmProvider(api_key=active.groq_api_key)
    tts_provider = SaharaTtsProvider(api_key=active.sahara_api_key)
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

    async def synthesize_shopping_text(text: str, revision: int):
        tts_session = None
        try:
            async with asyncio.timeout(45):
                tts_session = await asyncio.wait_for(tts_provider.synthesize(text, generation=revision), timeout=8)
                async for chunk in tts_session.chunks():
                    yield {"audio_base64": base64.b64encode(chunk.audio).decode("ascii"),
                        "generation": chunk.generation, "sample_rate": chunk.sample_rate,
                        "channels": chunk.channels, "format": chunk.format}
        finally:
            if tts_session is not None:
                await tts_session.cancel()

    app = create_voice_app(
        signing_secret=active.signing_secret,
        allowed_origins=set(active.allowed_origins),
        session_factories={"sahara-stream-pcm": make_session},
        allowed_llm_profiles={"groq-gpt-oss-120b"},
        allowed_tts_profiles={"sahara-tts-female-pcm"},
        control_secret=active.control_secret,
        shopping_turn_handler=handle_shopping_turn,
        command_journal=command_journal,
        shopping_tts_handler=synthesize_shopping_text,
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

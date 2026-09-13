from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ishop.config import RuntimeSettings, load_local_environment
from ishop.commerce.catalog import EvidenceSnapshot, ProductEvidence, VariantEvidence
from ishop.domain.models import CartSnapshot, Money
from ishop.llm.groq import GroqLlmProvider
from ishop.orchestration.controller import ShoppingController
from ishop.speech.sahara_stream import SaharaStreamingSession
from ishop.tts.sahara import SaharaTtsProvider
from ishop.transport.websocket import create_voice_app


def create_runtime_app(settings: RuntimeSettings | None = None) -> FastAPI:
    active = settings or RuntimeSettings.from_environment()

    def make_session(**kwargs: object) -> SaharaStreamingSession:
        return SaharaStreamingSession(api_key=active.sahara_api_key, **kwargs)

    llm_provider = GroqLlmProvider(api_key=active.groq_api_key)
    tts_provider = SaharaTtsProvider(api_key=active.sahara_api_key)
    controllers: dict[str, ShoppingController] = {}

    async def handle_shopping_turn(payload: dict[str, Any], grant: Any) -> dict[str, Any]:
        ready, readiness_reason = llm_provider.check_readiness()
        if not ready:
            return {
                "status": "error",
                "spoken_response": "The shopping reasoning service is not configured yet.",
                "reason": readiness_reason or "Selected LLM profile is unavailable",
            }
        evidence = _evidence_from_browser(payload["evidence"])
        current_cart = CartSnapshot.from_dict(payload["current_cart"])
        if evidence.shop_id != grant.shop_id or current_cart.shop_id != grant.shop_id:
            return {
                "status": "rejected",
                "spoken_response": "I could not verify this store context.",
                "reason": "Browser evidence tenant does not match the signed session grant",
            }
        controller = controllers.setdefault(
            grant.anonymous_session_id,
            ShoppingController(llm_provider=llm_provider),
        )
        result = await controller.handle_turn(
            session_id=grant.anonymous_session_id,
            turn_id=payload["turn_id"],
            request_revision=payload["request_revision"],
            page_epoch=payload["page_epoch"],
            transcript=payload["transcript"],
            evidence=evidence,
            current_cart=current_cart,
            current_product_id=payload.get("current_product_id"),
        )
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
        }
        if result.spoken_response:
            try:
                tts_session = await tts_provider.synthesize(
                    result.spoken_response,
                    generation=payload["request_revision"],
                )
                audio_chunks = []
                async for chunk in tts_session.chunks():
                    audio_chunks.append({
                        "audio_base64": base64.b64encode(chunk.audio).decode("ascii"),
                        "generation": chunk.generation,
                        "sample_rate": chunk.sample_rate,
                        "channels": chunk.channels,
                        "format": chunk.format,
                    })
                response["tts_audio_chunks"] = audio_chunks
            except Exception:
                response["tts_audio_chunks"] = []
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

    app = create_voice_app(
        signing_secret=active.signing_secret,
        allowed_origins=set(active.allowed_origins),
        session_factories={"sahara-stream-pcm": make_session},
        allowed_llm_profiles={"groq-gpt-oss-120b"},
        allowed_tts_profiles={"sahara-tts-female-pcm"},
        control_secret=active.control_secret,
        shopping_turn_handler=handle_shopping_turn,
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

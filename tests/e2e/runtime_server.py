"""Loopback-only connected browser fixture using production transport/controller/assets."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ishop.app import _evidence_from_browser
from ishop.commerce.catalog import EvidenceSnapshot
from ishop.domain.journal import CommandJournal
from ishop.domain.models import CartSnapshot, SessionGrant
from ishop.domain.session_store import SessionStore
from ishop.llm.fake import FakeLlmProvider
from ishop.orchestration.controller import ShoppingController
from ishop.transport.websocket import create_voice_app

SECRET = "connected-e2e-signing-secret-32-bytes-minimum"
SHOP = "e2e-store.myshopify.com"
ORIGIN = "http://127.0.0.1:8765"
ASSETS = Path(__file__).parents[2] / "apps" / "shopify" / "extensions" / "drake" / "assets"
state_path = os.environ.get("ISHOP_E2E_STATE_DB", ":memory:")
session_store = SessionStore(state_path)
command_journal = CommandJournal(state_path)
command_journal.restart_reconcile()
controllers: dict[str, ShoppingController] = {}
session_locks: dict[str, asyncio.Lock] = {}
cart_items: list[dict[str, Any]] = []


async def shopping_turn(payload: dict[str, Any], grant: SessionGrant) -> dict[str, Any]:
    session_id = grant.anonymous_session_id
    evidence_raw = payload.get("evidence")
    evidence = (
        _evidence_from_browser(evidence_raw)
        if evidence_raw is not None
        else EvidenceSnapshot(snapshot_id="", shop_id=grant.shop_id, currency="USD", observed_at_ms=0, products={})
    )
    cart_raw = payload.get("current_cart")
    current_cart = (
        CartSnapshot.from_dict(cart_raw)
        if cart_raw is not None
        else CartSnapshot(shop_id=grant.shop_id, currency="USD", lines=())
    )
    async with session_locks.setdefault(session_id, asyncio.Lock()):
        controller = controllers.setdefault(session_id, ShoppingController(FakeLlmProvider()))
        saved_state = session_store.load(session_id)
        if saved_state is not None:
            controller.restore_session_state(saved_state)
        result = await controller.handle_turn(
            session_id,
            payload["turn_id"],
            payload["request_revision"],
            payload["page_epoch"],
            payload["transcript"],
            evidence,
            current_cart,
            current_product_id=payload.get("current_product_id"),
            page_context=payload.get("page_context"),
            available_tools=set(payload.get("available_tools") or []),
            tool_observation=payload.get("tool_observation"),
            context_phase=payload.get("context_phase", "action"),
            cart_details=(cart_raw or {}).get("display_lines", []),
            displayed_search=payload.get("displayed_search"),
        )
        session_store.save(session_id, controller.export_session_state(session_id))
    response: dict[str, Any] = {
        "turn_id": result.turn_id, "request_revision": result.request_revision, "page_epoch": result.page_epoch,
        "status": result.status, "spoken_response": result.spoken_response, "reason": result.reason,
        "clarification_options": list(result.clarification_options), "clarification_fields": list(result.clarification_fields),
        "evidence_query": result.evidence_query, "result_product_ids": list(result.result_product_ids),
        "selected_tool": result.selected_tool, "result_set_id": result.result_set_id,
        "tool_request": result.tool_request,
    }
    if result.authorized_command:
        command = result.authorized_command
        response["authorized_command"] = {
            "schema_version": command.schema_version, "command_id": command.command_id,
            "session_id": command.session_id, "shop_id": command.shop_id, "turn_id": command.turn_id,
            "request_revision": command.request_revision, "page_epoch": command.page_epoch,
            "expires_at_ms": command.expires_at_ms, "expected_cart_fingerprint": command.expected_cart_fingerprint,
            "operation": command.operation.value, "parameters": dict(command.parameters),
        }
    return response


app = create_voice_app(signing_secret=SECRET, allowed_origins={ORIGIN}, session_factory=lambda **_: None,
                       shopping_turn_handler=shopping_turn, command_journal=command_journal)


def storefront_page(*, product: bool = False, search_products: list[tuple[str, str]] | None = None) -> HTMLResponse:
    product_data = (' data-current-product-id="1" data-current-product-handle="complete-snowboard"' if product else '')
    search_grid = ""
    if search_products is not None:
        cards = "".join(f'<li class="grid__item"><a href="/products/{handle}">{title}</a><a aria-hidden="true" href="/products/{handle}">image</a></li>'
                        for handle, title in search_products)
        search_grid = f'<main><ul id="product-grid" data-native-search-results>{cards}</ul></main>'
    return HTMLResponse(f"""<!doctype html><html><body>
<a id="cart-icon-bubble" href="/cart"><span class="visually-hidden">Cart</span></a>
<button type="button" data-cart-drawer-trigger aria-controls="CartDrawer">Open cart drawer</button>
<cart-drawer id="CartDrawer" hidden aria-hidden="true"></cart-drawer>
<div id="ishop-drake-root" data-shop-domain="{SHOP}" data-bootstrap-url="/bootstrap"
 data-search-url="/search"
 data-bootstrap-script-url="/assets/drake-bootstrap.js" data-bridge-script-url="/assets/drake-bridge.js"
 data-catalog-script-url="/assets/drake-catalog.js" data-voice-ws-url="ws://127.0.0.1:8765/ws"{product_data}></div>
{search_grid}
<script src="/assets/drake-voice.js"></script><script src="/assets/drake-widget.js"></script>
<script>document.querySelector('[data-cart-drawer-trigger]').addEventListener('click',()=>document.querySelector('cart-drawer').removeAttribute('hidden'));</script>
<script src="/assets/drake-bootstrap.js"></script></body></html>""")


@app.get("/")
async def index() -> HTMLResponse:
    return storefront_page()


@app.get("/products/complete-snowboard")
async def product_page() -> HTMLResponse:
    return storefront_page(product=True)


@app.get("/products/multi-location-snowboard")
async def multi_location_page() -> HTMLResponse:
    return storefront_page(product=True)


@app.get("/products/multi-managed-snowboard")
async def multi_managed_page() -> HTMLResponse:
    return storefront_page(product=True)


@app.get("/search")
async def native_search_page(request: Request) -> HTMLResponse:
    # Deliberately differs from predictive-search order. This is the order the
    # shopper actually sees and therefore the order Drake must preserve.
    products = [
        ("multi-managed-snowboard", "Multi-managed Snowboard"),
        ("complete-snowboard", "Complete Snowboard"),
        ("multi-location-snowboard", "Multi-location Snowboard"),
    ]
    maximum = request.query_params.get("filter.v.price.lte")
    minimum = request.query_params.get("filter.v.price.gte")
    prices = {"multi-managed-snowboard": 629.95, "complete-snowboard": 699.95, "multi-location-snowboard": 729.95}
    if maximum is not None:
        products = [item for item in products if prices[item[0]] <= float(maximum)]
    if minimum is not None:
        products = [item for item in products if prices[item[0]] >= float(minimum)]
    if request.query_params.get("sort_by") == "price-descending":
        products.sort(key=lambda item: prices[item[0]], reverse=True)
    elif request.query_params.get("sort_by") == "price-ascending":
        products.sort(key=lambda item: prices[item[0]])
    return storefront_page(search_products=products)


@app.get("/account")
async def account_page() -> HTMLResponse:
    return HTMLResponse("<h1>Synthetic Shopify account handoff</h1>")


@app.get("/checkout")
async def checkout_page() -> HTMLResponse:
    return HTMLResponse("<h1>Synthetic Shopify checkout handoff — no payment capability</h1>")


@app.get("/cart")
async def cart_page() -> HTMLResponse:
    return storefront_page()


@app.get("/assets/{name}")
async def asset(name: str):
    path = (ASSETS / name).resolve()
    if path.parent != ASSETS.resolve() or not path.is_file():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(path)


@app.get("/bootstrap")
async def bootstrap() -> dict[str, Any]:
    now = int(time.time() * 1000)
    grant = SessionGrant.create_signed("grant_connected_e2e", SHOP, ORIGIN, "sess_connected_e2e_0001", "e2e-v1",
        "sahara-stream-pcm", "groq-gpt-oss-120b", "sahara-tts-female-pcm", now, 300_000, SECRET)
    return {"grant": grant.__dict__}


@app.get("/search/suggest.json")
async def search() -> dict[str, Any]:
    return {"resources": {"results": {"products": [
        {"id": 1, "title": "Complete Snowboard", "handle": "complete-snowboard"},
        {"id": 2, "title": "Multi-location Snowboard", "handle": "multi-location-snowboard"},
        {"id": 3, "title": "Multi-managed Snowboard", "handle": "multi-managed-snowboard"},
    ]}}}


@app.get("/products/complete-snowboard.js")
async def product() -> dict[str, Any]:
    return {"id": 1, "title": "Complete Snowboard", "handle": "complete-snowboard", "url": "/products/complete-snowboard",
            "options": ["Color"], "variants": [
                {"id": 101, "title": "Ice", "options": ["Ice"], "price": 69995, "available": True},
                {"id": 102, "title": "Dawn", "options": ["Dawn"], "price": 69995, "available": True},
            ]}


@app.get("/products/multi-location-snowboard.js")
async def multi_location_product() -> dict[str, Any]:
    return {"id": 2, "title": "Multi-location Snowboard", "handle": "multi-location-snowboard",
            "url": "/products/multi-location-snowboard", "options": ["Title"],
            "variants": [{"id": 201, "title": "Default Title", "options": ["Default Title"], "price": 72995, "available": True}]}


@app.get("/products/multi-managed-snowboard.js")
async def multi_managed_product() -> dict[str, Any]:
    return {"id": 3, "title": "Multi-managed Snowboard", "handle": "multi-managed-snowboard",
            "url": "/products/multi-managed-snowboard", "options": ["Title"],
            "variants": [{"id": 301, "title": "Default Title", "options": ["Default Title"], "price": 62995, "available": True}]}


@app.get("/collections.json")
async def collections() -> dict[str, Any]:
    return {"collections": [{"id": 10, "title": "Snowboards", "handle": "snowboards"}]}


@app.get("/collections/snowboards/products.json")
async def collection_products() -> dict[str, Any]:
    return {"products": [await product(), await multi_location_product(), await multi_managed_product()]}


@app.get("/cart.js")
async def get_cart() -> dict[str, Any]:
    products = [await product(), await multi_location_product(), await multi_managed_product()]
    items = []
    for line in cart_items:
        item = dict(line)
        for catalog_product in products:
            for variant in catalog_product["variants"]:
                if str(variant["id"]) == str(line["variant_id"]):
                    item.update(product_id=catalog_product["id"], product_title=catalog_product["title"],
                                variant_title=variant["title"], final_price=variant["price"],
                                final_line_price=variant["price"] * line["quantity"])
        items.append(item)
    return {"currency": "USD", "items": items}


@app.post("/cart/add.js")
async def add_cart(request: Request) -> dict[str, Any]:
    payload = await request.json()
    assert set(payload) == {"items"} and len(payload["items"]) == 1
    item = payload["items"][0]
    existing = next((line for line in cart_items if str(line["variant_id"]) == str(item["id"]) and line.get("properties", {}) == item.get("properties", {})), None)
    if existing:
        existing["quantity"] += item["quantity"]
    else:
        cart_items.append({"key": f"line-{len(cart_items)+1}", "variant_id": item["id"], "quantity": item["quantity"], "properties": item.get("properties", {})})
    return {"items": cart_items}


@app.post("/cart/change.js")
async def change_cart(request: Request) -> dict[str, Any]:
    payload = await request.json()
    for item in cart_items:
        if item["key"] == payload["id"]:
            item["quantity"] = payload["quantity"]
    cart_items[:] = [item for item in cart_items if item["quantity"] > 0]
    return {"items": cart_items}


@app.post("/cart/clear.js")
async def clear_cart() -> dict[str, Any]:
    cart_items.clear()
    return {"items": []}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")

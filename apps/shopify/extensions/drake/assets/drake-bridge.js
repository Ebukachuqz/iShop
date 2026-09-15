/**
 * Central Storefront Bridge Controller for iShop (Drake).
 *
 * Enforces Safety invariants:
 * - S-06: Success follows verified state; independent read-back required.
 * - T-16: Adapter parity across WebMCP, Standard Actions, and Ajax;
 *         never retry an uncertain write on a fallback adapter (S-10).
 */

import { AjaxCartAdapter } from './drake-ajax.js';
import { StandardActionsAdapter } from './drake-standard-actions.js';
import { WebMcpAdapter } from './drake-webmcp.js';
import { cartFingerprint, computeExpectedCart, isCartEquivalent, validateCommandSafety } from './drake-browser-contracts.js';

export { AjaxCartAdapter, StandardActionsAdapter, WebMcpAdapter };

export function syncCartIndicators(cart, documentRef = typeof document !== 'undefined' ? document : null) {
  if (!documentRef || !cart || !Array.isArray(cart.lines)) return;
  const itemCount = cart.lines.reduce((total, line) => total + Math.max(0, Number(line.quantity) || 0), 0);
  const icon = documentRef.querySelector?.('#cart-icon-bubble');
  let bubble = icon?.querySelector?.('.cart-count-bubble');
  if (itemCount <= 0) {
    bubble?.remove?.();
  } else if (icon) {
    if (!bubble && documentRef.createElement) {
      bubble = documentRef.createElement('div');
      bubble.className = 'cart-count-bubble';
      const visible = documentRef.createElement('span');
      visible.setAttribute('aria-hidden', 'true');
      const accessible = documentRef.createElement('span');
      accessible.className = 'visually-hidden';
      bubble.append(visible, accessible);
      icon.append(bubble);
    }
    const spans = bubble?.querySelectorAll?.('span') || [];
    if (spans[0]) spans[0].textContent = String(itemCount);
    if (spans[1]) spans[1].textContent = `${itemCount} ${itemCount === 1 ? 'item' : 'items'}`;
  }
  for (const node of documentRef.querySelectorAll?.('[data-cart-count], .cart-count') || []) {
    node.textContent = String(itemCount);
    node.hidden = itemCount <= 0;
  }
  const detail = { cart, item_count: itemCount, source: 'ishop-drake' };
  const EventConstructor = documentRef.defaultView?.CustomEvent
    || (typeof CustomEvent === 'function' ? CustomEvent : null);
  if (EventConstructor) {
    documentRef.dispatchEvent?.(new EventConstructor('cart:updated', { detail }));
    documentRef.dispatchEvent?.(new EventConstructor('cart:refresh', { detail }));
  }
}

export class StorefrontBridge {
  constructor({ ajaxAdapter = null, actionsAdapter = null, webMcpAdapter = null, navigate = null, origin = null, documentRef = null } = {}) {
    this.ajax = ajaxAdapter || new AjaxCartAdapter();
    this.actions = actionsAdapter || new StandardActionsAdapter();
    this.webMcp = webMcpAdapter || new WebMcpAdapter();
    this.navigate = navigate || ((url) => window.location.assign(url));
    this.origin = origin || (typeof window !== 'undefined' ? window.location.origin : 'https://storefront.invalid');
    this.document = documentRef || (typeof document !== 'undefined' ? document : null);
  }

  _openCartDrawer() {
    const documentRef = this.document;
    if (!documentRef?.querySelector) return false;
    const drawer = documentRef.querySelector('cart-drawer, [data-cart-drawer], #CartDrawer, .cart-drawer');
    if (!drawer) return false;
    try {
      if (typeof drawer.open === 'function') drawer.open();
      else {
        const trigger = [
          '[data-cart-drawer-trigger]', '[aria-controls="CartDrawer"]',
          '[aria-controls*="cart-drawer" i]', '#cart-icon-bubble',
        ].map((selector) => documentRef.querySelector(selector)).find(Boolean);
        if (!trigger || typeof trigger.click !== 'function') return false;
        trigger.click();
      }
      drawer.removeAttribute?.('hidden');
      drawer.setAttribute?.('aria-hidden', 'false');
      drawer.classList?.add?.('active');
      return true;
    } catch (_) {
      return false;
    }
  }

  detectPreferredTransport() {
    if (this.webMcp.isAvailable() && (
      typeof this.webMcp.isWriteAvailable !== 'function' || this.webMcp.isWriteAvailable()
    )) {
      return 'native_webmcp';
    }
    if (this.actions.isAvailable()) {
      return 'storefront_actions';
    }
    return 'ajax_cart';
  }

  async getAvailableTools() {
    const available = new Set(['search_catalog', 'get_product', 'show_variant', 'get_cart', 'update_cart', 'cancel_cart', 'proceed_to_checkout', 'manage_orders']);
    if (this.webMcp.isAvailable() && typeof this.webMcp.getDeclaredTools === 'function') {
      for (const descriptor of await this.webMcp.getDeclaredTools()) {
        if (descriptor && typeof descriptor.name === 'string') available.add(descriptor.name);
      }
    }
    return [...available].sort();
  }

  async readAuthoritativeCart() {
    let webMcpError = null;
    if (this.webMcp.isAvailable()) {
      try {
        const canRead = typeof this.webMcp.canReadCart !== 'function' || await this.webMcp.canReadCart();
        if (canRead) return await this.webMcp.readCart();
      } catch (error) {
        webMcpError = error;
      }
    }

    try {
      return await this.ajax.readCart();
    } catch (ajaxError) {
      if (!webMcpError) throw ajaxError;
      const error = new Error(`Store cart read failed via WebMCP and Ajax: ${webMcpError.message}; ${ajaxError.message}`);
      error.cause = { webMcp: webMcpError, ajax: ajaxError };
      throw error;
    }
  }

  async describeCart(cart) {
    if (cart.display_lines) return cart;
    try {
      const observed = await this.ajax.readCart();
      if (observed.shop_id === cart.shop_id && isCartEquivalent(observed, cart)) {
        return observed;
      }
    } catch (_) { /* Keep the authoritative cart; presentation is optional. */ }
    return cart;
  }

  async executeCommand(command) {
    // Runtime-minted commands carry the schema envelope. Keep the adapter
    // usable with legacy simulator fixtures while the WebSocket boundary
    // remains the strict command validator.
    const safetyErrors = command?.schema_version ? validateCommandSafety(command) : [];
    if (safetyErrors.length) {
      return { ok: false, outcome: 'rejected', transport_used: 'ajax_cart', errors: safetyErrors };
    }
    if (Number.isFinite(command.expires_at_ms) && Date.now() > command.expires_at_ms) {
      return { ok: false, outcome: 'rejected', transport_used: 'ajax_cart', errors: ['Command expired before dispatch (S-09)'] };
    }
    if (command.operation === 'navigate_storefront') {
      let beforeCart = null;
      try {
        beforeCart = await this.readAuthoritativeCart();
      } catch (_) {
        beforeCart = { shop_id: command.shop_id || '', currency: 'USD', lines: [] };
      }
      const destination = new URL(command.parameters?.url || '/', this.origin);
      const pathname = destination.pathname;
      const trustedPath = pathname === '/' || pathname === '/cart' || pathname.startsWith('/cart') || pathname.startsWith('/products/') || pathname.startsWith('/collections/') || pathname.startsWith('/search') || pathname.startsWith('/pages/');
      if (destination.origin !== this.origin || !trustedPath) {
        return { ok: false, outcome: 'rejected', transport_used: 'navigation', errors: ['Navigation destination is not a trusted Shopify product or collection path'], before_cart: beforeCart, after_cart: beforeCart };
      }
      if (pathname === '/cart' && command.parameters?.presentation === 'drawer_or_page' && this._openCartDrawer()) {
        return { ok: true, outcome: 'navigation_handoff', transport_used: 'cart_drawer', errors: [], before_cart: beforeCart, after_cart: beforeCart };
      }
      this.navigate(destination.toString());
      return { ok: true, outcome: 'navigation_handoff', transport_used: 'navigation', errors: [], before_cart: beforeCart, after_cart: beforeCart };
    }

    const beforeCart = await this.readAuthoritativeCart();
    if (command.expected_cart_fingerprint) {
      const actualFingerprint = await this._fingerprintCart(beforeCart);
      if (actualFingerprint !== command.expected_cart_fingerprint) {
        return { ok: false, outcome: 'rejected', transport_used: 'ajax_cart', errors: ['Cart changed before dispatch; refresh required (S-09)'], before_cart: beforeCart, after_cart: beforeCart };
      }
    }

    if (command.operation === 'handoff_to_checkout') {
      if (!beforeCart.lines.length) {
        return { ok: false, outcome: 'rejected', transport_used: 'navigation', errors: ['Checkout handoff requires a non-empty cart'], before_cart: beforeCart, after_cart: beforeCart };
      }
      const checkoutUrl = new URL(command.parameters?.checkout_url || '/checkout', this.origin);
      if (checkoutUrl.origin !== this.origin || checkoutUrl.pathname !== '/checkout') {
        return { ok: false, outcome: 'rejected', transport_used: 'navigation', errors: ['Checkout destination is not a trusted same-origin checkout path'], before_cart: beforeCart, after_cart: beforeCart };
      }
      this.navigate(checkoutUrl.toString());
      return { ok: true, outcome: 'human_handoff', transport_used: 'navigation', errors: [], before_cart: beforeCart, after_cart: beforeCart };
    }

    if (command.operation === 'manage_orders') {
      const destination = new URL(command.parameters?.url || '/account', this.origin);
      if (destination.origin !== this.origin || !destination.pathname.startsWith('/account')) {
        return { ok: false, outcome: 'rejected', transport_used: 'navigation', errors: ['Order history destination is not a trusted Shopify account path'], before_cart: beforeCart, after_cart: beforeCart };
      }
      this.navigate(destination.toString());
      return { ok: true, outcome: 'navigation_handoff', transport_used: 'navigation', errors: [], before_cart: beforeCart, after_cart: beforeCart };
    }

    const transport = command.operation === 'clear_cart' ? 'ajax_cart' : this.detectPreferredTransport();

    // 1. Dispatch mutation according to preferred transport
    let mutationResult = null;
    let writeUncertain = false;

    try {
      if (transport === 'native_webmcp') {
        mutationResult = await this._dispatchWebMcp(command);
      } else if (transport === 'storefront_actions') {
        mutationResult = await this._dispatchStandardActions(command);
      } else {
        mutationResult = await this._dispatchAjax(command);
      }
    } catch (err) {
      writeUncertain = true;
      // S-10 & T-16: A failed or uncertain write must NEVER be blindly retried on a fallback adapter!
      const liveAfter = await this.readAuthoritativeCart();
      return {
        ok: false,
        outcome: 'uncertain',
        transport_used: transport,
        errors: [`Dispatch failed with potential write uncertainty: ${err.message}`],
        before_cart: beforeCart,
        after_cart: liveAfter,
      };
    }

    if (!mutationResult.ok) {
      const liveAfter = await this.readAuthoritativeCart();
      return {
        ok: false,
        outcome: 'rejected',
        transport_used: transport,
        errors: mutationResult.errors || ['Storefront rejected mutation'],
        before_cart: beforeCart,
        after_cart: liveAfter,
      };
    }

    // 2. Authoritative post-mutation read-back (S-06, T-08, S-08, T-10)
    const afterCart = await this.readAuthoritativeCart();
    const expectedCart = computeExpectedCart(beforeCart, command);

    const isMatch = isCartEquivalent(afterCart, expectedCart);
    if (!isMatch) {
      return {
        ok: false,
        outcome: 'failed_with_observed_change',
        transport_used: transport,
        errors: ['Authoritative cart read-back does not match expected effect (unintended extra lines or corrupted cart)'],
        before_cart: beforeCart,
        after_cart: afterCart,
      };
    }

    syncCartIndicators(afterCart);

    return {
      ok: true,
      outcome: 'verified_success',
      transport_used: transport,
      errors: [],
      before_cart: beforeCart,
      after_cart: afterCart,
    };
  }

  async _fingerprintCart(cart) {
    return cartFingerprint(cart);
  }

  async _dispatchWebMcp(command) {
    const params = command.parameters;
    return await this.webMcp.updateCart(params);
  }

  async _dispatchStandardActions(command) {
    return await this.actions.updateCart(command);
  }

  async _dispatchAjax(command) {
    const params = command.parameters;
    const op = command.operation;

    if (op === 'add_variant') {
      return await this.ajax.addVariant({
        variantId: params.variant_id,
        quantity: params.quantity,
        properties: params.properties,
        sellingPlanId: params.selling_plan_id,
      });
    } else if (op === 'set_line_quantity') {
      return await this.ajax.changeLineQuantity({
        lineKey: params.target_line_key,
        quantity: params.quantity,
      });
    } else if (op === 'remove_line') {
      return await this.ajax.changeLineQuantity({
        lineKey: params.target_line_key,
        quantity: 0,
      });
    } else if (op === 'clear_cart') {
      if (params.explicit_whole_cart !== true) return { ok: false, errors: ['Whole-cart clearing requires explicit scope'] };
      return await this.ajax.clearCart();
    }

    return { ok: false, errors: [`Unsupported Ajax operation '${op}'`] };
  }
}

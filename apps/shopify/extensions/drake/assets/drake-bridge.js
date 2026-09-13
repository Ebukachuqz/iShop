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
import { computeExpectedCart, isCartEquivalent } from './drake-browser-contracts.js';

export { AjaxCartAdapter, StandardActionsAdapter, WebMcpAdapter };

export class StorefrontBridge {
  constructor({ ajaxAdapter = null, actionsAdapter = null, webMcpAdapter = null, navigate = null, origin = null } = {}) {
    this.ajax = ajaxAdapter || new AjaxCartAdapter();
    this.actions = actionsAdapter || new StandardActionsAdapter();
    this.webMcp = webMcpAdapter || new WebMcpAdapter();
    this.navigate = navigate || ((url) => window.location.assign(url));
    this.origin = origin || (typeof window !== 'undefined' ? window.location.origin : 'https://storefront.invalid');
  }

  detectPreferredTransport() {
    if (this.webMcp.isAvailable()) {
      return 'native_webmcp';
    }
    if (this.actions.isAvailable()) {
      return 'storefront_actions';
    }
    return 'ajax_cart';
  }

  async readAuthoritativeCart() {
    const transport = this.detectPreferredTransport();
    switch (transport) {
      case 'native_webmcp':
        return await this.webMcp.readCart();
      case 'storefront_actions':
      case 'ajax_cart':
      default:
        return await this.ajax.readCart();
    }
  }

  async executeCommand(command) {
    const beforeCart = await this.readAuthoritativeCart();

    if (command.operation === 'navigate_storefront') {
      const destination = new URL(command.parameters?.url || '/', this.origin);
      const trustedPath = destination.pathname.startsWith('/products/') || destination.pathname.startsWith('/collections/');
      if (destination.origin !== this.origin || !trustedPath) {
        return { ok: false, outcome: 'rejected', transport_used: 'navigation', errors: ['Navigation destination is not a trusted Shopify product or collection path'], before_cart: beforeCart, after_cart: beforeCart };
      }
      this.navigate(destination.toString());
      return { ok: true, outcome: 'navigation_handoff', transport_used: 'navigation', errors: [], before_cart: beforeCart, after_cart: beforeCart };
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

    const transport = this.detectPreferredTransport();

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

    return {
      ok: true,
      outcome: 'verified_success',
      transport_used: transport,
      errors: [],
      before_cart: beforeCart,
      after_cart: afterCart,
    };
  }

  async _dispatchWebMcp(command) {
    const params = command.parameters;
    return await this.webMcp.updateCart(params);
  }

  async _dispatchStandardActions(command) {
    const params = command.parameters;
    return await this.actions.updateCart(params);
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
    }

    return { ok: false, errors: [`Unsupported Ajax operation '${op}'`] };
  }
}

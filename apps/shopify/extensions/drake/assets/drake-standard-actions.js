/**
 * Shopify Storefront Actions adapter (Shopify.actions.updateCart).
 * Inspects userErrors and warnings rather than trusting status/events alone (T-08).
 */

export class StandardActionsAdapter {
  constructor(actionsApi = null, shopId = null) {
    this.actionsApi = actionsApi || (typeof window !== 'undefined' ? window.Shopify?.actions : null);
    this.shopId = shopId;
  }

  isAvailable() {
    return Boolean(this.actionsApi && typeof this.actionsApi.updateCart === 'function');
  }

  /** Translate an internal cart command into Shopify CartLineInput. */
  translateUpdateCartInput(command) {
    if (!command || typeof command !== 'object') throw new Error('Cart command is required');
    if (!command.operation) return command; // already a Shopify.actions payload
    const p = command.parameters || {};
    const variantId = p.variant_id || p.merchandise_id;
    const asGid = (id) => {
      const value = String(id || '');
      return value.startsWith('gid://') ? value : `gid://shopify/ProductVariant/${value}`;
    };
    if (command.operation === 'add_variant') {
      if (!variantId) throw new Error('add_variant requires variant_id');
      const line = { merchandiseId: asGid(variantId), quantity: Number(p.quantity || 1) };
      if (p.attributes && typeof p.attributes === 'object') {
        line.attributes = Object.entries(p.attributes).map(([key, value]) => ({ key, value: String(value) }));
      }
      return { lines: [line] };
    }
    if (command.operation === 'set_line_quantity' || command.operation === 'remove_line') {
      const lineId = p.target_line_key || p.shopify_line_key || p.canonical_line_key;
      if (!lineId) throw new Error(`${command.operation} requires a line key`);
      return { lines: [{ id: String(lineId), quantity: command.operation === 'remove_line' ? 0 : Number(p.quantity) }] };
    }
    throw new Error(`Unsupported cart operation: ${command.operation}`);
  }

  async updateCart(command) {
    if (!this.isAvailable()) {
      throw new Error('Shopify.actions.updateCart is not available in this environment');
    }

    const result = await this.actionsApi.updateCart(this.translateUpdateCartInput(command));

    // Inspect userErrors and warnings (T-08)
    const userErrors = result?.userErrors || [];
    const warnings = result?.warnings || [];

    if (userErrors.length > 0) {
      return {
        ok: false,
        errors: userErrors.map((e) => e.message || String(e)),
        warnings: warnings.map((w) => w.message || String(w)),
      };
    }

    return {
      ok: true,
      errors: [],
      warnings: warnings.map((w) => w.message || String(w)),
      cart: result?.cart || null,
    };
  }

  normalizeCart(cartData) {
    const lines = (cartData?.lines || []).map((line) => {
      const normProps = {};
      const attributes = line.attributes || line.properties || [];
      if (Array.isArray(attributes)) {
        for (const attr of attributes) {
          normProps[String(attr.key)] = String(attr.value);
        }
      } else if (typeof attributes === 'object') {
        for (const k of Object.keys(attributes).sort()) {
          normProps[String(k)] = String(attributes[k]);
        }
      }

      return {
        line_key: String(line.id),
        variant_id: String(line.merchandise?.id || line.variant_id),
        quantity: Number(line.quantity || 0),
        selling_plan_id: line.sellingPlanAllocation?.sellingPlan?.id
          ? String(line.sellingPlanAllocation.sellingPlan.id)
          : null,
        properties: normProps,
      };
    });

    return {
      shop_id: this.shopId || cartData?.shop_id || 'unknown.myshopify.com',
      currency: cartData?.cost?.totalAmount?.currencyCode || cartData?.currency || 'USD',
      lines: lines,
    };
  }
}

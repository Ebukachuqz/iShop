/**
 * Shopify Storefront Actions adapter (Shopify.actions.updateCart).
 * Inspects userErrors and warnings rather than trusting status/events alone (T-08).
 */

export class StandardActionsAdapter {
  constructor(actionsApi = null) {
    this.actionsApi = actionsApi || (typeof window !== 'undefined' ? window.Shopify?.actions : null);
  }

  isAvailable() {
    return Boolean(this.actionsApi && typeof this.actionsApi.updateCart === 'function');
  }

  async updateCart(updates) {
    if (!this.isAvailable()) {
      throw new Error('Shopify.actions.updateCart is not available in this environment');
    }

    const result = await this.actionsApi.updateCart(updates);

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
      shop_id: cartData?.shop_id || 'store.myshopify.com',
      currency: cartData?.cost?.totalAmount?.currencyCode || cartData?.currency || 'USD',
      lines: lines,
    };
  }
}

/**
 * Shopify Ajax Cart API adapter.
 * Uses exact line keys for modifying existing lines to handle lines sharing variant IDs.
 */

export class AjaxCartAdapter {
  constructor(baseUrl = '', customFetch = null) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.fetch = customFetch || (typeof window !== 'undefined' ? window.fetch.bind(window) : null);
    if (!this.fetch) {
      throw new Error('AjaxCartAdapter requires a fetch implementation');
    }
  }

  async readCart() {
    const res = await this.fetch(`${this.baseUrl}/cart.js`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) {
      throw new Error(`Failed to read cart from Ajax API: HTTP ${res.status}`);
    }
    const data = await res.json();
    return this._normalizeCart(data);
  }

  async addVariant({ variantId, quantity = 1, properties = {}, sellingPlanId = null }) {
    const payload = {
      items: [
        {
          id: variantId,
          quantity: quantity,
          properties: properties || {},
          ...(sellingPlanId ? { selling_plan: sellingPlanId } : {}),
        },
      ],
    };

    const res = await this.fetch(`${this.baseUrl}/cart/add.js`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify(payload),
    });

    const body = await res.json();
    if (!res.ok) {
      const errorMsg = body.description || body.message || `HTTP ${res.status}`;
      return { ok: false, errors: [errorMsg] };
    }

    return { ok: true, errors: [] };
  }

  async changeLineQuantity({ lineKey, quantity }) {
    const payload = {
      id: lineKey,
      quantity: quantity,
    };

    const res = await this.fetch(`${this.baseUrl}/cart/change.js`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify(payload),
    });

    const body = await res.json();
    if (!res.ok) {
      const errorMsg = body.description || body.message || `HTTP ${res.status}`;
      return { ok: false, errors: [errorMsg] };
    }

    return { ok: true, errors: [] };
  }

  _normalizeCart(ajaxResponse) {
    const lines = (ajaxResponse.items || []).map((item) => {
      const normProps = {};
      if (item.properties && typeof item.properties === 'object') {
        const sortedKeys = Object.keys(item.properties).sort();
        for (const k of sortedKeys) {
          normProps[String(k)] = String(item.properties[k]);
        }
      }
      return {
        line_key: String(item.key || item.id),
        variant_id: String(item.variant_id || item.id),
        quantity: Number(item.quantity || 0),
        selling_plan_id: item.selling_plan_allocation?.selling_plan?.id
          ? String(item.selling_plan_allocation.selling_plan.id)
          : null,
        properties: normProps,
      };
    });

    return {
      shop_id: ajaxResponse.token ? `${ajaxResponse.token}.myshopify.com` : 'unknown.myshopify.com',
      currency: ajaxResponse.currency || 'USD',
      lines: lines,
    };
  }
}

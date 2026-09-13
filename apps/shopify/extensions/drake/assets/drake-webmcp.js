/**
 * Chrome/Shopify WebMCP imperative tool bridge adapter.
 * Accesses tools via document.modelContext.getTools() and executeTool().
 */

export class WebMcpAdapter {
  constructor(modelContext = null, shopId = null) {
    this.modelContext = modelContext || (typeof document !== 'undefined' ? document.modelContext : null);
    this.shopId = shopId;
  }

  isAvailable() {
    return Boolean(
      this.modelContext &&
      typeof this.modelContext.getTools === 'function' &&
      typeof this.modelContext.executeTool === 'function'
    );
  }

  async getDeclaredTools() {
    if (!this.isAvailable()) {
      return [];
    }
    const tools = await this.modelContext.getTools();
    return Array.isArray(tools) ? tools : [];
  }

  async getCartToolDescriptor() {
    const tools = await this.getDeclaredTools();
    return tools.find((t) => t.name === 'get_cart') || null;
  }

  async updateCartToolDescriptor() {
    const tools = await this.getDeclaredTools();
    return tools.find((t) => t.name === 'update_cart') || null;
  }

  async readCart() {
    const descriptor = await this.getCartToolDescriptor();
    if (!descriptor) {
      throw new Error('WebMCP get_cart tool is not declared on document.modelContext');
    }

    const rawResult = await this.modelContext.executeTool(descriptor, JSON.stringify({}));
    const parsed = typeof rawResult === 'string' ? JSON.parse(rawResult) : rawResult;
    return this.normalizeCart(this.unwrapPayload(parsed));
  }

  async updateCart(params) {
    const descriptor = await this.updateCartToolDescriptor();
    if (!descriptor) {
      throw new Error('WebMCP update_cart tool is not declared on document.modelContext');
    }

    const rawResult = await this.modelContext.executeTool(descriptor, JSON.stringify(params));
    const parsed = typeof rawResult === 'string' ? JSON.parse(rawResult) : rawResult;
    const payload = this.unwrapPayload(parsed);

    const userErrors = payload?.userErrors || payload?.errors || [];
    if (userErrors.length > 0) {
      return {
        ok: false,
        errors: userErrors.map((e) => e.message || String(e)),
      };
    }

    return {
      ok: true,
      errors: [],
      cart: payload?.cart ? this.normalizeCart(this.unwrapPayload(payload.cart)) : null,
    };
  }

  normalizeCart(data) {
    if (!data || typeof data !== 'object') throw new Error('WebMCP cart response is not an object');
    const rawLines = data?.lines || data?.items;
    if (!Array.isArray(rawLines)) throw new Error('WebMCP cart response has no recognized lines');
    const lines = rawLines.map((l) => {
      const normProps = {};
      const attrs = l.attributes || l.properties || [];
      if (Array.isArray(attrs)) {
        for (const a of attrs) {
          normProps[String(a.key)] = String(a.value);
        }
      } else if (typeof attrs === 'object') {
        for (const k of Object.keys(attrs).sort()) {
          normProps[String(k)] = String(attrs[k]);
        }
      }

      return {
        line_key: String(l.id || l.key),
        variant_id: String(l.merchandiseId || l.variant_id || l.id),
        quantity: Number(l.quantity || 0),
        selling_plan_id: l.sellingPlanId || l.selling_plan_id || null,
        properties: normProps,
      };
    });

    return {
      shop_id: this.shopId || data?.shop_id || 'unknown.myshopify.com',
      currency: data?.currency || 'USD',
      lines: lines,
    };
  }

  unwrapPayload(value) {
    let current = value;
    for (let index = 0; index < 4; index += 1) {
      if (current && typeof current === 'object' && current.structuredContent && typeof current.structuredContent === 'object') {
        current = current.structuredContent;
        continue;
      }
      if (current && typeof current === 'object' && current.data && typeof current.data === 'object' && !current.lines && !current.items && !current.cart) {
        current = current.data;
        continue;
      }
      break;
    }
    return current;
  }
}

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

  isWriteAvailable() {
    // The repository does not yet contain a captured update_cart input schema
    // or a validated translator from internal commands to that schema.
    return false;
  }

  async getDeclaredTools() {
    if (!this.isAvailable()) {
      return [];
    }
    const tools = await this.modelContext.getTools();
    return Array.isArray(tools) ? tools : [];
  }

  async invokeTool(name, argumentsObject = {}) {
    const tools = await this.getDeclaredTools();
    const descriptor = tools.find((tool) => tool.name === name);
    if (!descriptor) throw new Error(`WebMCP ${name} tool is not declared`);
    const raw = await this.modelContext.executeTool(descriptor, JSON.stringify(argumentsObject));
    return typeof raw === 'string' ? JSON.parse(raw) : raw;
  }

  searchCatalog(args) { return this.invokeTool('search_catalog', args); }
  browseStore(args = {}) { return this.invokeTool('browse_store', args); }
  getProduct(args) { return this.invokeTool('get_product', args); }
  showVariant(args) { return this.invokeTool('show_variant', args); }
  getCart() { return this.invokeTool('get_cart', {}); }
  cancelCart(args = {}) { return this.invokeTool('cancel_cart', args); }
  proceedToCheckout(args = {}) { return this.invokeTool('proceed_to_checkout', args); }
  manageOrders(args = {}) { return this.invokeTool('manage_orders', args); }
  searchShopPoliciesAndFaqs(args) { return this.invokeTool('search_shop_policies_and_faqs', args); }

  async getCartToolDescriptor() {
    const tools = await this.getDeclaredTools();
    return tools.find((t) => t.name === 'get_cart') || null;
  }

  async canReadCart() {
    return Boolean(await this.getCartToolDescriptor());
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
    return this.normalizeCart(this.extractCartPayload(parsed));
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

  extractCartPayload(value, depth = 0) {
    if (depth > 6) throw new Error('WebMCP cart response nesting is too deep');

    if (typeof value === 'string') {
      try {
        return this.extractCartPayload(JSON.parse(value), depth + 1);
      } catch (error) {
        if (error instanceof SyntaxError) throw new Error('WebMCP cart response contains invalid JSON');
        throw error;
      }
    }

    if (!value || typeof value !== 'object') {
      throw new Error('WebMCP cart response is not an object');
    }
    if (Array.isArray(value.lines) || Array.isArray(value.items)) return value;

    for (const key of ['cart', 'structuredContent', 'data', 'result']) {
      if (value[key] !== undefined && value[key] !== null) {
        try {
          return this.extractCartPayload(value[key], depth + 1);
        } catch (error) {
          if (!String(error?.message || '').startsWith('WebMCP cart response')) throw error;
        }
      }
    }

    if (Array.isArray(value.content)) {
      for (const block of value.content) {
        const candidate = block?.type === 'text' ? block.text : block;
        try {
          return this.extractCartPayload(candidate, depth + 1);
        } catch (error) {
          if (!String(error?.message || '').startsWith('WebMCP cart response')) throw error;
        }
      }
    }

    throw new Error('WebMCP cart response has no recognized cart payload');
  }
}

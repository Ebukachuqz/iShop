function productList(payload) {
  const source = payload?.products || payload?.items || payload?.results?.products || payload?.resources?.results?.products || payload?.data?.products;
  if (Array.isArray(source)) return source;
  if (Array.isArray(source?.nodes)) return source.nodes;
  if (Array.isArray(source?.edges)) return source.edges.map((edge) => edge.node);
  return [];
}

function variantList(product) {
  const source = product?.variants;
  if (Array.isArray(source)) return source;
  if (Array.isArray(source?.nodes)) return source.nodes;
  if (Array.isArray(source?.edges)) return source.edges.map((edge) => edge.node);
  return [];
}

function decimalPrice(value) {
  if (value && typeof value === 'object') return String(value.amount ?? value.value ?? '0');
  return String(value ?? '0');
}

function normalizeOptions(variant, productOptions) {
  if (Array.isArray(variant.selectedOptions)) {
    return Object.fromEntries(variant.selectedOptions.map((option) => [String(option.name), String(option.value)]));
  }
  if (Array.isArray(variant.options)) {
    return Object.fromEntries(variant.options.map((value, index) => [String(productOptions[index] || `Option ${index + 1}`), String(value)]));
  }
  return {};
}

export function normalizeCatalogProducts(payload, defaults = {}) {
  return productList(payload).map((product) => {
    const optionNames = (product.options || []).map((option) => String(option.name || option));
    const productId = String(product.id);
    return {
      product_id: productId,
      title: String(product.title || ''),
      handle: product.handle ? String(product.handle) : null,
      url: product.url ? String(product.url) : null,
      image_url: String(product.featuredImage?.url || product.featured_image?.url || product.featured_image || ''),
      options: optionNames,
      variants: variantList(product).map((variant) => ({
        variant_id: String(variant.id),
        product_id: productId,
        product_title: String(product.title || ''),
        variant_title: String(variant.title || ''),
        selected_options: normalizeOptions(variant, optionNames),
        price: {
          amount: decimalPrice(variant.price || variant.priceV2),
          currency: String(variant.price?.currencyCode || variant.priceV2?.currencyCode || defaults.currency || 'USD'),
        },
        available_for_sale: Boolean(variant.availableForSale ?? variant.available),
        quantity_available: Number.isInteger(variant.quantityAvailable) ? variant.quantityAvailable : null,
        inventory_policy: String(variant.inventoryPolicy || 'DENY').toUpperCase(),
      })),
    };
  });
}

export class WebMcpCatalogAdapter {
  constructor(modelContext = null) {
    this.modelContext = modelContext || (typeof document !== 'undefined' ? document.modelContext : null);
  }
  isAvailable() {
    return Boolean(this.modelContext?.getTools && this.modelContext?.executeTool);
  }
  async search(query, limit = 5) {
    if (!this.isAvailable()) throw new Error('webmcp_catalog_unavailable');
    const tools = await this.modelContext.getTools();
    const descriptor = tools.find((tool) => tool.name === 'search_catalog');
    if (!descriptor) throw new Error('webmcp_search_catalog_unavailable');
    const raw = await this.modelContext.executeTool(descriptor, JSON.stringify({ query, limit }));
    const payload = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return normalizeCatalogProducts(payload);
  }
}

export class AjaxCatalogAdapter {
  constructor({ baseUrl = '', fetchImpl = null, currency = null } = {}) {
    this.baseUrl = String(baseUrl).replace(/\/+$/, '');
    this.fetch = fetchImpl || window.fetch.bind(window);
    this.currency = currency || window.Shopify?.currency?.active || 'USD';
  }
  async search(query, limit = 5) {
    const params = new URLSearchParams({
      q: query,
      'resources[type]': 'product',
      'resources[limit]': String(limit),
      'resources[options][unavailable_products]': 'last',
    });
    const response = await this.fetch(`${this.baseUrl}/search/suggest.json?${params}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`catalog_search_failed_${response.status}`);
    const suggestions = await response.json();
    const products = productList(suggestions).slice(0, limit);
    const detailed = await Promise.all(products.map(async (product) => {
      if (!product.handle) return product;
      const detailResponse = await this.fetch(`${this.baseUrl}/products/${encodeURIComponent(product.handle)}.js`, { headers: { Accept: 'application/json' } });
      if (!detailResponse.ok) throw new Error(`product_detail_failed_${detailResponse.status}`);
      return detailResponse.json();
    }));
    const normalized = normalizeCatalogProducts({ products: detailed }, { currency: this.currency });
    for (const product of normalized) {
      for (const variant of product.variants) {
        const raw = detailed.find((candidate) => String(candidate.id) === product.product_id);
        const rawVariant = raw?.variants?.find((candidate) => String(candidate.id) === variant.variant_id);
        if (rawVariant && Number.isInteger(rawVariant.price)) variant.price.amount = (rawVariant.price / 100).toFixed(2);
      }
    }
    return normalized;
  }

  async getByHandle(handle) {
    if (!handle) return null;
    const response = await this.fetch(`${this.baseUrl}/products/${encodeURIComponent(handle)}.js`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`product_detail_failed_${response.status}`);
    const raw = await response.json();
    return normalizeCatalogProducts({ products: [raw] }, { currency: this.currency })[0] || null;
  }
}

export class StorefrontCatalog {
  constructor({ webMcp = null, ajax = null } = {}) {
    this.webMcp = webMcp || new WebMcpCatalogAdapter();
    this.ajax = ajax || new AjaxCatalogAdapter();
  }
  async search(query, limit = 5) {
    if (this.webMcp.isAvailable()) {
      try {
        const products = await this.webMcp.search(query, limit);
        if (products.length) return { source: 'native_webmcp', products };
      } catch (_) {
      }
    }
    return { source: 'ajax_product_json', products: await this.ajax.search(query, limit) };
  }

  async getByHandle(handle) {
    if (this.webMcp.isAvailable() && typeof this.webMcp.getByHandle === 'function') {
      try {
        const product = await this.webMcp.getByHandle(handle);
        if (product) return product;
      } catch (_) {
      }
    }
    return this.ajax.getByHandle(handle);
  }
}

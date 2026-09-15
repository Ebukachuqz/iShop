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

function safeStoreUrl(value) {
  if (!value) return null;
  try {
    const base = typeof window !== 'undefined' ? window.location.origin : 'https://storefront.invalid';
    const parsed = new URL(String(value), base);
    return parsed.origin === base ? `${parsed.pathname}${parsed.search}` : null;
  } catch (_) { return null; }
}

function normalizeCollections(payload) {
  const source = payload?.collections || payload?.items || payload?.results?.collections || payload?.data?.collections || [];
  const values = Array.isArray(source) ? source : (Array.isArray(source?.nodes) ? source.nodes : []);
  return values.slice(0, 20).map((item) => ({
    id: String(item.id || item.handle || ''), title: String(item.title || item.name || '').slice(0, 200),
    handle: item.handle ? String(item.handle) : null, url: safeStoreUrl(item.url || (item.handle ? `/collections/${item.handle}` : null)),
  })).filter((item) => item.id && item.title);
}

function normalizePolicyEntries(payload) {
  const source = payload?.entries || payload?.results || payload?.items || payload?.data?.entries || payload?.content || [];
  const values = Array.isArray(source) ? source : [];
  return values.slice(0, 10).map((item) => {
    const value = item?.type === 'text' ? { text: item.text } : item;
    return {
      title: String(value?.title || value?.name || 'Store policy').slice(0, 120),
      text: String(value?.text || value?.content || value?.excerpt || '').replace(/\s+/g, ' ').slice(0, 2000),
      url: safeStoreUrl(value?.url || value?.source_url || value?.sourceUrl),
    };
  }).filter((item) => item.text && item.url);
}

function decimalPrice(value) {
  const raw = value && typeof value === 'object' ? (value.amount ?? value.value) : value;
  if (raw === null || raw === undefined || raw === '' || !Number.isFinite(Number(raw))) {
    throw new Error('catalog_price_unavailable');
  }
  return String(raw);
}

function normalizeProductJsonPrices(products) {
  for (const product of products) {
    for (const variant of product.variants || []) {
      if (Number.isInteger(variant.price)) variant.price = (variant.price / 100).toFixed(2);
    }
  }
  return products;
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
  async getDeclaredTools() {
    if (!this.isAvailable()) return [];
    const tools = await this.modelContext.getTools();
    return Array.isArray(tools) ? tools : [];
  }
  async hasTool(name) {
    return (await this.getDeclaredTools()).some((tool) => tool?.name === name);
  }
  async invoke(name, args = {}) {
    const tools = await this.getDeclaredTools();
    const descriptor = tools.find((tool) => tool?.name === name);
    if (!descriptor) throw new Error(`webmcp_${name}_unavailable`);
    const raw = await this.modelContext.executeTool(descriptor, JSON.stringify(args));
    return typeof raw === 'string' ? JSON.parse(raw) : raw;
  }
  async search(query, limit = 5) {
    if (!this.isAvailable()) throw new Error('webmcp_catalog_unavailable');
    const payload = await this.invoke('search_catalog', { query, limit });
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
    normalizeProductJsonPrices(detailed);
    const normalized = normalizeCatalogProducts({ products: detailed }, { currency: this.currency });
    return normalized;
  }

  async getByHandle(handle) {
    if (!handle) return null;
    const response = await this.fetch(`${this.baseUrl}/products/${encodeURIComponent(handle)}.js`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`product_detail_failed_${response.status}`);
    const raw = await response.json();
    normalizeProductJsonPrices([raw]);
    return normalizeCatalogProducts({ products: [raw] }, { currency: this.currency })[0] || null;
  }

  async browse(args = {}) {
    const limit = Math.min(20, Math.max(1, Number(args.limit || 8)));
    if (args.mode === 'list_collections') {
      const response = await this.fetch(`${this.baseUrl}/collections.json?limit=${limit}`, { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`collection_list_failed_${response.status}`);
      const raw = await response.json();
      return { collections: normalizeCollections(raw), products: [] };
    }
    const reference = String(args.collection_reference || '').trim();
    if (!reference) throw new Error('collection_reference_required');
    const handle = reference.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const response = await this.fetch(`${this.baseUrl}/collections/${encodeURIComponent(handle)}/products.json?limit=${limit}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`collection_products_failed_${response.status}`);
    const raw = await response.json();
    normalizeProductJsonPrices(raw.products || []);
    return { collections: [], products: normalizeCatalogProducts(raw, { currency: this.currency }) };
  }
}

export class NativeSearchAdapter {
  constructor({ searchUrl = null, fetchImpl = null, documentRef = null, locationRef = null, currency = null } = {}) {
    this.searchUrl = searchUrl || '/search';
    const browser = typeof window !== 'undefined' ? window : null;
    this.fetch = fetchImpl || browser?.fetch?.bind(browser);
    this.document = documentRef || (typeof document !== 'undefined' ? document : null);
    this.location = locationRef || browser?.location;
    this.currency = currency || browser?.Shopify?.currency?.active || 'USD';
  }

  buildUrl(args = {}) {
    const target = new URL(this.searchUrl, this.location.origin);
    if (target.origin !== this.location.origin) throw new Error('native_search_cross_origin');
    const query = String(args.query || '').trim();
    if (!query) throw new Error('native_search_query_required');
    target.search = '';
    target.searchParams.set('q', query);
    target.searchParams.set('type', 'product');
    if (args.sort_by && ['relevance', 'price-ascending', 'price-descending'].includes(args.sort_by)) {
      target.searchParams.set('sort_by', args.sort_by);
    }
    if (args.min_price != null) target.searchParams.set('filter.v.price.gte', String(args.min_price));
    if (args.max_price != null) target.searchParams.set('filter.v.price.lte', String(args.max_price));
    return `${target.pathname}${target.search}`;
  }

  async observe(expectedArgs = {}, limit = 20) {
    const actual = new URL(this.location.href);
    const expected = new URL(this.buildUrl(expectedArgs), actual.origin);
    if (actual.pathname !== expected.pathname || actual.searchParams.get('q') !== expected.searchParams.get('q')) {
      throw new Error('native_search_arrival_mismatch');
    }
    for (const name of ['type', 'sort_by', 'filter.v.price.gte', 'filter.v.price.lte']) {
      const wanted = expected.searchParams.get(name);
      if (wanted !== null && actual.searchParams.get(name) !== wanted) throw new Error(`native_search_parameter_mismatch_${name}`);
    }
    let roots = [];
    for (let attempt = 0; attempt < 10 && !roots.length; attempt += 1) {
      roots = [...new Set([
        this.document.querySelector('#product-grid'),
        this.document.querySelector('main [id*="product-grid"]'),
        this.document.querySelector('main .product-grid'),
        this.document.querySelector('main [data-native-search-results]'),
        this.document.querySelector('main ul[class*="product-grid"]'),
        this.document.querySelector('main'),
      ].filter(Boolean))];
      roots = roots.filter((root) => root.querySelectorAll('a[href*="/products/"]').length);
      if (!roots.length && attempt < 9) await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!roots.length) throw new Error('native_search_grid_unavailable');
    // Themes often expose both a section wrapper and its nested product grid.
    // Choose the narrowest useful root instead of treating that as ambiguity.
    roots.sort((left, right) => {
      const count = (root) => root.querySelectorAll('a[href*="/products/"]').length;
      return count(left) - count(right);
    });
    const links = [...roots[0].querySelectorAll('a[href*="/products/"]')];
    const ordered = [];
    let enrichmentFailures = 0;
    const seen = new Set();
    for (const link of links) {
      if (ordered.length >= Math.min(50, Math.max(1, Number(limit || 20)))) break;
      if (link.closest('[hidden], [aria-hidden="true"]')) continue;
      const url = new URL(link.href, actual.origin);
      if (url.origin !== actual.origin || !url.pathname.includes('/products/')) continue;
      const handle = decodeURIComponent(url.pathname.split('/products/')[1]?.split('/')[0] || '');
      if (!handle || seen.has(handle)) continue;
      seen.add(handle);
      const productPath = `${url.pathname.replace(/\/$/, '')}.js`;
      let product = null;
      try {
        const response = await this.fetch(productPath, { headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error(`native_search_product_failed_${response.status}`);
        product = await response.json();
      } catch (_) {
        enrichmentFailures += 1;
        const card = link.closest('li, article, [class*="card"], [class*="product"]') || link;
        const titleNode = card.querySelector?.('h2, h3, [class*="title"]');
        const title = String(titleNode?.textContent || link.textContent || handle).replace(/\s+/g, ' ').trim();
        product = { id: `handle:${handle}`, handle, title, variants: [] };
      }
      product.url ||= `${url.pathname}${url.search}`;
      ordered.push(product);
    }
    normalizeProductJsonPrices(ordered);
    const products = normalizeCatalogProducts({ products: ordered }, { currency: this.currency });
    if (!products.length && links.length) throw new Error('native_search_products_unavailable');
    const minimum = expected.searchParams.get('filter.v.price.gte');
    const maximum = expected.searchParams.get('filter.v.price.lte');
    const eligiblePrices = products.map((product) => (product.variants || [])
      .filter((variant) => variant.available_for_sale !== false)
      .map((variant) => Number(variant.price?.amount))
      .filter((price) => Number.isFinite(price) && (minimum === null || price >= Number(minimum)) && (maximum === null || price <= Number(maximum))));
    if ((minimum !== null || maximum !== null) && eligiblePrices.some((prices) => !prices.length)) {
      throw new Error('native_search_filter_not_applied');
    }
    const sort = expected.searchParams.get('sort_by');
    const sortPrices = eligiblePrices.map((prices) => Math.min(...prices));
    if (sort === 'price-ascending' && sortPrices.some((price, index) => index && price < sortPrices[index - 1])) {
      throw new Error('native_search_sort_not_applied');
    }
    if (sort === 'price-descending' && sortPrices.some((price, index) => index && price > sortPrices[index - 1])) {
      throw new Error('native_search_sort_not_applied');
    }
    return {
      products,
      native_search: {
        schema_version: '1.0.0', adapter: 'shopify-theme-product-grid-v1', actual_url: `${actual.pathname}${actual.search}`,
        query: actual.searchParams.get('q') || '', sort_by: actual.searchParams.get('sort_by') || 'relevance',
        min_price: actual.searchParams.get('filter.v.price.gte'), max_price: actual.searchParams.get('filter.v.price.lte'),
        page: Number(actual.searchParams.get('page') || 1), rendered_count: ordered.length,
        enrichment_failures: enrichmentFailures,
      },
    };
  }
}

export class StorefrontCatalog {
  constructor({ webMcp = null, ajax = null, nativeSearch = null } = {}) {
    this.webMcp = webMcp || new WebMcpCatalogAdapter();
    this.ajax = ajax || new AjaxCatalogAdapter();
    this.nativeSearch = nativeSearch || (typeof document !== 'undefined'
      ? new NativeSearchAdapter({ searchUrl: document.getElementById('ishop-drake-root')?.dataset.searchUrl || '/search' })
      : null);
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

  async getAvailableTools() {
    const names = new Set(['search_catalog', 'browse_store', 'get_product']);
    if (this.webMcp.isAvailable() && await this.webMcp.hasTool('search_shop_policies_and_faqs')) names.add('search_shop_policies_and_faqs');
    return [...names];
  }

  async executeTool(request, page = {}) {
    const name = request?.name;
    const args = request?.arguments || {};
    const observedAt = Date.now();
    try {
      if (name === 'search_catalog') {
        const result = await this.search(String(args.query || ''), Number(args.limit || 8));
        return { tool: name, ok: true, source: result.source, data: { products: result.products, coverage: 'bounded' }, observed_at_ms: observedAt };
      }
      if (name === 'get_product') {
        let product = null;
        const reference = String(args.product_reference || args.product_id || args.handle || '').trim();
        const candidateList = Array.isArray(page.candidateProducts) ? page.candidateProducts : (Array.isArray(page.products) ? page.products : []);
        if (reference && candidateList.length) {
          product = candidateList.find((item) => (
            String(item.product_id) === reference ||
            String(item.id) === reference ||
            (item.handle && String(item.handle).toLowerCase() === reference.toLowerCase()) ||
            (item.title && String(item.title).toLowerCase() === reference.toLowerCase())
          )) || null;
        }
        if (!product && reference && !/^\d+$/.test(reference) && !reference.startsWith('gid://')) {
          try {
            product = await this.getByHandle(reference);
          } catch (_) {}
        }
        if (!product && page.currentProductHandle && (!reference || reference === String(page.currentProductId) || reference.toLowerCase() === page.currentProductHandle.toLowerCase())) {
          try {
            product = await this.getByHandle(page.currentProductHandle);
          } catch (_) {}
        }
        if (!product && reference && !/^\d+$/.test(reference) && !reference.startsWith('gid://')) {
          try {
            const searchResults = await this.search(reference, 8);
            product = searchResults.products.find((item) => item.title.toLowerCase() === reference.toLowerCase()) || null;
          } catch (_) {}
        }
        return { tool: name, ok: true, source: 'storefront_product', data: { products: product ? [product] : [], coverage: product ? 'exact' : 'none' }, observed_at_ms: observedAt };
      }
      if (name === 'browse_store') {
        if (this.webMcp.isAvailable() && await this.webMcp.hasTool(name)) {
          const raw = await this.webMcp.invoke(name, args);
          return { tool: name, ok: true, source: 'native_webmcp', data: { products: normalizeCatalogProducts(raw), collections: normalizeCollections(raw), coverage: 'provider_reported' }, observed_at_ms: observedAt };
        }
        const result = await this.ajax.browse(args);
        return { tool: name, ok: true, source: 'ajax_storefront', data: { ...result, coverage: 'bounded' }, observed_at_ms: observedAt };
      }
      if (name === 'search_shop_policies_and_faqs') {
        if (!this.webMcp.isAvailable() || !(await this.webMcp.hasTool(name))) throw new Error('store_policy_tool_unavailable');
        const raw = await this.webMcp.invoke(name, args);
        return { tool: name, ok: true, source: 'native_webmcp', data: { entries: normalizePolicyEntries(raw), coverage: 'provider_reported' }, observed_at_ms: observedAt };
      }
      throw new Error('unsupported_read_tool');
    } catch (error) {
      return { tool: String(name || ''), ok: false, source: 'storefront', data: {}, error: String(error?.message || error).slice(0, 300), observed_at_ms: observedAt };
    }
  }
}

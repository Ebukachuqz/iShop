import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AjaxCatalogAdapter, NativeSearchAdapter, StorefrontCatalog, WebMcpCatalogAdapter } from '../../../apps/shopify/extensions/drake/assets/drake-catalog.js';


test('native search URL is constrained and rendered grid order is authoritative (NS-01, NS-02, NS-14)', async () => {
  const links = [
    { href: 'https://shop.test/products/second', closest: () => null },
    { href: 'https://shop.test/products/first', closest: () => null },
    { href: 'https://shop.test/products/second?duplicate=1', closest: () => null },
  ];
  const grid = { querySelectorAll: () => links };
  const documentRef = { querySelector: (selector) => selector === '#product-grid' ? grid : null };
  const fetchImpl = async (url) => ({ ok: true, json: async () => ({
    id: url.includes('/second') ? 2 : 1, title: url.includes('/second') ? 'Second rendered' : 'First rendered',
    handle: url.includes('/second') ? 'second' : 'first', options: ['Title'],
    variants: [{ id: url.includes('/second') ? 20 : 10, title: 'Default Title', options: ['Default Title'], price: 10000, available: true }],
  }) });
  const locationRef = { origin: 'https://shop.test', href: 'https://shop.test/en/search?q=boards&type=product&sort_by=price-ascending' };
  const adapter = new NativeSearchAdapter({ searchUrl: '/en/search', fetchImpl, documentRef, locationRef, currency: 'USD' });
  assert.equal(adapter.buildUrl({ query: 'boards', sort_by: 'price-ascending' }), '/en/search?q=boards&type=product&sort_by=price-ascending');
  const observed = await adapter.observe({ query: 'boards', sort_by: 'price-ascending' });
  assert.deepEqual(observed.products.map((item) => item.product_id), ['2', '1']);
  assert.equal(observed.native_search.rendered_count, 2);
  assert.throws(() => new NativeSearchAdapter({ searchUrl: 'https://evil.test/search', fetchImpl, documentRef, locationRef }).buildUrl({ query: 'x' }), /cross_origin/);
});

test('native search tolerates nested theme wrappers and chooses the product grid', async () => {
  const links = [
    { href: 'https://shop.test/products/complete', closest: () => null },
    { href: 'https://shop.test/products/minimal', closest: () => null },
  ];
  const grid = { querySelectorAll: () => links };
  const main = { querySelectorAll: () => [...links, { href: 'https://shop.test/products/recommended', closest: () => null }] };
  const documentRef = { querySelector: (selector) => selector === '#product-grid' ? grid : (selector === 'main' ? main : null) };
  const fetchImpl = async (url) => ({ ok: true, json: async () => ({
    id: url.includes('complete') ? 1 : 2,
    title: url.includes('complete') ? 'The Complete Snowboard' : 'The Minimal Snowboard',
    handle: url.includes('complete') ? 'complete' : 'minimal', options: ['Title'],
    variants: [{ id: 10, title: 'Default Title', options: ['Default Title'], price: 69995, available: true }],
  }) });
  const adapter = new NativeSearchAdapter({
    searchUrl: '/search', fetchImpl, documentRef,
    locationRef: { origin: 'https://shop.test', href: 'https://shop.test/search?q=snowboard&type=product' },
    currency: 'USD',
  });
  const observed = await adapter.observe({ query: 'snowboard' });
  assert.deepEqual(observed.products.map((item) => item.title), ['The Complete Snowboard', 'The Minimal Snowboard']);
});

test('native search preserves rendered identity when one product enrichment fails', async () => {
  const cards = [
    { querySelector: () => ({ textContent: 'The Complete Snowboard' }) },
    { querySelector: () => ({ textContent: 'The Minimal Snowboard' }) },
  ];
  const links = [
    { href: 'https://shop.test/products/complete', textContent: '', closest: (selector) => selector.startsWith('[hidden]') ? null : cards[0] },
    { href: 'https://shop.test/products/minimal', textContent: '', closest: (selector) => selector.startsWith('[hidden]') ? null : cards[1] },
  ];
  const grid = { querySelectorAll: () => links };
  const documentRef = { querySelector: (selector) => selector === '#product-grid' ? grid : null };
  const fetchImpl = async (url) => url.includes('complete')
    ? { ok: false, status: 503 }
    : { ok: true, json: async () => ({ id: 2, title: 'The Minimal Snowboard', handle: 'minimal', variants: [] }) };
  const adapter = new NativeSearchAdapter({
    searchUrl: '/search', fetchImpl, documentRef,
    locationRef: { origin: 'https://shop.test', href: 'https://shop.test/search?q=snowboard&type=product' },
    currency: 'USD',
  });
  const observed = await adapter.observe({ query: 'snowboard' });
  assert.deepEqual(observed.products.map((item) => item.title), ['The Complete Snowboard', 'The Minimal Snowboard']);
  assert.equal(observed.products[0].url, '/products/complete');
  assert.equal(observed.native_search.enrichment_failures, 1);
});


test('Ajax browse lists collections and normalizes collection products', async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    if (String(url).startsWith('/collections.json')) return { ok: true, json: async () => ({ collections: [{ id: 1, title: 'Snowboards', handle: 'snowboards' }] }) };
    return { ok: true, json: async () => ({ products: [{ id: 2, title: 'Board', handle: 'board', options: ['Title'], variants: [{ id: 20, title: 'Default Title', options: ['Default Title'], price: 70000, available: true }] }] }) };
  };
  const adapter = new AjaxCatalogAdapter({ fetchImpl, currency: 'USD' });
  const listed = await adapter.browse({ mode: 'list_collections', limit: 8 });
  assert.equal(listed.collections[0].url, '/collections/snowboards');
  const products = await adapter.browse({ mode: 'collection_products', collection_reference: 'Snowboards', limit: 8 });
  assert.equal(products.products[0].variants[0].price.amount, '700.00');
  assert.match(calls[1], /collections\/snowboards\/products\.json/);
});


test('policy wrapper returns only attributable same-store passages', async () => {
  const modelContext = {
    getTools: async () => [{ name: 'search_shop_policies_and_faqs' }],
    executeTool: async () => ({ entries: [
      { title: 'Returns', text: 'Returns within 30 days.', url: '/policies/refund-policy' },
      { title: 'External', text: 'Untrusted external claim.', url: 'https://attacker.invalid/policy' },
    ] }),
  };
  const webMcp = new WebMcpCatalogAdapter(modelContext);
  const catalog = new StorefrontCatalog({ webMcp, ajax: {} });
  assert.equal((await catalog.getAvailableTools()).includes('search_shop_policies_and_faqs'), true);
  const observation = await catalog.executeTool({ name: 'search_shop_policies_and_faqs', arguments: { query: 'returns' } });
  assert.equal(observation.ok, true);
  assert.deepEqual(observation.data.entries, [{ title: 'Returns', text: 'Returns within 30 days.', url: '/policies/refund-policy' }]);
});


test('missing policy capability produces an explicit unavailable observation', async () => {
  const catalog = new StorefrontCatalog({
    webMcp: { isAvailable: () => false },
    ajax: {},
  });
  const observation = await catalog.executeTool({ name: 'search_shop_policies_and_faqs', arguments: { query: 'shipping' } });
  assert.equal(observation.ok, false);
  assert.match(observation.error, /unavailable/);
});

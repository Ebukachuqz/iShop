import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AjaxCatalogAdapter, StorefrontCatalog, WebMcpCatalogAdapter } from '../../../apps/shopify/extensions/drake/assets/drake-catalog.js';


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

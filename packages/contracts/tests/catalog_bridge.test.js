import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  AjaxCatalogAdapter,
  StorefrontCatalog,
  WebMcpCatalogAdapter,
} from '../../../apps/shopify/extensions/drake/assets/drake-catalog.js';

test('Ajax catalog fallback returns exact Shopify product and variant evidence', async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    if (url.startsWith('/search/suggest.json?')) {
      return {
        ok: true,
        json: async () => ({ resources: { results: { products: [{ id: 10, title: 'Black Shirt', handle: 'black-shirt' }] } } }),
      };
    }
    return {
      ok: true,
      json: async () => ({
        id: 10,
        title: 'Black Shirt',
        handle: 'black-shirt',
        options: [{ name: 'Size' }, { name: 'Color' }],
        variants: [{ id: 101, title: 'Large / Black', options: ['Large', 'Black'], price: 2499000, available: true }],
      }),
    };
  };
  const adapter = new AjaxCatalogAdapter({ fetchImpl, currency: 'NGN' });
  const products = await adapter.search('black shirt', 5);
  assert.match(calls[0], /q=black\+shirt/);
  assert.equal(calls[1], '/products/black-shirt.js');
  assert.deepEqual(products[0].variants[0], {
    variant_id: '101',
    product_id: '10',
    product_title: 'Black Shirt',
    variant_title: 'Large / Black',
    selected_options: { Size: 'Large', Color: 'Black' },
    price: { amount: '24990.00', currency: 'NGN' },
    available_for_sale: true,
    quantity_available: null,
    inventory_policy: 'DENY',
  });
});

test('catalog prefers declared WebMCP search and safely falls back for reads', async () => {
  const modelContext = {
    getTools: async () => [{ name: 'search_catalog' }],
    executeTool: async () => JSON.stringify({ products: [{ id: 'gid://shopify/Product/1', title: 'Shirt', variants: [] }] }),
  };
  const webMcp = new WebMcpCatalogAdapter(modelContext);
  const ajax = { search: async () => [{ product_id: 'fallback' }] };
  const preferred = await new StorefrontCatalog({ webMcp, ajax }).search('shirt');
  assert.equal(preferred.source, 'native_webmcp');
  assert.equal(preferred.products[0].product_id, 'gid://shopify/Product/1');

  const unavailable = { isAvailable: () => true, search: async () => { throw new Error('read failed'); } };
  const fallback = await new StorefrontCatalog({ webMcp: unavailable, ajax }).search('shirt');
  assert.equal(fallback.source, 'ajax_product_json');
  assert.equal(fallback.products[0].product_id, 'fallback');
});

test('deployed theme assets contain the cart bridge and catalog modules', () => {
  const here = fileURLToPath(new URL('.', import.meta.url));
  const extension = join(here, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake');
  const liquid = readFileSync(join(extension, 'blocks', 'drake-embed.liquid'), 'utf8');
  const bootstrap = readFileSync(join(extension, 'assets', 'drake-bootstrap.js'), 'utf8');
  assert.match(liquid, /drake-bridge\.js/);
  assert.match(liquid, /drake-catalog\.js/);
  assert.match(bootstrap, /new bridgeModule\.StorefrontBridge/);
  assert.match(bootstrap, /new catalogModule\.StorefrontCatalog/);
});

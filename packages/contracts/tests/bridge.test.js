import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { isCartEquivalent } from '../src/index.js';
import { cartFingerprint } from '../../../apps/shopify/extensions/drake/assets/drake-browser-contracts.js';
import { AjaxCartAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/ajax.js';
import { StandardActionsAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/standard_actions.js';
import { WebMcpAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/webmcp.js';
import { StorefrontBridge } from '../../../apps/shopify/extensions/drake/src/bridge/bridge.js';

describe('Storefront Bridge & Adapter Parity (T-16, S-06, S-10)', () => {
  test('show cart opens a recognized theme drawer before falling back to the cart page', async () => {
    let opened = 0;
    let navigated = 0;
    const drawer = { open: () => { opened += 1; }, removeAttribute() {}, setAttribute() {}, classList: { add() {} } };
    const documentRef = { querySelector: (selector) => selector.startsWith('cart-drawer') ? drawer : null };
    const bridge = new StorefrontBridge({
      documentRef, origin: 'https://shop.test', navigate: () => { navigated += 1; },
      ajaxAdapter: { readCart: async () => ({ shop_id: 'shop.test', currency: 'USD', lines: [] }) },
      webMcpAdapter: { isAvailable: () => false }, actionsAdapter: { isAvailable: () => false },
    });
    const receipt = await bridge.executeCommand({
      shop_id: 'shop.test', operation: 'navigate_storefront',
      parameters: { url: '/cart', presentation: 'drawer_or_page' },
    });
    assert.equal(receipt.outcome, 'navigation_handoff');
    assert.equal(receipt.transport_used, 'cart_drawer');
    assert.equal(opened, 1);
    assert.equal(navigated, 0);
  });

  test('show cart navigates to cart when the theme has no recognized drawer', async () => {
    let destination = '';
    const bridge = new StorefrontBridge({
      documentRef: { querySelector: () => null }, origin: 'https://shop.test', navigate: (url) => { destination = url; },
      ajaxAdapter: { readCart: async () => ({ shop_id: 'shop.test', currency: 'USD', lines: [] }) },
      webMcpAdapter: { isAvailable: () => false }, actionsAdapter: { isAvailable: () => false },
    });
    await bridge.executeCommand({
      shop_id: 'shop.test', operation: 'navigate_storefront',
      parameters: { url: '/cart', presentation: 'drawer_or_page' },
    });
    assert.equal(destination, 'https://shop.test/cart');
  });
  test('cart presentation uses matching observed line keys and rejects a changed cart', async () => {
    const cart = { shop_id: 'shop', currency: 'USD', lines: [{ variant_id: 'v1', quantity: 1, line_key: 'mcp-key' }] };
    const observed = { ...cart, lines: [{ ...cart.lines[0], line_key: 'ajax-key' }],
      display_lines: [{ line_key: 'ajax-key', variant_id: 'v1', title: 'Snowboard', quantity: 1 }] };
    const bridge = new StorefrontBridge({ ajaxAdapter: { readCart: async () => observed } });
    assert.equal(await bridge.describeCart(cart), observed);
    assert.equal(await cartFingerprint(cart), await cartFingerprint(observed));
    observed.lines[0].quantity = 2;
    assert.equal(await bridge.describeCart(cart), cart);
  });
  test('binds WebMCP cart evidence to the trusted storefront shop', () => {
    const adapter = new WebMcpAdapter(null, 'trusted-shop.myshopify.com');
    const cart = adapter.normalizeCart({ shop_id: 'store.myshopify.com', currency: 'USD', lines: [] });
    assert.equal(cart.shop_id, 'trusted-shop.myshopify.com');
  });

  test('does not select native WebMCP writes without a validated schema translator', () => {
    const bridge = new StorefrontBridge({
      webMcpAdapter: new WebMcpAdapter({ getTools: async () => [], executeTool: async () => ({}) }),
      actionsAdapter: { isAvailable: () => false },
      ajaxAdapter: {},
    });
    assert.equal(bridge.detectPreferredTransport(), 'ajax_cart');
  });

  test('browser cart fingerprints match the Python cart-v1 contract', async () => {
    assert.equal(
      await cartFingerprint({ shop_id: 'store.myshopify.com', currency: 'USD', lines: [] }),
      '0b67ea4fa5fd4ee2aa90a7b316be131823a2ce3359a887d95ac8fe38bc2cf6e0',
    );
    assert.equal(
      await cartFingerprint({
        shop_id: 'store.myshopify.com', currency: 'USD',
        lines: [
          { variant_id: 'v2', quantity: 1, selling_plan_id: 'plan-1', properties: { x: 'a&b=c' } },
          { variant_id: 'v1', quantity: 2, properties: { size: 'M', note: '雪' } },
          { variant_id: 'v1', quantity: 1, properties: { note: '雪', size: 'M' } },
        ],
      }),
      'b0181186fd87e3564b9f7f22f80d4538c25bd4956ba602e7e8d3ad1129ed6b17',
    );
  });

  test('dispatches once when an unchanged cart satisfies the shared fingerprint', async () => {
    const cart = { shop_id: 'store.myshopify.com', currency: 'USD', lines: [] };
    let writes = 0;
    const bridge = new StorefrontBridge({
      ajaxAdapter: {
        readCart: async () => writes === 0 ? cart : {
          ...cart, lines: [{ variant_id: 'v1', quantity: 1, properties: {}, selling_plan_id: null }],
        },
        addVariant: async () => { writes += 1; return { ok: true, errors: [] }; },
      },
      webMcpAdapter: { isAvailable: () => false },
      actionsAdapter: { isAvailable: () => false },
    });
    const receipt = await bridge.executeCommand({
      operation: 'add_variant',
      expected_cart_fingerprint: '0b67ea4fa5fd4ee2aa90a7b316be131823a2ce3359a887d95ac8fe38bc2cf6e0',
      parameters: { variant_id: 'v1', quantity: 1 },
    });
    assert.equal(receipt.outcome, 'verified_success');
    assert.equal(writes, 1);
  });

  test('reads cart data from supported WebMCP result envelopes', async () => {
    const cart = { currency: 'USD', lines: [{ id: 'line_1', merchandiseId: 'variant_1', quantity: 1 }] };
    const envelopes = [
      cart,
      { cart },
      { structuredContent: { cart } },
      { content: [{ type: 'text', text: JSON.stringify(cart) }] },
    ];

    for (const result of envelopes) {
      const adapter = new WebMcpAdapter({
        getTools: async () => [{ name: 'get_cart' }],
        executeTool: async () => result,
      }, 'store.myshopify.com');
      const normalized = await adapter.readCart();
      assert.equal(normalized.lines[0].variant_id, 'variant_1');
      assert.equal(normalized.shop_id, 'store.myshopify.com');
    }
  });

  test('falls back to Ajax when WebMCP cannot provide a readable cart', async () => {
    let ajaxReads = 0;
    const expected = { shop_id: 'store.myshopify.com', currency: 'USD', lines: [] };
    const bridge = new StorefrontBridge({
      webMcpAdapter: {
        isAvailable: () => true,
        canReadCart: async () => true,
        readCart: async () => { throw new Error('unsupported WebMCP envelope'); },
      },
      ajaxAdapter: { readCart: async () => { ajaxReads += 1; return expected; } },
      actionsAdapter: { isAvailable: () => false },
    });

    assert.deepEqual(await bridge.readAuthoritativeCart(), expected);
    assert.equal(ajaxReads, 1);
  });

  test('uses Ajax when WebMCP does not declare get_cart', async () => {
    let webMcpReads = 0;
    const expected = { shop_id: 'store.myshopify.com', currency: 'USD', lines: [] };
    const adapter = new WebMcpAdapter({
      getTools: async () => [{ name: 'update_cart' }],
      executeTool: async () => { webMcpReads += 1; },
    });
    const bridge = new StorefrontBridge({
      webMcpAdapter: adapter,
      ajaxAdapter: { readCart: async () => expected },
      actionsAdapter: { isAvailable: () => false },
    });

    assert.deepEqual(await bridge.readAuthoritativeCart(), expected);
    assert.equal(webMcpReads, 0);
  });

  test('navigation accepts only same-origin Shopify product and collection paths', async () => {
    const navigated = [];
    const cart = { shop_id: 'store.myshopify.com', currency: 'USD', lines: [] };
    const bridge = new StorefrontBridge({
      ajaxAdapter: { readCart: async () => cart },
      webMcpAdapter: { isAvailable: () => false },
      actionsAdapter: { isAvailable: () => false },
      origin: 'https://store.myshopify.com',
      navigate: (url) => navigated.push(url),
    });
    const accepted = await bridge.executeCommand({ operation: 'navigate_storefront', parameters: { url: '/products/snowboard' } });
    const rejected = await bridge.executeCommand({ operation: 'navigate_storefront', parameters: { url: 'https://example.com/products/snowboard' } });
    assert.equal(accepted.outcome, 'navigation_handoff');
    assert.equal(rejected.outcome, 'rejected');
    assert.deepEqual(navigated, ['https://store.myshopify.com/products/snowboard']);
  });

  test('T-16: WebMCP, Standard Actions, and Ajax adapters produce equivalent canonical carts', () => {
    // 1. Raw Ajax API response
    const ajaxRaw = {
      token: 'test_token',
      currency: 'USD',
      items: [
        {
          key: 'line_1',
          variant_id: 'var_123',
          quantity: 2,
          properties: { size: 'M', color: 'blue' },
          selling_plan_allocation: null,
        },
      ],
    };

    // 2. Raw Standard Actions response
    const actionsRaw = {
      shop_id: 'store.myshopify.com',
      currency: 'USD',
      lines: [
        {
          id: 'line_1',
          merchandise: { id: 'var_123' },
          quantity: 2,
          attributes: [
            { key: 'color', value: 'blue' },
            { key: 'size', value: 'M' },
          ],
          sellingPlanAllocation: null,
        },
      ],
    };

    // 3. Raw WebMCP response
    const webMcpRaw = {
      shop_id: 'store.myshopify.com',
      currency: 'USD',
      lines: [
        {
          id: 'line_1',
          merchandiseId: 'var_123',
          quantity: 2,
          properties: { color: 'blue', size: 'M' },
          sellingPlanId: null,
        },
      ],
    };

    const dummyFetch = async () => ({
      ok: true,
      json: async () => ajaxRaw,
    });

    const ajaxAdapter = new AjaxCartAdapter('', dummyFetch);
    const actionsAdapter = new StandardActionsAdapter();
    const webMcpAdapter = new WebMcpAdapter();

    const normalizedAjax = ajaxAdapter._normalizeCart(ajaxRaw);
    normalizedAjax.shop_id = 'store.myshopify.com'; // align for multiset comparison
    const normalizedActions = actionsAdapter.normalizeCart(actionsRaw);
    const normalizedWebMcp = webMcpAdapter.normalizeCart(webMcpRaw);

    // All three adapters must agree on canonical cart equivalence (T-16)
    assert.equal(
      isCartEquivalent(normalizedAjax, normalizedActions),
      true,
      'Ajax and Standard Actions normalized carts must be equivalent'
    );
    assert.equal(
      isCartEquivalent(normalizedActions, normalizedWebMcp),
      true,
      'Standard Actions and WebMCP normalized carts must be equivalent'
    );
  });

  test('T-16 & S-10: In-flight dispatch failure marks outcome uncertain and NEVER retries on fallback adapter', async () => {
    let fallbackCalls = 0;

    const failingWebMcp = {
      isAvailable: () => true,
      readCart: async () => ({
        shop_id: 'store.myshopify.com',
        currency: 'USD',
        lines: [],
      }),
      updateCart: async () => {
        throw new Error('Connection severed during write');
      },
    };

    const fallbackAjax = {
      addVariant: async () => {
        fallbackCalls++;
        return { ok: true, errors: [] };
      },
      readCart: async () => ({
        shop_id: 'store.myshopify.com',
        currency: 'USD',
        lines: [],
      }),
    };

    const bridge = new StorefrontBridge({
      webMcpAdapter: failingWebMcp,
      ajaxAdapter: fallbackAjax,
    });

    const command = {
      command_id: 'cmd_test_001',
      operation: 'add_variant',
      parameters: { variant_id: 'var_123', quantity: 1 },
    };

    const receipt = await bridge.executeCommand(command);

    assert.equal(receipt.outcome, 'uncertain');
    assert.equal(
      fallbackCalls,
      0,
      'Fallback adapter must NEVER be called after an uncertain write (T-16, S-10)'
    );
  });

  test('T-08: Storefront rejection returns rejected receipt with errors preserved', async () => {
    const rejectingAjax = {
      addVariant: async () => ({
        ok: false,
        errors: ['Item inventory exceeded'],
      }),
      readCart: async () => ({
        shop_id: 'store.myshopify.com',
        currency: 'USD',
        lines: [],
      }),
    };

    const bridge = new StorefrontBridge({
      ajaxAdapter: rejectingAjax,
      webMcpAdapter: { isAvailable: () => false },
      actionsAdapter: { isAvailable: () => false },
    });

    const command = {
      command_id: 'cmd_test_002',
      operation: 'add_variant',
      parameters: { variant_id: 'var_sold_out', quantity: 1 },
    };

    const receipt = await bridge.executeCommand(command);

    assert.equal(receipt.ok, false);
    assert.equal(receipt.outcome, 'rejected');
    assert.deepEqual(receipt.errors, ['Item inventory exceeded']);
  });

  test('R3: Bridge executeCommand fails if post-mutation cart contains unexpected extra lines', async () => {
    let callCount = 0;
    const mutatingAjax = {
      addVariant: async () => ({ ok: true, errors: [] }),
      readCart: async () => {
        callCount++;
        if (callCount === 1) {
          // before cart: hoodie only
          return {
            shop_id: 'store.myshopify.com',
            currency: 'USD',
            lines: [{ variant_id: 'var_hoodie', quantity: 1, properties: {} }],
          };
        }
        // after cart: hoodie + cap + unexpected bonus socks!
        return {
          shop_id: 'store.myshopify.com',
          currency: 'USD',
          lines: [
            { variant_id: 'var_hoodie', quantity: 1, properties: {} },
            { variant_id: 'var_cap', quantity: 1, properties: {} },
            { variant_id: 'var_unexpected_socks', quantity: 1, properties: {} },
          ],
        };
      },
    };

    const bridge = new StorefrontBridge({
      ajaxAdapter: mutatingAjax,
      webMcpAdapter: { isAvailable: () => false },
      actionsAdapter: { isAvailable: () => false },
    });

    const command = {
      command_id: 'cmd_test_003',
      operation: 'add_variant',
      parameters: { variant_id: 'var_cap', quantity: 1 },
    };

    const receipt = await bridge.executeCommand(command);

    assert.notEqual(receipt.outcome, 'verified_success', 'Bridge must NOT report verified_success when extra line was added');
    assert.equal(receipt.ok, false);
    assert.equal(receipt.outcome, 'failed_with_observed_change');
  });

  test('rejects an expired command before reading or mutating the cart', async () => {
    let reads = 0;
    const bridge = new StorefrontBridge({
      ajaxAdapter: { readCart: async () => { reads += 1; return { shop_id: 'store.myshopify.com', currency: 'USD', lines: [] }; } },
      webMcpAdapter: { isAvailable: () => false },
      actionsAdapter: { isAvailable: () => false },
    });
    const receipt = await bridge.executeCommand({
      schema_version: '1.0.0',
      command_id: 'cmd_expired_00000001',
      session_id: 'sess_test_00000001',
      shop_id: 'store.myshopify.com',
      turn_id: 'turn-expired',
      request_revision: 1,
      page_epoch: 1,
      expires_at_ms: Date.now() - 1,
      expected_cart_fingerprint: null,
      operation: 'add_variant',
      parameters: { variant_id: 'var_123', quantity: 1 },
    });
    assert.equal(receipt.outcome, 'rejected');
    assert.equal(reads, 0);
  });

  test('rejects a changed cart before dispatch when a fingerprint precondition is present', async () => {
    let writes = 0;
    const bridge = new StorefrontBridge({
      ajaxAdapter: {
        readCart: async () => ({ shop_id: 'store.myshopify.com', currency: 'USD', lines: [{ variant_id: 'changed', quantity: 1 }] }),
        addVariant: async () => { writes += 1; return { ok: true, errors: [] }; },
      },
      webMcpAdapter: { isAvailable: () => false },
      actionsAdapter: { isAvailable: () => false },
    });
    const receipt = await bridge.executeCommand({
      schema_version: '1.0.0',
      command_id: 'cmd_changed_00000001',
      session_id: 'sess_test_00000001',
      shop_id: 'store.myshopify.com',
      turn_id: 'turn-changed',
      request_revision: 1,
      page_epoch: 1,
      expires_at_ms: Date.now() + 60_000,
      expected_cart_fingerprint: 'stale-fingerprint',
      operation: 'add_variant',
      parameters: { variant_id: 'var_123', quantity: 1 },
    });
    assert.equal(receipt.outcome, 'rejected');
    assert.equal(writes, 0);
    assert.match(receipt.errors[0], /changed before dispatch/i);
  });

  test('clears the whole cart only through explicit Ajax authorization and verifies empty state', async () => {
    let cart = { shop_id: 'store.myshopify.com', currency: 'USD', lines: [{ variant_id: 'v1', quantity: 1, properties: {} }] };
    let clears = 0;
    const bridge = new StorefrontBridge({
      ajaxAdapter: { readCart: async () => cart, clearCart: async () => { clears += 1; cart = { ...cart, lines: [] }; return { ok: true, errors: [] }; } },
      webMcpAdapter: { isAvailable: () => false }, actionsAdapter: { isAvailable: () => true, updateCart: async () => { throw new Error('must not use actions'); } },
    });
    const receipt = await bridge.executeCommand({ operation: 'clear_cart', parameters: { explicit_whole_cart: true } });
    assert.equal(receipt.outcome, 'verified_success');
    assert.equal(clears, 1);
  });

  test('manage orders only navigates to the trusted Shopify account path', async () => {
    const visited = [];
    const cart = { shop_id: 'store.myshopify.com', currency: 'USD', lines: [] };
    const bridge = new StorefrontBridge({
      ajaxAdapter: { readCart: async () => cart }, webMcpAdapter: { isAvailable: () => false },
      actionsAdapter: { isAvailable: () => false }, navigate: (url) => visited.push(url), origin: 'https://store.example',
    });
    const receipt = await bridge.executeCommand({ operation: 'manage_orders', parameters: { url: '/account/orders' } });
    assert.equal(receipt.outcome, 'navigation_handoff');
    assert.equal(visited[0], 'https://store.example/account/orders');
  });
});

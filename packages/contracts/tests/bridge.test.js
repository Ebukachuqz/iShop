import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { isCartEquivalent } from '../src/index.js';
import { AjaxCartAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/ajax.js';
import { StandardActionsAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/standard_actions.js';
import { WebMcpAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/webmcp.js';
import { StorefrontBridge } from '../../../apps/shopify/extensions/drake/src/bridge/bridge.js';

describe('Storefront Bridge & Adapter Parity (T-16, S-06, S-10)', () => {
  test('binds WebMCP cart evidence to the trusted storefront shop', () => {
    const adapter = new WebMcpAdapter(null, 'trusted-shop.myshopify.com');
    const cart = adapter.normalizeCart({ shop_id: 'store.myshopify.com', currency: 'USD', lines: [] });
    assert.equal(cart.shop_id, 'trusted-shop.myshopify.com');
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
});

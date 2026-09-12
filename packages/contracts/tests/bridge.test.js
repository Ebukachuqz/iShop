import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { isCartEquivalent } from '../src/index.js';
import { AjaxCartAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/ajax.js';
import { StandardActionsAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/standard_actions.js';
import { WebMcpAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/webmcp.js';
import { StorefrontBridge } from '../../../apps/shopify/extensions/drake/src/bridge/bridge.js';

describe('Storefront Bridge & Adapter Parity (T-16, S-06, S-10)', () => {
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
});

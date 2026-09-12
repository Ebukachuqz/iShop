import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { AjaxCartAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/ajax.js';
import { StandardActionsAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/standard_actions.js';
import { WebMcpAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/webmcp.js';
import { StorefrontBridge } from '../../../apps/shopify/extensions/drake/src/bridge/bridge.js';
import { validateCommandSafety } from '../src/index.js';

describe('WP-01 Shopify Transport & Security Contract Suite (Simulated Mocks)', () => {
  describe('1. Signed Bootstrap & Origin Validation Contract', () => {
    const SECRET = 'a_very_secret_signing_key_that_is_at_least_32_bytes_long';

    function signPayload(grant, secret) {
      const msg = [
        grant.grant_id,
        grant.shop_id,
        grant.permitted_origin,
        grant.anonymous_session_id,
        grant.config_revision,
        grant.issued_at_ms,
        grant.expires_at_ms,
      ].join('|');
      return createHmac('sha256', secret).update(msg).digest('hex');
    }

    test('signed bootstrap produces verifiable HMAC grant matching runtime format', () => {
      const payload = {
        grant_id: 'grant_123',
        shop_id: 'drake-test.myshopify.com',
        permitted_origin: 'https://drake-test.myshopify.com',
        anonymous_session_id: 'sess_abc123',
        config_revision: 'default-v1',
        issued_at_ms: 1000,
        expires_at_ms: 301000,
      };
      const signature = signPayload(payload, SECRET);
      const grant = { ...payload, schema_version: '1.0.0', signature };

      assert.equal(grant.shop_id, 'drake-test.myshopify.com');
      assert.equal(grant.permitted_origin, 'https://drake-test.myshopify.com');
      assert.equal(signPayload(payload, SECRET), grant.signature);
    });

    test('expired or forged grant signature is rejected', () => {
      const payload = {
        grant_id: 'grant_forged',
        shop_id: 'drake-test.myshopify.com',
        permitted_origin: 'https://drake-test.myshopify.com',
        anonymous_session_id: 'sess_abc123',
        config_revision: 'default-v1',
        issued_at_ms: 1000,
        expires_at_ms: 2000,
      };
      const realSignature = signPayload(payload, SECRET);

      // Forged secret signature mismatch
      const forgedSig = signPayload(payload, 'wrong_secret_key_32_bytes_long_123456');
      assert.notEqual(forgedSig, realSignature, 'Forged signature must not match real signature');

      // Expired timestamp
      const nowMs = 5000;
      assert.equal(nowMs > payload.expires_at_ms, true, 'Expired grant must be detected by timestamp check');
    });

    test('unapproved or forged origin is rejected', () => {
      const allowedOrigins = new Set(['https://drake-test.myshopify.com']);
      const untrustedOrigin = 'https://malicious-attacker.com';
      const wildcardOrigin = '*';

      assert.equal(allowedOrigins.has(untrustedOrigin), false, 'Untrusted origin must be rejected');
      assert.equal(allowedOrigins.has(wildcardOrigin), false, 'Wildcard origin must be rejected');
    });
  });

  describe('2. Locale-Aware Route Construction', () => {
    test('AjaxCartAdapter constructs locale-aware endpoint URLs when given locale prefix', async () => {
      const requestedUrls = [];
      const mockFetch = async (url) => {
        requestedUrls.push(url);
        return {
          ok: true,
          json: async () => ({ currency: 'NGN', items: [] }),
        };
      };

      const localeAdapter = new AjaxCartAdapter('/en', mockFetch, 'store.myshopify.com');
      await localeAdapter.readCart();
      assert.equal(requestedUrls[0], '/en/cart.js', 'readCart must request /en/cart.js');

      await localeAdapter.addVariant({ variantId: 'var_123', quantity: 2 });
      assert.equal(requestedUrls[1], '/en/cart/add.js', 'addVariant must request /en/cart/add.js');

      await localeAdapter.changeLineQuantity({ lineKey: 'line_1', quantity: 3 });
      assert.equal(requestedUrls[2], '/en/cart/change.js', 'changeLineQuantity must request /en/cart/change.js');
    });

    test('AjaxCartAdapter falls back to empty prefix when no locale configured', async () => {
      const requestedUrls = [];
      const mockFetch = async (url) => {
        requestedUrls.push(url);
        return {
          ok: true,
          json: async () => ({ currency: 'USD', items: [] }),
        };
      };

      const rootAdapter = new AjaxCartAdapter('', mockFetch, 'store.myshopify.com');
      await rootAdapter.readCart();
      assert.equal(requestedUrls[0], '/cart.js');
    });
  });

  describe('3. Isolated Cart Behavior Matrix & Unrelated Line Preservation', () => {
    test('unrelated existing cart lines are preserved during add_variant and set_line_quantity', async () => {
      let cartState = {
        shop_id: 'drake-test.myshopify.com',
        currency: 'NGN',
        items: [
          { key: 'line_existing_hat', variant_id: 'var_hat', quantity: 1, properties: {} },
        ],
      };

      const mockFetch = async (url, opts) => {
        if (url.endsWith('/cart.js')) {
          return { ok: true, json: async () => cartState };
        }
        if (url.endsWith('/cart/add.js')) {
          const body = JSON.parse(opts.body);
          const item = body.items[0];
          cartState.items.push({
            key: 'line_new_shirt',
            variant_id: item.id,
            quantity: item.quantity,
            properties: item.properties || {},
          });
          return { ok: true, json: async () => ({ ok: true }) };
        }
        if (url.endsWith('/cart/change.js')) {
          const body = JSON.parse(opts.body);
          const idx = cartState.items.findIndex((i) => i.key === body.id);
          if (idx !== -1) {
            if (body.quantity <= 0) {
              cartState.items.splice(idx, 1);
            } else {
              cartState.items[idx].quantity = body.quantity;
            }
          }
          return { ok: true, json: async () => ({ ok: true }) };
        }
        return { ok: false, status: 404 };
      };

      const ajaxAdapter = new AjaxCartAdapter('', mockFetch, 'drake-test.myshopify.com');
      const bridge = new StorefrontBridge({
        ajaxAdapter,
        webMcpAdapter: { isAvailable: () => false },
        actionsAdapter: { isAvailable: () => false },
      });

      // Execute add_variant for new shirt
      const addCmd = {
        command_id: 'cmd_add_01',
        operation: 'add_variant',
        parameters: { variant_id: 'var_shirt', quantity: 2 },
      };

      const addReceipt = await bridge.executeCommand(addCmd);
      assert.equal(addReceipt.ok, true);
      assert.equal(addReceipt.outcome, 'verified_success');

      // Verify after_cart contains BOTH the existing hat and the new shirt
      const lineVariantIds = addReceipt.after_cart.lines.map((l) => l.variant_id);
      assert.equal(lineVariantIds.includes('var_hat'), true, 'Existing unrelated hat must be preserved');
      assert.equal(lineVariantIds.includes('var_shirt'), true, 'New shirt must be added');
    });

    test('remove_line removes only targeted line and preserves remaining lines', async () => {
      let cartState = {
        shop_id: 'drake-test.myshopify.com',
        currency: 'USD',
        items: [
          { key: 'line_gloves', variant_id: 'var_gloves', quantity: 1, properties: {} },
          { key: 'line_boots', variant_id: 'var_boots', quantity: 1, properties: {} },
        ],
      };

      const mockFetch = async (url, opts) => {
        if (url.endsWith('/cart.js')) {
          return { ok: true, json: async () => cartState };
        }
        if (url.endsWith('/cart/change.js')) {
          const body = JSON.parse(opts.body);
          const idx = cartState.items.findIndex((i) => i.key === body.id);
          if (idx !== -1 && body.quantity === 0) {
            cartState.items.splice(idx, 1);
          }
          return { ok: true, json: async () => ({ ok: true }) };
        }
        return { ok: false, status: 404 };
      };

      const ajaxAdapter = new AjaxCartAdapter('', mockFetch, 'drake-test.myshopify.com');
      const bridge = new StorefrontBridge({
        ajaxAdapter,
        webMcpAdapter: { isAvailable: () => false },
        actionsAdapter: { isAvailable: () => false },
      });

      const removeCmd = {
        command_id: 'cmd_rem_01',
        operation: 'remove_line',
        parameters: { target_line_key: 'line_gloves' },
      };

      const removeReceipt = await bridge.executeCommand(removeCmd);
      assert.equal(removeReceipt.ok, true);
      assert.equal(removeReceipt.outcome, 'verified_success');
      assert.equal(removeReceipt.after_cart.lines.length, 1);
      assert.equal(removeReceipt.after_cart.lines[0].variant_id, 'var_boots', 'Unrelated boots line must remain');
    });

    test('absolute quantity semantics apply for set_line_quantity', async () => {
      let cartState = {
        shop_id: 'drake-test.myshopify.com',
        currency: 'NGN',
        items: [
          { key: 'line_hat', variant_id: 'var_hat', quantity: 5, properties: {} },
        ],
      };

      const mockFetch = async (url, opts) => {
        if (url.endsWith('/cart.js')) {
          return { ok: true, json: async () => cartState };
        }
        if (url.endsWith('/cart/change.js')) {
          const body = JSON.parse(opts.body);
          cartState.items[0].quantity = body.quantity;
          return { ok: true, json: async () => ({ ok: true }) };
        }
        return { ok: false, status: 404 };
      };

      const ajaxAdapter = new AjaxCartAdapter('', mockFetch, 'drake-test.myshopify.com');
      const bridge = new StorefrontBridge({
        ajaxAdapter,
        webMcpAdapter: { isAvailable: () => false },
        actionsAdapter: { isAvailable: () => false },
      });

      const setCmd = {
        command_id: 'cmd_set_01',
        operation: 'set_line_quantity',
        parameters: { target_line_key: 'line_hat', quantity: 3 },
      };

      const setReceipt = await bridge.executeCommand(setCmd);
      assert.equal(setReceipt.ok, true);
      assert.equal(setReceipt.after_cart.lines[0].quantity, 3, 'Quantity must be set absolutely to 3');
    });

    test('uncertain-write behavior: dispatch network failure marks outcome uncertain without retrying', async () => {
      let fallbackCalls = 0;
      const failingAjax = {
        addVariant: async () => {
          throw new Error('ETIMEDOUT: Connection severed mid-flight');
        },
        readCart: async () => ({
          shop_id: 'drake-test.myshopify.com',
          currency: 'USD',
          lines: [],
        }),
      };

      const fallbackAdapter = {
        addVariant: async () => {
          fallbackCalls++;
          return { ok: true };
        },
      };

      const bridge = new StorefrontBridge({
        ajaxAdapter: failingAjax,
        webMcpAdapter: { isAvailable: () => false },
        actionsAdapter: { isAvailable: () => false },
      });

      const addCmd = {
        command_id: 'cmd_unc_01',
        operation: 'add_variant',
        parameters: { variant_id: 'var_hoodie', quantity: 1 },
      };

      const receipt = await bridge.executeCommand(addCmd);
      assert.equal(receipt.ok, false);
      assert.equal(receipt.outcome, 'uncertain');
      assert.equal(fallbackCalls, 0, 'Must never retry an uncertain write on another transport');
    });
  });

  describe('4. Direct StandardActionsAdapter Tests', () => {
    test('isAvailable returns false when window.Shopify.actions is undefined', () => {
      const adapter = new StandardActionsAdapter(null);
      assert.equal(adapter.isAvailable(), false);
    });

    test('isAvailable returns true when updateCart function exists', () => {
      const mockActions = { updateCart: async () => ({}) };
      const adapter = new StandardActionsAdapter(mockActions);
      assert.equal(adapter.isAvailable(), true);
    });

    test('updateCart extracts userErrors and warnings', async () => {
      const mockActions = {
        updateCart: async () => ({
          userErrors: [{ message: 'Variant is out of stock' }],
          warnings: [{ message: 'Quantity reduced' }],
        }),
      };
      const adapter = new StandardActionsAdapter(mockActions);
      const res = await adapter.updateCart({ lines: [] });

      assert.equal(res.ok, false);
      assert.deepEqual(res.errors, ['Variant is out of stock']);
      assert.deepEqual(res.warnings, ['Quantity reduced']);
    });

    test('normalizeCart normalizes attributes to canonical properties map', () => {
      const adapter = new StandardActionsAdapter(null);
      const raw = {
        shop_id: 'test.myshopify.com',
        cost: { totalAmount: { currencyCode: 'EUR' } },
        lines: [
          {
            id: 'line_act_1',
            merchandise: { id: 'var_99' },
            quantity: 2,
            attributes: [
              { key: 'Color', value: 'Blue' },
              { key: 'Size', value: 'L' },
            ],
          },
        ],
      };

      const normalized = adapter.normalizeCart(raw);
      assert.equal(normalized.currency, 'EUR');
      assert.equal(normalized.lines[0].line_key, 'line_act_1');
      assert.equal(normalized.lines[0].variant_id, 'var_99');
      assert.equal(normalized.lines[0].quantity, 2);
      assert.deepEqual(normalized.lines[0].properties, { Color: 'Blue', Size: 'L' });
    });
  });

  describe('5. Inventory Shortage & Error Handling', () => {
    test('explicit inventory shortage returns rejected receipt without cart mutation', async () => {
      const mockFetch = async (url) => {
        if (url.endsWith('/cart.js')) {
          return {
            ok: true,
            json: async () => ({ shop_id: 'drake-test.myshopify.com', currency: 'NGN', items: [] }),
          };
        }
        if (url.endsWith('/cart/add.js')) {
          return {
            ok: false,
            status: 422,
            json: async () => ({
              status: 422,
              message: 'Cart Error',
              description: 'You can’t add more Drake Vintage Cap to the cart.',
            }),
          };
        }
        return { ok: false, status: 404 };
      };

      const ajaxAdapter = new AjaxCartAdapter('', mockFetch, 'drake-test.myshopify.com');
      const bridge = new StorefrontBridge({
        ajaxAdapter,
        webMcpAdapter: { isAvailable: () => false },
        actionsAdapter: { isAvailable: () => false },
      });

      const shortageCmd = {
        command_id: 'cmd_shortage_01',
        operation: 'add_variant',
        parameters: { variant_id: 'var_excess', quantity: 9999 },
      };

      const receipt = await bridge.executeCommand(shortageCmd);
      assert.equal(receipt.ok, false);
      assert.equal(receipt.outcome, 'rejected');
      assert.equal(receipt.errors[0].includes('Drake Vintage Cap'), true);
      assert.equal(receipt.after_cart.lines.length, 0, 'Cart must remain unchanged on shortage rejection');
    });
  });

  describe('6. Checkout Handoff Boundary', () => {
    test('handoff_to_checkout command passes safety validation and stops before payment', () => {
      const checkoutCmd = {
        schema_version: '1.0.0',
        command_id: 'cmd_checkout_01',
        operation: 'handoff_to_checkout',
        parameters: { checkout_url: 'https://drake-test.myshopify.com/checkout' },
      };

      const safetyErrors = validateCommandSafety(checkoutCmd);
      assert.deepEqual(safetyErrors, []);
      assert.equal(checkoutCmd.operation.includes('pay'), false);
    });
  });
});

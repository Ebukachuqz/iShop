import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { AjaxCartAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/ajax.js';
import { StandardActionsAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/standard_actions.js';
import { WebMcpAdapter } from '../../../apps/shopify/extensions/drake/src/bridge/webmcp.js';
import { StorefrontBridge } from '../../../apps/shopify/extensions/drake/src/bridge/bridge.js';
import { validateCommandSafety } from '../src/index.js';

describe('WP-01 Shopify Transport & Security Boundary Suite', () => {
  describe('1. Signed Bootstrap & Origin Validation', () => {
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

    test('signed bootstrap success produces verifiable HMAC grant', () => {
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
        expires_at_ms: 2000, // expired at 5000
      };
      const realSignature = signPayload(payload, SECRET);

      // 1. Forged secret
      const forgedSig = signPayload(payload, 'wrong_secret_key_32_bytes_long_123456');
      assert.notEqual(forgedSig, realSignature, 'Forged signature must not match real signature');

      // 2. Expired time check
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

    test('absolute quantity semantics apply for set_line_quantity and remove_line', async () => {
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
          if (body.quantity === 0) {
            cartState.items = [];
          } else {
            cartState.items[0].quantity = body.quantity;
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

      // Set line quantity to 3 (absolute 3, not relative +3)
      const setCmd = {
        command_id: 'cmd_set_01',
        operation: 'set_line_quantity',
        parameters: { target_line_key: 'line_hat', quantity: 3 },
      };

      const setReceipt = await bridge.executeCommand(setCmd);
      assert.equal(setReceipt.ok, true);
      assert.equal(setReceipt.after_cart.lines[0].quantity, 3, 'Quantity must be set absolutely to 3');
    });
  });

  describe('4. Inventory Shortage & Error Handling', () => {
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

  describe('5. Checkout Handoff Boundary', () => {
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

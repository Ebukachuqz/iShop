/**
 * @ishop/contracts/browser
 * Pure browser-safe contracts, canonical cart definitions, and validation utilities.
 * Completely free of Node.js built-ins (node:fs, node:path, node:url).
 */

export const SCHEMA_VERSION = '1.0.0';

export const ALLOWED_COMMAND_OPERATIONS = Object.freeze([
  'search_catalog',
  'browse_store',
  'get_product',
  'show_variant',
  'read_cart',
  'add_variant',
  'set_line_quantity',
  'remove_line',
  'navigate_storefront',
  'handoff_to_checkout',
]);

export const FORBIDDEN_OPERATIONS = Object.freeze([
  'payment',
  'pay',
  'charge',
  'submit_card',
  'place_order',
  'execute_script',
  'eval',
  'arbitrary_url',
  'clear_cart',
  'manage_orders',
]);

/**
 * Normalizes custom properties by sorting keys and preserving string values.
 */
export function normalizeProperties(properties) {
  if (!properties || typeof properties !== 'object') {
    return {};
  }
  const sorted = {};
  for (const key of Object.keys(properties).sort()) {
    sorted[key] = String(properties[key]);
  }
  return sorted;
}

/**
 * Computes canonical identity key for a cart line:
 * (variant_id, selling_plan_id, normalized_properties)
 */
export function canonicalLineKey(line) {
  const variantId = String(line.variant_id ?? '');
  const sellingPlanId = line.selling_plan_id ? String(line.selling_plan_id) : 'none';
  const normProps = normalizeProperties(line.properties);
  const propsKey = Object.entries(normProps)
    .map(([k, v]) => `${k}=${v}`)
    .join('&');
  return `${variantId}::${sellingPlanId}::${propsKey}`;
}

/**
 * Canonical cart multiset equality.
 * Two carts are equivalent if and only if:
 * 1. shop and currency match
 * 2. the multiset of (variant_id, selling_plan_id, normalized_properties, quantity) matches exactly.
 * Display order, ephemeral line keys, and cart tokens are ignored.
 */
export function isCartEquivalent(cartA, cartB) {
  if (!cartA || !cartB) return false;
  if (cartA.shop_id !== cartB.shop_id) return false;
  if (cartA.currency !== cartB.currency) return false;

  const multisetA = new Map();
  for (const line of cartA.lines || []) {
    const key = canonicalLineKey(line);
    const qty = Number(line.quantity || 0);
    multisetA.set(key, (multisetA.get(key) || 0) + qty);
  }

  const multisetB = new Map();
  for (const line of cartB.lines || []) {
    const key = canonicalLineKey(line);
    const qty = Number(line.quantity || 0);
    multisetB.set(key, (multisetB.get(key) || 0) + qty);
  }

  if (multisetA.size !== multisetB.size) return false;

  for (const [key, qtyA] of multisetA.entries()) {
    const qtyB = multisetB.get(key);
    if (qtyB !== qtyA) return false;
  }

  return true;
}

/**
 * Computes the expected CartSnapshot after applying a command to beforeCart (S-06, S-08, T-10).
 */
export function computeExpectedCart(beforeCart, command) {
  if (!beforeCart) return null;
  const shopId = beforeCart.shop_id;
  const currency = beforeCart.currency;
  const params = command?.parameters || {};
  const op = command?.operation;

  // Deep copy existing lines
  const lines = (beforeCart.lines || []).map((l) => ({
    variant_id: l.variant_id,
    quantity: Number(l.quantity || 0),
    selling_plan_id: l.selling_plan_id || null,
    properties: normalizeProperties(l.properties),
    line_key: l.line_key || l.id || null,
  }));

  if (op === 'clear_cart') {
    return { shop_id: shopId, currency, lines: [] };
  }

  const targetLineKey = params.target_line_key || params.line_key;
  const variantId = params.variant_id ? String(params.variant_id) : null;
  const targetQuantity = Number(params.quantity ?? 1);
  const targetSellingPlan = params.selling_plan_id || null;
  const targetProperties = normalizeProperties(params.properties);

  if (op === 'remove_line') {
    const filtered = lines.filter((l) => {
      if (targetLineKey && (l.line_key === targetLineKey || canonicalLineKey(l) === targetLineKey)) {
        return false;
      }
      if (variantId && String(l.variant_id) === variantId) {
        if (!targetLineKey || canonicalLineKey(l) === canonicalLineKey({ variant_id: variantId, selling_plan_id: targetSellingPlan, properties: targetProperties })) {
          return false;
        }
      }
      return true;
    });
    return { shop_id: shopId, currency, lines: filtered };
  }

  if (op === 'set_line_quantity' || op === 'update_line_quantity') {
    if (targetQuantity <= 0) {
      const filtered = lines.filter((l) => {
        if (targetLineKey && (l.line_key === targetLineKey || canonicalLineKey(l) === targetLineKey)) {
          return false;
        }
        if (variantId && String(l.variant_id) === variantId) {
          return false;
        }
        return true;
      });
      return { shop_id: shopId, currency, lines: filtered };
    }

    let updated = false;
    for (const l of lines) {
      if (
        (targetLineKey && (l.line_key === targetLineKey || canonicalLineKey(l) === targetLineKey)) ||
        (variantId && String(l.variant_id) === variantId && canonicalLineKey(l) === canonicalLineKey({ variant_id: variantId, selling_plan_id: targetSellingPlan, properties: targetProperties }))
      ) {
        l.quantity = targetQuantity;
        updated = true;
        break;
      }
    }
    if (!updated && variantId) {
      lines.push({
        variant_id: variantId,
        quantity: targetQuantity,
        selling_plan_id: targetSellingPlan,
        properties: targetProperties,
        line_key: params.shopify_line_key || null,
      });
    }
    return { shop_id: shopId, currency, lines };
  }

  if (op === 'add_variant' || op === 'add_line' || op === 'add_item') {
    const targetCanonical = canonicalLineKey({
      variant_id: variantId,
      selling_plan_id: targetSellingPlan,
      properties: targetProperties,
    });

    let existing = null;
    for (const l of lines) {
      if (canonicalLineKey(l) === targetCanonical) {
        existing = l;
        break;
      }
    }

    if (existing) {
      existing.quantity += targetQuantity;
    } else {
      lines.push({
        variant_id: variantId,
        quantity: targetQuantity,
        selling_plan_id: targetSellingPlan,
        properties: targetProperties,
        line_key: params.shopify_line_key || null,
      });
    }
    return { shop_id: shopId, currency, lines };
  }

  return { shop_id: shopId, currency, lines };
}

/**
 * Validates basic command safety rules (Safety S-01, S-02).
 */
export function validateCommandSafety(command) {
  const errors = [];
  if (!command || typeof command !== 'object') {
    return ['Command must be an object'];
  }

  if (command.schema_version !== SCHEMA_VERSION) {
    errors.push(`Unsupported schema_version: '${command.schema_version}'. Expected '${SCHEMA_VERSION}'`);
  }

  if (!ALLOWED_COMMAND_OPERATIONS.includes(command.operation)) {
    errors.push(`Operation '${command.operation}' is not an authorized closed command.`);
  }

  if (FORBIDDEN_OPERATIONS.includes(command.operation)) {
    errors.push(`Safety violation S-01: Operation '${command.operation}' is strictly forbidden.`);
  }

  return errors;
}

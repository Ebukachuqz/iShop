/**
 * @ishop/contracts
 * Core contracts, canonical cart definitions, and validation utilities.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

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
 * Loads a JSON schema by name from schema/ directory.
 */
export function loadSchema(schemaName) {
  const schemaPath = join(__dirname, '..', 'schema', `${schemaName}.json`);
  const content = readFileSync(schemaPath, 'utf-8');
  return JSON.parse(content);
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

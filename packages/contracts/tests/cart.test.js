import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  canonicalLineKey,
  isCartEquivalent,
  normalizeProperties,
} from '../src/index.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

test('canonical cart equivalence (T-25, S-08)', async (t) => {
  const fixturePath = join(__dirname, '..', 'fixtures', 'canonical_cart_variations.json');
  const fixtures = JSON.parse(readFileSync(fixturePath, 'utf-8'));

  await t.test('identical carts with different line ordering and unsorted properties are equivalent', () => {
    const isEq = isCartEquivalent(fixtures.cart_a, fixtures.cart_b_reordered_and_properties_unsorted);
    assert.equal(isEq, true, 'Cart multiset equivalence must ignore line order and property key order');
  });

  await t.test('carts with different subscription plans are NOT equivalent', () => {
    const isEq = isCartEquivalent(fixtures.cart_a, fixtures.cart_c_different_plan);
    assert.equal(isEq, false, 'Different selling_plan_id must not be merged or treated as equivalent');
  });

  await t.test('carts with different quantities are NOT equivalent', () => {
    const isEq = isCartEquivalent(fixtures.cart_a, fixtures.cart_d_different_quantity);
    assert.equal(isEq, false, 'Different quantities must not be equivalent');
  });

  await t.test('property normalization sorts keys and preserves strings', () => {
    const norm = normalizeProperties({ z: 1, a: 2, m: 'text' });
    assert.deepEqual(Object.keys(norm), ['a', 'm', 'z']);
    assert.equal(norm.z, '1');
    assert.equal(norm.a, '2');
  });
});

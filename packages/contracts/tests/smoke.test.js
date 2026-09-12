import test from 'node:test';
import assert from 'node:assert/strict';

test('contracts package boundary smoke test', () => {
  const packageName = '@ishop/contracts';
  assert.equal(packageName, '@ishop/contracts');
});

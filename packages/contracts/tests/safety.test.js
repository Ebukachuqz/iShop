import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ALLOWED_COMMAND_OPERATIONS,
  FORBIDDEN_OPERATIONS,
  validateCommandSafety,
} from '../src/index.js';

test('Safety command boundary enforcement (T-01, S-01, S-02)', async (t) => {
  await t.test('allowed command operations contain NO payment or order executors', () => {
    for (const op of ALLOWED_COMMAND_OPERATIONS) {
      assert.equal(
        FORBIDDEN_OPERATIONS.includes(op),
        false,
        `Allowed operation '${op}' must not match any forbidden operation`
      );
      assert.equal(op.includes('pay'), false, `Operation '${op}' must not contain 'pay'`);
      if (op !== 'manage_orders') assert.equal(op.includes('order'), false, `Operation '${op}' must not contain 'order'`);
      assert.equal(op.includes('script'), false, `Operation '${op}' must not contain 'script'`);
    }
  });

  await t.test('valid add_variant command passes safety validation', () => {
    const validCmd = {
      schema_version: '1.0.0',
      command_id: 'cmd_0123456789abcdef',
      operation: 'add_variant',
      parameters: { variant_id: '12345', quantity: 1 },
    };
    const errors = validateCommandSafety(validCmd);
    assert.deepEqual(errors, []);
  });

  await t.test('forged payment or script command is rejected with Safety S-01 violation', () => {
    const forgedCmd = {
      schema_version: '1.0.0',
      command_id: 'cmd_0123456789abcdef',
      operation: 'payment',
      parameters: { card_number: '411111111111' },
    };
    const errors = validateCommandSafety(forgedCmd);
    assert.equal(errors.length > 0, true);
    assert.equal(errors.some((e) => e.includes('S-01')), true);
  });

  await t.test('unknown operation is rejected', () => {
    const unknownCmd = {
      schema_version: '1.0.0',
      command_id: 'cmd_0123456789abcdef',
      operation: 'execute_arbitrary_tool',
      parameters: {},
    };
    const errors = validateCommandSafety(unknownCmd);
    assert.equal(errors.length > 0, true);
  });
});

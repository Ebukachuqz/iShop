import { test } from 'node:test';
import assert from 'node:assert';
import { resolvePythonRunner } from '../../../scripts/command-dispatcher.mjs';

test('R10 Regression: Command dispatcher must resolve uv run --frozen or .venv python before system python', () => {
  assert.strictEqual(
    typeof resolvePythonRunner,
    'function',
    'command-dispatcher.mjs must export resolvePythonRunner'
  );

  // When uv is available, it must use uv run --frozen python
  const uvMock = (cmd) => cmd === 'uv' ? { status: 0 } : { status: 1 };
  const resUv = resolvePythonRunner(['-m', 'pytest', 'tests'], {
    spawnCheck: uvMock,
    existsSync: () => false,
  });
  assert.strictEqual(resUv.cmd, 'uv');
  assert.deepStrictEqual(resUv.args.slice(0, 3), ['run', '--frozen', 'python']);

  // When uv is missing but .venv exists, it must use the .venv interpreter
  const venvMockExists = (path) => path.includes('.venv');
  const resVenv = resolvePythonRunner(['-m', 'pytest', 'tests'], {
    spawnCheck: () => ({ status: 1 }),
    existsSync: venvMockExists,
  });
  assert.match(resVenv.cmd, /[\\/]\.venv[\\/]/);
  assert.deepStrictEqual(resVenv.args, ['-m', 'pytest', 'tests']);
});

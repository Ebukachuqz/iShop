import { test } from 'node:test';
import assert from 'node:assert';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

test('R8 Regression: Browser bridge and browser contracts must not depend on Node built-ins', async () => {
  const browserContractsPath = join(__dirname, '..', 'src', 'browser.js');
  const bridgePath = join(__dirname, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'src', 'bridge', 'bridge.js');
  const deployedContractsPath = join(__dirname, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'assets', 'drake-browser-contracts.js');

  // 1. packages/contracts/src/browser.js must exist
  assert.strictEqual(
    existsSync(browserContractsPath),
    true,
    'packages/contracts/src/browser.js must exist as a dedicated browser-safe module'
  );

  // 2. browser.js must not contain any node: imports
  const browserCode = readFileSync(browserContractsPath, 'utf-8');
  const deployedBrowserCode = readFileSync(deployedContractsPath, 'utf-8');
  assert.strictEqual(
    deployedBrowserCode,
    browserCode,
    'The deployed browser contract asset must match packages/contracts/src/browser.js exactly',
  );
  assert.strictEqual(
    /from\s+['"]node:/.test(browserCode),
    false,
    'browser.js must not import any node:* built-in modules'
  );

  // 3. bridge.js must import from browser.js or browser contracts, never index.js
  const bridgeCode = readFileSync(bridgePath, 'utf-8');
  assert.strictEqual(
    bridgeCode.includes('src/index.js'),
    false,
    'bridge.js must import from browser-safe contracts (browser.js), never Node index.js'
  );
  assert.strictEqual(
    /from\s+['"]node:/.test(bridgeCode),
    false,
    'bridge.js must not import any node:* built-in modules'
  );

  // 4. browser.js exports must include required pure contract functions
  const browserMod = await import('../src/browser.js');
  assert.strictEqual(typeof browserMod.isCartEquivalent, 'function');
  assert.strictEqual(typeof browserMod.canonicalLineKey, 'function');
  assert.strictEqual(typeof browserMod.normalizeProperties, 'function');
  assert.strictEqual(typeof browserMod.validateCommandSafety, 'function');
  assert.strictEqual(typeof browserMod.loadSchema, 'undefined', 'loadSchema requires node:fs and must not be in browser.js');
});

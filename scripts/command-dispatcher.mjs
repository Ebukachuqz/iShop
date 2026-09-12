#!/usr/bin/env node
/**
 * Cross-platform Command Dispatcher for iShop (Drake)
 * Implements the command contract defined in docs/DEVELOPMENT.md.
 * Ensures future suites fail with an actionable "not implemented" error rather than a false pass.
 */

import { spawnSync } from 'node:child_process';
import process from 'node:process';

const command = process.argv[2];
const args = process.argv.slice(3);

function run(cmd, cmdArgs) {
  const result = spawnSync(cmd, cmdArgs, {
    stdio: 'inherit',
    shell: true,
  });
  if (result.error) {
    console.error(`Execution error running ${cmd}:`, result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

function notImplemented(commandName, ownerPackage, plannedDoc) {
  console.error(`\n[iShop Command Contract] Error: '${commandName}' is not yet implemented.`);
  console.error(`Owner package: ${ownerPackage}`);
  console.error(`Specification: docs/plans/work-packages/${plannedDoc}`);
  console.error(`Status: Implementation will arrive with ${ownerPackage}. A false-pass is prohibited by SAFETY S-13.\n`);
  process.exit(1);
}

switch (command) {
  case 'bootstrap': {
    console.log('[iShop] Bootstrapping workspace dependencies...');
    // Run pnpm install for Node workspace
    console.log('[iShop] Installing Node.js workspace dependencies...');
    run('pnpm', ['install']);
    // Verify Python environment
    console.log('[iShop] Verifying Python runtime environment...');
    run('python', ['-m', 'pip', 'install', '-e', 'services/runtime']);
    console.log('[iShop] Bootstrap complete.');
    break;
  }

  case 'dev': {
    notImplemented('dev', 'WP-01 & WP-07', 'WP-01.md');
    break;
  }

  case 'check': {
    console.log('[iShop] Running workspace validation checks...');
    console.log('--- Check 1: Documentation link & authority integrity ---');
    run('python', ['scripts/check-docs.py']);

    console.log('\n--- Check 2: Secret exclusions & private data integrity ---');
    run('python', ['scripts/check-secrets.py']);

    console.log('\n--- Check 3: Architecture and package boundaries ---');
    run('python', ['scripts/check-boundaries.py']);

    console.log('\n[iShop] All workspace checks passed.');
    break;
  }

  case 'test:unit': {
    console.log('[iShop] Running unit smoke and domain tests...');
    run('python', ['-m', 'pytest', 'services/runtime/tests', '-v']);
    break;
  }

  case 'test:integration': {
    notImplemented('test:integration', 'WP-04', 'WP-04.md');
    break;
  }

  case 'test:e2e': {
    notImplemented('test:e2e', 'WP-10 & WP-11', 'WP-11.md');
    break;
  }

  case 'test:live': {
    notImplemented('test:live', 'WP-01 & WP-02', 'WP-01.md');
    break;
  }

  case 'eval:validate': {
    notImplemented('eval:validate', 'WP-06', 'WP-06.md');
    break;
  }

  case 'eval:run': {
    notImplemented('eval:run', 'WP-06 & WP-08', 'WP-06.md');
    break;
  }

  case 'eval:report': {
    notImplemented('eval:report', 'WP-06 & WP-12', 'WP-12.md');
    break;
  }

  default: {
    console.error(`[iShop] Unknown command: '${command}'`);
    console.error('Available commands: bootstrap, dev, check, test:unit, test:integration, test:e2e, test:live, eval:validate, eval:run, eval:report');
    process.exit(1);
  }
}

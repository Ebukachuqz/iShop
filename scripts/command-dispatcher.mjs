#!/usr/bin/env node
/**
 * Cross-platform Command Dispatcher for iShop (Drake)
 * Implements the command contract defined in docs/DEVELOPMENT.md.
 * Ensures future suites fail with an actionable "not implemented" error rather than a false pass.
 * Ensures robust failure propagation across all checks and test suites.
 */

import { spawnSync } from 'node:child_process';
import process from 'node:process';

const command = process.argv[2];
const args = process.argv.slice(3);
const isCI = args.includes('--ci') || process.env.CI === 'true';

let overallExitCode = 0;

function runStep(name, cmd, cmdArgs) {
  console.log(`\n--- Running: ${name} ---`);
  const result = spawnSync(cmd, cmdArgs, {
    stdio: 'inherit',
    shell: true,
  });

  if (result.error) {
    console.error(`[Error] Execution failure in ${name}:`, result.error.message);
    overallExitCode = 1;
    return false;
  }

  const code = result.status ?? 1;
  if (code !== 0) {
    console.error(`[Failure] ${name} exited with status code ${code}`);
    overallExitCode = 1;
    return false;
  }

  console.log(`[Success] ${name} passed.`);
  return true;
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
    console.log('[iShop] Bootstrapping workspace dependencies with reproducible locks...');
    // 1. Pnpm workspace dependencies
    runStep('pnpm install', 'pnpm', ['install']);
    // 2. Python dependencies via uv sync
    runStep('uv sync', 'python', ['-m', 'uv', 'sync']);

    if (overallExitCode !== 0) {
      console.error('\n[iShop] Bootstrap failed.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] Bootstrap complete and locked.');
    break;
  }

  case 'dev': {
    notImplemented('dev', 'WP-01 & WP-07', 'WP-01.md');
    break;
  }

  case 'check': {
    console.log(`[iShop] Running workspace validation checks (mode: ${isCI ? 'CI' : 'local'})...`);

    // Check 1: Documentation integrity (passes --ci if in CI mode)
    const docsArgs = ['scripts/check-docs.py'];
    if (isCI) {
      docsArgs.push('--ci');
    }
    runStep('Documentation & Mapping Integrity', 'python', docsArgs);

    // Check 2: Secret exclusions & private data integrity
    runStep('Secret Exclusions & Privacy Audit', 'python', ['scripts/check-secrets.py']);

    // Check 3: Architecture and package boundaries
    runStep('Architectural Boundaries Audit', 'python', ['scripts/check-boundaries.py']);

    if (overallExitCode !== 0) {
      console.error('\n[iShop] One or more workspace validation checks FAILED.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] All workspace checks passed.');
    break;
  }

  case 'test:unit': {
    console.log('[iShop] Running offline unit and domain tests across JavaScript and Python...');

    // 1. JavaScript tests (Node.js test runner)
    runStep(
      'JavaScript Unit & Contract Tests',
      'node',
      ['--test', 'packages/contracts/tests']
    );

    // 2. Python tests (pytest discovering services/runtime and negative harness tests)
    runStep(
      'Python Unit & Domain Tests',
      'python',
      ['-m', 'pytest', 'services/runtime/tests', 'tests', '-v']
    );

    if (overallExitCode !== 0) {
      console.error('\n[iShop] One or more unit test suites FAILED.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] All unit tests passed.');
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

#!/usr/bin/env node
/**
 * Cross-platform Command Dispatcher for iShop (Drake)
 * Implements the command contract defined in docs/DEVELOPMENT.md.
 * Ensures future suites fail with an actionable "not implemented" error rather than a false pass.
 * Ensures robust failure propagation across all checks and test suites.
 */

import { spawnSync } from 'node:child_process';
import process from 'node:process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const command = process.argv[2];
const args = process.argv.slice(3);
const isCI = args.includes('--ci') || process.env.CI === 'true';

let overallExitCode = 0;

export function resolvePythonRunner(pythonArgs, overrides = {}) {
  const spawnCheck = overrides.spawnCheck || ((cmd, checkArgs) => spawnSync(cmd, checkArgs, { shell: true }));
  const checkExists = overrides.existsSync || existsSync;

  // 1. If uv is installed (e.g. in CI or developer machine), run via uv run --frozen
  try {
    const uvCheck = spawnCheck('uv', ['--version']);
    if (uvCheck && uvCheck.status === 0) {
      return {
        cmd: 'uv',
        args: ['run', '--frozen', 'python', ...pythonArgs],
      };
    }
  } catch {
    // Ignore and proceed to check virtualenv
  }

  // 2. If .venv exists in workspace, use its python
  const venvWin = join(process.cwd(), '.venv', 'Scripts', 'python.exe');
  const venvUnix = join(process.cwd(), '.venv', 'bin', 'python');
  if (checkExists(venvWin)) {
    return { cmd: venvWin, args: pythonArgs };
  }
  if (checkExists(venvUnix)) {
    return { cmd: venvUnix, args: pythonArgs };
  }

  // 3. Fallback to system python
  return { cmd: 'python', args: pythonArgs };
}

export function runStep(name, cmd, cmdArgs) {
  console.log(`\n--- Running: ${name} ---`);
  const pathSep = process.platform === 'win32' ? ';' : ':';
  const customPythonPath = ['services/runtime/src', '.', process.env.PYTHONPATH]
    .filter(Boolean)
    .join(pathSep);

  const result = spawnSync(cmd, cmdArgs, {
    stdio: 'inherit',
    shell: true,
    env: {
      ...process.env,
      PYTHONPATH: customPythonPath,
    },
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

export function runPythonStep(name, pythonArgs) {
  const resolved = resolvePythonRunner(pythonArgs);
  return runStep(name, resolved.cmd, resolved.args);
}

function notImplemented(commandName, ownerPackage, plannedDoc) {
  console.error(`\n[iShop Command Contract] Error: '${commandName}' is not yet implemented.`);
  console.error(`Owner package: ${ownerPackage}`);
  console.error(`Specification: docs/plans/work-packages/${plannedDoc}`);
  console.error(`Status: Implementation will arrive with ${ownerPackage}. A false-pass is prohibited by SAFETY S-13.\n`);
  process.exit(1);
}

import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const currentFilePath = fileURLToPath(import.meta.url);
const executedFilePath = process.argv[1] ? resolve(process.argv[1]) : '';
const isMainModule = executedFilePath === currentFilePath;

if (isMainModule) {
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

    runStep(
      'Shopify App Type Check',
      'pnpm',
      ['--filter', '@ishop/shopify-app', 'typecheck']
    );

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
      ['--test', 'packages/contracts/tests/*.test.js']
    );

    runStep(
      'Shopify App Unit Tests',
      'pnpm',
      ['--filter', '@ishop/shopify-app', 'test']
    );

    // 2. Python tests (pytest discovering services/runtime and negative harness tests)
    runPythonStep(
      'Python Unit & Domain Tests',
      ['-m', 'pytest', 'services/runtime/tests', 'tests', 'evals/tests', '-v']
    );

    if (overallExitCode !== 0) {
      console.error('\n[iShop] One or more unit test suites FAILED.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] All unit tests passed.');
    break;
  }

  case 'test:integration': {
    console.log('[iShop] Running deterministic integration tests across JavaScript and Python...');

    // 1. JavaScript bridge and adapter parity integration tests
    runStep(
      'JavaScript Storefront Bridge Integration Tests',
      'node',
      ['--test', 'packages/contracts/tests/bridge.test.js']
    );

    // 2. Python commerce catalog resolution and cart reconciliation integration tests
    runPythonStep(
      'Python Commerce & Reconciliation Integration Tests',
      ['-m', 'pytest', 'services/runtime/tests/test_commerce.py', '-v']
    );

    if (overallExitCode !== 0) {
      console.error('\n[iShop] One or more integration test suites FAILED.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] All integration tests passed.');
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
    console.log('[iShop] Validating evaluation manifest...');
    runPythonStep(
      'Evaluation Manifest Validation',
      ['-m', 'evals.cli.validate', ...args]
    );

    if (overallExitCode !== 0) {
      console.error('\n[iShop] Manifest validation FAILED.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] Manifest validation passed.');
    break;
  }

  case 'eval:run': {
    console.log('[iShop] Running evaluation suite...');
    runPythonStep(
      'Evaluation Run Execution',
      ['-m', 'evals.cli.run', ...args]
    );

    if (overallExitCode !== 0) {
      console.error('\n[iShop] Evaluation run FAILED.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] Evaluation run completed.');
    break;
  }

  case 'eval:asr': {
    console.log('[iShop] Running the frozen ASR panel...');
    runPythonStep('ASR Panel Evaluation', ['-m', 'evals.cli.asr_benchmark', ...args]);
    if (overallExitCode !== 0) {
      console.error('\n[iShop] ASR panel evaluation failed.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] ASR panel evaluation completed.');
    break;
  }

  case 'eval:report': {
    console.log('[iShop] Generating evaluation report...');
    runPythonStep(
      'Evaluation Report Generation',
      ['-m', 'evals.cli.report', ...args]
    );

    if (overallExitCode !== 0) {
      console.error('\n[iShop] Evaluation report generation FAILED.');
      process.exit(overallExitCode);
    }
    console.log('\n[iShop] Evaluation report generated.');
    break;
  }

  default: {
    console.error(`[iShop] Unknown command: '${command}'`);
    console.error('Available commands: bootstrap, dev, check, test:unit, test:integration, test:e2e, test:live, eval:validate, eval:run, eval:asr, eval:report');
    process.exit(1);
  }
}
}

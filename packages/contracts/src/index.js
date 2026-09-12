/**
 * @ishop/contracts
 * Core contracts, canonical cart definitions, and validation utilities.
 * Re-exports browser-safe contracts and provides Node.js schema loaders.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Re-export all browser-safe contracts
export * from './browser.js';

/**
 * Loads a JSON schema by name from schema/ directory (Node.js only).
 */
export function loadSchema(schemaName) {
  const schemaPath = join(__dirname, '..', 'schema', `${schemaName}.json`);
  const content = readFileSync(schemaPath, 'utf-8');
  return JSON.parse(content);
}


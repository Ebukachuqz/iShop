import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { syncCartIndicators } from '../../../apps/shopify/extensions/drake/assets/drake-bridge.js';

function element(className = '') {
  const node = {
    className,
    children: [],
    textContent: '',
    hidden: false,
    removed: false,
    attributes: {},
    append(...children) { this.children.push(...children); },
    remove() { this.removed = true; },
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelector(selector) {
      return selector === '.cart-count-bubble'
        ? this.children.find((child) => child.className === 'cart-count-bubble' && !child.removed) || null
        : null;
    },
    querySelectorAll(selector) { return selector === 'span' ? this.children : []; },
  };
  return node;
}

test('verified cart observations refresh the Shopify theme cart badge without reload', () => {
  const icon = element();
  const genericCount = element('cart-count');
  const documentRef = {
    querySelector: (selector) => selector === '#cart-icon-bubble' ? icon : null,
    querySelectorAll: (selector) => selector === '[data-cart-count], .cart-count' ? [genericCount] : [],
    createElement: () => element(),
  };

  syncCartIndicators({ lines: [{ quantity: 2 }, { quantity: 1 }] }, documentRef);
  const bubble = icon.querySelector('.cart-count-bubble');
  assert.ok(bubble);
  assert.equal(bubble.children[0].textContent, '3');
  assert.equal(bubble.children[1].textContent, '3 items');
  assert.equal(genericCount.textContent, '3');
  assert.equal(genericCount.hidden, false);

  syncCartIndicators({ lines: [] }, documentRef);
  assert.equal(bubble.removed, true);
  assert.equal(genericCount.textContent, '0');
  assert.equal(genericCount.hidden, true);
});

test('open widget has a larger desktop viewport and optional edge resizing', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const css = readFileSync(join(here, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'assets', 'drake-bootstrap.css'), 'utf8');
  assert.match(css, /width:\s*min\(34rem,/);
  assert.match(css, /height:\s*min\(46rem,/);
  assert.match(css, /resize:\s*both/);
});

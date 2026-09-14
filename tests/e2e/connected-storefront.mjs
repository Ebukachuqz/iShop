import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..', '..');
const python = join(root, '.venv', 'Scripts', 'python.exe');
const chrome = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const profile = mkdtempSync(join(tmpdir(), 'ishop-e2e-'));
const stateDb = join(profile, 'state.sqlite3');
function spawnRuntime() {
  return spawn(python, [join(root, 'tests', 'e2e', 'runtime_server.py')], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: `${join(root, 'services', 'runtime', 'src')};${root}`, ISHOP_E2E_STATE_DB: stateDb },
    stdio: ['ignore', 'inherit', 'inherit'],
  });
}
let runtime = spawnRuntime();
const browser = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-debugging-port=9225', `--user-data-dir=${profile}`], { stdio: 'ignore' });

const delay = (ms) => new Promise((resolveDelay) => setTimeout(resolveDelay, ms));
async function waitFor(fn, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { try { const value = await fn(); if (value) return value; } catch {} await delay(100); }
  throw new Error('Timed out waiting for connected browser state');
}

let socket;
let nextId = 1;
const pending = new Map();
async function cdp(method, params = {}) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolveCall, rejectCall) => pending.set(id, { resolveCall, rejectCall }));
}
async function evaluate(expression) {
  const response = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text);
  return response.result.result.value;
}

async function submit(text, expectedState = 'ready') {
  await evaluate(`document.querySelector('.drake-text-input').value=${JSON.stringify(text)}; document.querySelector('.drake-send').click(); true`);
  if (expectedState) {
    try {
      await waitFor(() => evaluate(`document.querySelector('#ishop-drake-root')?.dataset.state === ${JSON.stringify(expectedState)}`));
    } catch (error) {
      throw new Error(`${error.message} after ${JSON.stringify(text)} expected ${expectedState}; cart=${JSON.stringify(await evaluate("fetch('/cart.js').then(r=>r.json())"))}: ${await evaluate("document.body.innerText")}`);
    }
  }
}

let assertions = 0;
function check(value, expected, message) {
  assert.equal(value, expected, message);
  assertions += 1;
}

try {
  await waitFor(async () => (await fetch('http://127.0.0.1:8765/')).ok);
  await waitFor(async () => (await fetch('http://127.0.0.1:9225/json/version')).ok);
  const target = await (await fetch(`http://127.0.0.1:9225/json/new?${encodeURIComponent('http://127.0.0.1:8765/')}`, { method: 'PUT' })).json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolveOpen, rejectOpen) => { socket.onopen = resolveOpen; socket.onerror = rejectOpen; });
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    const waiter = pending.get(message.id);
    if (waiter) { pending.delete(message.id); message.error ? waiter.rejectCall(new Error(message.error.message)) : waiter.resolveCall(message); }
  };
  await cdp('Runtime.enable');
  await waitFor(() => evaluate("document.querySelector('#ishop-drake-root')?.dataset.bootstrapState === 'ready'"));
  await waitFor(() => evaluate("Boolean(window.IShopDrake?.client && window.IShopDrake?.bridge)"));
  await evaluate("document.querySelector('.drake-launcher').click(); true");
  await submit('Find snowboard');
  try {
    await waitFor(() => evaluate("document.querySelector('.drake-conversation').innerText.includes('Complete Snowboard')"));
  } catch (error) {
    throw new Error(`${error.message}: ${await evaluate("document.body.innerText")}`);
  }
  check(await evaluate("document.querySelectorAll('.drake-card').length"), 3, 'search result cards');
  check(await evaluate("[...document.querySelectorAll('.drake-card__variant')].some(n=>n.innerText.includes('699.95 USD'))"), true, 'decimal prices');

  runtime.kill();
  await new Promise((resolveExit) => runtime.once('exit', resolveExit));
  runtime = spawnRuntime();
  await waitFor(async () => (await fetch('http://127.0.0.1:8765/')).ok);
  await submit('Which is cheapest in your search?');
  check(await evaluate("document.querySelector('.drake-card__title').innerText.includes('Multi-managed Snowboard')"), true, 'result context restored after runtime restart');

  check(await evaluate("document.querySelector('.drake-conversation').innerText.includes('Find snowboard')"), true, 'visible history survives runtime reconnect');
  await submit('Which is closest to $700?');
  check(await evaluate("document.querySelector('.drake-card__title').innerText.includes('Complete Snowboard')"), true, 'nearest-price ranking');

  await submit('Browse the store collections');
  check(await evaluate("document.querySelector('.drake-conversation').innerText.includes('Snowboards')"), true, 'collection browse');
  await submit('Describe the Complete Snowboard');
  check(await evaluate("document.querySelector('.drake-conversation').innerText.includes('available in Color')"), true, 'product details');

  await submit('Add the Complete Snowboard to my cart', 'clarifying');
  check(await evaluate("document.querySelector('.drake-conversation').innerText.includes('Ice')"), true, 'variant clarification');
  await submit('Ice', 'completed');
  let cart = await evaluate("fetch('/cart.js').then(r=>r.json())");
  check(cart.items.length, 1, 'one cart line after clarified add');
  check(String(cart.items[0].variant_id), '101', 'clarified exact variant');
  check(cart.items[0].quantity, 1, 'initial quantity');
  check(await evaluate("document.querySelector('#cart-icon-bubble .cart-count-bubble span[aria-hidden=true]').textContent"), '1', 'theme cart badge refreshes without reload');

  await submit('Add two Ice Complete Snowboard to my cart', 'completed');
  cart = await evaluate("fetch('/cart.js').then(r=>r.json())");
  check(cart.items[0].quantity, 3, 'increment quantity');
  check(await evaluate("document.querySelector('#cart-icon-bubble .cart-count-bubble span[aria-hidden=true]').textContent"), '3', 'theme cart badge reflects quantity increment');
  await submit('Set quantity to 2 for the Ice Complete Snowboard', 'completed');
  cart = await evaluate("fetch('/cart.js').then(r=>r.json())");
  check(cart.items[0].quantity, 2, 'absolute quantity');
  await submit('Show cart');
  check(await evaluate("document.querySelector('.drake-conversation').innerText.includes('2 items')"), true, 'authoritative cart read');
  await submit('Remove the Ice Complete Snowboard from my cart', 'completed');
  cart = await evaluate("fetch('/cart.js').then(r=>r.json())");
  check(cart.items.length, 0, 'line removal');
  check(await evaluate("document.querySelector('#cart-icon-bubble .cart-count-bubble')"), null, 'empty cart removes theme badge');

  await submit('Add the Complete Snowboard to my cart', 'clarifying');
  await submit('Cancel that');
  await submit('Ice');
  cart = await evaluate("fetch('/cart.js').then(r=>r.json())");
  check(cart.items.length, 0, 'cancelled clarification cannot revive mutation');

  await submit('Add one Ice Complete Snowboard to my cart', 'completed');
  await submit('Empty my entire cart', 'completed');
  cart = await evaluate("fetch('/cart.js').then(r=>r.json())");
  check(cart.items.length, 0, 'explicit whole-cart clear');

  await submit('Preview the Ice Complete Snowboard', null);
  await waitFor(() => evaluate("location.pathname === '/products/complete-snowboard' && location.search === '?variant=101'"));
  await waitFor(() => evaluate("document.querySelector('#ishop-drake-root')?.dataset.bootstrapState === 'ready'"));
  check(await evaluate("document.querySelector('.drake-conversation').innerText.includes('Empty my entire cart')"), true, 'bounded recent conversation restored after navigation document');
  check(await evaluate("window.IShopDrake.pageContext.previous_path === '/'"), true, 'previous page recorded');
  await waitFor(() => evaluate("Boolean(window.IShopDrake?.client && window.IShopDrake?.bridge)"));
  await evaluate("document.querySelector('.drake-launcher').click(); true");
  await submit('Add this product to my cart', 'clarifying');
  await submit('Ice', 'completed');
  cart = await evaluate("fetch('/cart.js').then(r=>r.json())");
  check(String(cart.items[0].variant_id), '101', 'current-page reference add');

  await submit('What is your return policy?', 'failed');
  check(await evaluate("document.querySelector('.drake-error').innerText.includes('safely choose') || document.querySelector('.drake-error').innerText.includes('unavailable')"), true, 'policy capability unavailable is honest');

  await submit('Show my order history', null);
  await waitFor(() => evaluate("location.pathname === '/account'"));
  check(await evaluate("location.pathname"), '/account', 'trusted order history handoff');
  await cdp('Page.navigate', { url: 'http://127.0.0.1:8765/' });
  await waitFor(() => evaluate("document.querySelector('#ishop-drake-root')?.dataset.bootstrapState === 'ready'"));
  await waitFor(() => evaluate("Boolean(window.IShopDrake?.client && window.IShopDrake?.bridge)"));
  await evaluate("document.querySelector('.drake-launcher').click(); true");
  await submit('Take me to checkout', null);
  await waitFor(() => evaluate("location.pathname === '/checkout'"));
  check(await evaluate("document.body.innerText.includes('no payment capability')"), true, 'checkout handoff stops before payment');

  console.log(JSON.stringify({ tests: assertions, passed: assertions, browser: 'chrome-headless', runtime: 'actual-fastapi-controller', shopify_boundary: 'strict-synthetic' }));
} finally {
  try { socket?.close(); } catch {}
  runtime.kill();
  browser.kill();
  await delay(200);
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = fileURLToPath(new URL('.', import.meta.url));
const source = readFileSync(join(here, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'assets', 'drake-voice.js'), 'utf8');
const widgetSource = readFileSync(join(here, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'assets', 'drake-widget.js'), 'utf8');

function loadVoice() {
  const window = {};
  class FakeAudio {
    constructor(src) {
      this.src = src;
      this.onended = null;
      this.onerror = null;
    }
    play() { return Promise.resolve(); }
    pause() {}
  }
  const MockURL = function (url, base) { return new URL(url, base); };
  MockURL.createObjectURL = () => 'blob:mock';
  MockURL.revokeObjectURL = () => {};
  vm.runInNewContext(source, {
    window,
    document: { getElementById: () => null },
    URL: MockURL,
    Blob,
    Audio: FakeAudio,
    AudioBuffer: undefined,
  });
  return window.IShopVoiceSession;
}

function loadWidgetState() {
  const window = {};
  vm.runInNewContext(widgetSource, {
    window,
    document: { getElementById: () => null },
  });
  return window.IShopDrakeWidget;
}

test('voice widget exposes provider-neutral session and PCM utilities', () => {
  const voice = loadVoice();
  assert.equal(typeof voice.VoiceSessionClient, 'function');
  assert.equal(typeof voice.PcmCapture, 'function');
  assert.equal(typeof voice.AudioPlayback, 'function');
  const pcm = new Int16Array(512);
  const converted = voice.floatToPcm16(new Float32Array(512).fill(0.5));
  assert.equal(converted.byteLength, pcm.byteLength);
  assert.equal(new Int16Array(converted)[0], 16383);
  assert.equal(
    voice.buildVoiceWebSocketUrl('wss://runtime.example/ws', 'shop.myshopify.com'),
    'wss://runtime.example/ws/voice/shop.myshopify.com',
  );
  assert.throws(
    () => voice.buildVoiceWebSocketUrl('ws://runtime.example/ws', 'shop.myshopify.com'),
    /voice_url_requires_tls/,
  );
});

test('microphone capture emits provider-sized PCM chunks', () => {
  const voice = loadVoice();
  const chunks = [];
  const capture = new voice.PcmCapture({ onChunk: (chunk) => chunks.push(chunk), chunkBytes: 1024 });
  capture._buffer(new Uint8Array(256).buffer);
  capture._buffer(new Uint8Array(768).buffer);
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].byteLength, 1024);

  capture._buffer(new Uint8Array(256).buffer);
  capture.stop(true);
  assert.equal(chunks.length, 2);
  assert.equal(chunks[1].byteLength, 1024);
});

test('voice widget sends authenticated turns and suppresses duplicate finals', async () => {
  const voice = loadVoice();
  const sockets = [];
  class FakeWebSocket {
    constructor() {
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
      queueMicrotask(() => { this.readyState = 1; this.onopen(); });
    }
    send(value) { this.sent.push(value); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  }
  const events = [];
  const client = new voice.VoiceSessionClient({
    url: 'wss://runtime.example/ws/voice/shop.myshopify.com',
    grant: { grant_id: 'grant_1' },
    WebSocket: FakeWebSocket,
    onEvent: (event) => events.push(event),
  });
  await client.startTurn({ revision: 1 });
  client.sendAudio(new Uint8Array(1024));
  client.finishTurn();
  const socket = sockets[0];
  socket.onmessage({ data: JSON.stringify({ type: 'partial_transcript', revision: 1, text: 'add medium' }) });
  socket.onmessage({ data: JSON.stringify({ type: 'final_transcript', revision: 1, text: 'add large' }) });
  socket.onmessage({ data: JSON.stringify({ type: 'final_transcript', revision: 1, text: 'add large' }) });
  assert.deepEqual(JSON.parse(socket.sent[0]), { type: 'authenticate', grant: { grant_id: 'grant_1' } });
  assert.deepEqual(JSON.parse(socket.sent[1]).type, 'start_turn');
  assert.deepEqual(JSON.parse(socket.sent[3]), { type: 'finish_turn' });
  assert.equal(events.filter((event) => event.type === 'final_transcript').length, 1);
  assert.equal(events[0].authorizes_interpretation, undefined);
});

test('voice client rejects an incompatible runtime before sending shopping work', () => {
  const voice = loadVoice();
  const events = [];
  const client = new voice.VoiceSessionClient({ url: 'wss://runtime.example/ws', grant: {}, onEvent: (event) => events.push(event) });
  let closed = false;
  client.socket = { close: () => { closed = true; }, readyState: 1 };
  client._receive(JSON.stringify({ type: 'authenticated', protocol_version: '0.9.0', command_schema_version: '1.0.0', tool_registry_version: '1.0.0' }));
  assert.equal(closed, true);
  assert.equal(events[0].error_code, 'incompatible_runtime');
});

test('widget state distinguishes listening, interpretation, failure, and verified completion', () => {
  const { WidgetState } = loadWidgetState();
  const state = new WidgetState();
  assert.equal(state.transition('listening').label, 'Listening');
  state.setPartial('add medium');
  assert.equal(state.snapshot().partial, 'add medium');
  assert.equal(state.acceptFinal('add large').name, 'interpreting');
  assert.equal(state.transition('failed', { error: 'Try again' }).error, 'Try again');
  assert.throws(() => state.transition('completed'), /verified_receipt_required/);
  assert.equal(
    state.transition('completed', { verifiedReceipt: { command_id: 'command_1' } }).name,
    'completed',
  );
  assert.equal(state.transition('failed', { suppressError: true, error: 'Already shown in chat' }).error, '');
});

test('candidate references remain stable and reject unknown ordinals', () => {
  const { CandidateSet } = loadWidgetState();
  const candidates = new CandidateSet([
    { product_id: 'p1', title: 'Black Shirt' },
    { product_id: 'p2', title: 'Blue Shirt' },
  ]);
  assert.equal(candidates.resolve(2).product_id, 'p2');
  assert.throws(() => candidates.resolve(3), /candidate_index_unavailable/);
});

test('cart failures are translated into shopper-facing language', () => {
  const { shopperError } = loadWidgetState();
  const stale = shopperError('Cart changed before dispatch; refresh required (S-09)');
  assert.equal(stale, 'Your cart changed before I could update it, so I made no new change. Please try again.');
  assert.doesNotMatch(stale, /dispatch|S-09/i);
  assert.doesNotMatch(shopperError('Dispatch failed with potential write uncertainty'), /dispatch/i);
});

test('widget renders untrusted shopper and store text without HTML insertion', () => {
  assert.equal(widgetSource.includes('.innerHTML'), false);
  assert.equal(widgetSource.includes('.insertAdjacentHTML'), false);
  assert.match(widgetSource, /node\.textContent = text/);
  assert.match(widgetSource, /aria-live/);
  assert.match(widgetSource, /Microphone access failed\. Type your request instead\./);
  assert.match(widgetSource, /shopping_runtime_failed/);
  assert.match(widgetSource, /shopping service failed while processing/);
});

test('widget uses decision-first ingress before collecting store context', () => {
  assert.match(widgetSource, /context_phase:\s*includeStoreContext \? "action" : "decision"/);
  assert.match(widgetSource, /event\.status === "context_required"/);
  const contextGate = widgetSource.indexOf('if (includeStoreContext) {');
  const cartRead = widgetSource.indexOf('await this.bridge.readAuthoritativeCart()', contextGate);
  assert.ok(contextGate >= 0 && cartRead > contextGate);
});

test('audio playback interrupts previous generation and rejects obsolete chunks', () => {
  const voice = loadVoice();
  const playback = new voice.AudioPlayback();
  assert.equal(playback.generation, 0);

  const chunk1 = { generation: 0, audio: new Uint8Array(16).buffer, format: 'wav' };
  assert.equal(playback.enqueue(chunk1), true);

  playback.interrupt();
  assert.equal(playback.generation, 1);
  assert.equal(playback.queue.length, 0);

  const staleChunk = { generation: 0, audio: new Uint8Array(16).buffer, format: 'wav' };
  assert.equal(playback.enqueue(staleChunk), false);

  const freshChunk = { generation: 1, audio: new Uint8Array(16).buffer, format: 'wav' };
  assert.equal(playback.enqueue(freshChunk), true);
});

test('voice client drops events with stale revisions and duplicate finals', async () => {
  const voice = loadVoice();
  const events = [];
  class FakeWebSocket {
    constructor() {
      this.readyState = 0;
      this.sent = [];
      queueMicrotask(() => { this.readyState = 1; if (this.onopen) this.onopen(); });
    }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  }
  const client = new voice.VoiceSessionClient({
    url: 'wss://runtime.example/ws/voice/shop.myshopify.com',
    grant: { grant_id: 'grant_1' },
    WebSocket: FakeWebSocket,
    onEvent: (event) => events.push(event),
  });

  await client.startTurn({ revision: 2 });
  // Event from revision 1 (stale) must be dropped
  client._receive(JSON.stringify({ type: 'partial_transcript', revision: 1, text: 'old turn' }));
  assert.equal(events.length, 0);

  // Event from revision 2 (current) must be accepted
  client._receive(JSON.stringify({ type: 'partial_transcript', revision: 2, text: 'current turn' }));
  assert.equal(events.length, 1);
  assert.equal(events[0].text, 'current turn');

  // First final for revision 2 accepted
  client._receive(JSON.stringify({ type: 'final_transcript', revision: 2, text: 'final text' }));
  assert.equal(events.filter((e) => e.type === 'final_transcript').length, 1);

  // Duplicate final for revision 2 dropped
  client._receive(JSON.stringify({ type: 'final_transcript', revision: 2, text: 'duplicate final' }));
  assert.equal(events.filter((e) => e.type === 'final_transcript').length, 1);
});

test('voice client cancelTurn sends cancellation and notifies event handler', async () => {
  const voice = loadVoice();
  const events = [];
  let sentData = [];
  class FakeWebSocket {
    constructor() {
      this.readyState = 0;
      this.sent = sentData;
      queueMicrotask(() => { this.readyState = 1; if (this.onopen) this.onopen(); });
    }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  }
  const client = new voice.VoiceSessionClient({
    url: 'wss://runtime.example/ws/voice/shop.myshopify.com',
    grant: { grant_id: 'grant_1' },
    WebSocket: FakeWebSocket,
    onEvent: (event) => events.push(event),
  });

  await client.startTurn({ revision: 3 });
  client.cancelTurn();
  assert.equal(sentData.some((d) => d.includes('cancel_turn')), true);
  assert.equal(events.some((e) => e.type === 'turn_canceled' && e.revision === 3), true);
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = fileURLToPath(new URL('.', import.meta.url));
const source = readFileSync(join(here, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'assets', 'drake-voice.js'), 'utf8');
const widgetSource = readFileSync(join(here, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'assets', 'drake-widget.js'), 'utf8');

function loadVoice(AudioOverride = null) {
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
    Audio: AudioOverride || FakeAudio,
    AudioBuffer: undefined,
  });
  return window.IShopVoiceSession;
}

function loadWidgetState(customDoc) {
  const window = {};
  const mockStorage = {
    _data: {},
    getItem: (k) => mockStorage._data[k] || null,
    setItem: (k, v) => { mockStorage._data[k] = String(v); },
    removeItem: (k) => { delete mockStorage._data[k]; },
  };
  const doc = customDoc || { getElementById: () => null };
  vm.runInNewContext(widgetSource, {
    window,
    document: doc,
    crypto,
    sessionStorage: mockStorage,
    location: { host: 'shop.myshopify.com', pathname: '/', search: '' },
    console,
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
  const wav = voice.pcm16ToWav(new Uint8Array([1, 0, 2, 0]).buffer, 16000, 1);
  assert.equal(new TextDecoder().decode(new Uint8Array(wav, 0, 4)), 'RIFF');
  assert.equal(new DataView(wav).getUint32(24, true), 16000);
  assert.equal(new DataView(wav).getUint32(40, true), 4);
  assert.throws(() => voice.pcm16ToWav(new Uint8Array([1]).buffer), /unaligned_pcm_frame/);
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

test('continuous voice detector starts after sustained speech and ends after trailing silence', () => {
  const { VoiceActivityDetector } = loadVoice();
  const detector = new VoiceActivityDetector({ sampleRate: 1000, minimumSpeechMs: 200, trailingSilenceMs: 300 });
  const speech = new Float32Array(100).fill(0.1);
  const silence = new Float32Array(100);
  assert.equal(detector.process(speech).started, false);
  assert.equal(detector.process(speech).started, true);
  assert.equal(detector.process(silence).ended, false);
  assert.equal(detector.process(silence).ended, false);
  assert.equal(detector.process(silence).ended, true);
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
  assert.match(widgetSource, /Start conversation/);
  assert.match(widgetSource, /End conversation/);
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

test('blocked audio can be replayed and obsolete callbacks cannot disrupt a newer reply', async () => {
  const audios = [];
  let blocked = true;
  class Audio {
    constructor(src) { this.src = src; audios.push(this); }
    pause() {}
    play() { return blocked ? Promise.reject({ name: 'NotAllowedError' }) : Promise.resolve(); }
  }
  const statuses = [];
  const playback = new (loadVoice(Audio).AudioPlayback)({ onStatus: (status) => statuses.push(status) });
  playback.enqueue({ generation: 0, audio: new ArrayBuffer(16), format: 'wav' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(statuses.at(-1), 'blocked');
  assert.equal(playback.queue.length, 1);
  blocked = false;
  playback.retry();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(statuses.at(-1), 'speaking');
  const obsoleteEnd = audios.at(-1).onended;
  playback.interrupt();
  playback.enqueue({ generation: 1, audio: new ArrayBuffer(16), format: 'wav' });
  const current = playback.active;
  obsoleteEnd();
  assert.equal(playback.active, current);
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

test('integrated widget and voice client submits exactly one shopping turn on duplicate finals', async () => {
  const voice = loadVoice();

  // Mock DOM environment for DrakeWidget
  function makeMockElement(tag, className, text) {
    const el = {
      tagName: tag.toUpperCase(),
      className: className || '',
      textContent: text !== undefined ? String(text) : '',
      hidden: false,
      attributes: {},
      dataset: { shopDomain: 'shop.myshopify.com' },
      children: [],
      classList: {
        add: (c) => { el.className = `${el.className} ${c}`.trim(); },
        remove: (c) => { el.className = el.className.replace(c, '').trim(); },
        contains: (c) => el.className.includes(c),
      },
      setAttribute: (k, v) => { el.attributes[k] = String(v); },
      getAttribute: (k) => el.attributes[k],
      removeAttribute: (k) => { delete el.attributes[k]; },
      append: (...nodes) => { el.children.push(...nodes); },
      appendChild: (node) => { el.children.push(node); return node; },
      replaceChildren: (...nodes) => { el.children = [...nodes]; },
      insertBefore: (node, ref) => {
        const idx = el.children.indexOf(ref);
        if (idx >= 0) el.children.splice(idx, 0, node);
        else el.children.push(node);
        return node;
      },
      addEventListener: (type, handler) => {
        el._listeners = el._listeners || {};
        el._listeners[type] = el._listeners[type] || [];
        el._listeners[type].push(handler);
      },
      querySelector: (selector) => {
        if (selector === 'form') return makeMockElement('form');
        return makeMockElement('div');
      },
      querySelectorAll: () => [],
      focus: () => {},
      value: '',
    };
    return el;
  }

  const mockDoc = { createElement: makeMockElement, getElementById: () => null };
  const { DrakeWidget } = loadWidgetState(mockDoc);

  const sentMessages = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      queueMicrotask(() => {
        this.readyState = 1;
        if (this.onopen) this.onopen();
      });
    }
    send(data) { sentMessages.push(data); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  }

  const root = makeMockElement('div', 'ishop-drake-root');
  const client = new voice.VoiceSessionClient({
    url: 'wss://runtime.example/ws/voice/shop.myshopify.com',
    grant: { grant_id: 'grant_1' },
    WebSocket: FakeWebSocket,
  });

  const widget = new DrakeWidget(root, voice);
  widget.setClient(client, {
    bridge: { readAuthoritativeCart: async () => ({ shop_id: 'shop.myshopify.com', currency: 'USD', lines: [] }) },
    catalog: { searchProducts: async () => [] },
  });

  await client.connect();
  await client.startTurn({ revision: 1 });

  // Simulate speech provider emitting duplicate final transcripts over the socket
  client._receive(JSON.stringify({ type: 'final_transcript', revision: 1, text: 'Find snowboard' }));
  client._receive(JSON.stringify({ type: 'final_transcript', revision: 1, text: 'Find snowboard' }));
  client._receive(JSON.stringify({ type: 'final_transcript', revision: 1, text: 'Find snowboard' }));

  // Yield to microtasks for async submitShoppingRequest
  await new Promise((resolve) => setTimeout(resolve, 20));

  // Verify only ONE shopping_turn was transmitted
  const shoppingTurns = sentMessages.filter((m) => {
    try { return JSON.parse(m).type === 'shopping_turn'; } catch (_) { return false; }
  });
  assert.equal(shoppingTurns.length, 1);
  assert.equal(JSON.parse(shoppingTurns[0]).transcript, 'Find snowboard');

  // Verify shopper message was added to history exactly once
  const shopperMessages = widget.messageHistory.filter((m) => m.role === 'shopper');
  assert.equal(shopperMessages.length, 1);
  assert.equal(shopperMessages[0].text, 'Find snowboard');
});

test('microphone denial transitions to failed state and typing text submits cleanly', async () => {
  const voice = loadVoice();

  function makeMockElement(tag, className, text) {
    const el = {
      tagName: tag.toUpperCase(),
      className: className || '',
      textContent: text !== undefined ? String(text) : '',
      hidden: false,
      attributes: {},
      dataset: { shopDomain: 'shop.myshopify.com' },
      children: [],
      classList: {
        add: (c) => { el.className = `${el.className} ${c}`.trim(); },
        remove: (c) => { el.className = el.className.replace(c, '').trim(); },
        contains: (c) => el.className.includes(c),
      },
      setAttribute: (k, v) => { el.attributes[k] = String(v); },
      getAttribute: (k) => el.attributes[k],
      removeAttribute: (k) => { delete el.attributes[k]; },
      append: (...nodes) => { el.children.push(...nodes); },
      appendChild: (node) => { el.children.push(node); return node; },
      replaceChildren: (...nodes) => { el.children = [...nodes]; },
      insertBefore: (node, ref) => {
        const idx = el.children.indexOf(ref);
        if (idx >= 0) el.children.splice(idx, 0, node);
        else el.children.push(node);
        return node;
      },
      addEventListener: (type, handler) => {
        el._listeners = el._listeners || {};
        el._listeners[type] = el._listeners[type] || [];
        el._listeners[type].push(handler);
      },
      querySelector: (selector) => {
        if (selector === 'form') return makeMockElement('form');
        return makeMockElement('div');
      },
      querySelectorAll: () => [],
      focus: () => {},
      value: '',
    };
    return el;
  }

  const mockDoc = { createElement: makeMockElement, getElementById: () => null };
  const { DrakeWidget } = loadWidgetState(mockDoc);

  const sentMessages = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      queueMicrotask(() => { this.readyState = 1; if (this.onopen) this.onopen(); });
    }
    send(data) { sentMessages.push(data); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  }

  const root = makeMockElement('div', 'ishop-drake-root');
  const failingVoice = {
    ...voice,
    PcmCapture: class {
      async start() { throw new Error('NotAllowedError: Permission denied'); }
      stop() {}
    },
  };

  const client = new voice.VoiceSessionClient({
    url: 'wss://runtime.example/ws/voice/shop.myshopify.com',
    grant: { grant_id: 'grant_1' },
    WebSocket: FakeWebSocket,
  });

  const widget = new DrakeWidget(root, failingVoice);
  widget.setClient(client, {
    bridge: { readAuthoritativeCart: async () => ({ shop_id: 'shop.myshopify.com', currency: 'USD', lines: [] }) },
    catalog: { searchProducts: async () => [] },
  });

  await client.connect();

  // Trigger mic start -> fails due to mic permission denial
  await widget.startListening();
  assert.equal(widget.state.name, 'failed');
  assert.match(widget.error.textContent, /Microphone access failed/);
  assert.equal(widget.micButton.hidden, false);
  assert.equal(widget.stopButton.hidden, true);

  // Fallback: Shopper types message instead
  widget.textInput.value = 'Find snowboard';
  await widget.submitText();
  await new Promise((resolve) => setTimeout(resolve, 20));

  const shoppingTurns = sentMessages.filter((m) => {
    try { return JSON.parse(m).type === 'shopping_turn'; } catch (_) { return false; }
  });
  assert.equal(shoppingTurns.length, 1);
  assert.equal(JSON.parse(shoppingTurns[0]).transcript, 'Find snowboard');
  assert.equal(widget.state.name, 'interpreting');
});

test('text delivery and card presentation succeed independent of delayed or failed TTS', async () => {
  const voice = loadVoice();

  function makeMockElement(tag, className, text) {
    const el = {
      tagName: tag.toUpperCase(),
      className: className || '',
      textContent: text !== undefined ? String(text) : '',
      hidden: false,
      attributes: {},
      dataset: { shopDomain: 'shop.myshopify.com' },
      children: [],
      classList: {
        add: (c) => { el.className = `${el.className} ${c}`.trim(); },
        remove: (c) => { el.className = el.className.replace(c, '').trim(); },
        contains: (c) => el.className.includes(c),
      },
      setAttribute: (k, v) => { el.attributes[k] = String(v); },
      getAttribute: (k) => el.attributes[k],
      removeAttribute: (k) => { delete el.attributes[k]; },
      append: (...nodes) => { el.children.push(...nodes); },
      appendChild: (node) => { el.children.push(node); return node; },
      replaceChildren: (...nodes) => { el.children = [...nodes]; },
      insertBefore: (node, ref) => {
        const idx = el.children.indexOf(ref);
        if (idx >= 0) el.children.splice(idx, 0, node);
        else el.children.push(node);
        return node;
      },
      addEventListener: (type, handler) => {
        el._listeners = el._listeners || {};
        el._listeners[type] = el._listeners[type] || [];
        el._listeners[type].push(handler);
      },
      querySelector: (selector) => {
        if (selector === 'form') return makeMockElement('form');
        return makeMockElement('div');
      },
      querySelectorAll: () => [],
      focus: () => {},
      value: '',
    };
    return el;
  }

  const mockDoc = {
    createElement: makeMockElement,
    getElementById: () => null,
    querySelector: (selector) => (selector === 'form' ? makeMockElement('form') : makeMockElement('div')),
    body: makeMockElement('body'),
  };
  const { DrakeWidget } = loadWidgetState(mockDoc);

  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      queueMicrotask(() => { this.readyState = 1; if (this.onopen) this.onopen(); });
    }
    send() {}
    close() { this.readyState = 3; }
  }

  const root = makeMockElement('div', 'ishop-drake-root');
  const client = new voice.VoiceSessionClient({
    url: 'wss://runtime.example/ws/voice/shop.myshopify.com',
    grant: { grant_id: 'grant_1' },
    WebSocket: FakeWebSocket,
  });

  const widget = new DrakeWidget(root, voice);
  widget.setClient(client, {
    bridge: { readAuthoritativeCart: async () => ({ shop_id: 'shop.myshopify.com', currency: 'USD', lines: [] }) },
    catalog: { searchProducts: async () => [{ product_id: '1', title: 'Complete Snowboard', variants: [] }] },
  });
  widget.pendingCatalogProducts = [{ product_id: '1', title: 'Complete Snowboard', variants: [] }];

  await client.connect();

  // Server delivers shopping_result with text and product IDs without any TTS audio
  client._receive(JSON.stringify({
    type: 'shopping_result',
    turn_id: 'turn_1',
    request_revision: 1,
    page_epoch: 1,
    status: 'completed',
    spoken_response: 'Found 1 snowboard matching your search.',
    result_product_ids: ['1'],
  }));

  // Assert text message is rendered immediately
  const assistantMessages = widget.messageHistory.filter((m) => m.role === 'assistant');
  assert.equal(assistantMessages.length, 1);
  assert.equal(assistantMessages[0].text, 'Found 1 snowboard matching your search.');

  // Assert product cards rendered
  assert.equal(widget.candidates.products.length, 1);
  assert.equal(widget.candidates.products[0].title, 'Complete Snowboard');

  // Assert state is ready even without TTS
  assert.equal(widget.state.name, 'ready');
});

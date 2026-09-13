import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = fileURLToPath(new URL('.', import.meta.url));
const source = readFileSync(join(here, '..', '..', '..', 'apps', 'shopify', 'extensions', 'drake', 'assets', 'drake-voice.js'), 'utf8');

function loadVoice() {
  const window = {};
  vm.runInNewContext(source, {
    window,
    document: { getElementById: () => null },
    URL,
    Blob,
    AudioBuffer: undefined,
  });
  return window.IShopVoiceSession;
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

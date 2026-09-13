(function () {
  "use strict";

  class VoiceSessionClient {
    constructor(options) {
      this.url = options.url;
      this.grant = options.grant;
      this.WebSocket = options.WebSocket || window.WebSocket;
      this.onEvent = options.onEvent || function () {};
      this.socket = null;
      this.revision = 0;
      this.finalRevisions = new Set();
    }

    connect() {
      if (this.socket && this.socket.readyState <= 1) return Promise.resolve();
      return new Promise((resolve, reject) => {
        const socket = new this.WebSocket(this.url);
        this.socket = socket;
        socket.onopen = () => {
          socket.send(JSON.stringify({ type: "authenticate", grant: this.grant }));
          resolve();
        };
        socket.onmessage = (message) => this._receive(message.data);
        socket.onerror = () => reject(new Error("voice_transport_failed"));
        socket.onclose = () => {
          if (this.socket === socket) this.socket = null;
          this.onEvent({ type: "closed", revision: this.revision });
        };
      });
    }

    async startTurn(options) {
      const nextRevision = options.revision ?? this.revision + 1;
      if (!Number.isInteger(nextRevision) || nextRevision <= this.revision) {
        throw new Error("stale_revision");
      }
      await this.connect();
      this.revision = nextRevision;
      this.finalRevisions.delete(nextRevision);
      this._send({
        type: "start_turn",
        revision: nextRevision,
        language: options.language || "pcm",
        sample_rate: options.sampleRate || 16000,
        channels: options.channels || 1,
      });
      return nextRevision;
    }

    sendAudio(audio) {
      if (!this.socket || this.socket.readyState !== 1) throw new Error("voice_not_connected");
      if (!(audio instanceof ArrayBuffer) && !(ArrayBuffer.isView(audio))) throw new TypeError("audio_must_be_binary");
      const bytes = audio instanceof ArrayBuffer ? new Uint8Array(audio) : new Uint8Array(audio.buffer, audio.byteOffset, audio.byteLength);
      if (bytes.byteLength < 1024 || bytes.byteLength > 32768 || bytes.byteLength % 2 !== 0) throw new RangeError("audio_chunk_size");
      this.socket.send(bytes);
    }

    finishTurn() {
      this._send({ type: "finish_turn" });
    }

    sendShoppingTurn(payload) {
      if (!payload || typeof payload !== "object") throw new TypeError("shopping_turn_payload");
      this._send({ type: "shopping_turn", ...payload });
    }

    sendCommandResult(commandId, result) {
      if (!commandId || !result || typeof result !== "object") throw new TypeError("command_result_payload");
      this._send({ type: "command_result", command_id: commandId, result });
    }

    cancelTurn() {
      if (this.socket && this.socket.readyState === 1) this._send({ type: "cancel_turn" });
      this.onEvent({ type: "turn_canceled", revision: this.revision });
    }

    close() {
      this.cancelTurn();
      if (this.socket) this.socket.close();
      this.socket = null;
    }

    _send(message) {
      if (!this.socket || this.socket.readyState !== 1) throw new Error("voice_not_connected");
      this.socket.send(JSON.stringify(message));
    }

    _receive(raw) {
      let event;
      try { event = JSON.parse(raw); } catch (_) { return; }
      const revision = Number(event.revision || this.revision);
      if (revision < this.revision) return;
      if (event.type === "final_transcript") {
        if (this.finalRevisions.has(revision)) return;
        this.finalRevisions.add(revision);
      }
      this.onEvent({ ...event, revision });
    }
  }

  class PcmCapture {
    constructor(options) {
      this.sampleRate = options.sampleRate || 16000;
      this.channels = options.channels || 1;
      this.onChunk = options.onChunk;
      this.context = null;
      this.stream = null;
      this.source = null;
      this.node = null;
    }

    async start() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error("microphone_unavailable");
      if (!window.AudioWorkletNode) throw new Error("audio_worklet_unavailable");
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: this.channels, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      this.context = new AudioContext({ sampleRate: this.sampleRate });
      const sourceCode = "class DrakePcmProcessor extends AudioWorkletProcessor { process(inputs) { const input = inputs[0]; if (input && input[0]) this.port.postMessage(input[0]); return true; } } registerProcessor('drake-pcm-processor', DrakePcmProcessor);";
      const sourceUrl = URL.createObjectURL(new Blob([sourceCode], { type: "application/javascript" }));
      try {
        await this.context.audioWorklet.addModule(sourceUrl);
      } finally {
        URL.revokeObjectURL(sourceUrl);
      }
      this.source = this.context.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.context, "drake-pcm-processor", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: this.channels });
      this.node.port.onmessage = (event) => this.onChunk(floatToPcm16(event.data));
      this.source.connect(this.node);
      this.node.connect(this.context.destination);
    }

    stop() {
      if (this.node) this.node.disconnect();
      if (this.source) this.source.disconnect();
      if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
      if (this.context) this.context.close();
      this.node = null;
      this.source = null;
      this.stream = null;
      this.context = null;
    }
  }

  class AudioPlayback {
    constructor() {
      this.generation = 0;
      this.queue = [];
      this.active = null;
    }

    enqueue(chunk) {
      if (!chunk || chunk.generation !== this.generation) return false;
      this.queue.push(chunk);
      this._playNext();
      return true;
    }

    interrupt() {
      this.generation += 1;
      this.queue = [];
      if (this.active) {
        this.active.pause();
        this.active.src = "";
        this.active = null;
      }
    }

    _playNext() {
      if (this.active || !this.queue.length) return;
      const chunk = this.queue.shift();
      const audio = new Audio(URL.createObjectURL(new Blob([chunk.audio], { type: "audio/" + (chunk.format || "wav") })));
      this.active = audio;
      audio.onended = () => { URL.revokeObjectURL(audio.src); this.active = null; this._playNext(); };
      audio.onerror = () => { URL.revokeObjectURL(audio.src); this.active = null; this._playNext(); };
      audio.play().catch(() => { this.active = null; });
    }
  }

  function floatToPcm16(samples) {
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      const value = Math.max(-1, Math.min(1, samples[i]));
      pcm[i] = value < 0 ? value * 32768 : value * 32767;
    }
    return pcm.buffer;
  }

  function buildVoiceWebSocketUrl(baseUrl, shopDomain) {
    const url = new URL(baseUrl);
    if (!['ws:', 'wss:'].includes(url.protocol)) throw new Error('voice_url_protocol');
    if (url.protocol !== 'wss:' && !['localhost', '127.0.0.1'].includes(url.hostname)) {
      throw new Error('voice_url_requires_tls');
    }
    if (!url.pathname.includes('/voice/')) {
      url.pathname = url.pathname.replace(/\/$/, '') + '/voice/' + encodeURIComponent(shopDomain);
    }
    return url.toString();
  }

  if (typeof window !== "undefined") {
    window.IShopVoiceSession = {
      VoiceSessionClient,
      PcmCapture,
      AudioPlayback,
      floatToPcm16,
      buildVoiceWebSocketUrl,
    };
    const root = document.getElementById("ishop-drake-root");
    const bootstrapUrl = root && root.dataset.bootstrapScriptUrl;
    const widgetUrl = root && root.dataset.widgetScriptUrl;
    const loadBootstrap = () => {
      if (!bootstrapUrl) return;
      const script = document.createElement("script");
      script.src = bootstrapUrl;
      script.defer = true;
      document.head.appendChild(script);
    };
    if (widgetUrl) {
      const script = document.createElement("script");
      script.src = widgetUrl;
      script.onload = loadBootstrap;
      script.defer = true;
      document.head.appendChild(script);
    } else {
      loadBootstrap();
    }
  }
})();

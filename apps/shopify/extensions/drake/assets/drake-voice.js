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

    close(options) {
      if (!options || options.cancel !== false) this.cancelTurn();
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
      if (event.type === "authenticated" && (event.protocol_version !== "1.0.0" || event.command_schema_version !== "1.0.0" || event.tool_registry_version !== "1.0.0")) {
        this.onEvent({ type: "error", error_code: "incompatible_runtime", revision: this.revision });
        this.close({ cancel: false });
        return;
      }
      const revision = Number(event.request_revision || event.revision || this.revision);
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
      this.pendingBytes = new Uint8Array(0);
      this.chunkBytes = options.chunkBytes || 4096;
      this.continuous = Boolean(options.continuous);
      this.onSpeechStart = options.onSpeechStart || function () {};
      this.onSpeechEnd = options.onSpeechEnd || function () {};
      this.detector = this.continuous ? new VoiceActivityDetector({ sampleRate: this.sampleRate, ...(options.vad || {}) }) : null;
      this.preRollBytes = Math.round(this.sampleRate * this.channels * 2 * 0.25);
      this.preRoll = new Uint8Array(0);
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
      this.node.port.onmessage = (event) => this._process(event.data);
      this.source.connect(this.node);
      this.node.connect(this.context.destination);
    }

    _process(samples) {
      const pcm = floatToPcm16(samples);
      if (!this.detector) {
        this._buffer(pcm);
        return;
      }
      const activity = this.detector.process(samples);
      if (!activity.active && !activity.started) {
        this._retainPreRoll(pcm);
        return;
      }
      if (activity.started) {
        this.onSpeechStart();
        if (this.preRoll.byteLength) this._buffer(this.preRoll.buffer);
        this.preRoll = new Uint8Array(0);
      }
      this._buffer(pcm);
      if (activity.ended) {
        this._flush();
        this.onSpeechEnd(activity);
      }
    }

    _retainPreRoll(buffer) {
      const incoming = new Uint8Array(buffer);
      const joined = new Uint8Array(this.preRoll.byteLength + incoming.byteLength);
      joined.set(this.preRoll); joined.set(incoming, this.preRoll.byteLength);
      this.preRoll = joined.slice(Math.max(0, joined.byteLength - this.preRollBytes));
    }

    _buffer(buffer) {
      const incoming = new Uint8Array(buffer);
      const combined = new Uint8Array(this.pendingBytes.byteLength + incoming.byteLength);
      combined.set(this.pendingBytes);
      combined.set(incoming, this.pendingBytes.byteLength);
      let offset = 0;
      while (combined.byteLength - offset >= this.chunkBytes) {
        this.onChunk(combined.slice(offset, offset + this.chunkBytes).buffer);
        offset += this.chunkBytes;
      }
      this.pendingBytes = combined.slice(offset);
    }

    _flush() {
      if (!this.pendingBytes.byteLength) return;
      const finalChunk = new Uint8Array(Math.max(1024, this.pendingBytes.byteLength));
      finalChunk.set(this.pendingBytes);
      this.pendingBytes = new Uint8Array(0);
      this.onChunk(finalChunk.buffer);
    }

    stop(flush = false) {
      if (flush) this._flush();
      else this.pendingBytes = new Uint8Array(0);
      this.preRoll = new Uint8Array(0);
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

  class VoiceActivityDetector {
    constructor(options = {}) {
      this.sampleRate = options.sampleRate || 16000;
      this.minimumSpeechSamples = this.sampleRate * ((options.minimumSpeechMs || 200) / 1000);
      this.trailingSilenceSamples = this.sampleRate * ((options.trailingSilenceMs || 900) / 1000);
      this.maximumSpeechSamples = this.sampleRate * (options.maximumSpeechSeconds || 30);
      this.noiseFloor = 0.004;
      this.speechSamples = 0;
      this.silenceSamples = 0;
      this.totalActiveSamples = 0;
      this.active = false;
    }

    process(samples) {
      let sum = 0;
      for (let index = 0; index < samples.length; index += 1) sum += samples[index] * samples[index];
      const rms = samples.length ? Math.sqrt(sum / samples.length) : 0;
      const threshold = Math.max(0.015, this.noiseFloor * 3);
      const speech = rms >= threshold;
      let started = false;
      let ended = false;
      let maximumReached = false;
      if (!this.active) {
        if (speech) this.speechSamples += samples.length;
        else {
          this.speechSamples = 0;
          this.noiseFloor = (this.noiseFloor * 0.95) + (rms * 0.05);
        }
        if (this.speechSamples >= this.minimumSpeechSamples) {
          this.active = true; started = true; this.silenceSamples = 0; this.totalActiveSamples = this.speechSamples;
        }
      } else {
        this.totalActiveSamples += samples.length;
        this.silenceSamples = speech ? 0 : this.silenceSamples + samples.length;
        maximumReached = this.totalActiveSamples >= this.maximumSpeechSamples;
        if (this.silenceSamples >= this.trailingSilenceSamples || maximumReached) {
          this.active = false; ended = true; this.speechSamples = 0; this.silenceSamples = 0; this.totalActiveSamples = 0;
        }
      }
      return { active: this.active || ended, started, ended, maximumReached, rms };
    }
  }

  class AudioPlayback {
    constructor(options = {}) {
      this.onStatus = options.onStatus || function () {};
      this.blocked = false;
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
      this.blocked = false;
      this.generation += 1;
      this.queue = [];
      if (this.active) {
        URL.revokeObjectURL(this.active.src);
        this.active.onended = null;
        this.active.onerror = null;
        this.active.pause();
        this.active.src = "";
        this.active = null;
      }
      this.onStatus("idle");
    }

    retry() { this.blocked = false; this._playNext(); }

    _playNext() {
      if (this.blocked || this.active || !this.queue.length) return;
      const chunk = this.queue.shift();
      const rawPcm = (chunk.container || "").toLowerCase() === "none" &&
        (chunk.format || "").toLowerCase() === "pcm";
      const playable = rawPcm
        ? pcm16ToWav(chunk.audio, chunk.sampleRate || 16000, chunk.channels || 1)
        : chunk.audio;
      const mime = rawPcm ? "audio/wav" : "audio/" + (chunk.format || "wav");
      const audio = new Audio(URL.createObjectURL(new Blob([playable], { type: mime })));
      this.active = audio;
      const finish = () => {
        if (this.active !== audio) return;
        URL.revokeObjectURL(audio.src); this.active = null;
        this.onStatus("idle"); this._playNext();
      };
      audio.onended = finish;
      audio.onerror = () => { finish(); this.onStatus("unavailable"); };
      audio.play().then(() => {
        if (this.active === audio) this.onStatus("speaking");
      }).catch((error) => {
        if (this.active !== audio) return;
        URL.revokeObjectURL(audio.src); this.active = null;
        if (error?.name === "NotAllowedError") {
          this.blocked = true; this.queue.unshift(chunk); this.onStatus("blocked");
        } else { this.onStatus("unavailable"); this._playNext(); }
      });
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

  function pcm16ToWav(pcmBuffer, sampleRate = 16000, channels = 1) {
    if (!pcmBuffer || typeof pcmBuffer.byteLength !== "number") throw new TypeError("pcm_buffer_required");
    if (pcmBuffer.byteLength % (2 * channels) !== 0) throw new Error("unaligned_pcm_frame");
    const output = new ArrayBuffer(44 + pcmBuffer.byteLength);
    const view = new DataView(output);
    const write = (offset, value) => {
      for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index));
    };
    write(0, "RIFF"); view.setUint32(4, 36 + pcmBuffer.byteLength, true); write(8, "WAVE");
    write(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, channels, true); view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * channels * 2, true); view.setUint16(32, channels * 2, true);
    view.setUint16(34, 16, true); write(36, "data"); view.setUint32(40, pcmBuffer.byteLength, true);
    new Uint8Array(output, 44).set(new Uint8Array(pcmBuffer));
    return output;
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
      VoiceActivityDetector,
      AudioPlayback,
      floatToPcm16,
      pcm16ToWav,
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

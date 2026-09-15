(function () {
  "use strict";

  const VALID_STATES = new Set(["initializing", "ready", "listening", "interpreting", "checking", "clarifying", "updating", "completed", "reconnecting", "failed"]);
  const STATE_LABELS = {
    initializing: "Starting Drake", ready: "Ready to help", listening: "Listening",
    interpreting: "Understanding your request", checking: "Checking the store",
    clarifying: "I need a little more information", updating: "Updating your cart",
    completed: "Cart update verified", reconnecting: "Reconnecting", failed: "Something went wrong",
  };

  class CandidateSet {
    constructor(products) {
      if (!Array.isArray(products)) throw new TypeError("candidate_products");
      this.products = products.filter((product) => product && product.product_id && product.title).slice(0, 8);
    }
    resolve(index) {
      const product = this.products[index - 1];
      if (!product) throw new Error("candidate_index_unavailable");
      return product;
    }
  }

  class WidgetState {
    constructor() {
      this.name = "initializing";
      this.partial = "";
      this.final = "";
      this.error = "";
      this.verifiedReceipt = null;
    }
    transition(name, detail) {
      if (!VALID_STATES.has(name)) throw new Error("invalid_widget_state");
      if (name === "completed" && !detail?.verifiedReceipt) throw new Error("verified_receipt_required");
      this.name = name;
      this.error = name === "failed" && !detail?.suppressError ? String(detail?.error || "Please try again.") : "";
      this.verifiedReceipt = detail?.verifiedReceipt || null;
      return this.snapshot();
    }
    setPartial(text) {
      this.partial = String(text || "");
      return this.snapshot();
    }
    acceptFinal(text) {
      this.final = String(text || "");
      this.partial = "";
      this.name = "interpreting";
      return this.snapshot();
    }
    snapshot() {
      return { name: this.name, label: STATE_LABELS[this.name], partial: this.partial, final: this.final, error: this.error, verifiedReceipt: this.verifiedReceipt };
    }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function shopperError(detail) {
    const message = String(detail || "");
    if (/changed before dispatch|refresh required|S-09/i.test(message)) {
      return "Your cart changed before I could update it, so I made no new change. Please try again.";
    }
    if (/authoritative cart read-back|could not be verified/i.test(message)) {
      return "I couldn't verify the final cart. Please review it before trying again.";
    }
    if (/uncertain|connection severed|dispatch failed/i.test(message)) {
      return "I lost confirmation from the store. Please review your cart before trying again.";
    }
    return message || "I couldn't complete that cart update.";
  }

  class DrakeWidget {
    constructor(root, voice) {
      this.root = root;
      this.voice = voice;
      this.state = new WidgetState();
      this.client = null;
      this.bridge = null;
      this.catalog = null;
      this.currentProductId = null;
      this.currentProductHandle = null;
      this.candidates = new CandidateSet([]);
      this.capture = null;
      this.continuousMode = false;
      this.continuousTurn = null;
      this.continuousAudioQueue = [];
      this.playback = voice ? new voice.AudioPlayback({ onStatus: (status) => {
        if (!this.audioStatus) return;
        this.audioStatus.textContent = { speaking: "Drake is speaking", blocked: "Press Play reply to hear Drake", unavailable: "Audio unavailable. Your reply is shown above.", idle: "" }[status] || "";
        this.playButton.hidden = status !== "blocked";
      } }) : null;
      this.pendingTranscript = "";
      this.pendingCatalogProducts = [];
      this.pendingCatalogQuery = null;
      this.pendingTurn = null;
      this.pendingReceipts = new Map();
      this.messageHistory = [];
      this.historyKey = `ishop:messages:${root.dataset.shopDomain || location.host}`;
      this.revisionKey = `ishop:revision:${root.dataset.shopDomain || location.host}`;
      this.nativeSearchKey = `ishop:native-search:${root.dataset.shopDomain || location.host}`;
      this.conversationResumeKey = `ishop:continuous-resume:${root.dataset.shopDomain || location.host}`;
      this.lastRevision = 0;
      this.pageEpoch = 1;
      this.pageContext = null;
      try {
        const shopKey = root.dataset.shopDomain || location.host;
        this.lastRevision = Math.max(0, Number(sessionStorage.getItem(this.revisionKey) || 0));
        const key = `ishop:page-epoch:${shopKey}`;
        this.pageEpoch = Number(sessionStorage.getItem(key) || 0) + 1;
        sessionStorage.setItem(key, String(this.pageEpoch));
        const pathKey = `ishop:last-path:${shopKey}`;
        const currentPath = `${location.pathname}${location.search}`;
        const previousPath = sessionStorage.getItem(pathKey);
        sessionStorage.setItem(pathKey, currentPath);
        this.pageContext = {
          page_id: `page_${crypto.randomUUID().replaceAll("-", "")}`,
          path: currentPath,
          previous_path: previousPath,
          product_id: root.dataset.currentProductId || null,
          variant_id: new URLSearchParams(location.search).get("variant"),
          observed_at_ms: Date.now(),
        };
      } catch (_) {
        this.pageEpoch = 1;
      }
      this.open = false;
      this.render();
      this.restoreMessages();
      this.bind();
      this.setState("initializing");
    }
    restoreMessages() {
      try {
        const parsed = JSON.parse(sessionStorage.getItem(this.historyKey) || "[]");
        if (!Array.isArray(parsed)) return;
        this.messageHistory = parsed.filter((item) => item && ["shopper", "assistant"].includes(item.role) && typeof item.text === "string").slice(-16);
        for (const item of this.messageHistory) {
          const message = element("p", `drake-message drake-message--${item.role}`, item.text);
          this.conversation.insertBefore(message, this.cards);
        }
      } catch (_) {
        this.messageHistory = [];
      }
    }
    render() {
      this.root.hidden = false;
      this.root.classList.add("drake-widget");
      this.launcher = element("button", "drake-launcher");
      this.launcher.type = "button";
      this.launcher.setAttribute("aria-expanded", "false");
      this.launcher.setAttribute("aria-controls", "drake-panel");
      this.launcher.setAttribute("aria-label", "Open Drake shopping assistant");
      const launcherMark = element("span", "drake-launcher__mark", "D");
      launcherMark.setAttribute("aria-hidden", "true");
      this.launcher.append(launcherMark);
      this.panel = element("section", "drake-panel");
      this.panel.id = "drake-panel";
      this.panel.hidden = true;
      this.panel.setAttribute("role", "dialog");
      this.panel.setAttribute("aria-modal", "false");
      this.panel.setAttribute("aria-labelledby", "drake-title");
      const header = element("header", "drake-header");
      const identity = element("div", "drake-identity");
      const title = element("h2", "drake-title", "Drake");
      title.id = "drake-title";
      identity.append(title, element("p", "drake-subtitle", "AI shopping assistant"));
      this.closeButton = element("button", "drake-icon-button", "Close");
      this.closeButton.type = "button";
      this.closeButton.setAttribute("aria-label", "Close Drake assistant");
      header.append(identity, this.closeButton);
      this.status = element("div", "drake-status");
      this.status.setAttribute("role", "status");
      this.status.setAttribute("aria-live", "polite");
      this.statusDot = element("span", "drake-status__dot");
      this.statusText = element("span", "drake-status__text");
      this.status.append(this.statusDot, this.statusText);
      this.conversation = element("div", "drake-conversation");
      this.conversation.setAttribute("aria-label", "Conversation with Drake");
      this.caption = element("p", "drake-caption");
      this.caption.setAttribute("aria-label", "Live speech caption");
      this.error = element("p", "drake-error");
      this.error.setAttribute("role", "alert");
      this.error.hidden = true;
      this.cards = element("div", "drake-cards");
      this.conversation.append(
        element("p", "drake-message drake-message--assistant", "Hi, I’m Drake. Tell me what you’re shopping for, or type below."),
        this.cards,
        this.caption,
        this.error,
      );
      const form = element("form", "drake-composer");
      this.textInput = element("input", "drake-text-input");
      this.textInput.type = "text";
      this.textInput.autocomplete = "off";
      this.textInput.placeholder = "Ask Drake to find something";
      this.textInput.setAttribute("aria-label", "Message Drake");
      this.sendButton = element("button", "drake-send", "Send");
      this.sendButton.type = "submit";
      form.append(this.textInput, this.sendButton);
      const controls = element("div", "drake-controls");
      this.micButton = element("button", "drake-mic", "Start speaking");
      this.micButton.type = "button";
      this.stopButton = element("button", "drake-stop", "Stop");
      this.stopButton.type = "button";
      this.stopButton.hidden = true;
      this.conversationButton = element("button", "drake-conversation-start", "Start conversation");
      this.conversationButton.type = "button";
      this.endConversationButton = element("button", "drake-conversation-end", "End conversation");
      this.endConversationButton.type = "button";
      this.endConversationButton.hidden = true;
      controls.append(this.micButton, this.stopButton, this.conversationButton, this.endConversationButton);
      this.audioStatus = element("p", "drake-audio-status");
      this.audioStatus.setAttribute("role", "status");
      this.playButton = element("button", "drake-play", "Play reply");
      this.playButton.type = "button";
      this.playButton.hidden = true;
      this.playButton.addEventListener("click", () => this.playback?.retry());
      this.editVoiceButton = element("button", "drake-edit-voice", "Correct transcript");
      this.editVoiceButton.type = "button";
      this.editVoiceButton.hidden = true;
      this.editVoiceButton.addEventListener("click", () => {
        this.textInput.value = this.lastVoiceTranscript || "";
        this.textInput.focus();
      });
      this.editVoiceButton.title = "Edit the recognized words, then send a new request";
      controls.append(this.audioStatus, this.playButton, this.editVoiceButton);
      this.privacy = element("p", "drake-privacy", "Your microphone starts only when you press Start speaking or Start conversation.");
      this.panel.append(header, this.status, this.conversation, form, controls, this.privacy);
      this.root.replaceChildren(this.launcher, this.panel);
    }
    bind() {
      this.launcher.addEventListener("click", () => this.setOpen(!this.open));
      this.closeButton.addEventListener("click", () => this.setOpen(false));
      this.micButton.addEventListener("click", () => this.startListening());
      this.stopButton.addEventListener("click", () => this.finishListening());
      this.conversationButton.addEventListener("click", () => this.startConversation());
      this.endConversationButton.addEventListener("click", () => this.endConversation());
      if (typeof document.addEventListener === "function") document.addEventListener("visibilitychange", () => {
        if (document.hidden && this.continuousMode && !this.navigationInProgress) this.endConversation("Conversation paused because this tab is hidden.");
      });
      this.panel.querySelector("form").addEventListener("submit", (event) => { event.preventDefault(); this.submitText(); });
    }
    setClient(client, integrations) {
      this.client = client;
      this.client.revision = Math.max(Number(this.client.revision || 0), this.lastRevision);
      this.bridge = integrations?.bridge || null;
      this.catalog = integrations?.catalog || null;
      this.currentProductId = integrations?.currentProductId || null;
      this.currentProductHandle = integrations?.currentProductHandle || null;
      this.client.onEvent = (event) => this.handleVoiceEvent(event);
      this.setState("ready");
      this.initializePageSearch();
    }
    setOpen(open) {
      this.open = Boolean(open);
      this.panel.hidden = !this.open;
      this.launcher.setAttribute("aria-expanded", String(this.open));
      this.launcher.setAttribute("aria-label", this.open ? "Close Drake shopping assistant" : "Open Drake shopping assistant");
      if (!this.open && this.continuousMode) this.endConversation();
      if (this.open) this.textInput.focus();
    }
    async startConversation() {
      if (!this.client || !this.voice || this.continuousMode) return;
      try {
        if (this.playback) this.playback.interrupt();
        this.continuousMode = true;
        this.capture = new this.voice.PcmCapture({
          sampleRate: 16000, channels: 1, continuous: true,
          onSpeechStart: () => this.beginContinuousUtterance(),
          onSpeechEnd: (activity) => this.finishContinuousUtterance(activity),
          onChunk: (chunk) => this.sendContinuousAudio(chunk),
        });
        await this.capture.start();
        try { sessionStorage.setItem(this.conversationResumeKey, "requested"); } catch (_) {}
        this.micButton.hidden = true; this.stopButton.hidden = true;
        this.conversationButton.hidden = true; this.endConversationButton.hidden = false;
        this.privacy.textContent = "Conversation microphone is on. Drake sends audio only after speech is detected.";
        this.setState("listening");
      } catch (_) {
        this.continuousMode = false;
        if (this.capture) this.capture.stop();
        this.capture = null;
        this.setState("failed", { error: "Microphone access failed. Type your request instead." });
      }
    }
    beginContinuousUtterance() {
      if (!this.continuousMode) return;
      if (this.playback) this.playback.interrupt();
      this.continuousAudioQueue = [];
      this.continuousTurn = this.client.startTurn({}).then(() => {
        this.lastRevision = this.client.revision;
        try { sessionStorage.setItem(this.revisionKey, String(this.lastRevision)); } catch (_) {}
        for (const chunk of this.continuousAudioQueue.splice(0)) this.client.sendAudio(chunk);
      }).catch(() => this.endConversation("The speech service could not start. You can still type."));
      this.setState("listening");
    }
    sendContinuousAudio(chunk) {
      if (!this.continuousTurn) return;
      this.continuousAudioQueue.push(chunk);
      if (this.continuousAudioQueue.length > 16) this.continuousAudioQueue.shift();
      this.continuousTurn.then(() => {
        const next = this.continuousAudioQueue.shift();
        if (next) this.client.sendAudio(next);
      }).catch(() => {});
    }
    finishContinuousUtterance(activity = {}) {
      const turn = this.continuousTurn;
      this.continuousTurn = null;
      if (!turn) return;
      if (activity.maximumReached) {
        this.continuousAudioQueue = [];
        turn.then(() => this.client.cancelTurn()).catch(() => {});
        this.addMessage("assistant", "That voice request reached 30 seconds, so I did not act on a possibly incomplete instruction. Please try a shorter request.");
        this.setState("listening");
        return;
      }
      turn.then(() => {
        for (const chunk of this.continuousAudioQueue.splice(0)) this.client.sendAudio(chunk);
        this.client.finishTurn();
      }).catch(() => {});
      this.setState("interpreting");
    }
    endConversation(message = "") {
      if (this.capture) this.capture.stop(false);
      this.capture = null; this.continuousMode = false; this.continuousTurn = null; this.continuousAudioQueue = [];
      if (this.client) this.client.cancelTurn();
      if (this.playback) this.playback.interrupt();
      try { sessionStorage.removeItem(this.conversationResumeKey); } catch (_) {}
      this.micButton.hidden = false; this.stopButton.hidden = true;
      this.conversationButton.hidden = false; this.endConversationButton.hidden = true;
      this.privacy.textContent = message || "Your microphone starts only when you press Start speaking or Start conversation.";
      this.setState("ready");
    }
    setState(name, detail) {
      const view = this.state.transition(name, detail);
      this.root.dataset.state = view.name;
      this.statusText.textContent = view.label;
      this.error.textContent = view.error;
      this.error.hidden = !view.error;
      const busy = ["listening", "interpreting", "checking", "updating", "reconnecting"].includes(view.name);
      this.root.setAttribute("aria-busy", String(busy));
      return view;
    }
    async startListening() {
      if (!this.client || !this.voice) {
        this.setState("failed", { error: "Voice is not connected. You can still type your request." });
        return;
      }
      try {
        if (this.playback) this.playback.interrupt();
        await this.client.startTurn({});
        this.lastRevision = this.client.revision;
        try { sessionStorage.setItem(this.revisionKey, String(this.lastRevision)); } catch (_) {}
        this.capture = new this.voice.PcmCapture({ sampleRate: 16000, channels: 1, onChunk: (chunk) => this.client.sendAudio(chunk) });
        await this.capture.start();
        this.micButton.hidden = true;
        this.stopButton.hidden = false;
        this.setState("listening");
      } catch (_) {
        if (this.capture) this.capture.stop();
        this.capture = null;
        if (this.client) this.client.cancelTurn();
        this.micButton.hidden = false;
        this.stopButton.hidden = true;
        this.setState("failed", { error: "Microphone access failed. Type your request instead." });
      }
    }
    finishListening() {
      if (this.capture) this.capture.stop(true);
      this.capture = null;
      this.micButton.hidden = false;
      this.stopButton.hidden = true;
      if (this.client) this.client.finishTurn();
      this.setState("interpreting");
    }
    cancel() {
      if (this.capture) this.capture.stop();
      this.capture = null;
      if (this.client) this.client.cancelTurn();
      if (this.playback) this.playback.interrupt();
      this.micButton.hidden = false;
      this.stopButton.hidden = true;
      this.setState("ready");
    }
    async submitText() {
      const text = this.textInput.value.trim();
      if (!text) return;
      if (this.playback) this.playback.interrupt();
      this.textInput.value = "";
      this.addMessage("shopper", text);
      this.state.acceptFinal(text);
      await this.submitShoppingRequest(text);
    }
    handleVoiceEvent(event) {
      if (event.type === "partial_transcript") {
        this.state.setPartial(event.text);
        this.caption.textContent = event.text || "";
      } else if (event.type === "final_transcript") {
        this.lastVoiceTranscript = event.text || "";
        if (this.editVoiceButton) this.editVoiceButton.hidden = false;
        this.state.acceptFinal(event.text);
        this.caption.textContent = "";
        this.addMessage("shopper", event.text || "");
        this.submitShoppingRequest(event.text || "");
      } else if (event.type === "error") {
        if (event.request_revision != null && Number(event.request_revision) !== Number(this.client?.revision)) return;
        const messages = {
          invalid_shopping_turn: "I couldn’t validate the store information for that request.",
          shopping_runtime_failed: "The shopping service failed while processing that request.",
          provider_start_failed: "The speech service could not start. Please try again.",
          provider_stream_failed: "The speech service did not finish transcribing. Please try again.",
          incompatible_runtime: "Drake was updated. Please refresh this page to reconnect safely.",
          RESOURCE_EXHAUSTED: "The speech service is busy. Please wait or type your request.",
          QUOTA_EXCEEDED: "The speech service limit has been reached. You can still type.",
          INSUFFICIENT_AUDIO_ACTIVITY: "I didn’t hear enough speech. Please try again or type.",
        };
        this.setState("failed", { error: messages[event.error_code] || "I couldn’t process that. Please try again or type your request." });
      } else if (event.type === "shopping_result") {
        if (event.status === "context_required") {
          this.submitShoppingRequest(this.pendingTranscript, null, this.pendingTurn);
          return;
        }
        if (event.status === "tool_required" && event.tool_request) {
          this.retrieveToolObservation(event.tool_request, event.turn_id, event.request_revision);
          return;
        }
        if (event.status === "evidence_required" && event.evidence_query) {
          this.retrieveCatalogEvidence(
            event.evidence_query,
            event.turn_id,
            event.request_revision,
            event.selected_tool,
            event.tool_request,
          );
          return;
        }
        if (Array.isArray(event.result_product_ids) && event.result_product_ids.length) {
          const pool = (this.pendingCatalogProducts && this.pendingCatalogProducts.length) ? this.pendingCatalogProducts : (this.candidates?.products || []);
          const byId = new Map(pool.map((product) => [String(product.product_id), product]));
          const matched = event.result_product_ids.map((id) => byId.get(String(id))).filter(Boolean);
          if (matched.length) {
            this.renderProducts(matched);
          }
        }
        if (event.spoken_response) this.addMessage("assistant", event.spoken_response);
        this.playTts(event.tts_audio_chunks);
        if (event.status === "clarification_needed") {
          this.setState("clarifying");
        } else if (event.authorized_command) {
          this.executeAuthorizedCommand(event.authorized_command);
        } else if (event.status === "error") {
          this.setState("failed", { suppressError: Boolean(event.spoken_response), error: event.spoken_response || "I couldn’t complete that request." });
          this.pendingTurn = null;
        } else {
          this.setState("ready");
          this.pendingTurn = null;
        }
      } else if (event.type === "tts_unavailable") {
        if (Number(event.request_revision) !== Number(this.client?.revision) || Number(event.page_epoch) !== Number(this.pageEpoch)) return;
        console.warn('[Drake] Spoken reply unavailable:', event.failure_code || 'unknown');
        this.audioStatus.textContent = event.failure_code === "session_initialization_error"
          ? "The selected Pidgin voice could not start. Your text reply is shown above."
          : event.retryable
          ? "The voice service is temporarily unavailable. Your text reply is shown above."
          : "Spoken reply unavailable from the selected voice. Your text reply is shown above.";
        this.playButton.hidden = true;
      } else if (event.type === "shopping_tts") {
        if (!this.client || Number(event.request_revision) !== Number(this.client.revision) || Number(event.page_epoch) !== Number(this.pageEpoch)) return;
        this.playTts(event.tts_audio_chunks);
      } else if (event.type === "command_result_ack") {
        const receipt = this.pendingReceipts.get(event.command_id);
        // Late or duplicate acknowledgements must never regress a newer turn.
        if (!receipt) return;
        if (event.request_revision != null && receipt.request_revision != null &&
            Number(event.request_revision) < Number(receipt.request_revision)) return;
        this.pendingReceipts.delete(event.command_id);
        if (event.verified) {
          this.setState("completed", { verifiedReceipt: receipt.result });
        } else {
          const detail = shopperError(receipt.result?.errors?.[0]);
          this.setState("failed", { error: detail });
        }
        this.pendingTurn = null;
      } else if (event.type === "closed") {
        this.setState("reconnecting");
      }
    }

    playTts(chunks) {
      if (!this.playback || !Array.isArray(chunks)) return;
      for (const chunk of chunks) {
        if (!chunk || typeof chunk.audio_base64 !== "string") continue;
        try {
          const binary = atob(chunk.audio_base64);
          const audio = new Uint8Array(binary.length);
          for (let index = 0; index < binary.length; index += 1) audio[index] = binary.charCodeAt(index);
          this.playback.enqueue({
            audio: audio.buffer,
            generation: this.playback.generation,
            format: chunk.format || "wav",
            container: chunk.container || chunk.format || "wav",
            sampleRate: Number(chunk.sample_rate) || 16000,
            channels: Number(chunk.channels) || 1,
          });
        } catch (error) {
          console.error('[Drake] Voice playback failed:', error);
        }
      }
    }

    renderProducts(products, query = null) {
      this.candidates = new CandidateSet(products);
      this.cards.replaceChildren();
      this.candidates.products.forEach((product, index) => {
        const card = element("article", "drake-card");
        card.append(element("h3", "drake-card__title", `${index + 1}. ${product.title}`));
        (product.variants || []).slice(0, 3).forEach((variant) => {
          const options = Object.entries(variant.selected_options || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
          const price = variant.price ? `${variant.price.amount} ${variant.price.currency}` : "Price unavailable";
          card.append(element("p", "drake-card__variant", `${variant.variant_title || "Standard"} · ${price}${options ? ` · ${options}` : ""}`));
        });
        const choose = element("button", "drake-card__choose", `Choose ${index + 1}`);
        choose.type = "button";
        choose.addEventListener("click", () => {
          this.textInput.value = product.title;
          this.textInput.focus();
        });
        card.append(choose);
        this.cards.append(card);
      });
    }

    async retrieveCatalogEvidence(query, turnId, requestRevision, toolName = "search_catalog", toolRequest = null) {
      try {
        if (!this.pendingTurn || this.pendingTurn.turnId !== turnId || this.pendingTurn.requestRevision !== requestRevision) return;
        if (!toolName || !["search_catalog", "get_product"].includes(toolName)) {
          throw new Error("catalog_evidence_tool_required");
        }
        if (toolName === "search_catalog" && this.catalog?.nativeSearch) {
          const arguments_ = { ...(toolRequest?.arguments || {}), query };
          const objective = {
            schema_version: "1.0.0", shop_id: this.root.dataset.shopDomain || location.host,
            turn_id: turnId, request_revision: requestRevision, transcript: this.pendingTranscript,
            arguments: arguments_, expires_at_ms: Date.now() + 120000,
          };
          sessionStorage.setItem(this.nativeSearchKey, JSON.stringify(objective));
          const destination = this.catalog.nativeSearch.buildUrl(arguments_);
          this.setState("checking");
          this.navigationInProgress = true;
          location.assign(destination);
          return;
        }
        this.setState("checking");
        let products = [];
        const matchingCandidate = this.candidates.products.find((p) =>
          String(p.product_id) === String(query) || (p.handle && p.handle.toLowerCase() === String(query).toLowerCase())
        );
        if (matchingCandidate) {
          products = [matchingCandidate];
        } else if (/^\d+$/.test(String(query)) || String(query).startsWith("gid://")) {
          const observation = await this.catalog.executeTool(
            { name: "get_product", arguments: { product_reference: query } },
            { candidateProducts: this.candidates.products, currentProductHandle: this.currentProductHandle, currentProductId: this.currentProductId }
          );
          products = Array.isArray(observation?.data?.products) ? observation.data.products : [];
        } else {
          const catalogResult = await this.catalog.search(query, 8);
          products = catalogResult.products || [];
        }
        this.pendingCatalogProducts = products;
        this.pendingCatalogQuery = query;
        const observation = {
          tool: toolName,
          ok: true,
          source: "catalog_adapter",
          data: { products, coverage: "bounded_search" },
          observed_at_ms: Date.now(),
        };
        await this.submitShoppingRequest(this.pendingTranscript, {
          query,
          products,
        }, this.pendingTurn, observation);
      } catch (_) {
        this.setState("failed", { error: toolName === "get_product"
          ? "I couldn’t identify that product. Please tell me its exact name."
          : "I couldn’t search this store right now. Please try again." });
      }
    }

    async resumeNativeSearch() {
      if (this.nativeSearchResuming) return;
      let objective = null;
      try { objective = JSON.parse(sessionStorage.getItem(this.nativeSearchKey) || "null"); } catch (_) {}
      if (!objective || objective.schema_version !== "1.0.0") return;
      if (objective.shop_id !== (this.root.dataset.shopDomain || location.host) || objective.expires_at_ms < Date.now()) {
        sessionStorage.removeItem(this.nativeSearchKey);
        return;
      }
      this.nativeSearchResuming = true;
      this.setOpen(true);
      this.pendingTranscript = String(objective.transcript || "");
      this.pendingTurn = { turnId: objective.turn_id, requestRevision: objective.request_revision, pageEpoch: this.pageEpoch };
      this.client.revision = Math.max(this.client.revision, Number(objective.request_revision));
      this.setState("checking");
      try {
        const observed = await this.catalog.nativeSearch.observe(objective.arguments, 20);
        this.currentNativeSearch = observed;
        this.pendingCatalogProducts = observed.products;
        this.pendingCatalogQuery = objective.arguments.query;
        const observation = {
          tool: "search_catalog", ok: true, source: "native_search_rendered",
          data: { products: observed.products, coverage: "rendered_page", native_search: observed.native_search },
          observed_at_ms: Date.now(),
        };
        sessionStorage.removeItem(this.nativeSearchKey);
        await this.submitShoppingRequest(this.pendingTranscript, { query: this.pendingCatalogQuery, products: observed.products }, this.pendingTurn, observation);
      } catch (error) {
        sessionStorage.removeItem(this.nativeSearchKey);
        console.error('[Drake] Native search observation failed:', error);
        this.addMessage("assistant", "Here are the store’s search results. If you want me to open one, please tell me its product name.");
        this.pendingTurn = null;
        this.setState("ready");
      } finally {
        this.nativeSearchResuming = false;
        this.offerConversationResume();
      }
    }

    offerConversationResume() {
      try {
        if (sessionStorage.getItem(this.conversationResumeKey) !== "requested") return;
        sessionStorage.removeItem(this.conversationResumeKey);
        this.setOpen(true);
        this.privacy.textContent = "Your conversation paused during navigation. Press Resume conversation to continue hands-free.";
        this.conversationButton.textContent = "Resume conversation";
      } catch (_) {}
    }

    async initializePageSearch() {
      if (sessionStorage.getItem(this.nativeSearchKey)) {
        await this.resumeNativeSearch();
        return;
      }
      await this.refreshCurrentNativeSearch();
      this.offerConversationResume();
    }

    async refreshCurrentNativeSearch() {
      if (!this.catalog?.nativeSearch || !location.pathname.endsWith('/search')) return;
      const params = new URLSearchParams(location.search);
      if (!params.get('q')) return;
      const arguments_ = { query: params.get('q') };
      if (params.get('sort_by')) arguments_.sort_by = params.get('sort_by');
      if (params.get('filter.v.price.gte')) arguments_.min_price = params.get('filter.v.price.gte');
      if (params.get('filter.v.price.lte')) arguments_.max_price = params.get('filter.v.price.lte');
      try {
        const observed = await this.catalog.nativeSearch.observe(arguments_, 20);
        this.currentNativeSearch = observed;
        this.pendingCatalogProducts = observed.products;
        this.pendingCatalogQuery = arguments_.query;
        this.candidates = new CandidateSet(observed.products);
      } catch (_) {
        this.currentNativeSearch = null;
      }
    }

    async retrieveToolObservation(request, turnId, requestRevision) {
      try {
        if (!this.pendingTurn || this.pendingTurn.turnId !== turnId || this.pendingTurn.requestRevision !== requestRevision) return;
        this.setState("checking");
        const observation = await this.catalog.executeTool(request, {
          currentProductHandle: this.currentProductHandle,
          currentProductId: this.currentProductId,
          candidateProducts: this.candidates.products,
        });
        const products = Array.isArray(observation?.data?.products) ? observation.data.products : [];
        this.pendingCatalogProducts = products.length ? products : this.pendingCatalogProducts;
        this.pendingCatalogQuery = request.arguments?.query || request.arguments?.product_reference || request.arguments?.collection_reference || request.name;
        await this.submitShoppingRequest(this.pendingTranscript, {
          query: this.pendingCatalogQuery,
          products: this.pendingCatalogProducts,
        }, this.pendingTurn, observation);
      } catch (_) {
        this.setState("failed", { error: "I couldn’t use that store capability right now. Please try again." });
      }
    }

    async submitShoppingRequest(text, catalogEvidence = null, existingTurn = null, toolObservation = null) {
      if (!this.client || !this.bridge || !this.catalog) {
        this.setState("failed", { error: "Drake is not connected to this store yet. You can try again shortly." });
        return;
      }
      try {
        this.pendingTranscript = text;
        this.setState("checking");
        await this.client.connect();
        const includeStoreContext = Boolean(existingTurn || catalogEvidence || toolObservation);
        let currentCart = null;
        if (includeStoreContext) {
          try {
            currentCart = await this.bridge.readAuthoritativeCart();
            if (typeof this.bridge.describeCart === "function") currentCart = await this.bridge.describeCart(currentCart);
          } catch (_) {
            throw new Error("authoritative_cart_unavailable");
          }
        }
        let bridgeTools = [];
        try { if (includeStoreContext) {
          bridgeTools = typeof this.bridge.getAvailableTools === "function" ? await this.bridge.getAvailableTools() : [];
        } } catch (_) {}
        let catalogTools = [];
        try { if (includeStoreContext) {
          catalogTools = typeof this.catalog.getAvailableTools === "function" ? await this.catalog.getAvailableTools() : [];
        } } catch (_) {}
        const availableTools = [...new Set([...bridgeTools, ...catalogTools])].sort();
        
        const combinedProducts = [];
        const seenIds = new Set();
        const addProduct = (p) => {
          if (p && p.product_id && !seenIds.has(String(p.product_id))) {
            seenIds.add(String(p.product_id));
            combinedProducts.push(p);
          }
        };
        if (catalogEvidence?.products) {
          catalogEvidence.products.forEach(addProduct);
        }
        if (includeStoreContext && this.currentProductHandle && typeof this.catalog.getByHandle === "function") {
          try {
            const currentProduct = await this.catalog.getByHandle(this.currentProductHandle);
            if (currentProduct) addProduct(currentProduct);
          } catch (_) {}
        }
        if (this.candidates?.products?.length) {
          this.candidates.products.forEach(addProduct);
        }

        const turn = existingTurn || {
          turnId: `turn_${crypto.randomUUID().replaceAll("-", "")}`,
          requestRevision: Math.max(1, this.client.revision + 1),
          pageEpoch: this.pageEpoch,
        };
        if (!existingTurn) {
          this.client.revision = turn.requestRevision;
          this.lastRevision = turn.requestRevision;
          try { sessionStorage.setItem(this.revisionKey, String(this.lastRevision)); } catch (_) {}
          this.pendingTurn = turn;
        }
        const payload = {
          turn_id: turn.turnId,
          request_revision: turn.requestRevision,
          page_epoch: turn.pageEpoch,
          transcript: text,
          current_product_id: this.currentProductId,
          page_context: this.pageContext,
          context_phase: includeStoreContext ? "action" : "decision",
        };
        if (!includeStoreContext && this.currentNativeSearch) {
          payload.evidence = {
            snapshot_id: `native_${Date.now()}`, shop_id: this.root.dataset.shopDomain || location.host,
            currency: window.Shopify?.currency?.active || "USD", observed_at_ms: Date.now(),
            query: this.currentNativeSearch.native_search.query, products: this.currentNativeSearch.products,
          };
          payload.displayed_search = this.currentNativeSearch.native_search;
        }
        if (includeStoreContext) {
          payload.evidence = {
            snapshot_id: `browser_${Date.now()}`,
            shop_id: currentCart.shop_id,
            currency: currentCart.currency,
            observed_at_ms: Date.now(),
            query: catalogEvidence?.query || this.pendingCatalogQuery || null,
            products: combinedProducts,
          };
          payload.current_cart = currentCart;
          payload.available_tools = availableTools;
          if (toolObservation) payload.tool_observation = toolObservation;
        }
        this.client.sendShoppingTurn(payload);
        this.setState("interpreting");
      } catch (error) {
        console.error('[Drake] Store context request failed:', error);
        this.setState("failed", { error: "I couldn’t read the store right now. Please try again." });
      }
    }

    async executeAuthorizedCommand(command) {
      if (!command || !this.bridge || !this.client) {
        this.setState("failed", { error: "The requested cart action is unavailable." });
        return;
      }
      this.setState("updating");
      try {
        if (command.operation === "navigate_storefront") this.navigationInProgress = true;
        const result = await this.bridge.executeCommand(command);
        this.pendingReceipts.set(command.command_id, {
          result,
          request_revision: command.request_revision,
        });
        this.client.sendCommandResult(command.command_id, result);
        if (result.outcome === "verified_success" || result.outcome === "verified_no_op") {
          this.setState("updating");
        } else if (result.outcome === "human_handoff" || result.outcome === "navigation_handoff") {
          this.setState("completed", { verifiedReceipt: result });
        } else {
          this.setState("failed", { error: shopperError(result.errors?.[0]) });
        }
      } catch (_) {
        this.setState("failed", { error: "I couldn’t verify the cart update." });
      }
    }
    addMessage(role, text) {
      const message = element("p", `drake-message drake-message--${role}`, text);
      this.conversation.insertBefore(message, this.caption);
      this.conversation.scrollTop = this.conversation.scrollHeight;
      this.messageHistory.push({ role, text: String(text || "") });
      this.messageHistory = this.messageHistory.slice(-16);
      try { sessionStorage.setItem(this.historyKey, JSON.stringify(this.messageHistory)); } catch (_) {}
    }
  }

  window.IShopDrakeWidget = { CandidateSet, DrakeWidget, WidgetState, STATE_LABELS, shopperError };
  const root = document.getElementById("ishop-drake-root");
  if (root && window.IShopVoiceSession) {
    const widget = new DrakeWidget(root, window.IShopVoiceSession);
    window.IShopDrake = widget;
    window.addEventListener("ishop:voice-configured", (event) => widget.setClient(event.detail.client, event.detail));
    window.addEventListener("ishop:bootstrap-failed", () => widget.setState("failed", { error: "Drake could not connect. Please try again later." }));
  }
})();

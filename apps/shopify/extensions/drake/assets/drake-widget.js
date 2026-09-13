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
      this.error = name === "failed" ? String(detail?.error || "Please try again.") : "";
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

  class DrakeWidget {
    constructor(root, voice) {
      this.root = root;
      this.voice = voice;
      this.state = new WidgetState();
      this.client = null;
      this.bridge = null;
      this.catalog = null;
      this.candidates = new CandidateSet([]);
      this.capture = null;
      this.playback = voice ? new voice.AudioPlayback() : null;
      this.open = false;
      this.render();
      this.bind();
      this.setState("initializing");
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
      controls.append(this.micButton, this.stopButton);
      this.panel.append(header, this.status, this.conversation, form, controls, element("p", "drake-privacy", "Your microphone starts only when you press Start speaking."));
      this.root.replaceChildren(this.launcher, this.panel);
    }
    bind() {
      this.launcher.addEventListener("click", () => this.setOpen(!this.open));
      this.closeButton.addEventListener("click", () => this.setOpen(false));
      this.micButton.addEventListener("click", () => this.startListening());
      this.stopButton.addEventListener("click", () => this.finishListening());
      this.panel.querySelector("form").addEventListener("submit", (event) => { event.preventDefault(); this.submitText(); });
    }
    setClient(client, integrations) {
      this.client = client;
      this.bridge = integrations?.bridge || null;
      this.catalog = integrations?.catalog || null;
      this.client.onEvent = (event) => this.handleVoiceEvent(event);
      this.setState("ready");
    }
    setOpen(open) {
      this.open = Boolean(open);
      this.panel.hidden = !this.open;
      this.launcher.setAttribute("aria-expanded", String(this.open));
      this.launcher.setAttribute("aria-label", this.open ? "Close Drake shopping assistant" : "Open Drake shopping assistant");
      if (this.open) this.textInput.focus();
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
        this.state.acceptFinal(event.text);
        this.caption.textContent = "";
        this.addMessage("shopper", event.text || "");
        this.submitShoppingRequest(event.text || "");
      } else if (event.type === "error") {
        this.setState("failed", { error: "I couldn’t process that. Please try again or type your request." });
      } else if (event.type === "shopping_result") {
        if (event.spoken_response) this.addMessage("assistant", event.spoken_response);
        this.playTts(event.tts_audio_chunks);
        if (event.status === "clarification_needed") {
          this.setState("clarifying");
        } else if (event.authorized_command) {
          this.executeAuthorizedCommand(event.authorized_command);
        } else if (event.status === "rejected" || event.status === "error") {
          this.setState("failed", { error: event.spoken_response || "I couldn’t complete that request." });
        }
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
          });
        } catch (_) {
          this.setState("failed", { error: "Voice playback failed. The text response is still available." });
        }
      }
    }

    renderProducts(products) {
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
          this.textInput.value = `the ${index + 1} one`;
          this.textInput.focus();
        });
        card.append(choose);
        this.cards.append(card);
      });
    }

    async submitShoppingRequest(text) {
      if (!this.client || !this.bridge || !this.catalog) {
        this.setState("failed", { error: "Drake is not connected to this store yet. You can try again shortly." });
        return;
      }
      try {
        this.setState("checking");
        const [catalogResult, currentCart] = await Promise.all([
          this.catalog.search(text, 8),
          this.bridge.readAuthoritativeCart(),
        ]);
        this.renderProducts(catalogResult.products);
        const requestRevision = Math.max(1, this.client.revision + 1);
        this.client.revision = requestRevision;
        this.client.sendShoppingTurn({
          turn_id: `turn_${crypto.randomUUID().replaceAll("-", "")}`,
          request_revision: requestRevision,
          page_epoch: 1,
          transcript: text,
          evidence: {
            snapshot_id: `browser_${Date.now()}`,
            shop_id: currentCart.shop_id,
            currency: currentCart.currency,
            observed_at_ms: Date.now(),
            products: catalogResult.products,
          },
          current_cart: currentCart,
        });
        this.setState("interpreting");
      } catch (_) {
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
        const result = await this.bridge.executeCommand(command);
        this.client.sendCommandResult(command.command_id, result);
        if (result.outcome === "verified_success" || result.outcome === "verified_no_op") {
          this.setState("completed", { verifiedReceipt: result });
          this.addMessage("assistant", "Your cart is updated and verified.");
        } else if (result.outcome === "human_handoff") {
          this.setState("completed", { verifiedReceipt: result });
        } else {
          this.setState("failed", { error: result.errors?.[0] || "The cart did not reach the requested state." });
        }
      } catch (_) {
        this.setState("failed", { error: "I couldn’t verify the cart update." });
      }
    }
    addMessage(role, text) {
      const message = element("p", `drake-message drake-message--${role}`, text);
      this.conversation.insertBefore(message, this.caption);
      this.conversation.scrollTop = this.conversation.scrollHeight;
    }
  }

  window.IShopDrakeWidget = { CandidateSet, DrakeWidget, WidgetState, STATE_LABELS };
  const root = document.getElementById("ishop-drake-root");
  if (root && window.IShopVoiceSession) {
    const widget = new DrakeWidget(root, window.IShopVoiceSession);
    window.IShopDrake = widget;
    window.addEventListener("ishop:voice-configured", (event) => widget.setClient(event.detail.client, event.detail));
    window.addEventListener("ishop:bootstrap-failed", () => widget.setState("failed", { error: "Drake could not connect. Please try again later." }));
  }
})();

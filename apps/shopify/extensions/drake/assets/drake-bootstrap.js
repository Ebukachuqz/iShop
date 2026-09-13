(() => {
  const root = document.getElementById("ishop-drake-root");
  if (!root) return;

  const url = new URL(root.dataset.bootstrapUrl, window.location.origin);
  url.searchParams.set("origin", window.location.origin);
  const resumeKey = `ishop:resume:${root.dataset.shopDomain || window.location.host}`;
  const savedResume = sessionStorage.getItem(resumeKey);
  if (savedResume) url.searchParams.set("resume", savedResume);

  fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        root.dataset.bootstrapState = "failed";
        root.dataset.bootstrapStatus = String(response.status);
        throw new Error(`Bootstrap failed with ${response.status}`);
      }
      return response.json();
    })
    .then(async ({ grant, resume_reference: resumeReference }) => {
      if (resumeReference) sessionStorage.setItem(resumeKey, resumeReference);
      const [bridgeModule, catalogModule] = await Promise.all([
        import(root.dataset.bridgeScriptUrl),
        import(root.dataset.catalogScriptUrl),
      ]);
      const ajax = new bridgeModule.AjaxCartAdapter(null, null, root.dataset.shopDomain);
      const webMcp = new bridgeModule.WebMcpAdapter(null, root.dataset.shopDomain);
      const actions = new bridgeModule.StandardActionsAdapter(null, root.dataset.shopDomain);
      const bridge = new bridgeModule.StorefrontBridge({
        ajaxAdapter: ajax,
        webMcpAdapter: webMcp,
        actionsAdapter: actions,
      });
      const catalog = new catalogModule.StorefrontCatalog();
      root.dataset.bootstrapState = "ready";
      delete root.dataset.bootstrapStatus;
      window.dispatchEvent(
        new CustomEvent("ishop:bootstrap-ready", {
          detail: { grant, shopDomain: root.dataset.shopDomain, currentProductId: root.dataset.currentProductId || null, currentProductHandle: root.dataset.currentProductHandle || null },
        }),
      );
      if (root.dataset.voiceWsUrl && window.IShopVoiceSession) {
        window.dispatchEvent(new CustomEvent("ishop:voice-configured", {
          detail: {
            client: new window.IShopVoiceSession.VoiceSessionClient({
              url: window.IShopVoiceSession.buildVoiceWebSocketUrl(
                root.dataset.voiceWsUrl,
                root.dataset.shopDomain,
              ),
              grant,
            }),
              bridge,
              catalog,
              currentProductId: root.dataset.currentProductId || null,
              currentProductHandle: root.dataset.currentProductHandle || null,
          },
        }));
      }
    })
    .catch(() => {
      root.dataset.bootstrapState ||= "failed";
      window.dispatchEvent(new CustomEvent("ishop:bootstrap-failed"));
    });
})();

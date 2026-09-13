(() => {
  const root = document.getElementById("ishop-drake-root");
  if (!root) return;

  const url = new URL(root.dataset.bootstrapUrl, window.location.origin);
  url.searchParams.set("origin", window.location.origin);

  fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        root.dataset.bootstrapState = "failed";
        root.dataset.bootstrapStatus = String(response.status);
        throw new Error(`Bootstrap failed with ${response.status}`);
      }
      return response.json();
    })
  .then(({ grant }) => {
      root.dataset.bootstrapState = "ready";
      delete root.dataset.bootstrapStatus;
      window.dispatchEvent(
        new CustomEvent("ishop:bootstrap-ready", {
          detail: { grant, shopDomain: root.dataset.shopDomain },
        }),
      );
      if (root.dataset.voiceWsUrl && window.IShopVoiceSession) {
        window.dispatchEvent(new CustomEvent("ishop:voice-configured", {
          detail: {
            client: new window.IShopVoiceSession.VoiceSessionClient({
              url: root.dataset.voiceWsUrl,
              grant,
            }),
          },
        }));
      }
    })
    .catch(() => {
      root.dataset.bootstrapState ||= "failed";
      window.dispatchEvent(new CustomEvent("ishop:bootstrap-failed"));
    });
})();

(() => {
  const root = document.getElementById("ishop-drake-root");
  if (!root) return;

  const url = new URL(root.dataset.bootstrapUrl, window.location.origin);
  url.searchParams.set("origin", window.location.origin);

  fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) throw new Error(`Bootstrap failed with ${response.status}`);
      return response.json();
    })
    .then(({ grant }) => {
      window.dispatchEvent(
        new CustomEvent("ishop:bootstrap-ready", {
          detail: { grant, shopDomain: root.dataset.shopDomain },
        }),
      );
    })
    .catch(() => {
      window.dispatchEvent(new CustomEvent("ishop:bootstrap-failed"));
    });
})();

/* Same-origin Hub writes use the session opened by the desktop launcher. */
(() => {
  "use strict";
  const send = window.fetch.bind(window);
  const storageKey = "hub.ui.session";
  let proof = "";
  try { proof = localStorage.getItem(storageKey) || ""; } catch (_) { /* Storage can be unavailable. */ }

  const fragment = new URLSearchParams(location.hash.slice(1));
  const ticket = fragment.get("hub-session");
  let ready = Promise.resolve();
  if (ticket) {
    const view = fragment.get("view");
    history.replaceState(history.state, "", location.pathname + location.search + (view ? "#" + view : ""));
    ready = send("/api/ui/session", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticket })
    }).then(async response => {
      if (!response.ok) return;
      proof = (await response.json()).session;
      try { localStorage.setItem(storageKey, proof); } catch (_) { /* Keep the session on this page. */ }
    }).catch(() => {});
  }

  window.fetch = async (input, init) => {
    const request = input instanceof Request ? input : null;
    const method = (init?.method || request?.method || "GET").toUpperCase();
    if (method !== "POST") return send(input, init);
    const url = new URL(request?.url || input, document.baseURI);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return send(input, init);
    await ready;
    try { proof = localStorage.getItem(storageKey) || proof; } catch (_) { /* Use the page session. */ }
    if (!proof) return send(input, init);
    const headers = new Headers(init?.headers || request?.headers);
    headers.set("X-Hoard-UI-Session", proof);
    return send(input, { ...init, headers });
  };
})();

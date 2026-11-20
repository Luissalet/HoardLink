/* Same-origin Hub writes use the session opened by the desktop window or launcher. */
(() => {
  "use strict";
  const send = window.fetch.bind(window);
  const shell = window.hoardHubSession;
  const storageKey = "hub.ui.session";
  let proof = "";
  let renewal;
  let notice;
  try { proof = localStorage.getItem(storageKey) || ""; } catch (_) { /* Storage can be unavailable. */ }

  async function exchange(ticket) {
    if (!ticket) return false;
    const response = await send("/api/ui/session", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticket })
    });
    if (!response.ok) return false;
    const { session } = await response.json();
    if (!session) return false;
    proof = session;
    try { localStorage.setItem(storageKey, proof); } catch (_) { /* Keep the session on this page. */ }
    notice?.remove();
    notice = null;
    return true;
  }

  function renew(ticket) {
    if (!renewal) {
      renewal = (async () => {
        if (await exchange(ticket)) return true;
        return shell ? exchange(await shell.requestTicket()) : false;
      })().catch(() => false).finally(() => { renewal = null; });
    }
    return renewal;
  }

  function showRecovery() {
    if (notice || !document.body) return;
    const lang = document.documentElement.dataset.hoardApp === "hub"
      ? document.documentElement.lang : navigator.language;
    let spanish = lang.startsWith("es");
    try { spanish = (localStorage.getItem("hub.lang") || lang).startsWith("es"); } catch (_) { /* Use page language. */ }
    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = "/ui/browser-session.css";
    if (!document.querySelector('link[href="/ui/browser-session.css"]')) document.head.append(style);

    notice = document.createElement("div");
    notice.className = "hub-session-recovery";
    const message = document.createElement("span");
    message.role = "status";
    message.textContent = spanish ? "La sesión de escritura no está disponible." : "The write session is unavailable.";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = spanish ? "Recuperar sesión" : "Restore session";
    button.onclick = async () => {
      button.disabled = true;
      if (!await renew()) {
        message.textContent = spanish ? "No se ha podido recuperar la sesión. Inténtalo de nuevo."
          : "The session could not be restored. Try again.";
        button.disabled = false;
      }
    };
    notice.append(message, button);
    document.body.append(notice);
  }

  const fragment = new URLSearchParams(location.hash.slice(1));
  const ticket = fragment.get("hub-session");
  if (ticket) {
    const view = fragment.get("view");
    history.replaceState(history.state, "", location.pathname + location.search + (view ? "#" + view : ""));
  }
  if (shell || ticket) renew(ticket);
  shell?.onRefresh(() => { renew(); });

  window.fetch = async (input, init) => {
    const request = input instanceof Request ? input : null;
    const method = (init?.method || request?.method || "GET").toUpperCase();
    if (method !== "POST") return send(input, init);
    const url = new URL(request?.url || input, document.baseURI);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return send(input, init);
    if (renewal) await renewal;
    try { proof = localStorage.getItem(storageKey) || proof; } catch (_) { /* Use the page session. */ }
    if (proof) {
      const headers = new Headers(init?.headers || request?.headers);
      headers.set("X-Hoard-UI-Session", proof);
      init = { ...init, headers };
    }
    const response = await send(input, init);
    if (shell && response.status === 403) {
      const error = await response.clone().json().catch(() => null);
      if (error?.code === "hub_ui_session_required") showRecovery();
    }
    return response;
  };
})();

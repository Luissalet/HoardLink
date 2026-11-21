/* Same-origin Hub writes use the session opened by the desktop window or launcher. */
(() => {
  "use strict";
  const send = window.fetch.bind(window);
  const shell = window.hoardHubSession;
  const previousWindow = !shell && window.hoardWindow?.shell === "hoard-window";
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

  function isSpanish() {
    const lang = document.documentElement.dataset.hoardApp === "hub"
      ? document.documentElement.lang : navigator.language;
    try { return (localStorage.getItem("hub.lang") || lang).startsWith("es"); }
    catch (_) { return lang.startsWith("es"); }
  }

  function showRecovery() {
    if (notice || !document.body) return;
    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = "/ui/browser-session.css";
    if (!document.querySelector('link[href="/ui/browser-session.css"]')) document.head.append(style);

    notice = document.createElement("div");
    notice.className = "hub-session-recovery";
    const message = document.createElement("span");
    message.role = "status";
    notice.append(message);
    if (previousWindow) {
      const translate = () => {
        message.textContent = isSpanish()
          ? "Esta ventana es de una versión anterior. Ciérrala y vuelve a abrir el Hub."
          : "This window is from an earlier version. Close it and reopen the Hub.";
      };
      translate();
      new MutationObserver(translate).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
      notice.classList.add("hub-session-upgrade");
      const content = document.getElementById("hub-content");
      if (content) content.prepend(notice);
      else document.body.append(notice);
      return;
    }
    const spanish = isSpanish();
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
    notice.append(button);
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
  if (previousWindow) {
    window.addEventListener("DOMContentLoaded", async () => {
      if (renewal) await renewal;
      if (!proof) showRecovery();
    }, { once: true });
  }

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
    if ((shell || previousWindow) && response.status === 403) {
      const error = await response.clone().json().catch(() => null);
      if (error?.code === "hub_ui_session_required") showRecovery();
    }
    return response;
  };
})();

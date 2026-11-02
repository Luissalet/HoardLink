/* A delivery notification opens this Hub page before navigating to HomeHoard. */
(() => {
  "use strict";
  const status = document.getElementById("status");
  const retry = document.getElementById("retry");
  let language;
  try { language = localStorage.getItem("hub.lang"); } catch (_) { /* browser storage may be disabled */ }
  const es = (language || navigator.language || "es").toLowerCase().startsWith("es");
  document.documentElement.lang = es ? "es" : "en";
  retry.textContent = es ? "Reintentar" : "Retry";
  async function open() {
    retry.hidden = true;
    status.textContent = es ? "Abriendo HomeHoard…" : "Opening HomeHoard…";
    try {
      const id = location.pathname.match(/^\/purchases\/(\d+)\/home\/?$/)[1];
      const response = await fetch(`/api/purchases/${id}/open-home`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
      });
      const result = await response.json();
      if (!response.ok || !result.ok || !result.url) throw new Error(result.error || `HTTP ${response.status}`);
      location.replace(result.url);
    } catch (error) {
      status.textContent = (es ? "No se ha podido abrir HomeHoard: " : "Could not open HomeHoard: ") + error.message;
      retry.hidden = false;
    }
  }
  retry.onclick = open;
  open();
})();

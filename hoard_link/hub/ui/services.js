/* Family owners: use the Hub's existing type, colours and controls. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const { el, api, L, fmtWhen } = H.ctx;
  const text = (es, en) => L({ es, en });
  const labels = { web: ["Web", "Web"], media: ["Descargas", "Downloads"], stt: ["Transcripción", "Transcription"],
    tts: ["Voz", "Voice"], docs: ["Documentos y OCR", "Documents and OCR"], embed: ["Embeddings", "Embeddings"] };
  const states = { ready: ["Responde", "Responding"], down: ["No responde", "Not responding"],
    missing: ["Sin registrar", "Not registered"], incompatible: ["Pendiente de actualizar", "Update needed"], disabled: ["Apagado", "Disabled"] };
  let list, note, button;
  async function load(quiet) {
    if (!list) return;
    if (!quiet) button.disabled = true;
    try {
      const r = await api("/api/services/status" + (quiet ? "" : "?refresh=1"));
      list.replaceChildren();
      if (!r.ok) { note.textContent = r.error || text("No se pudo consultar el Hub.", "Could not reach the Hub."); return; }
      for (const s of r.services || []) {
        const row = el("div", "svc-row");
        row.setAttribute("role", "listitem");
        row.appendChild(el("strong", "", text(...(labels[s.service] || [s.service, s.service]))));
        const owner = el(s.url ? "a" : "span", "", s.name);
        if (s.url) { owner.href = s.url; owner.target = "_blank"; owner.rel = "noopener noreferrer"; }
        row.appendChild(owner);
        row.appendChild(el("span", s.available ? "ok" : "warn", text(...(states[s.state] || [s.state, s.state]))));
        list.appendChild(row);
      }
      note.textContent = text("Catálogos comprobados: ", "Catalogues checked: ") + fmtWhen(r.checked_at) +
        text(". Los modelos y las herramientas se comprueban al usarlos.", ". Models and tools are checked when used.");
    } catch { note.textContent = text("No se pudo consultar el Hub. Pulsa Actualizar para reintentar.", "Could not reach the Hub. Press Refresh to retry."); }
    finally { button.disabled = false; }
  }
  H.register("services", {
    placement: "tab", label: { es: "Servicios", en: "Services" }, order: 81,
    mount(container) {
      const style = el("style");
      style.textContent = ".svc-list{max-width:900px}.svc-row{display:grid;grid-template-columns:minmax(130px,1fr) minmax(160px,1.5fr) minmax(120px,1fr);gap:12px;padding:14px 0;border-bottom:1px solid var(--line);align-items:baseline}.svc-row a{color:var(--text);text-underline-offset:3px}.svc-note{margin-top:16px;max-width:75ch}@media(max-width:600px){.svc-row{grid-template-columns:1fr auto}.svc-row>strong{grid-column:1/-1}.svc-row{gap:6px}}";
      container.appendChild(style);
      const bar = el("div", "tab-tools"); button = el("button", "small", text("Actualizar", "Refresh"));
      button.onclick = () => load(false); bar.appendChild(button); container.appendChild(bar);
      list = el("div", "svc-list"); list.setAttribute("role", "list"); container.appendChild(list);
      note = el("p", "hint svc-note", text("Consultando los servicios…", "Checking services…"));
      note.setAttribute("aria-live", "polite"); container.appendChild(note);
    }, load, tick: () => load(true),
  });
})();

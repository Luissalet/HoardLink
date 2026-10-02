/* Hoard Hub UI — global search ("Buscar" / "Search"): one box, every app's own search, results grouped by app.
   Ctrl+K focuses it from anywhere on the page. Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const ctx = H.ctx;
  const { el, api, toast, L } = ctx;

  const S = {
    label: { es: "Buscar", en: "Search" },
    ph: { es: "Busca en todas las apps y en el correo… (Ctrl+K)", en: "Search every app and the mail… (Ctrl+K)" },
    go: { es: "Buscar", en: "Search" },
    searching: { es: "buscando…", en: "searching…" },
    nothing: { es: "Sin resultados.", en: "No results." },
    hits: { es: "resultados", en: "results" },
    in_ms: { es: "ms", en: "ms" },
    skipped: { es: "Sin búsqueda propia", en: "No search of their own" },
    err: { es: "no ha respondido", en: "did not answer" },
    open: { es: "Abrir", en: "Open" },
    own_mail: { es: "Correo y chats", en: "Mail and chats" },
    own_refs: { es: "Enlaces", en: "Links" },
    own_notify: { es: "Avisos", en: "Notifications" },
    hint: { es: "Pregunta a cada app por su cuenta y junta las respuestas.", en: "Asks each app on its own and gathers the answers." },
  };
  const t = (k) => L(S[k]);
  const OWN = { mail: "own_mail", refs: "own_refs", notify: "own_notify" };
  const GLYPH = { mail: "@", refs: "↔", notify: "!" };
  let box, input, out, status, tabKey = "search";

  function injectCss() {
    if (document.getElementById("css-search")) return;
    const css = document.createElement("style");
    css.id = "css-search";
    css.textContent = `
      .sr-group { margin-top: 12px; }
      .sr-ghead { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; font-weight: 600; }
      .sr-ghead img, .sr-ghead .sr-glyph { width: 20px; height: 20px; border-radius: 5px; object-fit: cover; background: var(--hoard-sunken); flex: none; }
      .sr-ghead .sr-glyph { display: inline-flex; align-items: center; justify-content: center; font-size: 12px; color: var(--accent-2); border: 1px solid var(--line); }
      .sr-ghead small { color: var(--muted); font-weight: 400; font-family: var(--mono); }
      .sr-hit { display: grid; gap: 1px; padding: 6px 10px; margin-bottom: 4px; border: 1px solid var(--line); border-radius: 9px; background: var(--card-2); cursor: pointer; }
      .sr-hit:hover { border-color: var(--hoard-border-hover); }
      .sr-hit .sr-title { font-weight: 600; }
      .sr-hit .sr-snip { color: var(--muted); font-size: 12.5px; }
      .sr-hit .sr-url { color: var(--blue); font-size: 11.5px; font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .sr-err { color: var(--red); font-size: 12px; }
      .sr-skipped { margin-top: 12px; color: var(--muted); font-size: 12px; }
    `;
    document.head.appendChild(css);
  }

  async function openHit(group, hit) {
    if (hit.url) { window.open(hit.url, "_blank", "noopener"); return; }
    if (group.own) return;
    const r = await api(`/api/apps/${encodeURIComponent(group.app)}/open`, { mode: "window" });
    if (!r.ok) toast(r.error || "error", "err");
  }

  async function run() {
    const q = input.value.trim();
    out.innerHTML = "";
    if (!q) { status.textContent = ""; return; }
    status.textContent = t("searching");
    const r = await api(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
    if (!r.ok) { status.textContent = ""; out.appendChild(el("div", "hint", r.error || "error")); return; }
    status.textContent = `${r.hits} ${t("hits")} · ${r.took_ms} ${t("in_ms")}`;
    const withHits = r.groups.filter((g) => g.results.length);
    const failed = r.groups.filter((g) => !g.results.length && g.error);
    if (!withHits.length) out.appendChild(el("div", "hint", t("nothing")));
    for (const g of withHits) {
      const box2 = el("div", "sr-group");
      const head = el("div", "sr-ghead");
      if (g.own) head.appendChild(el("span", "sr-glyph", GLYPH[g.app] || "·"));
      else { const img = el("img"); img.src = `/api/apps/${encodeURIComponent(g.app)}/icon`; img.alt = ""; img.onerror = () => img.remove(); head.appendChild(img); }
      head.appendChild(el("span", "", g.own ? t(OWN[g.app] || "label") : g.name));
      head.appendChild(el("small", "", `${g.tool} · ${g.ms} ms`));
      box2.appendChild(head);
      for (const h of g.results) {
        const row = el("div", "sr-hit");
        row.appendChild(el("div", "sr-title", h.title));
        if (h.snippet) row.appendChild(el("div", "sr-snip", h.snippet));
        if (h.url) row.appendChild(el("div", "sr-url", h.url));
        row.onclick = () => openHit(g, h);
        box2.appendChild(row);
      }
      out.appendChild(box2);
    }
    for (const g of failed) out.appendChild(el("div", "sr-err", `${g.name || g.app} · ${g.tool}: ${t("err")} (${g.error})`));
    const nos = (r.skipped || []).filter((s) => s.reason === "no search tool");
    if (nos.length) out.appendChild(el("div", "sr-skipped", `${t("skipped")}: ${nos.map((s) => s.app).join(", ")}`));
  }

  function focusSearch() {
    const toggle = document.getElementById("family-toggle");
    const body = document.getElementById("family-body");
    if (body && body.hidden && toggle) toggle.click();
    const btn = document.querySelector(`#family-tabs .tab[data-tab="${tabKey}"]`);
    if (btn) btn.click();
    if (input) { input.focus(); input.select(); }
  }

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); focusSearch(); }
  });

  H.register("search", {
    placement: "tab",
    label: S.label,
    order: 20,
    mount(container) {
      injectCss();
      box = container;
      const tools = el("div", "tab-tools");
      input = el("input", "sr-q"); input.type = "search"; input.placeholder = t("ph"); input.style.minWidth = "360px";
      input.onkeydown = (e) => { if (e.key === "Enter") run(); };
      const btn = el("button", "primary small", t("go")); btn.onclick = run;
      status = el("span", "hint");
      const hint = el("span", "hint", t("hint"));
      for (const x of [input, btn, status, hint]) tools.appendChild(x);
      box.appendChild(tools);
      out = el("div", "sr-out"); box.appendChild(out);
    },
    load() { if (input) input.placeholder = t("ph"); },
  });
})();

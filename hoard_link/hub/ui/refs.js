/* Hoard Hub UI — references between apps ("Enlaces" / "Links").
   Search by a hoard:// uri (its neighbourhood, 1 or 2 hops) or by a label fragment; recent links below.
   Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const ctx = H.ctx;
  const { el, api, toast, L, fmtWhen } = ctx;

  const S = {
    label: { es: "Enlaces", en: "Links" },
    ph: { es: "hoard://app/tipo/id o parte de un nombre…", en: "hoard://app/kind/id or part of a name…" },
    go: { es: "Buscar", en: "Search" },
    depth1: { es: "1 salto", en: "1 hop" }, depth2: { es: "2 saltos", en: "2 hops" },
    hint: { es: "Qué está conectado con un registro de cualquier app.", en: "What is connected to a record of any app." },
    recent: { es: "Enlaces recientes", en: "Recent links" },
    none: { es: "Aún no hay enlaces. Las apps los crean al vincular registros (por ejemplo una compra con su factura).", en: "No links yet. Apps create them when they connect records (for example a purchase and its invoice)." },
    no_match: { es: "Nada con ese nombre.", en: "Nothing with that name." },
    alone: { es: "Este registro aún no está enlazado con nada.", en: "This record is not linked to anything yet." },
    matches: { es: "Registros encontrados", en: "Records found" },
    around: { es: "Alrededor de", en: "Around" },
    open_app: { es: "Abrir app", en: "Open app" },
    unlink: { es: "Quitar", en: "Remove" },
    confirm: { es: "¿Quitar este enlace?", en: "Remove this link?" },
    removed: { es: "Enlace quitado", en: "Link removed" },
    links: { es: "enlaces", en: "links" },
    by: { es: "por", en: "by" },
    bad_uri: { es: "No es una referencia hoard:// válida.", en: "Not a valid hoard:// reference." },
  };
  const t = (k) => L(S[k]);
  let box, input, depthSel, out, recentBox;

  function injectCss() {
    if (document.getElementById("css-refs")) return;
    const css = document.createElement("style");
    css.id = "css-refs";
    css.textContent = `
      .rf-group { margin-top: 10px; }
      .rf-ghead { display: flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 4px; }
      .rf-ghead img { width: 20px; height: 20px; border-radius: 5px; object-fit: cover; background: var(--hoard-sunken); }
      .rf-ghead a { color: var(--blue); font-weight: 400; font-size: 12px; text-decoration: none; }
      .rf-node { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; padding: 5px 9px; margin-bottom: 4px; border: 1px solid var(--line); border-radius: 9px; background: var(--card-2); cursor: pointer; }
      .rf-node:hover { border-color: var(--hoard-border-hover); }
      .rf-node.root { border-color: var(--accent); cursor: default; }
      .rf-node .rf-label { font-weight: 600; }
      .rf-node .rf-uri { color: var(--muted); font-family: var(--mono); font-size: 11.5px; }
      .rf-rel { color: var(--accent-2); font-size: 11.5px; }
      .rf-edges { margin-top: 10px; display: grid; gap: 3px; font-size: 12.5px; }
      .rf-edge { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; padding: 3px 0; border-top: 1px solid var(--line); }
      .rf-edge .rf-end { color: var(--text); cursor: pointer; } .rf-edge .rf-end:hover { color: var(--accent-2); }
      .rf-edge .rf-arrow { color: var(--muted); font-family: var(--mono); }
      .rf-edge .rf-meta { color: var(--muted); font-size: 11.5px; margin-left: auto; }
      .rf-title { margin: 12px 0 4px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .4px; }
    `;
    document.head.appendChild(css);
  }

  const labelOf = (n) => n.label || [n.kind, n.id].filter(Boolean).join(" ") || n.uri;
  const short = (u) => (u || "").replace(/^hoard:\/\//, "");

  function appHead(app, appUrl) {
    const h = el("div", "rf-ghead");
    const img = el("img"); img.src = (window.hubAppIcon ? window.hubAppIcon(app) : `/api/apps/${encodeURIComponent(app)}/icon`); img.alt = ""; img.onerror = () => img.remove();
    h.appendChild(img); h.appendChild(el("span", "", app || "?"));
    if (app) { const a = el("a", "", t("open_app")); a.href = "#"; a.onclick = async (e) => { e.preventDefault(); const r = await api(`/api/apps/${encodeURIComponent(app)}/open`, { mode: "window" }); if (!r.ok) toast(r.error || "error", "err"); }; h.appendChild(a); }
    return h;
  }

  async function go(value) {
    const v = (value !== undefined ? value : input.value).trim();
    if (value !== undefined) input.value = v;
    out.innerHTML = "";
    if (!v) { return showRecent(); }
    if (v.startsWith("hoard://")) {
      const r = await api(`/api/refs?uri=${encodeURIComponent(v)}&depth=${depthSel.value}`);
      if (!r.ok) { out.appendChild(el("div", "hint", r.error || t("bad_uri"))); return; }
      return renderAround(r);
    }
    const r = await api(`/api/refs?q=${encodeURIComponent(v)}`);
    if (!r.ok) { out.appendChild(el("div", "hint", r.error || "error")); return; }
    const ms = r.matches || [];
    if (!ms.length) { out.appendChild(el("div", "hint", t("no_match"))); return; }
    out.appendChild(el("div", "rf-title", t("matches")));
    for (const m of ms) {
      const row = el("div", "rf-node");
      row.appendChild(el("span", "rf-label", m.title));
      row.appendChild(el("span", "rf-uri", short(m.uri)));
      row.appendChild(el("span", "hint", `${m.links} ${t("links")}`));
      row.onclick = () => go(m.uri);
      out.appendChild(row);
    }
  }

  function renderAround(r) {
    out.appendChild(el("div", "rf-title", `${t("around")} ${short(r.uri)}`));
    const root = r.nodes.find((n) => n.uri === r.uri);
    const others = r.nodes.filter((n) => n.uri !== r.uri);
    const byUri = Object.fromEntries(r.nodes.map((n) => [n.uri, n]));
    const rootRow = el("div", "rf-node root");
    rootRow.appendChild(el("span", "rf-label", root ? labelOf(root) : short(r.uri)));
    rootRow.appendChild(el("span", "rf-uri", short(r.uri)));
    out.appendChild(rootRow);
    if (!others.length) { out.appendChild(el("div", "hint", t("alone"))); return; }
    const groups = {};
    for (const n of others) (groups[n.app || "?"] = groups[n.app || "?"] || []).push(n);
    for (const app of Object.keys(groups).sort()) {
      const g = el("div", "rf-group"); g.appendChild(appHead(app, groups[app][0].app_url));
      for (const n of groups[app]) {
        const row = el("div", "rf-node");
        const rels = r.edges.filter((e) => (e.from === n.uri && e.to === r.uri) || (e.to === n.uri && e.from === r.uri));
        row.appendChild(el("span", "rf-label", labelOf(n)));
        row.appendChild(el("span", "rf-uri", short(n.uri)));
        for (const e of rels) row.appendChild(el("span", "rf-rel", (e.from === r.uri ? "→ " : "← ") + e.rel));
        if (n.depth > 1) row.appendChild(el("span", "hint", `${n.depth}`));
        row.onclick = () => go(n.uri);
        g.appendChild(row);
      }
      out.appendChild(g);
    }
    const edges = el("div", "rf-edges");
    for (const e of r.edges) edges.appendChild(edgeRow(e, byUri, true));
    out.appendChild(edges);
  }

  function edgeRow(e, byUri, withRemove) {
    const row = el("div", "rf-edge");
    const end = (uri, label) => { const s = el("span", "rf-end", label || (byUri && byUri[uri] ? labelOf(byUri[uri]) : short(uri))); s.title = uri; s.onclick = () => go(uri); return s; };
    row.appendChild(end(e.from, e.from_label));
    row.appendChild(el("span", "rf-arrow", `—${e.rel}→`));
    row.appendChild(end(e.to, e.to_label));
    const meta = el("span", "rf-meta", `${t("by")} ${e.by} · ${fmtWhen(e.ts)}`);
    row.appendChild(meta);
    if (withRemove) {
      const b = el("button", "ghost small", t("unlink"));
      b.onclick = async (ev) => { ev.stopPropagation(); if (!confirm(t("confirm"))) return; const r = await api("/api/refs/remove", { id: e.id }); if (r.ok) { toast(t("removed"), "ok"); go(input.value); showRecent(); } else toast(r.error || "error", "err"); };
      row.appendChild(b);
    }
    return row;
  }

  async function showRecent() {
    const r = await api("/api/refs/recent?limit=30");
    recentBox.innerHTML = "";
    recentBox.appendChild(el("div", "rf-title", t("recent")));
    if (!r.ok || !(r.edges || []).length) { recentBox.appendChild(el("div", "hint", t("none"))); return; }
    const list = el("div", "rf-edges");
    for (const e of r.edges) list.appendChild(edgeRow(e, null, true));
    recentBox.appendChild(list);
  }

  H.register("refs", {
    placement: "tab",
    label: S.label,
    order: 40,
    mount(container) {
      injectCss();
      box = container;
      const tools = el("div", "tab-tools");
      input = el("input", "rf-q"); input.type = "search"; input.placeholder = t("ph"); input.style.minWidth = "320px";
      input.onkeydown = (e) => { if (e.key === "Enter") go(); };
      depthSel = el("select"); for (const [v, k] of [["1", "depth1"], ["2", "depth2"]]) { const o = el("option", "", t(k)); o.value = v; depthSel.appendChild(o); }
      const btn = el("button", "primary small", t("go")); btn.onclick = () => go();
      const hint = el("span", "hint", t("hint"));
      for (const x of [input, depthSel, btn, hint]) tools.appendChild(x);
      box.appendChild(tools);
      out = el("div", "rf-out"); box.appendChild(out);
      recentBox = el("div", "rf-recent"); box.appendChild(recentBox);
    },
    load() {
      if (input) { input.placeholder = t("ph"); }
      showRecent();
      if (input && input.value.trim()) go();
    },
    tick() { if (!input || !input.value.trim()) showRecent(); },
  });
})();

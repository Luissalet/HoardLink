/* Hoard Hub UI — the family's web service ("Web"): what the one shared fetcher is doing.
   Status (browser tier, engines, cache), the hosts table with "Desbloquear", a quick fetch box (readable text, markdown or
   page meta of any URL, optionally on this machine's own network) and a search box. Everything the pages return is shown as
   text, never as HTML. Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const ctx = H.ctx;
  const { el, api, toast, L, fmtWhen } = ctx;

  const S = {
    label: { es: "Web", en: "Web" },
    refresh: { es: "Actualizar", en: "Refresh" },
    off: { es: "El servicio web está apagado (hub.json: web.enabled).", en: "The web service is turned off (hub.json: web.enabled)." },
    browser: { es: "Navegador", en: "Browser" },
    browser_ok: { es: "disponible", en: "available" },
    browser_off: { es: "no disponible", en: "unavailable" },
    engines: { es: "Buscadores", en: "Engines" },
    cache: { es: "Caché", en: "Cache" },
    entries: { es: "páginas", en: "pages" },
    robots: { es: "robots.txt respetado", en: "robots.txt honoured" },
    robots_off: { es: "robots.txt ignorado", en: "robots.txt ignored" },
    interval: { es: "intervalo", en: "interval" },
    human_open: { es: "ventana abierta", en: "window open" },
    hosts: { es: "Sitios", en: "Hosts" },
    no_hosts: { es: "La familia aún no ha pedido nada a ningún sitio.", en: "The family has not fetched anything yet." },
    h_host: { es: "Sitio", en: "Host" }, h_status: { es: "Último estado", en: "Last status" },
    h_blocked: { es: "Bloqueado hasta", en: "Blocked until" }, h_interval: { es: "Intervalo", en: "Interval" },
    h_tier: { es: "Vía preferida", en: "Preferred tier" }, h_caller: { es: "Última app", en: "Last app" },
    h_when: { es: "Última petición", en: "Last request" },
    unblock: { es: "Desbloquear", en: "Unblock" },
    unblocked: { es: "Desbloqueado", en: "Unblocked" },
    open_human: { es: "Abrir para resolverlo", en: "Open to solve it" },
    opened: { es: "Abierto: resuélvelo y cierra la ventana", en: "Opened: solve it and close the window" },
    fetch_title: { es: "Leer una página", en: "Read a page" },
    url_ph: { es: "https://… (una página, un feed)", en: "https://… (a page, a feed)" },
    mode_readable: { es: "Texto legible", en: "Readable text" },
    mode_markdown: { es: "Markdown", en: "Markdown" },
    mode_meta: { es: "Metadatos", en: "Meta" },
    mode_feed: { es: "Feed", en: "Feed" },
    local: { es: "red local", en: "local network" },
    local_tip: { es: "Permite direcciones de este equipo y de tu red (solo para ti)", en: "Allow this machine's and your network's addresses (for you only)" },
    read: { es: "Leer", en: "Read" },
    reading: { es: "leyendo…", en: "reading…" },
    from_cache: { es: "de la caché", en: "from cache" },
    stale: { es: "caducada", en: "stale" },
    search_title: { es: "Buscar en la web", en: "Search the web" },
    q_ph: { es: "Qué buscar…", en: "What to look for…" },
    news: { es: "noticias", en: "news" },
    search: { es: "Buscar", en: "Search" },
    searching: { es: "buscando…", en: "searching…" },
    no_hits: { es: "Sin resultados.", en: "No results." },
    recent: { es: "Actividad reciente", en: "Recent activity" },
    none: { es: "—", en: "—" },
    error: { es: "error", en: "error" },
    ms: { es: "ms", en: "ms" },
  };
  const t = (k) => L(S[k]);
  let box, statusBox, hostsBox, recentBox, fetchOut, searchOut, urlIn, modeSel, localCb, queryIn, newsCb;

  function injectCss() {
    if (document.getElementById("css-web")) return;
    const css = document.createElement("style");
    css.id = "css-web";
    css.textContent = `
      .wb-head { margin: 14px 0 6px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .4px; }
      .wb-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 4px; }
      .wb-tbl { border-collapse: collapse; width: 100%; font-size: 12.5px; }
      .wb-tbl th, .wb-tbl td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
      .wb-tbl th { color: var(--muted); font-weight: 500; }
      .wb-tbl .ok { color: var(--green); } .wb-tbl .bad { color: var(--red); } .wb-tbl .warn { color: var(--amber); }
      .wb-meta { color: var(--muted); font-size: 12px; font-family: var(--mono); margin: 6px 0; }
      .wb-text { white-space: pre-wrap; word-break: break-word; max-height: 360px; overflow: auto; padding: 8px 10px; border: 1px solid var(--line); border-radius: 9px; background: var(--card-2); font-size: 12.5px; }
      .wb-kv { display: grid; grid-template-columns: 130px 1fr; gap: 2px 10px; font-size: 12.5px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 9px; background: var(--card-2); }
      .wb-kv b { color: var(--muted); font-weight: 500; } .wb-kv span { word-break: break-word; }
      .wb-hit { display: grid; gap: 1px; padding: 6px 10px; margin-bottom: 4px; border: 1px solid var(--line); border-radius: 9px; background: var(--card-2); }
      .wb-hit a { color: var(--text); font-weight: 600; text-decoration: none; } .wb-hit a:hover { color: var(--accent-2); }
      .wb-hit .u { color: var(--blue); font-size: 11.5px; font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .wb-hit .s { color: var(--muted); font-size: 12.5px; }
      .wb-err { color: var(--red); font-size: 12px; }
    `;
    document.head.appendChild(css);
  }

  const chip = (label, value, cls) => { const c = el("span", "chip" + (cls ? " " + cls : "")); c.appendChild(document.createTextNode(label + " ")); c.appendChild(el("b", "", value)); return c; };
  const fmtBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);
  const stCls = (s) => (s >= 200 && s < 400 ? "ok" : s >= 400 ? "bad" : "warn");

  function renderStatus(s) {
    statusBox.innerHTML = "";
    if (!s || !s.ok) { statusBox.appendChild(el("div", "wb-err", (s && s.error) || t("error"))); return; }
    if (!s.enabled) { statusBox.appendChild(el("div", "hint", t("off"))); return; }
    const row = el("div", "wb-chips");
    const b = s.browser || {};
    const bc = chip(t("browser"), b.available ? t("browser_ok") : t("browser_off"), b.available ? "on" : "warn");
    if (b.reason) bc.title = b.reason;
    row.appendChild(bc);
    if (b.human_window_open) row.appendChild(chip("", t("human_open"), "info"));
    row.appendChild(chip(t("engines"), (s.engines || []).join(", ") || t("none")));
    const c = s.cache || {};
    row.appendChild(chip(t("cache"), `${c.entries || 0} ${t("entries")} · ${fmtBytes(c.bytes || 0)}`));
    row.appendChild(chip(t("interval"), `${s.default_min_interval_s}s`));
    row.appendChild(el("span", "chip " + (s.respect_robots ? "on" : "warn"), s.respect_robots ? t("robots") : t("robots_off")));
    statusBox.appendChild(row);
    renderRecent(s.recent || []);
  }

  function renderRecent(rows) {
    recentBox.innerHTML = "";
    if (!rows.length) return;
    const tbl = el("table", "wb-tbl");
    for (const r of rows.slice(0, 10)) {
      const tr = el("tr");
      tr.appendChild(el("td", "", fmtWhen(r.ts)));
      tr.appendChild(el("td", "", r.caller));
      tr.appendChild(el("td", "", r.kind));
      tr.appendChild(el("td", "", r.host || r.detail || ""));
      tr.appendChild(el("td", r.ok ? "ok" : "bad", r.status ? String(r.status) : (r.error_kind || (r.ok ? "ok" : t("error")))));
      tr.appendChild(el("td", "", `${r.ms} ${t("ms")}${r.from_cache ? " · " + t("from_cache") : ""}`));
      tbl.appendChild(tr);
    }
    recentBox.appendChild(tbl);
  }

  function renderHosts(r) {
    hostsBox.innerHTML = "";
    if (!r || !r.ok) { hostsBox.appendChild(el("div", "wb-err", (r && r.error) || t("error"))); return; }
    if (!r.hosts.length) { hostsBox.appendChild(el("div", "hint", t("no_hosts"))); return; }
    const tbl = el("table", "wb-tbl");
    const head = el("tr");
    for (const k of ["h_host", "h_status", "h_blocked", "h_interval", "h_tier", "h_caller", "h_when"]) head.appendChild(el("th", "", t(k)));
    head.appendChild(el("th"));
    tbl.appendChild(head);
    for (const h of r.hosts) {
      const tr = el("tr");
      tr.appendChild(el("td", "", h.host));
      tr.appendChild(el("td", h.last_status ? stCls(h.last_status) : "", h.last_status ? String(h.last_status) : (h.last_error ? t("error") : t("none"))));
      const blocked = el("td", h.blocked_now ? "warn" : "", h.blocked_now ? `${fmtWhen(h.blocked_until_ts)} (${h.block_reason || "?"})` : t("none"));
      tr.appendChild(blocked);
      tr.appendChild(el("td", "", `${h.effective_min_interval_s}s`));
      tr.appendChild(el("td", "", h.preferred_tier || "http"));
      tr.appendChild(el("td", "", h.last_caller || t("none")));
      tr.appendChild(el("td", "", h.last_fetch_ts ? fmtWhen(h.last_fetch_ts) : t("none")));
      const act = el("td");
      if (h.blocked_now) {
        const b = el("button", "small", t("unblock"));
        b.onclick = async () => {
          const res = await api("/api/web/hosts/clear", { host: h.host });
          if (res.ok) { toast(`${t("unblocked")}: ${h.host}`, "ok"); load(); } else toast(res.error || t("error"), "err");
        };
        act.appendChild(b);
        const o = el("button", "small", t("open_human"));
        o.onclick = async () => {
          const res = await api("/api/web/open", { url: `https://${h.host}/` });
          if (res.ok) toast(t("opened"), "ok"); else toast(res.error || t("error"), "err");
        };
        act.appendChild(o);
      }
      tr.appendChild(act);
      tbl.appendChild(tr);
    }
    hostsBox.appendChild(tbl);
  }

  function kv(pairs) {
    const g = el("div", "wb-kv");
    for (const [k, v] of pairs) if (v !== undefined && v !== null && v !== "" && !(Array.isArray(v) && !v.length)) { g.appendChild(el("b", "", k)); g.appendChild(el("span", "", Array.isArray(v) ? v.join(", ") : String(v))); }
    return g;
  }

  function showFetch(r) {
    fetchOut.innerHTML = "";
    if (!r) return;
    const bits = [];
    if (r.status) bits.push(`HTTP ${r.status}`);
    if (r.tier) bits.push(r.tier);
    if (r.from_cache) bits.push(t("from_cache"));
    if (r.stale) bits.push(t("stale"));
    if (r.elapsed_ms !== undefined) bits.push(`${r.elapsed_ms} ${t("ms")}`);
    if (r.final_url && r.final_url !== r.url) bits.push("→ " + r.final_url);
    fetchOut.appendChild(el("div", "wb-meta", bits.join(" · ")));
    if (!r.ok) { fetchOut.appendChild(el("div", "wb-err", `${r.error || t("error")}${r.error_kind ? " (" + r.error_kind + ")" : ""}`)); return; }
    const ex = r.extract;
    if (ex && ex.error) fetchOut.appendChild(el("div", "wb-err", ex.error));
    else if (ex && ex.kind === "readable") fetchOut.appendChild(el("div", "wb-text", `${ex.title ? ex.title + "\n\n" : ""}${ex.text}`));
    else if (ex && ex.kind === "markdown") fetchOut.appendChild(el("div", "wb-text", ex.markdown));
    else if (ex && ex.kind === "meta") fetchOut.appendChild(kv([["title", ex.title], ["description", ex.description], ["image", ex.image], ["favicon", ex.favicon],
      ["canonical", ex.canonical], ["lang", ex.lang], ["site", ex.site_name], ["author", ex.author], ["published", ex.published],
      ["feeds", (ex.feeds || []).map((f) => f.url)], ["keywords", ex.keywords]]));
    else if (ex && ex.kind === "feed") fetchOut.appendChild(el("div", "wb-text", `${ex.title || ""} (${ex.format})\n\n` + (ex.items || []).map((i) => `• ${i.title}\n  ${i.link}`).join("\n")));
    else fetchOut.appendChild(el("div", "wb-text", r.text || ""));
  }

  async function doFetch() {
    const url = urlIn.value.trim();
    if (!url) return;
    fetchOut.innerHTML = ""; fetchOut.appendChild(el("div", "hint", t("reading")));
    const body = { url, extract: modeSel.value };
    if (localCb.checked) body.profile = "operator_local";
    const r = await api("/api/web/fetch", body);
    showFetch(r);
    load(true);
  }

  async function doSearch() {
    const query = queryIn.value.trim();
    if (!query) return;
    searchOut.innerHTML = ""; searchOut.appendChild(el("div", "hint", t("searching")));
    const r = await api("/api/web/search", { query, limit: 10, news: newsCb.checked });
    searchOut.innerHTML = "";
    if (!r.hits || !r.hits.length) searchOut.appendChild(el("div", "hint", t("no_hits")));
    for (const h of r.hits || []) {
      const row = el("div", "wb-hit");
      const a = el("a", "", h.title || h.url); a.href = h.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      row.appendChild(a);
      row.appendChild(el("div", "u", `${h.url} · ${h.engine}${h.published ? " · " + h.published : ""}`));
      if (h.snippet) row.appendChild(el("div", "s", h.snippet));
      searchOut.appendChild(row);
    }
    for (const [engine, why] of Object.entries(r.errors || {})) searchOut.appendChild(el("div", "wb-err", `${engine}: ${why}`));
    load(true);
  }

  async function load(quiet) {
    if (!box) return;
    const [s, h] = await Promise.all([api("/api/web/status"), api("/api/web/hosts")]);
    renderStatus(s);
    if (s && s.enabled === false) { hostsBox.innerHTML = ""; return; }
    renderHosts(h);
  }

  H.register("web", {
    placement: "tab",
    label: S.label,
    order: 80,
    mount(container) {
      injectCss();
      box = container;
      const tools = el("div", "tab-tools");
      const rb = el("button", "small", t("refresh")); rb.onclick = () => load();
      tools.appendChild(rb);
      box.appendChild(tools);
      statusBox = el("div"); box.appendChild(statusBox);

      box.appendChild(el("div", "wb-head", t("fetch_title")));
      const ft = el("div", "tab-tools");
      urlIn = el("input"); urlIn.type = "url"; urlIn.placeholder = t("url_ph"); urlIn.style.minWidth = "360px";
      urlIn.onkeydown = (e) => { if (e.key === "Enter") doFetch(); };
      modeSel = el("select");
      for (const m of ["readable", "markdown", "meta", "feed"]) { const o = el("option", "", t("mode_" + m)); o.value = m; modeSel.appendChild(o); }
      const lab = el("label", "hint"); lab.title = t("local_tip");
      localCb = el("input"); localCb.type = "checkbox"; lab.appendChild(localCb); lab.appendChild(document.createTextNode(" " + t("local")));
      const go = el("button", "primary small", t("read")); go.onclick = doFetch;
      for (const x of [urlIn, modeSel, lab, go]) ft.appendChild(x);
      box.appendChild(ft);
      fetchOut = el("div"); box.appendChild(fetchOut);

      box.appendChild(el("div", "wb-head", t("search_title")));
      const st = el("div", "tab-tools");
      queryIn = el("input"); queryIn.type = "search"; queryIn.placeholder = t("q_ph"); queryIn.style.minWidth = "320px";
      queryIn.onkeydown = (e) => { if (e.key === "Enter") doSearch(); };
      const nl = el("label", "hint"); newsCb = el("input"); newsCb.type = "checkbox"; nl.appendChild(newsCb); nl.appendChild(document.createTextNode(" " + t("news")));
      const sb = el("button", "primary small", t("search")); sb.onclick = doSearch;
      for (const x of [queryIn, nl, sb]) st.appendChild(x);
      box.appendChild(st);
      searchOut = el("div"); box.appendChild(searchOut);

      box.appendChild(el("div", "wb-head", t("hosts")));
      hostsBox = el("div"); box.appendChild(hostsBox);
      box.appendChild(el("div", "wb-head", t("recent")));
      recentBox = el("div", "audit"); box.appendChild(recentBox);
    },
    load,
    tick: () => load(true),
  });
})();

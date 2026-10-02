/* Hoard Hub UI — notifications tab ("Avisos" / "Notifications", Hermes).
   History with filters (app, sphere, state), unseen badge, channel settings with "Probar", and the per-sphere
   routing (read here, edited in the spheres dialog). Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const ctx = H.ctx;
  const { el, api, toast, L, fmtWhen } = ctx;

  const S = {
    label: { es: "Avisos", en: "Notifications" },
    all_apps: { es: "Todas las apps", en: "All apps" },
    all_spheres: { es: "Todas las esferas", en: "All spheres" },
    state_all: { es: "Todos", en: "All" },
    state_delivered: { es: "Enviados", en: "Delivered" },
    state_held: { es: "Retenidos", en: "Held" },
    state_unseen: { es: "Sin ver", en: "Unseen" },
    held_quiet: { es: "Silencio", en: "Quiet hours" },
    held_duplicate: { es: "Repetido", en: "Duplicate" },
    held_digest: { es: "Para el resumen", en: "For the digest" },
    held_rate: { es: "Demasiados", en: "Rate limit" },
    held_disabled: { es: "Desactivado", en: "Disabled" },
    search: { es: "Buscar…", en: "Search…" },
    mark_all: { es: "Marcar todo como visto", en: "Mark all as seen" },
    settings: { es: "Ajustes", en: "Settings" },
    empty: { es: "Aún no hay avisos. Las apps envían los suyos a /api/notify.", en: "No notifications yet. Apps send theirs to /api/notify." },
    held_why: { es: "Retenido", en: "Held" },
    skipped: { es: "no usado", en: "not used" },
    unsupported: { es: "no disponible aquí", en: "not available here" },
    // settings
    enabled: { es: "Avisos activados", en: "Notifications enabled" },
    dedupe: { es: "Ventana de repetidos (horas)", en: "Duplicate window (hours)" },
    rate: { es: "Máximo de avisos por app y hora", en: "Max pushes per app per hour" },
    ch_enabled: { es: "Activado", en: "Enabled" },
    windows: { es: "Windows (aviso de escritorio)", en: "Windows (desktop toast)" },
    ntfy: { es: "ntfy", en: "ntfy" }, telegram: { es: "Telegram", en: "Telegram" }, email: { es: "Correo", en: "Mail" },
    server: { es: "Servidor", en: "Server" }, topic: { es: "Tema", en: "Topic" }, token: { es: "Token (opcional)", en: "Token (optional)" },
    bot_token: { es: "Token del bot", en: "Bot token" }, chat_id: { es: "Chat id", en: "Chat id" },
    detect_chat: { es: "Detectar chat", en: "Detect chat" },
    detect_hint: { es: "Escribe antes algo al bot.", en: "Write something to the bot first." },
    via: { es: "Enviar con", en: "Send with" },
    via_faustus: { es: "La pasarela de correo del hub", en: "The hub's mail gateway" },
    via_smtp: { es: "SMTP directo", en: "Direct SMTP" },
    to: { es: "Destinatarios (separados por coma)", en: "Recipients (comma separated)" },
    host: { es: "Servidor SMTP", en: "SMTP host" }, port: { es: "Puerto", en: "Port" }, user: { es: "Usuario", en: "User" },
    password: { es: "Contraseña", en: "Password" }, tls: { es: "Cifrado TLS", en: "TLS" }, from: { es: "Remitente", en: "From" },
    test: { es: "Probar", en: "Test" },
    save: { es: "Guardar ajustes", en: "Save settings" },
    saved: { es: "Ajustes guardados", en: "Settings saved" },
    test_ok: { es: "Enviado por ", en: "Sent through " },
    test_fail: { es: "No se pudo enviar: ", en: "Could not send: " },
    routing: { es: "Enrutado por esfera", en: "Routing per sphere" },
    routing_edit: { es: "Editar en esferas…", en: "Edit in spheres…" },
    routing_hint: { es: "Qué canal usa cada prioridad. Se edita en el diálogo de esferas.", en: "Which channel each priority uses. Edited in the spheres dialog." },
    digest_ch: { es: "resumen", en: "digest" },
    urgent: { es: "Urgente", en: "Urgent" }, high: { es: "Alta", en: "High" }, normal: { es: "Normal", en: "Normal" }, low: { es: "Baja", en: "Low" },
    chat_found: { es: "Chat encontrado: ", en: "Chat found: " },
    status_ok: { es: "listo", en: "ready" }, status_no: { es: "sin configurar", en: "not configured" },
  };
  const t = (k) => L(S[k]);
  const CHANNELS = ["windows", "ntfy", "telegram", "email"];
  const PRIOS = ["urgent", "high", "normal", "low"];

  let box, list, settingsBox, badge;
  let rows = [];
  let filters = { app: "", sphere: "", state: "all", q: "" };
  let cfgView = null;
  let shownIds = new Set();      // rows already on screen during a previous tick: those get auto-marked
  let settingsOpen = false;

  function injectCss() {
    if (document.getElementById("css-notify")) return;
    const css = document.createElement("style");
    css.id = "css-notify";
    css.textContent = `
      .nt-row { display: grid; grid-template-columns: 8px 1fr auto; gap: 4px 12px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); align-items: start; }
      .nt-row.seen { opacity: .72; }
      .nt-row .nt-bar { align-self: stretch; border-radius: 4px; background: var(--hoard-border-hover); min-height: 100%; }
      .nt-row.p-urgent .nt-bar { background: var(--red); } .nt-row.p-high .nt-bar { background: var(--amber); } .nt-row.p-normal .nt-bar { background: var(--blue); }
      .nt-title { font-weight: 600; } .nt-title.unseen::before { content: "● "; color: var(--accent); }
      .nt-body { color: var(--muted); font-size: 12.5px; white-space: pre-wrap; word-break: break-word; }
      .nt-meta { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 3px; font-size: 11.5px; color: var(--muted); }
      .nt-when { color: var(--muted); font-size: 12px; white-space: nowrap; }
      .nt-list { display: grid; gap: 6px; }
      #tab-notify .tab-tools select { background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 5px 8px; font: inherit; font-size: 13px; max-width: 200px; }
      .nt-set { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin-bottom: 10px; display: grid; gap: 10px; background: var(--card-2); }
      .nt-set fieldset { border: 1px solid var(--line); border-radius: 9px; margin: 0; padding: 6px 10px 10px; min-width: 0; }
      .nt-set legend { font-size: 12px; color: var(--muted); padding: 0 6px; }
      .nt-set .nt-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px; align-items: end; }
      .nt-set label.nt-f { display: grid; gap: 3px; font-size: 12.5px; }
      .nt-set label.nt-f > span { color: var(--muted); font-size: 12px; }
      .nt-set input[type=text], .nt-set input[type=password], .nt-set input[type=number], .nt-set select { width: 100%; box-sizing: border-box; background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font: inherit; font-size: 13px; }
      .nt-set .nt-inline { display: inline-flex; gap: 6px; align-items: center; font-size: 12.5px; }
      .nt-set .nt-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .nt-set table { border-collapse: collapse; font-size: 12.5px; }
      .nt-set th, .nt-set td { padding: 3px 14px 3px 0; text-align: left; font-weight: 400; vertical-align: top; }
      .nt-set th { color: var(--muted); font-weight: 500; }
      .nt-set tr.hdr th { font-size: 11.5px; }
    `;
    document.head.appendChild(css);
  }

  function setBadge(n) {
    if (!badge) badge = document.getElementById("notify-badge");
    if (!badge) return;
    badge.textContent = n > 0 ? String(n) : "";
    badge.classList.toggle("warn", n > 0);
  }

  function query() {
    const p = new URLSearchParams({ limit: "80" });
    if (filters.app) p.set("app", filters.app);
    if (filters.sphere) p.set("sphere", filters.sphere);
    if (filters.q) p.set("q", filters.q);
    if (filters.state === "delivered") p.set("held", "none");
    else if (filters.state === "held") p.set("held", "any");
    else if (filters.state === "unseen") p.set("unseen", "1");
    else if (filters.state.startsWith("held_")) p.set("held", filters.state.slice(5));
    return "/api/notify?" + p.toString();
  }

  function chip(text, cls, title) { const c = el("span", "chip " + (cls || ""), text); if (title) c.title = title; return c; }

  function renderRow(n) {
    const row = el("div", `nt-row p-${n.priority}${n.seen ? " seen" : ""}`); row.dataset.id = String(n.id);
    row.appendChild(el("span", "nt-bar"));
    const main = el("div");
    main.appendChild(el("div", "nt-title" + (n.seen ? "" : " unseen"), n.title));
    if (n.body) main.appendChild(el("div", "nt-body", n.body));
    const meta = el("div", "nt-meta");
    const appChip = chip(n.app, "info"); meta.appendChild(appChip);
    meta.appendChild(chip(n.sphere));
    if (n.group) meta.appendChild(chip(n.group));
    if (n.held) meta.appendChild(chip(`${t("held_why")}: ${t("held_" + n.held) || n.held}`, "warn"));
    for (const c of n.channels || []) {
      let cls = c.ok ? "on" : (c.skipped ? "" : "bad");
      let label = `${c.ok ? "✓" : ((c.skipped || c.unsupported) ? "–" : "✕")} ${c.channel}`;
      let title = c.error || "";
      if (c.unsupported) { title = t("unsupported"); cls = ""; }
      else if (c.skipped) title = t("skipped") + (c.error ? ` (${c.error})` : "");
      meta.appendChild(chip(label, cls, title));
    }
    if (n.url) { const a = el("a", "chip info", "↗"); a.href = n.url; a.target = "_blank"; a.rel = "noopener"; meta.appendChild(a); }
    main.appendChild(meta);
    row.appendChild(main);
    row.appendChild(el("span", "nt-when", fmtWhen(n.ts)));
    return row;
  }

  function renderList() {
    list.innerHTML = "";
    if (!rows.length) { list.appendChild(el("div", "hint", t("empty"))); return; }
    for (const n of rows) list.appendChild(renderRow(n));
  }

  async function loadRows() {
    const r = await api(query());
    if (!r || !r.ok) return;
    rows = r.notifications || [];
    setBadge(r.unseen || 0);
    fillAppFilter();
    renderList();
  }

  function fillAppFilter() {
    const sel = box.querySelector(".nt-app");
    const apps = new Set(rows.map((n) => n.app)); if (filters.app) apps.add(filters.app);
    const have = new Set([...sel.options].map((o) => o.value));
    for (const a of [...apps].sort()) if (!have.has(a)) { const o = el("option", "", a); o.value = a; sel.appendChild(o); }
  }

  async function markAll() {
    const r = await api("/api/notify/read", { all: true });
    if (r.ok) { setBadge(r.unseen || 0); await loadRows(); }
  }

  // ---- settings ---------------------------------------------------------------------------------------------
  function fieldText(labelKey, value, opts = {}) {
    const lab = el("label", "nt-f"); lab.appendChild(el("span", "", t(labelKey)));
    const inp = el("input"); inp.type = opts.password ? "password" : (opts.number ? "number" : "text"); inp.value = value == null ? "" : String(value);
    if (opts.password) inp.autocomplete = "new-password";
    if (opts.placeholder) inp.placeholder = opts.placeholder;
    lab.appendChild(inp);
    return { node: lab, input: inp };
  }
  function fieldCheck(labelKey, value) {
    const lab = el("label", "nt-inline"); const cb = el("input"); cb.type = "checkbox"; cb.checked = !!value;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(t(labelKey)));
    return { node: lab, input: cb };
  }

  async function loadSettings() {
    const r = await api("/api/notify/config");
    if (!r || !r.ok) return;
    cfgView = r;
    renderSettings();
  }

  function statusChip(name) {
    const st = (cfgView.status || {})[name] || {};
    if (name === "windows" && st.supported === false) return chip(t("unsupported"), "warn");
    return chip(st.configured ? t("status_ok") : t("status_no"), st.configured ? "on" : "warn", st.detail || "");
  }

  function renderSettings() {
    settingsBox.innerHTML = "";
    if (!cfgView) return;
    const ch = cfgView.channels || {};
    const f = {};
    const top = el("div", "nt-grid");
    f.enabled = fieldCheck("enabled", cfgView.enabled); top.appendChild(f.enabled.node);
    f.dedupe = fieldText("dedupe", (cfgView.dedupe_window_s || 0) / 3600, { number: true }); top.appendChild(f.dedupe.node);
    f.rate = fieldText("rate", cfgView.rate_per_app, { number: true }); top.appendChild(f.rate.node);
    settingsBox.appendChild(top);

    const fieldset = (name) => {
      const fs = el("fieldset"); const lg = el("legend"); lg.appendChild(document.createTextNode(t(name) + " "));
      lg.appendChild(statusChip(name)); fs.appendChild(lg);
      return fs;
    };
    const testBtn = (name) => { const b = el("button", "ghost small", t("test")); b.dataset.test = name; b.onclick = () => testChannel(name, collectAll); return b; };

    // windows
    const w = fieldset("windows"); f.w_en = fieldCheck("ch_enabled", (ch.windows || {}).enabled);
    const wr = el("div", "nt-actions"); wr.appendChild(f.w_en.node); wr.appendChild(testBtn("windows")); w.appendChild(wr); settingsBox.appendChild(w);
    // ntfy
    const n = fieldset("ntfy"); const ng = el("div", "nt-grid"); const nc = ch.ntfy || {};
    f.n_en = fieldCheck("ch_enabled", nc.enabled); ng.appendChild(f.n_en.node);
    f.n_server = fieldText("server", nc.server); ng.appendChild(f.n_server.node);
    f.n_topic = fieldText("topic", nc.topic); ng.appendChild(f.n_topic.node);
    f.n_token = fieldText("token", nc.token, { password: true }); ng.appendChild(f.n_token.node);
    n.appendChild(ng); const nr = el("div", "nt-actions"); nr.appendChild(testBtn("ntfy")); n.appendChild(nr); settingsBox.appendChild(n);
    // telegram
    const tg = fieldset("telegram"); const tgg = el("div", "nt-grid"); const tc = ch.telegram || {};
    f.t_en = fieldCheck("ch_enabled", tc.enabled); tgg.appendChild(f.t_en.node);
    f.t_token = fieldText("bot_token", tc.bot_token, { password: true }); tgg.appendChild(f.t_token.node);
    f.t_chat = fieldText("chat_id", tc.chat_id); tgg.appendChild(f.t_chat.node);
    tg.appendChild(tgg); const tr = el("div", "nt-actions");
    const det = el("button", "ghost small", t("detect_chat")); det.dataset.action = "detect"; det.title = t("detect_hint");
    det.onclick = async () => {
      const s = await saveAll(collectAll(), true); if (!s.ok) return;
      const r = await api("/api/notify/telegram/discover", {});
      if (r.ok) { f.t_chat.input.value = r.chat_id; toast(t("chat_found") + (r.name || r.chat_id), "ok"); } else toast(r.error || "error", "err");
    };
    tr.appendChild(det); tr.appendChild(testBtn("telegram")); tg.appendChild(tr); settingsBox.appendChild(tg);
    // email
    const em = fieldset("email"); const eg = el("div", "nt-grid"); const ec = ch.email || {};
    f.e_en = fieldCheck("ch_enabled", ec.enabled); eg.appendChild(f.e_en.node);
    const viaLab = el("label", "nt-f"); viaLab.appendChild(el("span", "", t("via")));
    f.e_via = el("select"); for (const v of ["faustus", "smtp"]) { const o = el("option", "", t("via_" + v)); o.value = v; f.e_via.appendChild(o); }
    f.e_via.value = ec.via || "faustus"; viaLab.appendChild(f.e_via); eg.appendChild(viaLab);
    f.e_to = fieldText("to", (ec.to || []).join(", ")); eg.appendChild(f.e_to.node);
    em.appendChild(eg);
    const sg = el("div", "nt-grid");
    f.e_host = fieldText("host", ec.host); sg.appendChild(f.e_host.node);
    f.e_port = fieldText("port", ec.port, { number: true }); sg.appendChild(f.e_port.node);
    f.e_user = fieldText("user", ec.user); sg.appendChild(f.e_user.node);
    f.e_pass = fieldText("password", ec.password, { password: true }); sg.appendChild(f.e_pass.node);
    f.e_from = fieldText("from", ec.from); sg.appendChild(f.e_from.node);
    f.e_tls = fieldCheck("tls", ec.tls !== false); sg.appendChild(f.e_tls.node);
    em.appendChild(sg);
    const syncVia = () => { sg.hidden = f.e_via.value !== "smtp"; };
    f.e_via.onchange = syncVia; syncVia();
    const er = el("div", "nt-actions"); er.appendChild(testBtn("email")); em.appendChild(er); settingsBox.appendChild(em);

    const collectAll = () => ({
      enabled: f.enabled.input.checked,
      dedupe_window_s: Math.round(Number(f.dedupe.input.value || 0) * 3600),
      rate_per_app: Number(f.rate.input.value || 20),
      channels: {
        windows: { enabled: f.w_en.input.checked },
        ntfy: { enabled: f.n_en.input.checked, server: f.n_server.input.value.trim(), topic: f.n_topic.input.value.trim(), token: f.n_token.input.value },
        telegram: { enabled: f.t_en.input.checked, bot_token: f.t_token.input.value, chat_id: f.t_chat.input.value.trim() },
        email: { enabled: f.e_en.input.checked, via: f.e_via.value, to: f.e_to.input.value, host: f.e_host.input.value.trim(),
          port: Number(f.e_port.input.value || 465), user: f.e_user.input.value.trim(), password: f.e_pass.input.value,
          from: f.e_from.input.value.trim(), tls: f.e_tls.input.checked },
      },
    });

    const act = el("div", "nt-actions");
    const save = el("button", "primary small", t("save")); save.dataset.action = "save";
    save.onclick = () => saveAll(collectAll(), false);
    act.appendChild(save); settingsBox.appendChild(act);

    // routing (read-only here)
    const rt = el("fieldset"); rt.appendChild(el("legend", "", t("routing")));
    const tbl = el("table"); const hd = el("tr", "hdr"); hd.appendChild(el("th"));
    for (const p of PRIOS) hd.appendChild(el("th", "", t(p)));
    tbl.appendChild(hd);
    for (const [id, s] of Object.entries(cfgView.routing || {})) {
      const row = el("tr"); const th = el("th", "", L(s.name) || id); row.appendChild(th);
      for (const p of PRIOS) row.appendChild(el("td", "", ((s.notify || {})[p] || []).map((c) => c === "digest" ? t("digest_ch") : c).join(", ") || "–"));
      tbl.appendChild(row);
    }
    rt.appendChild(tbl); rt.appendChild(el("div", "hint", t("routing_hint")));
    const edit = el("button", "ghost small", t("routing_edit")); edit.dataset.action = "routing";
    edit.onclick = () => { if (window.HubSpheres) window.HubSpheres.openDialog(); };
    rt.appendChild(edit); settingsBox.appendChild(rt);
  }

  async function saveAll(patch, quiet) {
    const r = await api("/api/notify/config", patch);
    if (!r.ok) { toast(r.error || "error", "err"); return r; }
    cfgView = r;
    if (!quiet) { toast(t("saved"), "ok"); renderSettings(); }
    return r;
  }

  async function testChannel(name, collect) {
    const s = await saveAll(collect(), true); if (!s.ok) return;
    const r = await api("/api/notify/test", { channel: name });
    if (!r.ok) { toast(r.error || "error", "err"); return; }
    const res = r.result || {};
    if (res.ok) toast(t("test_ok") + name, "ok");
    else toast(t("test_fail") + (res.unsupported ? t("unsupported") : (res.error || "?")), "err");
    loadRows();
  }

  function toggleSettings() {
    settingsOpen = !settingsOpen;
    settingsBox.hidden = !settingsOpen;
    if (settingsOpen) loadSettings();
  }

  // ---- facet ----------------------------------------------------------------------------------------------------
  async function pollBadge() {
    try { const r = await api("/api/notify?limit=1"); if (r && r.ok) setBadge(r.unseen || 0); } catch (e) { /* ignore */ }
  }

  H.register("notify", {
    placement: "tab",
    label: S.label,
    order: 30,
    mount(container) {
      injectCss();
      box = container;
      const tools = el("div", "tab-tools");
      const mkSel = (cls, opts, onchange) => { const s = el("select", cls); for (const [v, label] of opts) { const o = el("option", "", label); o.value = v; s.appendChild(o); } s.onchange = onchange; return s; };
      const appSel = mkSel("nt-app", [["", t("all_apps")]], () => { filters.app = appSel.value; loadRows(); });
      const sphereSel = mkSel("nt-sphere", [["", t("all_spheres")]], () => { filters.sphere = sphereSel.value; loadRows(); });
      const stateSel = mkSel("nt-state", [["all", t("state_all")], ["delivered", t("state_delivered")], ["held", t("state_held")], ["unseen", t("state_unseen")],
        ["held_quiet", t("held_quiet")], ["held_duplicate", t("held_duplicate")], ["held_digest", t("held_digest")], ["held_rate", t("held_rate")]],
        () => { filters.state = stateSel.value; loadRows(); });
      const q = el("input", "narrow nt-q"); q.type = "search"; q.placeholder = t("search");
      let timer = null; q.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { filters.q = q.value.trim(); loadRows(); }, 250); };
      const mark = el("button", "ghost small nt-mark", t("mark_all")); mark.onclick = markAll;
      const set = el("button", "ghost small nt-settings", t("settings")); set.onclick = toggleSettings;
      for (const x of [appSel, sphereSel, stateSel, q, mark, set]) tools.appendChild(x);
      box.appendChild(tools);
      settingsBox = el("div", "nt-set"); settingsBox.hidden = true; box.appendChild(settingsBox);
      list = el("div", "nt-list"); box.appendChild(list);
      pollBadge(); setInterval(pollBadge, 15000);
      // the sphere filter lists the hub's spheres
      api("/api/spheres").then((r) => { if (r && r.ok) for (const s of r.spheres) { const o = el("option", "", L(s.name) || s.id); o.value = s.id; sphereSel.appendChild(o); } }).catch(() => {});
      // relabel on language change
      ctx.bus.addEventListener("lang", () => {
        appSel.options[0].textContent = t("all_apps"); sphereSel.options[0].textContent = t("all_spheres");
        [...stateSel.options].forEach((o, i) => { o.textContent = t(["state_all", "state_delivered", "state_held", "state_unseen", "held_quiet", "held_duplicate", "held_digest", "held_rate"][i]); });
        q.placeholder = t("search"); mark.textContent = t("mark_all"); set.textContent = t("settings");
        if (settingsOpen && cfgView) renderSettings();
      });
    },
    async load() { await loadRows(); },
    async tick() {
      await loadRows();
      // rows that were already on screen at the previous tick have been seen: clear them
      const stay = rows.filter((n) => !n.seen && shownIds.has(n.id)).map((n) => n.id);
      shownIds = new Set(rows.filter((n) => !n.seen).map((n) => n.id));
      if (stay.length) { const r = await api("/api/notify/read", { ids: stay }); if (r.ok) setBadge(r.unseen || 0); }
    },
  });
})();

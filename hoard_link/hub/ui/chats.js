// Hoard Hub — Chats tab (facet "chats"): Slack, Microsoft Teams / Outlook (Graph) and JSON-lines sources, and what they brought.
// Sources list with state and last fetch, an add/edit form per kind (Microsoft 365 with the device-code login), and the recent
// messages grouped by channel with priority chips.
(() => {
  "use strict";
  if (!window.HubFacets) return;
  const { $, el, api, toast, L, fmtWhen, bus } = window.HubFacets.ctx;
  const MASK = "••••";

  const T = {
    title: { es: "Chats", en: "Chats" },
    sources: { es: "Fuentes", en: "Sources" }, add: { es: "Añadir fuente", en: "Add source" },
    none: { es: "Sin fuentes. Añade Slack, Microsoft 365 o un fichero JSON-lines.", en: "No sources. Add Slack, Microsoft 365 or a JSON-lines file." },
    fetch_now: { es: "Leer ahora", en: "Fetch now" }, test: { es: "Probar", en: "Test" }, edit: { es: "Editar", en: "Edit" },
    remove: { es: "Quitar", en: "Remove" }, enable: { es: "Activar", en: "Enable" }, disable: { es: "Desactivar", en: "Disable" },
    login: { es: "Iniciar sesión", en: "Sign in" }, confirm_remove: { es: "¿Quitar esta fuente? Los mensajes ya leídos se conservan.", en: "Remove this source? Messages already read are kept." },
    last: { es: "última lectura", en: "last fetch" }, never: { es: "nunca", en: "never" }, every: { es: "cada", en: "every" }, min: { es: "min", en: "min" },
    ok_state: { es: "bien", en: "ok" }, off_state: { es: "desactivada", en: "disabled" }, wait: { es: "espera (límite de la API)", en: "waiting (API rate limit)" },
    new_n: (n) => ({ es: `${n} nuevos`, en: `${n} new` }),
    kind: { es: "Tipo", en: "Kind" }, name: { es: "Nombre", en: "Name" }, sphere: { es: "Esfera", en: "Sphere" },
    by_spheres: { es: "(según las esferas)", en: "(as the spheres say)" }, interval: { es: "Cada (min)", en: "Every (min)" },
    token: { es: "Token (xoxp-/xoxb-)", en: "Token (xoxp-/xoxb-)" }, channels: { es: "Canales (vacío = todos los que sigues)", en: "Channels (empty = all you are in)" },
    dms: { es: "Incluir mensajes directos", en: "Include direct messages" }, backfill: { es: "Días atrás la 1.ª vez", en: "Days back the 1st time" },
    access_token: { es: "Access token (opcional si inicias sesión)", en: "Access token (optional if you sign in)" },
    client_id: { es: "Client ID (app de Entra ID)", en: "Client ID (Entra ID app)" }, tenant: { es: "Tenant (vacío = common)", en: "Tenant (empty = common)" },
    teams: { es: "Chats de Teams", en: "Teams chats" }, outlook: { es: "Bandeja de Outlook", en: "Outlook inbox" },
    path: { es: "Ruta del fichero .jsonl", en: "Path to the .jsonl file" }, save: { es: "Guardar", en: "Save" }, cancel: { es: "Cancelar", en: "Cancel" },
    saved: { es: "guardada", en: "saved" }, fetched: (n) => ({ es: `leídos ${n} nuevos`, en: `${n} new read` }),
    login_title: { es: "Inicio de sesión de Microsoft", en: "Microsoft sign-in" },
    login_steps: { es: "Abre esta dirección e introduce el código:", en: "Open this address and enter the code:" },
    login_ok: { es: "Sesión iniciada.", en: "Signed in." }, login_pending: { es: "esperando a que lo confirmes…", en: "waiting for you to confirm…" },
    login_bad: { es: "No se pudo iniciar sesión", en: "Sign-in failed" },
    recent: { es: "Mensajes recientes", en: "Recent messages" }, all_sources: { es: "Todas las fuentes", en: "All sources" },
    all_spheres: { es: "Todas las esferas", en: "All spheres" }, search: { es: "Buscar…", en: "Search…" }, nothing: { es: "Sin mensajes todavía.", en: "No messages yet." },
    attention: { es: "atención", en: "attention" }, low: { es: "baja", en: "low" },
    k_slack: { es: "Slack", en: "Slack" }, k_graph: { es: "Microsoft 365 (Teams / Outlook)", en: "Microsoft 365 (Teams / Outlook)" }, k_jsonl: { es: "Fichero JSON-lines", en: "JSON-lines file" },
    hint_slack: { es: "Un token de usuario o de bot con channels:history, channels:read, groups:read, im:read, mpim:read, users:read.", en: "A user or bot token with channels:history, channels:read, groups:read, im:read, mpim:read, users:read." },
    hint_graph: { es: "Pega un access token, o rellena Client ID (app pública con flujo de código de dispositivo) y pulsa «Iniciar sesión». Permisos: Chat.Read, Mail.Read, User.Read.", en: "Paste an access token, or fill in Client ID (a public app with the device-code flow) and press “Sign in”. Scopes: Chat.Read, Mail.Read, User.Read." },
    hint_jsonl: { es: "Una línea JSON por mensaje: {ts, channel, from, text, direct, mentions_me}.", en: "One JSON line per message: {ts, channel, from, text, direct, mentions_me}." },
  };
  const t = (k, ...a) => { const v = T[k]; return L(typeof v === "function" ? v(...a) : v || { es: k, en: k }); };

  if (!document.getElementById("css-chats")) {
    const st = document.createElement("style"); st.id = "css-chats";
    st.textContent = `
      .ch-sources { display: grid; gap: 6px; margin-bottom: 8px; }
      .ch-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 8px 12px; padding: 10px; margin-bottom: 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); }
      .ch-form label { display: grid; gap: 2px; font-size: 12px; color: var(--muted); }
      .ch-form label.check { display: flex; gap: 6px; align-items: center; }
      .ch-form .wide { grid-column: 1 / -1; }
      .ch-form .act { display: flex; gap: 8px; }
      .ch-login { padding: 10px 12px; margin-bottom: 10px; border: 1px solid var(--accent); border-radius: 10px; background: var(--card-2); }
      .ch-code { font-family: var(--mono); font-size: 22px; letter-spacing: .12em; margin: 6px 0; }
      .ch-group { margin-bottom: 10px; }
      .ch-group h4 { margin: 0 0 4px; font-size: 13px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .ch-msg { display: grid; grid-template-columns: 110px 1fr auto; gap: 8px; padding: 3px 8px; border-radius: 6px; font-size: 13px; }
      .ch-msg:nth-child(odd) { background: var(--card-2); }
      .ch-msg.attention { border-left: 3px solid var(--amber); }
      .ch-msg .who { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .ch-msg .txt { white-space: pre-wrap; word-break: break-word; }
      .ch-msg .when { color: var(--muted); font-size: 11.5px; white-space: nowrap; }
    `;
    document.head.appendChild(st);
  }

  let sourcesEl, formEl, loginEl, groupsEl, srcSel, sphSel, qInp, lastSig = "", editing = null, loginPoll = null, spheres = [];
  const chip = (text, cls = "") => el("span", `chip ${cls}`.trim(), text);
  const ago = (ts) => {
    if (!ts) return t("never");
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return L({ es: "hace un momento", en: "just now" });
    if (s < 5400) return L({ es: `hace ${Math.round(s / 60)} min`, en: `${Math.round(s / 60)} min ago` });
    if (s < 129600) return L({ es: `hace ${Math.round(s / 3600)} h`, en: `${Math.round(s / 3600)} h ago` });
    return fmtWhen(ts);
  };
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v !== undefined && v !== null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

  function mount(container) {
    const tools = el("div", "tab-tools");
    const addBtn = el("button", "primary small", t("add"));
    addBtn.onclick = () => openForm(null);
    tools.appendChild(addBtn);
    tools.appendChild(el("span", "hint", ""));
    container.appendChild(tools);
    loginEl = el("div"); loginEl.hidden = true; container.appendChild(loginEl);
    formEl = el("div"); formEl.hidden = true; container.appendChild(formEl);
    sourcesEl = el("div", "ch-sources"); container.appendChild(sourcesEl);
    const head = el("div", "tab-tools");
    head.appendChild(el("b", "", t("recent")));
    srcSel = el("select", "small"); srcSel.onchange = () => { lastSig = ""; loadMessages(); };
    sphSel = el("select", "small"); sphSel.onchange = () => { lastSig = ""; loadMessages(); };
    qInp = el("input", "narrow"); qInp.type = "search"; qInp.placeholder = t("search");
    qInp.oninput = () => { clearTimeout(qInp._t); qInp._t = setTimeout(() => { lastSig = ""; loadMessages(); }, 300); };
    for (const x of [srcSel, sphSel, qInp]) head.appendChild(x);
    container.appendChild(head);
    groupsEl = el("div"); container.appendChild(groupsEl);
    bus.addEventListener("lang", () => { addBtn.textContent = t("add"); qInp.placeholder = t("search"); lastSig = ""; load(); });
    bus.addEventListener("sphere", () => { /* the sphere filter stays what the person chose here */ });
  }

  // ---- sources ----------------------------------------------------------------------------------
  function stateLine(s) {
    const st = s.state || {};
    const parts = [`${t("every")} ${s.interval_min} ${t("min")}`, `${t("last")}: ${ago(st.last_fetch_ts)}`];
    if (st.last_fetch_ts && st.last_ok) parts.push(t("new_n", st.last_new || 0));
    return parts.join(" · ");
  }
  function sourceCard(s) {
    const st = s.state || {};
    const card = el("div", "rule" + (s.enabled ? "" : " off"));
    const left = el("div");
    const name = el("div", "r-name", `${s.name}  `); name.appendChild(el("span", "hint", s.id)); left.appendChild(name);
    const chips = el("div", "mg-chips"); chips.style.display = "flex"; chips.style.gap = "5px"; chips.style.flexWrap = "wrap";
    chips.appendChild(chip(t("k_" + s.kind), "info"));
    chips.appendChild(chip(s.sphere || t("by_spheres")));
    if (!s.enabled) chips.appendChild(chip(t("off_state"), "warn"));
    else if (st.last_fetch_ts) chips.appendChild(chip(st.last_ok ? t("ok_state") : "✗", st.last_ok ? "on" : "bad"));
    if (st.retry_after_ts && st.retry_after_ts > Date.now() / 1000) chips.appendChild(chip(t("wait"), "warn"));
    if (st.me) chips.appendChild(chip(st.me));
    left.appendChild(chips);
    left.appendChild(el("div", "r-then", stateLine(s)));
    if (st.last_error) left.appendChild(el("div", "r-last bad", st.last_error));
    if (st.login && st.login.state && st.login.state !== "ok") left.appendChild(el("div", "r-last", `${t("login_title")}: ${st.login.state}`));
    card.appendChild(left);
    const acts = el("div", "r-actions");
    const b = (label, cls, fn) => { const x = el("button", cls, label); x.onclick = fn; acts.appendChild(x); return x; };
    b(t("fetch_now"), "small", async (ev) => {
      ev.target.disabled = true;
      const r = await api("/api/chats/fetch", { id: s.id, force: true });
      toast(r.ok ? (r.skipped ? r.skipped : L(T.fetched(r.new || 0))) : `${s.name}: ${r.error}`, r.ok ? "ok" : "err");
      lastSig = ""; load();
    });
    b(t("test"), "small ghost", async () => { const r = await api(`/api/chats/sources/${encodeURIComponent(s.id)}/test`, {}); toast(r.ok ? `${s.name}: ${r.detail || "ok"}` : `${s.name}: ${r.error}`, r.ok ? "ok" : "err"); load(); });
    if (s.kind === "graph") b(t("login"), "small ghost", () => startLogin(s));
    b(t("edit"), "small ghost", () => openForm(s));
    b(s.enabled ? t("disable") : t("enable"), "small ghost", async () => { await api("/api/chats/sources", { id: s.id, enabled: !s.enabled }); lastSig = ""; load(); });
    b(t("remove"), "small ghost danger", async () => { if (!confirm(t("confirm_remove"))) return; await api("/api/chats/sources/remove", { id: s.id }); lastSig = ""; load(); });
    card.appendChild(acts);
    return card;
  }

  // ---- form ------------------------------------------------------------------------------------------
  function openForm(src) {
    editing = src; formEl.hidden = false; formEl.innerHTML = "";
    const f = el("div", "ch-form"); formEl.appendChild(f);
    const inputs = {};
    let into = f;   // where field() / check() put their label: the form, or the per-kind body
    const field = (key, label, type = "text", value = "", cls = "") => {
      const lab = el("label", cls, label); const inp = el("input"); inp.type = type; inp.value = value ?? ""; if (type === "password") inp.autocomplete = "off";
      lab.appendChild(inp); into.appendChild(lab); inputs[key] = inp; return inp;
    };
    const check = (key, label, value) => {
      const lab = el("label", "check"); const inp = el("input"); inp.type = "checkbox"; inp.checked = !!value;
      lab.appendChild(inp); lab.appendChild(document.createTextNode(label)); into.appendChild(lab); inputs[key] = inp; return inp;
    };
    const kindSel = el("select");
    for (const k of ["slack", "graph", "jsonl"]) { const o = el("option", "", t("k_" + k)); o.value = k; kindSel.appendChild(o); }
    kindSel.value = src ? src.kind : "slack"; kindSel.disabled = !!src;
    const kl = el("label", "", t("kind")); kl.appendChild(kindSel); f.appendChild(kl);
    const body = el("div"); body.style.display = "contents"; f.appendChild(body);

    function kindFields() {
      body.innerHTML = ""; for (const k of Object.keys(inputs)) delete inputs[k];
      into = body;
      const v = (k, d = "") => (src && src[k] !== undefined ? src[k] : d);
      field("name", t("name"), "text", v("name"));
      const sl = el("label", "", t("sphere")); const ss = el("select");
      const o0 = el("option", "", t("by_spheres")); o0.value = ""; ss.appendChild(o0);
      for (const sp of spheres) { const o = el("option", "", L(sp.name) || sp.id); o.value = sp.id; ss.appendChild(o); }
      ss.value = v("sphere"); sl.appendChild(ss); body.appendChild(sl); inputs.sphere = ss;
      field("interval_min", t("interval"), "number", v("interval_min", 5));
      const kind = kindSel.value;
      if (kind === "slack") {
        field("token", t("token"), "password", v("token"), "wide");
        field("channels", t("channels"), "text", (v("channels", []) || []).join(", "), "wide");
        check("include_dms", t("dms"), v("include_dms", false));
        field("backfill_days", t("backfill"), "number", v("backfill_days", 2));
        body.appendChild(el("div", "hint wide", t("hint_slack")));
      } else if (kind === "graph") {
        field("access_token", t("access_token"), "password", v("access_token"), "wide");
        field("client_id", t("client_id"), "text", v("client_id"));
        field("tenant", t("tenant"), "text", v("tenant"));
        check("teams_chats", t("teams"), v("teams_chats", true));
        check("outlook", t("outlook"), v("outlook", false));
        body.appendChild(el("div", "hint wide", t("hint_graph")));
      } else {
        field("path", t("path"), "text", v("path"), "wide");
        body.appendChild(el("div", "hint wide", t("hint_jsonl")));
      }
    }
    kindSel.onchange = kindFields; kindFields();
    const act = el("div", "act wide");
    const save = el("button", "primary small", t("save"));
    save.onclick = async () => {
      const kind = kindSel.value;
      const payload = { kind, name: inputs.name.value.trim(), sphere: inputs.sphere.value, interval_min: Number(inputs.interval_min.value) || 5 };
      if (src) payload.id = src.id;
      if (kind === "slack") Object.assign(payload, { token: inputs.token.value, channels: inputs.channels.value, include_dms: inputs.include_dms.checked, backfill_days: Number(inputs.backfill_days.value) || 0 });
      else if (kind === "graph") Object.assign(payload, { access_token: inputs.access_token.value, client_id: inputs.client_id.value.trim(), tenant: inputs.tenant.value.trim(), teams_chats: inputs.teams_chats.checked, outlook: inputs.outlook.checked });
      else payload.path = inputs.path.value.trim();
      const r = await api("/api/chats/sources", payload);
      if (!r.ok) { toast(r.error, "err"); return; }
      toast(`${r.source.name}: ${t("saved")}`, "ok"); formEl.hidden = true; editing = null; lastSig = ""; load();
    };
    const cancel = el("button", "ghost small", t("cancel")); cancel.onclick = () => { formEl.hidden = true; editing = null; };
    act.appendChild(save); act.appendChild(cancel); f.appendChild(act);
  }

  // ---- device-code login ------------------------------------------------------------------------------
  async function startLogin(s) {
    const r = await api(`/api/chats/sources/${encodeURIComponent(s.id)}/login`, {});
    if (!r.ok) { toast(`${t("login_bad")}: ${r.error}`, "err"); return; }
    loginEl.hidden = false; loginEl.innerHTML = "";
    const box = el("div", "ch-login"); loginEl.appendChild(box);
    box.appendChild(el("b", "", `${t("login_title")} — ${s.name}`));
    box.appendChild(el("div", "hint", t("login_steps")));
    const a = el("a", "", r.verification_uri); a.href = r.verification_uri; a.target = "_blank"; a.rel = "noopener noreferrer"; box.appendChild(a);
    box.appendChild(el("div", "ch-code", r.user_code));
    const status = el("div", "hint", t("login_pending")); box.appendChild(status);
    const close = el("button", "ghost small", t("cancel")); close.onclick = () => { clearInterval(loginPoll); loginEl.hidden = true; }; box.appendChild(close);
    clearInterval(loginPoll);
    loginPoll = setInterval(async () => {
      const list = await api("/api/chats/sources");
      const cur = ((list.sources || []).find((x) => x.id === s.id) || {}).state;
      const lg = cur && cur.login;
      if (!lg || lg.state === "pending") return;
      clearInterval(loginPoll);
      status.textContent = lg.state === "ok" ? t("login_ok") : `${t("login_bad")}: ${lg.state}${lg.message ? " (" + lg.message + ")" : ""}`;
      status.className = lg.state === "ok" ? "hint" : "r-last bad";
      lastSig = ""; load();
    }, 2000);
  }

  // ---- messages -----------------------------------------------------------------------------------------
  function renderMessages(msgs, sources) {
    groupsEl.innerHTML = "";
    if (!msgs.length) { groupsEl.appendChild(el("div", "hint", t("nothing"))); return; }
    const names = Object.fromEntries(sources.map((s) => [s.id, s.name]));
    const groups = new Map();
    for (const m of msgs) {
      const key = `${m.source}\u0001${m.channel}`;
      if (!groups.has(key)) groups.set(key, { label: m.channel_label || m.channel, source: m.source, list: [] });
      groups.get(key).list.push(m);
    }
    for (const g of groups.values()) {
      const box = el("div", "ch-group");
      const h = el("h4", "", g.label);
      h.appendChild(chip(names[g.source] || g.source, "info"));
      h.appendChild(el("span", "badge", String(g.list.length)));
      const att = g.list.filter((m) => m.priority === "attention").length;
      if (att) h.appendChild(chip(`${t("attention")} ${att}`, "warn"));
      box.appendChild(h);
      for (const m of g.list.slice().reverse()) {
        const row = el("div", "ch-msg" + (m.priority === "attention" ? " attention" : "") + (m.priority === "low" ? " off" : ""));
        row.appendChild(el("span", "who", m.from_name || m.from_id));
        const txt = el("span", "txt", m.text); if ((m.reasons || []).length) txt.title = m.reasons.join(", ");
        row.appendChild(txt);
        row.appendChild(el("span", "when", ago(m.date_ts)));
        box.appendChild(row);
      }
      groupsEl.appendChild(box);
    }
  }

  async function loadMessages(sources) {
    sources = sources || ((await api("/api/chats/sources")).sources || []);
    const r = await api("/api/chats/messages?" + qs({ source: srcSel.value, sphere: sphSel.value, q: qInp.value.trim(), limit: 150 }));
    const sig = JSON.stringify([r.messages, sources.map((s) => s.id)]);
    if (sig === lastSig) return;
    lastSig = sig;
    renderMessages(r.messages || [], sources);
  }

  async function load() {
    try { const r = await api("/api/spheres"); spheres = r && r.ok ? (r.spheres || []) : []; } catch (e) { spheres = []; }
    const list = await api("/api/chats/sources");
    if (!list.ok) { sourcesEl.textContent = list.error || "error"; return; }
    const sources = list.sources || [];
    sourcesEl.innerHTML = "";
    if (!sources.length) sourcesEl.appendChild(el("div", "hint", t("none")));
    for (const s of sources) sourcesEl.appendChild(sourceCard(s));
    const keepSrc = srcSel.value, keepSph = sphSel.value;
    srcSel.innerHTML = ""; const a0 = el("option", "", t("all_sources")); a0.value = ""; srcSel.appendChild(a0);
    for (const s of sources) { const o = el("option", "", s.name); o.value = s.id; srcSel.appendChild(o); }
    srcSel.value = keepSrc;
    sphSel.innerHTML = ""; const p0 = el("option", "", t("all_spheres")); p0.value = ""; sphSel.appendChild(p0);
    for (const sp of spheres) { const o = el("option", "", L(sp.name) || sp.id); o.value = sp.id; sphSel.appendChild(o); }
    sphSel.value = keepSph;
    const badge = document.getElementById("chats-badge");
    if (badge) { const bad = sources.filter((s) => s.enabled && s.state && s.state.last_ok === false).length; badge.textContent = sources.length ? String(sources.length) : ""; badge.className = "badge" + (bad ? " bad" : ""); }
    await loadMessages(sources);
  }

  window.HubFacets.register("chats", {
    placement: "tab", label: T.title, order: 45, mount,
    load: () => load().catch((e) => console.error("chats tab", e)),
    tick: () => { if (!editing) load().catch(() => {}); },
  });
})();

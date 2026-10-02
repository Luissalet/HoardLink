// Hoard Hub — "Hoy" / "Today" (facet today, placement "top").
// Four columns: Agenda (overdue in red, today, tomorrow, next 7 days), Attention (mail + chats), News, System.
// Follows the sphere chip (ctx.bus "sphere" events and data-sphere on <html>); buttons to send the digest now and
// to get the family calendar (.ics) link. Plain DOM, every text es + en, nothing from the data is injected as HTML.
(() => {
  "use strict";
  if (!window.HubFacets) return;
  const { $, el, api, toast, L, lang, bus } = window.HubFacets.ctx;

  const T = {
    title: { es: "Hoy", en: "Today" },
    agenda: { es: "Agenda", en: "Agenda" },
    attention: { es: "Atención", en: "Attention" },
    news: { es: "Novedades", en: "News" },
    system: { es: "Sistema", en: "System" },
    overdue: { es: "Vencido", en: "Overdue" },
    today: { es: "Hoy", en: "Today" },
    tomorrow: { es: "Mañana", en: "Tomorrow" },
    week: { es: "Próximos 7 días", en: "Next 7 days" },
    nothing: { es: "Nada pendiente.", en: "Nothing pending." },
    mail: { es: "Correo", en: "Mail" },
    chat: { es: "Chat", en: "Chat" },
    incidents: { es: "Incidencias abiertas", en: "Open incidents" },
    jobs: { es: "Trabajos en marcha", en: "Jobs running" },
    notifs: { es: "Avisos de hoy", en: "Today's notifications" },
    sent: { es: "enviados", en: "sent" },
    held: { es: "retenidos", en: "held" },
    send: { es: "Enviar resumen ahora", en: "Send digest now" },
    sending: { es: "Enviando…", en: "Sending…" },
    sent_ok: { es: "Resumen enviado", en: "Digest sent" },
    sent_held: { es: "Resumen guardado, pero no se ha empujado", en: "Digest saved, but not pushed" },
    calendar: { es: "Calendario", en: "Calendar" },
    copy: { es: "Copiar enlace", en: "Copy link" },
    copy_lan: { es: "Copiar enlace de la red local", en: "Copy LAN link" },
    copied: { es: "Enlace copiado", en: "Link copied" },
    rotate: { es: "Cambiar el token", en: "Rotate the token" },
    rotate_confirm: { es: "Los calendarios ya suscritos dejarán de funcionar hasta que pongas el enlace nuevo. ¿Cambiar el token?", en: "Calendars already subscribed will stop working until you paste the new link. Rotate the token?" },
    rotated: { es: "Token cambiado", en: "Token rotated" },
    export_folder: { es: "Carpeta de exportación (se reescribe cada hora)", en: "Export folder (rewritten every hour)" },
    lan_port: { es: "Puerto en la red local (0 = apagado)", en: "LAN port (0 = off)" },
    save: { es: "Guardar", en: "Save" },
    saved: { es: "Guardado", en: "Saved" },
    ics_hint: { es: "Suscríbete a este enlace desde el móvil o el calendario del escritorio. Lleva un token secreto: compártelo solo con tus dispositivos. Con el puerto de red local abierto, cualquiera de tu red que tenga el enlace puede leerlo.", en: "Subscribe to this link from your phone or desktop calendar. It carries a secret token: share it only with your own devices. With the LAN port open, anyone on your network who has the link can read it." },
    last_digest: { es: "Último resumen", en: "Last digest" },
    loading: { es: "Cargando…", en: "Loading…" },
    failed: { es: "No se pudo cargar Hoy", en: "Could not load Today" },
    problems: { es: "Algunas apps no respondieron", en: "Some apps did not answer" },
    all_day: { es: "todo el día", en: "all day" },
    urgent: { es: "urgente", en: "urgent" },
    high: { es: "alta", en: "high" },
    open: { es: "Mostrar", en: "Show" },
    close: { es: "Ocultar", en: "Hide" },
  };
  const t = (k) => L(T[k]);

  function injectCss() {
    if ($("#css-today")) return;
    const s = document.createElement("style"); s.id = "css-today";
    s.textContent = `
      .facet-top#facet-today { border-bottom: 1px solid var(--line); background: var(--bg-2); }
      .today-box { padding: 8px 22px 12px; }
      .today-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
      .today-head .today-title { font-weight: 600; font-size: 15px; }
      .today-head .today-date { color: var(--muted); font-size: 12.5px; }
      .today-head .today-sphere { color: var(--accent); font-size: 12px; text-transform: uppercase; letter-spacing: .4px; }
      .today-head .spacer { flex: 1; }
      .today-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-top: 10px; align-items: start; }
      .today-col { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; min-width: 0; max-height: 300px; overflow: auto; }
      .today-col h3 { margin: 0 0 6px; font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; display: flex; gap: 6px; align-items: center; }
      .today-col h4 { margin: 8px 0 3px; font-size: 11.5px; font-weight: 600; color: var(--muted); }
      .today-col h4.bad { color: var(--red); }
      .t-row { display: flex; gap: 8px; align-items: baseline; padding: 2px 0; font-size: 12.5px; min-width: 0; }
      .t-row .t-when { color: var(--muted); font-family: var(--mono); font-size: 11.5px; flex: none; min-width: 3.4em; }
      .t-row .t-main { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .t-row .t-main a { color: var(--text); text-decoration: none; }
      .t-row .t-main a:hover { text-decoration: underline; color: var(--accent-2); }
      .t-row .t-app { color: var(--muted); font-size: 11.5px; flex: none; max-width: 38%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .t-row.bad .t-main, .t-row.bad .t-when { color: var(--red); }
      .t-row .t-tag { font-size: 10.5px; padding: 0 6px; border-radius: 6px; background: var(--card-2); color: var(--muted); flex: none; }
      .t-row .t-tag.urgent { background: #3a1f24; color: var(--red); }
      .t-row .t-tag.high { background: #3a2a10; color: var(--amber); }
      .today-empty { color: var(--muted); font-size: 12.5px; padding: 2px 0; }
      .today-warn { margin-top: 8px; color: var(--amber); font-size: 12px; }
      .today-ics { margin-top: 10px; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; display: grid; gap: 8px; }
      .today-ics .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .today-ics input[type=text] { flex: 1; min-width: 220px; font: inherit; font-family: var(--mono); font-size: 12px; color: var(--text); background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; }
      .today-ics label { color: var(--muted); font-size: 12px; display: grid; gap: 3px; }
      .today-ics input.narrow { max-width: 110px; min-width: 80px; flex: none; }
      @media (max-width: 640px) { .today-box { padding-left: 12px; padding-right: 12px; } }
    `;
    document.head.appendChild(s);
  }

  const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } };
  const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u)) ? u : "";

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso + "T12:00:00");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString(lang() === "es" ? "es-ES" : undefined, { weekday: "long", day: "numeric", month: "long" });
  }
  function itemWhen(it, withDay) {
    const start = String(it.start || "");
    const day = start.slice(0, 10);
    let when = "";
    if (withDay) {
      const d = new Date(day + "T12:00:00");
      when = isNaN(d) ? day.slice(5) : d.toLocaleDateString(lang() === "es" ? "es-ES" : undefined, { weekday: "short", day: "numeric" });
    }
    if (!it.all_day && start.length > 10) {
      const d = new Date(start);
      const hhmm = isNaN(d) ? start.slice(11, 16) : d.toLocaleTimeString(lang() === "es" ? "es-ES" : undefined, { hour: "2-digit", minute: "2-digit" });
      when = (when ? when + " " : "") + hhmm;
    }
    return when;
  }
  function textOrLink(text, url, cls) {
    const box = el("span", cls || "t-main");
    const href = safeUrl(url);
    if (href) { const a = el("a", "", text); a.href = href; a.target = "_blank"; a.rel = "noopener"; a.title = text; box.appendChild(a); }
    else { box.textContent = text; box.title = text; }
    return box;
  }

  const state = { open: lsGet("hub.today.open", "1") !== "0", sphere: "", data: null, loadedAt: 0, loading: false, error: "", icsOpen: false, ics: null, sending: false };
  let root = null;

  function currentSphere() {
    return state.sphere || (document.documentElement.dataset && document.documentElement.dataset.sphere) || "";
  }
  function setSphere(id) {
    id = id || "";
    if (id === state.sphere) return;
    state.sphere = id;
    load(true);
  }

  function agendaCol(data) {
    const col = el("div", "today-col");
    col.appendChild(el("h3", "", t("agenda")));
    const ag = data.agenda || {};
    let any = false;
    for (const [key, cls] of [["overdue", "bad"], ["today", ""], ["tomorrow", ""], ["week", ""]]) {
      const list = ag[key] || [];
      if (!list.length) continue;
      any = true;
      col.appendChild(el("h4", cls, `${t(key)} · ${list.length}`));
      for (const it of list) {
        const row = el("div", "t-row" + (key === "overdue" ? " bad" : ""));
        row.appendChild(el("span", "t-when", itemWhen(it, key === "overdue" || key === "week") || (key === "today" || key === "tomorrow" ? t("all_day") : "")));
        row.appendChild(textOrLink(it.title || "", it.url));
        if (it.priority === "urgent" || it.priority === "high") row.appendChild(el("span", "t-tag " + it.priority, t(it.priority)));
        if (it.app_name) row.appendChild(el("span", "t-app", it.app_name));
        if (it.detail) row.title = it.detail;
        col.appendChild(row);
      }
    }
    if (!any) col.appendChild(el("div", "today-empty", t("nothing")));
    return col;
  }

  function attentionCol(data) {
    const col = el("div", "today-col");
    col.appendChild(el("h3", "", t("attention")));
    const att = data.attention || {};
    const rows = [...(att.mail || []).map((r) => ["mail", r]), ...(att.chats || []).map((r) => ["chat", r])];
    if (!rows.length) col.appendChild(el("div", "today-empty", t("nothing")));
    for (const [kind, r] of rows.slice(0, 40)) {
      const row = el("div", "t-row");
      row.appendChild(el("span", "t-tag", t(kind)));
      const who = r.from_name || r.from_addr || r.from || r.channel || "";
      const what = r.subject || r.snippet || r.text || "";
      const main = el("span", "t-main", who ? `${who} — ${what}` : what);
      main.title = `${who} — ${what}`;
      row.appendChild(main);
      col.appendChild(row);
    }
    return col;
  }

  function newsCol(data) {
    const col = el("div", "today-col");
    col.appendChild(el("h3", "", t("news")));
    const list = data.news || [];
    if (!list.length) col.appendChild(el("div", "today-empty", t("nothing")));
    for (const n of list.slice(0, 40)) {
      const row = el("div", "t-row");
      row.appendChild(textOrLink(n.title || "", n.url));
      if (n.watch) row.appendChild(el("span", "t-app", n.watch));
      col.appendChild(row);
    }
    return col;
  }

  function systemCol(data) {
    const col = el("div", "today-col");
    col.appendChild(el("h3", "", t("system")));
    const sys = data.system || {};
    let any = false;
    if ((sys.incidents || []).length) {
      any = true;
      col.appendChild(el("h4", "bad", `${t("incidents")} · ${sys.incidents.length}`));
      for (const i of sys.incidents) {
        const row = el("div", "t-row bad");
        row.appendChild(el("span", "t-when", i.app || "?"));
        row.appendChild(el("span", "t-main", [i.to_state, i.probable_cause].filter(Boolean).join(" — ")));
        col.appendChild(row);
      }
    }
    if ((sys.jobs || []).length) {
      any = true;
      col.appendChild(el("h4", "", `${t("jobs")} · ${sys.jobs.length}`));
      for (const j of sys.jobs) {
        const row = el("div", "t-row");
        const pct = typeof j.progress === "number" && j.progress >= 0 && j.progress <= 1 ? Math.round(j.progress * 100) + " %" : "";
        row.appendChild(el("span", "t-when", pct));
        row.appendChild(textOrLink(j.title || j.kind || j.job_id || "", j.url));
        if (j.app) row.appendChild(el("span", "t-app", j.app));
        col.appendChild(row);
      }
    }
    const notifs = data.notifications || [];
    if (notifs.length) {
      any = true;
      const sent = notifs.filter((n) => n.kind === "sent").length;
      col.appendChild(el("h4", "", `${t("notifs")} · ${sent} ${t("sent")}, ${notifs.length - sent} ${t("held")}`));
    }
    if (!any) col.appendChild(el("div", "today-empty", t("nothing")));
    return col;
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
    try {
      const ta = el("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); const ok = document.execCommand("copy"); ta.remove(); return ok;
    } catch (e) { return false; }
  }

  function icsPanel() {
    const box = el("div", "today-ics");
    const info = state.ics;
    if (!info) { box.appendChild(el("div", "hint", t("loading"))); return box; }
    if (!info.ok) { box.appendChild(el("div", "hint", info.error || t("failed"))); return box; }
    const sph = currentSphere() || "all";
    const url = (info.urls && info.urls[sph]) || info.url;
    const lan = (info.lan_urls && info.lan_urls[sph]) || info.lan_url;
    const full = (u) => (u && u.startsWith("/") ? location.origin + u : u);
    const row1 = el("div", "row");
    const input = el("input"); input.type = "text"; input.readOnly = true; input.value = full(url) || ""; input.onfocus = () => input.select();
    row1.appendChild(input);
    const copy = el("button", "primary small", t("copy"));
    copy.onclick = async () => { if (await copyText(input.value)) toast(t("copied"), "ok"); };
    row1.appendChild(copy);
    if (lan) {
      const copyLan = el("button", "ghost small", t("copy_lan"));
      copyLan.onclick = async () => { if (await copyText(full(lan))) toast(t("copied"), "ok"); };
      row1.appendChild(copyLan);
    }
    const rot = el("button", "ghost small danger", t("rotate"));
    rot.onclick = async () => {
      if (!window.confirm(t("rotate_confirm"))) return;
      const res = await api("/api/today/ics/rotate", {});
      if (res && res.ok) { state.ics = res; toast(t("rotated"), "ok"); renderBody(); } else toast((res && res.error) || t("failed"), "err");
    };
    row1.appendChild(rot);
    box.appendChild(row1);
    if (info.lan_error) box.appendChild(el("div", "today-warn", info.lan_error));
    const row2 = el("div", "row");
    const lab1 = el("label", "", t("export_folder")); const ef = el("input"); ef.type = "text"; ef.value = info.export_path || ""; lab1.appendChild(ef);
    const lab2 = el("label", "", t("lan_port")); const lp = el("input", "narrow"); lp.type = "text"; lp.value = String(info.lan_port || 0); lab2.appendChild(lp);
    const save = el("button", "ghost small", t("save"));
    save.onclick = async () => {
      const res = await api("/api/today/ics/config", { export_path: ef.value.trim(), lan_port: parseInt(lp.value, 10) || 0 });
      if (res && res.ok) { state.ics = res; toast(t("saved"), "ok"); renderBody(); } else toast((res && res.error) || t("failed"), "err");
    };
    row2.appendChild(lab1); row2.appendChild(lab2); row2.appendChild(save);
    box.appendChild(row2);
    box.appendChild(el("div", "hint", t("ics_hint")));
    return box;
  }

  function renderBody() {
    if (!root) return;
    const body = root.querySelector(".today-body");
    if (!body) return;
    body.textContent = "";
    body.hidden = !state.open;
    if (!state.open) return;
    const data = state.data;
    if (!data) { body.appendChild(el("div", "today-empty", state.error || t("loading"))); return; }
    const cols = el("div", "today-cols");
    cols.appendChild(agendaCol(data)); cols.appendChild(attentionCol(data)); cols.appendChild(newsCol(data)); cols.appendChild(systemCol(data));
    body.appendChild(cols);
    const bad = (data.errors && data.errors.agenda) || [];
    if (bad.length) body.appendChild(el("div", "today-warn", `${t("problems")}: ${bad.map((e) => e.app).join(", ")}`));
    if (state.icsOpen) body.appendChild(icsPanel());
  }

  function renderHead() {
    if (!root) return;
    const head = root.querySelector(".today-head");
    head.textContent = "";
    const toggle = el("button", "ghost small", (state.open ? "▾ " : "▸ ") + t("title"));
    toggle.title = state.open ? t("close") : t("open");
    toggle.setAttribute("aria-expanded", state.open ? "true" : "false");
    toggle.onclick = () => { state.open = !state.open; lsSet("hub.today.open", state.open ? "1" : "0"); renderHead(); renderBody(); if (state.open && !state.data) load(true); };
    head.appendChild(toggle);
    const d = state.data;
    if (d) head.appendChild(el("span", "today-date", fmtDate(d.date)));
    if (d && d.sphere && d.sphere !== "all") head.appendChild(el("span", "today-sphere", d.sphere));
    if (d && d.last_digest && d.last_digest.ts) {
      head.appendChild(el("span", "hint", `${t("last_digest")}: ${window.HubFacets.ctx.fmtWhen(d.last_digest.ts)}`));
    }
    head.appendChild(el("span", "spacer"));
    const send = el("button", "primary small", state.sending ? t("sending") : t("send"));
    send.disabled = state.sending;
    send.onclick = sendDigest;
    head.appendChild(send);
    const cal = el("button", "ghost small", t("calendar"));
    cal.onclick = async () => {
      state.icsOpen = !state.icsOpen;
      if (state.icsOpen) { state.open = true; lsSet("hub.today.open", "1"); state.ics = null; renderHead(); renderBody(); state.ics = await api("/api/today/ics"); renderHead(); }
      renderBody();
    };
    head.appendChild(cal);
  }

  async function sendDigest() {
    if (state.sending) return;
    state.sending = true; renderHead();
    try {
      const res = await api("/api/today/digest", { sphere: currentSphere() || undefined, send: true });
      if (res && res.ok) {
        const s = res.sent || {};
        toast(s.held ? `${t("sent_held")} (${s.held})` : t("sent_ok"), s.held ? "info" : "ok");
        load(true);
      } else toast((res && res.error) || t("failed"), "err");
    } catch (e) { toast(t("failed"), "err", String(e)); }
    state.sending = false; renderHead();
  }

  async function load(force) {
    if (state.loading) return;
    if (!force && state.data && Date.now() - state.loadedAt < 60000) return;
    if (!state.open && state.data) return;
    state.loading = true;
    try {
      const sph = currentSphere();
      const data = await api("/api/today" + (sph ? "?sphere=" + encodeURIComponent(sph) : ""));
      if (data && data.ok) { state.data = data; state.error = ""; state.loadedAt = Date.now(); }
      else state.error = (data && data.error) || t("failed");
    } catch (e) { state.error = t("failed"); }
    state.loading = false;
    renderHead(); renderBody();
  }

  window.HubFacets.register("today", {
    placement: "top",
    label: T.title,
    order: 10,
    mount(container) {
      injectCss();
      root = el("div", "today-box");
      root.appendChild(el("div", "today-head"));
      root.appendChild(el("div", "today-body"));
      container.appendChild(root);
      bus.addEventListener("sphere", (e) => setSphere(e && e.detail));
      bus.addEventListener("lang", () => { renderHead(); renderBody(); });
      try {
        new MutationObserver(() => setSphere(document.documentElement.dataset.sphere || "")).observe(document.documentElement, { attributes: true, attributeFilter: ["data-sphere"] });
      } catch (e) { /* ignore */ }
      state.sphere = (document.documentElement.dataset && document.documentElement.dataset.sphere) || "";
      renderHead(); renderBody();
    },
    load() { return load(true); },
    tick() { if (state.open && !document.hidden) load(false); },
  });
})();

// One mailbox at a time, with a dedicated reader. All content stays in document flow.
(() => {
  "use strict";
  if (!window.HubFacets) return;
  const { $, el, api, toast, L, fmtWhen, bus } = window.HubFacets.ctx;

  const T = {
    title: { es: "Correo", en: "Mail" },
    on: { es: "Activar lectura", en: "Turn reading on" }, off: { es: "Desactivar", en: "Turn off" },
    now: { es: "Leer ahora", en: "Read now" }, check: { es: "Comprobar cuentas", en: "Check accounts" },
    disabled: { es: "La lectura del correo está desactivada: el hub no lee nada.", en: "Mail reading is off: the hub reads nothing." },
    not_configured: { es: "No encuentro la carpeta de Faustus (hub.json → faustus_dir, o HOARD_FAUSTUS_DIR).", en: "Faustus folder not found (hub.json faustus_dir, or HOARD_FAUSTUS_DIR)." },
    accounts: { es: "Cuentas", en: "Accounts" }, no_accounts: { es: "sin cuentas leídas todavía", en: "no accounts read yet" },
    last: { es: "Última lectura", en: "Last pass" }, next: { es: "Próxima", en: "Next" }, never: { es: "nunca", en: "never" },
    new_n: { es: "nuevos", en: "new" }, running: { es: "leyendo…", en: "reading…" }, manual: { es: "solo manual", en: "manual only" },
    sphere: { es: "Esfera", en: "Sphere" }, all: { es: "Todas", en: "All" },
    attention: { es: "Necesita tu atención", en: "Needs you" }, unclaimed: { es: "Sin dueño", en: "Unclaimed" }, recent: { es: "Recientes", en: "Recent" },
    nothing: { es: "Nada por aquí.", en: "Nothing here." },
    dismiss: { es: "Descartar", en: "Dismiss" }, dismiss_all: { es: "Descartar la categoría", en: "Dismiss category" }, open: { es: "Abrir", en: "Open" },
    close: { es: "Cerrar", en: "Close" }, confirm_all: { es: "¿Descartar todos los de esta categoría?", en: "Dismiss every one in this category?" },
    settings: { es: "Ajustes", en: "Settings" }, interval: { es: "Cada (min, 0 = solo manual)", en: "Every (min, 0 = manual only)" },
    retention: { es: "Guardar el texto (días)", en: "Keep the text (days)" }, owner: { es: "Usuario de Faustus (si hay varios)", en: "Faustus user (when several)" },
    save: { es: "Guardar", en: "Save" }, saved: { es: "guardado", en: "saved" },
    pass_ok: (r) => ({ es: `${r.new} nuevos de ${r.seen} leídos`, en: `${r.new} new of ${r.seen} read` }),
    claimed_by: { es: "reclamado por", en: "claimed by" }, attachments: { es: "Adjuntos", en: "Attachments" }, links: { es: "Enlaces", en: "Links" },
    c_all: { es: "todo", en: "all" }, c_promo: { es: "promo", en: "promo" }, c_social: { es: "social", en: "social" },
    c_security: { es: "seguridad", en: "security" }, c_dev: { es: "dev", en: "dev" }, c_other: { es: "otros", en: "other" },
    interests: { es: "Apps con interés registrado", en: "Apps with a registered interest" },
    sent_to: { es: "para", en: "for" }, why: { es: "por qué", en: "why" },
    back: { es: "Volver a la bandeja", en: "Back to inbox" },
    search: { es: "Buscar en esta lista", en: "Search this list" },
    loading: { es: "Cargando correo…", en: "Loading mail…" },
    retry: { es: "Reintentar", en: "Try again" },
    failed: { es: "No se pudo cargar el correo.", en: "Mail could not be loaded." },
    no_subject: { es: "Sin asunto", en: "No subject" },
    previous: { es: "Anterior", en: "Previous" }, next_page: { es: "Siguiente", en: "Next" },
    range: (start, end, total) => ({ es: `${start}–${end} de ${total}`, en: `${start}–${end} of ${total}` }),
    cached: { es: "Hasta 100 sin dueño de los últimos 7 días y 40 recientes", en: "Up to 100 unclaimed from the last 7 days and 40 recent" },
  };
  const t = (k, ...a) => { const v = T[k]; return L(typeof v === "function" ? v(...a) : v || { es: k, en: k }); };

  let box, statusEl, formEl, colsEl, sphereSel, cat = "all", lastSig = "", lastStatus = null, lastActive = "";
  let mailForm = null, queue = "", query = "", page = 0, selected = null;
  let snapshot = null, listScope = null, listRequest = 0, readerRequest = 0, listError = "";
  let queueEl, filtersEl, listEl, readerEl, searchEl;
  const PAGE_SIZE = 20;

  const ago = (ts) => {
    if (!ts) return t("never");
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return L({ es: "hace un momento", en: "just now" });
    if (s < 5400) return L({ es: `hace ${Math.round(s / 60)} min`, en: `${Math.round(s / 60)} min ago` });
    if (s < 129600) return L({ es: `hace ${Math.round(s / 3600)} h`, en: `${Math.round(s / 3600)} h ago` });
    return fmtWhen(ts);
  };
  const inFuture = (ts) => {
    const s = ts - Date.now() / 1000;
    if (s <= 0) return L({ es: "en breve", en: "soon" });
    return s < 5400 ? L({ es: `en ${Math.max(1, Math.round(s / 60))} min`, en: `in ${Math.max(1, Math.round(s / 60))} min` }) : fmtWhen(ts);
  };
  const sphereId = () => (sphereSel && sphereSel.value) || "";
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v !== undefined && v !== null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const chip = (text, cls = "") => el("span", `chip ${cls}`.trim(), text);

  function mount(container) {
    box = container;
    const tools = el("div", "tab-tools");
    const sphereLabel = el("span", "hint", t("sphere") + ":");
    tools.appendChild(sphereLabel);
    sphereSel = el("select", "small");
    sphereSel.setAttribute("aria-label", t("sphere"));
    sphereSel.onchange = () => { lastSig = ""; loadLists(); };
    tools.appendChild(sphereSel);
    const settingsBtn = el("button", "ghost small", t("settings"));
    settingsBtn.setAttribute("aria-expanded", "false");
    settingsBtn.onclick = () => { formEl.hidden = !formEl.hidden; settingsBtn.setAttribute("aria-expanded", String(!formEl.hidden)); };
    tools.appendChild(settingsBtn);
    box.appendChild(tools);
    statusEl = el("div", "mg-status"); box.appendChild(statusEl);
    formEl = el("div", "mg-form"); formEl.hidden = true; box.appendChild(formEl);
    colsEl = el("section", "mg-mailbox"); box.appendChild(colsEl);
    queueEl = el("nav", "mg-queues"); queueEl.setAttribute("aria-label", t("title")); colsEl.appendChild(queueEl);
    filtersEl = el("div", "mg-filters"); colsEl.appendChild(filtersEl);
    listEl = el("div", "mg-list"); colsEl.appendChild(listEl);
    readerEl = el("article", "mg-reader"); readerEl.hidden = true; colsEl.appendChild(readerEl);
    bus.addEventListener("sphere", () => { followActive(); });
    bus.addEventListener("lang", () => {
      sphereLabel.textContent = t("sphere") + ":"; sphereSel.setAttribute("aria-label", t("sphere")); queueEl.setAttribute("aria-label", t("title")); settingsBtn.textContent = t("settings");
      loadSpheres().then(() => { lastSig = ""; renderStatus(); buildForm(); loadLists(); });
    });
  }

  async function loadSpheres() {
    let spheres = [], active = "";
    try { const r = await api("/api/spheres"); if (r && r.ok) { spheres = r.spheres || []; active = r.active || ""; } } catch (e) { /* the spheres facet may be absent */ }
    const keep = sphereSel.value;
    sphereSel.innerHTML = "";
    const all = el("option", "", t("all")); all.value = ""; sphereSel.appendChild(all);
    for (const s of spheres) { const o = el("option", "", L(s.name) || s.id); o.value = s.id; sphereSel.appendChild(o); }
    if (!spheres.length) { const o = el("option", "", "personal"); o.value = "personal"; sphereSel.appendChild(o); }
    sphereSel.value = keep && [...sphereSel.options].some((o) => o.value === keep) ? keep : (active || document.documentElement.dataset.sphere || "");
    if (![...sphereSel.options].some((o) => o.value === sphereSel.value)) sphereSel.value = "";
    lastActive = active;
  }
  function followActive() {
    const a = document.documentElement.dataset.sphere || "";
    if (a && [...sphereSel.options].some((o) => o.value === a)) { sphereSel.value = a; lastSig = ""; loadLists(); }
  }

  function renderStatus() {
    const s = lastStatus; statusEl.innerHTML = "";
    if (!s) return;
    const actions = el("div", "mg-status-actions"), info = el("div", "mg-status-info");
    statusEl.appendChild(actions); statusEl.appendChild(info);
    const toggle = el("button", s.enabled ? "ghost small" : "primary small", s.enabled ? t("off") : t("on"));
    toggle.onclick = async () => {
      const r = await api("/api/mail/config", { enabled: !s.enabled });
      toast(r.ok ? (r.config.enabled ? t("on") + " ✓" : t("off") + " ✓") : r.error, r.ok ? "ok" : "err");
      await loadStatus(true);
    };
    actions.appendChild(toggle);
    if (s.enabled) {
      const now = el("button", "primary", s.running ? t("running") : t("now")); now.disabled = !!s.running;
      now.onclick = async () => {
        now.disabled = true; now.textContent = t("running");
        const r = await api("/api/mail/fetch", {});
        toast(r.ok ? t("pass_ok", r) : (r.error || "error"), r.ok ? "ok" : "err");
        lastSig = ""; await loadStatus(true); loadLists();
      };
      actions.appendChild(now);
    }
    const chk = el("button", "ghost small", t("check"));
    chk.onclick = async () => { chk.disabled = true; await loadStatus(true, true); chk.disabled = false; };
    actions.appendChild(chk);
    if (!s.enabled) info.appendChild(el("span", "hint", t("disabled")));
    else if (!s.configured) info.appendChild(el("span", "chip bad", t("not_configured")));
    info.appendChild(el("span", "hint", t("accounts") + ":"));
    if (!(s.accounts || []).length) info.appendChild(el("span", "hint", t("no_accounts")));
    for (const a of s.accounts || []) info.appendChild(chip(`${a.account} → ${a.sphere}`, "info"));
    const lp = s.last_pass;
    const last = el("span", "hint");
    last.textContent = `${t("last")}: ${lp ? ago(lp.ts) + (lp.ok ? ` · ${L(T.pass_ok(lp))}` : "") : t("never")}`;
    info.appendChild(last);
    if (lp && !lp.ok && lp.error) info.appendChild(chip(lp.error, "bad"));
    if (s.enabled) info.appendChild(el("span", "hint", `${t("next")}: ${s.next_pass_ts ? inFuture(s.next_pass_ts) : t("manual")}`));
    const badge = document.getElementById("mailgate-badge");
    if (badge) { const n = (s.counts || {}).attention || 0; badge.textContent = n ? String(n) : ""; badge.className = "badge" + (n ? " warn" : ""); }
  }

  function buildForm() {
    if (mailForm && !formEl.hidden) return;
    mailForm = null;
    formEl.innerHTML = "";
    if (!lastStatus) return;
    api("/api/mail/config").then((r) => {
      if (!r || !r.ok) return;
      const c = r.config; formEl.innerHTML = "";
      const mk = (label, key, type, width) => {
        const inp = el("input"); inp.name = key; inp.type = type; inp.value = c[key]; if (width) inp.style.width = width;
        const lab = el("label", "", label); lab.appendChild(inp); formEl.appendChild(lab); return inp;
      };
      const iv = mk(t("interval"), "interval_min", "number"), rt = mk(t("retention"), "retention_days", "number"), ow = mk(t("owner"), "owner", "text", "180px");
      const collect = () => ({interval_min:Number(iv.value),retention_days:Number(rt.value),owner:ow.value});
      const before = collect();
      mailForm = {collect,before};
      const save = el("button", "primary small", t("save"));
      save.onclick = async () => {
        const res = await api("/api/mail/config", window.HubFacets.ctx.changedFields(before,collect()));
        if (res.ok) mailForm.before = Object.assign(before,collect());
        toast(res.ok ? t("saved") : res.error, res.ok ? "ok" : "err"); loadStatus(true);
      };
      formEl.appendChild(save);
    }).catch(() => {});
  }

  async function loadStatus(force, refresh) {
    let r;
    try { r = await api("/api/mail/status" + (refresh ? "?refresh=1" : "")); }
    catch (e) { r = {ok: false, error: e.message}; }
    if (!r || !r.ok) {
      statusEl.textContent = `${t("failed")} ${(r && r.error) || ""}`;
      const retry = el("button", "ghost", t("retry")); retry.onclick = () => loadStatus(true);
      statusEl.appendChild(retry); return false;
    }
    const changed = !lastStatus || r.enabled !== lastStatus.enabled || (r.counts || {}).total !== (lastStatus.counts || {}).total ||
      JSON.stringify(r.last_pass) !== JSON.stringify(lastStatus.last_pass);
    lastStatus = r; renderStatus();
    if (force || changed) { lastSig = ""; if (!formEl.hidden || !formEl.children.length) buildForm(); }
    return changed;
  }

  const safeUrl = (value) => {
    try { if (!value) return null; const u = new URL(value, location.href); return ["http:", "https:"].includes(u.protocol) ? u.href : null; } catch { return null; }
  };
  function link(value, label) {
    const url = safeUrl(value);
    if (!url) return el("span", "", label || value || "");
    const a = el("a", "", label || new URL(url).hostname);
    a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.title = url;
    return a;
  }
  // Render text without accepting HTML from messages, and keep tracking URLs readable.
  function appendText(node, text) {
    const re = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|https?:\/\/[^\s<>]+/g;
    let start = 0;
    for (const m of text.matchAll(re)) {
      node.appendChild(document.createTextNode(text.slice(start, m.index)));
      node.appendChild(link(m[2] || m[0], m[1])); start = m.index + m[0].length;
    }
    node.appendChild(document.createTextNode(text.slice(start)));
  }
  function messageBody(msg) {
    const body = el("div", "mg-body");
    for (const block of String(msg.text || msg.snippet || "(…)").split(/\n\s*\n/)) {
      const heading = block.match(/^#{1,6}\s+([^\n]+)$/);
      const p = el(heading ? "h3" : "p"); appendText(p, heading ? heading[1] : block); body.appendChild(p);
    }
    for (const [key, items] of [["attachments", msg.attachments || []], ["links", msg.links || []]]) {
      if (!items.length) continue;
      const section = el("section", "mg-related"); section.appendChild(el("h3", "", t(key)));
      const ul = el("ul");
      for (const item of items) {
        const li = el("li"); li.appendChild(link(item.url, item.name || item.label || undefined));
        if (item.size) li.appendChild(el("span", "hint", ` · ${Math.round(item.size / 1024)} KB`));
        ul.appendChild(li);
      }
      section.appendChild(ul); body.appendChild(section);
    }
    return body;
  }
  function backToList(focus = true) {
    const previous = selected && String(selected.id);
    ++readerRequest; selected = null; readerEl.hidden = true;
    queueEl.hidden = filtersEl.hidden = listEl.hidden = false;
    renderList();
    if (focus) { const target = [...listEl.querySelectorAll("button.mg-message")].find((b) => b.dataset.messageId === previous) || listEl.querySelector("button.mg-message"); if (target) target.focus(); }
  }
  function renderReader(msg) {
    readerEl.innerHTML = "";
    const actions = el("div", "mg-reader-actions");
    const back = el("button", "ghost", t("back")); back.onclick = () => backToList(); actions.appendChild(back);
    if (queue !== "recent") {
      const dismiss = el("button", "ghost", t("dismiss"));
      dismiss.onclick = async () => {
        dismiss.disabled = true;
        try {
          const r = await api("/api/mail/dismiss", { ids: [msg.id] });
          if (!r.ok) throw new Error(r.error || t("failed"));
          backToList(); lastSig = ""; await Promise.all([loadStatus(true), loadLists()]);
        } catch (e) { toast(e.message, "err"); dismiss.disabled = false; }
      };
      actions.appendChild(dismiss);
    }
    readerEl.appendChild(actions);
    const h = el("h2", "mg-reader-title", msg.subject || t("no_subject")); h.tabIndex = -1; readerEl.appendChild(h);
    readerEl.appendChild(el("div", "mg-reader-from", [msg.from_name, msg.from_addr].filter(Boolean).join(" · ")));
    readerEl.appendChild(el("div", "mg-reader-meta", [fmtWhen(msg.date_ts), msg.sphere].filter(Boolean).join(" · ")));
    const claims = el("div", "mg-chips");
    for (const c of msg.claims || []) claims.appendChild(chip(`${t("claimed_by")} ${c.app}`));
    for (const reason of msg.reasons || []) claims.appendChild(chip(reason));
    readerEl.appendChild(claims); readerEl.appendChild(messageBody(msg)); h.focus();
  }
  async function openMessage(m) {
    const token = ++readerRequest, scope = sphereId(); selected = m;
    queueEl.hidden = filtersEl.hidden = listEl.hidden = true; readerEl.hidden = false;
    readerEl.innerHTML = "";
    const back = el("button", "ghost", t("back")); back.onclick = () => backToList(); readerEl.appendChild(back);
    readerEl.appendChild(el("p", "hint", t("loading")));
    try {
      const r = await api(`/api/mail/messages/${encodeURIComponent(m.id)}`);
      if (token !== readerRequest || scope !== sphereId()) return;
      if (!r.ok) throw new Error(r.error || t("failed"));
      selected = r.message; renderReader(r.message);
    } catch (e) {
      if (token !== readerRequest || scope !== sphereId()) return;
      readerEl.lastChild.remove(); readerEl.appendChild(el("p", "mg-error", `${t("failed")} ${e.message}`));
      const retry = el("button", "", t("retry")); retry.onclick = () => openMessage(m); readerEl.appendChild(retry);
    }
  }
  function row(m) {
    const r = el("button", "mg-message"); r.type = "button"; r.dataset.messageId = m.id;
    r.appendChild(el("span", "mg-sender", m.from_name || m.from_addr || "—"));
    const text = el("span", "mg-summary");
    text.appendChild(el("strong", "mg-subject", m.subject || t("no_subject")));
    if (m.snippet) text.appendChild(el("span", "mg-snippet", m.snippet));
    const tags = [...(m.claims || []).map((c) => `${t("claimed_by")} ${c.app}`), ...(m.priority === "attention" ? m.reasons || [] : [])];
    if (m.n_attachments) tags.push(`${t("attachments")}: ${m.n_attachments}`);
    if (tags.length) text.appendChild(el("span", "mg-tags", tags.join(" · ")));
    r.appendChild(text);
    r.appendChild(el("span", "mg-date", ago(m.date_ts)));
    r.onclick = () => openMessage(m); return r;
  }
  function filteredMessages() {
    const rows = snapshot ? snapshot[queue] || [] : [];
    return rows.filter((m) => (queue !== "unclaimed" || cat === "all" || m.category === cat) &&
      (!query || [m.subject, m.from_name, m.from_addr, m.snippet].join(" ").toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  }
  function renderList() {
    if (selected) return;
    queueEl.innerHTML = ""; filtersEl.innerHTML = ""; listEl.innerHTML = "";
    for (const key of ["attention", "unclaimed", "recent"]) {
      const b = el("button", "mg-queue", t(key)); b.type = "button";
      b.setAttribute("aria-pressed", String(queue === key));
      b.appendChild(el("span", "mg-count", String(snapshot ? snapshot[key].length : "—")));
      b.onclick = () => { queue = key; page = 0; renderList(); }; queueEl.appendChild(b);
    }
    if (queue === "unclaimed" && snapshot) {
      const label = el("label", "mg-category", L({es:"Categoría",en:"Category"}));
      const sel = el("select");
      for (const key of ["all", "promo", "social", "security", "dev", "other"]) {
        const n = snapshot.unclaimed.filter((m) => key === "all" || m.category === key).length;
        if (!n && key !== "all" && key !== cat) continue;
        const o = el("option", "", `${t("c_" + key)} (${n})`); o.value = key; sel.appendChild(o);
      }
      sel.value = cat; sel.onchange = () => { cat = sel.value; page = 0; renderList(); }; label.appendChild(sel); filtersEl.appendChild(label);
    }
    searchEl = el("input", "mg-search"); searchEl.type = "search"; searchEl.placeholder = t("search");
    searchEl.setAttribute("aria-label", t("search")); searchEl.value = query;
    searchEl.oninput = () => { query = searchEl.value; page = 0; renderRows(); };
    filtersEl.appendChild(searchEl);
    if (listError) {
      const error = el("div", "mg-error", listError);
      const retry = el("button", "ghost", t("retry")); retry.onclick = loadLists; error.appendChild(retry); listEl.appendChild(error);
    }
    renderRows();
  }
  function renderRows() {
    const old = listEl.querySelector(".mg-results"); if (old) old.remove();
    const results = el("div", "mg-results"); listEl.appendChild(results);
    if (!snapshot) { results.appendChild(el("p", "mg-empty", listError ? "" : t("loading"))); return; }
    const messages = filteredMessages();
    page = Math.min(page, Math.max(0, Math.ceil(messages.length / PAGE_SIZE) - 1));
    const top = el("div", "mg-list-heading");
    top.appendChild(el("h2", "", t(queue)));
    top.appendChild(el("span", "hint", messages.length ? t("range", page * PAGE_SIZE + 1, Math.min((page + 1) * PAGE_SIZE, messages.length), messages.length) : "0"));
    results.appendChild(top);
    if (queue === "unclaimed" && cat !== "all" && messages.length) {
      const dismiss = el("button", "ghost mg-bulk", t("dismiss_all"));
      dismiss.onclick = async () => {
        if (!confirm(t("confirm_all"))) return;
        // Category operation deliberately includes every loaded message of that category, irrespective of search.
        const ids = snapshot.unclaimed.filter((m) => m.category === cat).map((m) => m.id);
        try { const r = await api("/api/mail/dismiss", {ids}); if (!r.ok) throw new Error(r.error); lastSig = ""; await Promise.all([loadStatus(true), loadLists()]); }
        catch (e) { toast(e.message, "err"); }
      }; results.appendChild(dismiss);
    }
    if (!messages.length) results.appendChild(el("p", "mg-empty", t("nothing")));
    for (const m of messages.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) results.appendChild(row(m));
    const foot = el("div", "mg-list-footer"); foot.appendChild(el("span", "hint", t("cached")));
    if (messages.length > PAGE_SIZE) {
      for (const [label, delta, disabled] of [["previous", -1, page === 0], ["next_page", 1, (page + 1) * PAGE_SIZE >= messages.length]]) {
        const b = el("button", "ghost", t(label)); b.disabled = disabled;
        b.onclick = () => { page += delta; renderRows(); listEl.querySelector("h2").scrollIntoView({block:"start"}); }; foot.appendChild(b);
      }
    }
    results.appendChild(foot);
    if (queue === "recent" && (snapshot.interests || []).length) results.appendChild(el("p", "hint", `${t("interests")}: ${snapshot.interests.map((i) => i.app).join(", ")}`));
  }
  async function loadLists() {
    const sp = sphereId(), token = ++listRequest;
    if (listScope !== sp) {
      listScope = sp; snapshot = null; lastSig = ""; query = ""; cat = "all"; queue = ""; page = 0;
      backToList(false);
    }
    try {
      const responses = await Promise.all([
        api("/api/mail/attention?" + qs({ sphere: sp, days: 7, limit: 40 })),
        api("/api/mail/unclaimed?" + qs({ sphere: sp, days: 7, limit: 100 })),
        api("/api/mail/messages?" + qs({ sphere: sp, kind: "mail", limit: 40 })),
        api("/api/mail/interests"),
      ]);
      if (token !== listRequest || sp !== sphereId()) return;
      for (const r of responses.slice(0, 3)) if (!r || !r.ok) throw new Error((r && r.error) || t("failed"));
      const sig = JSON.stringify(responses.map((r) => r.messages || r.interests));
      const hadError = !!listError; listError = "";
      if (sig === lastSig && !hadError) return;
      lastSig = sig; snapshot = Object.fromEntries(["attention", "unclaimed", "recent"].map((k, i) => [k, responses[i].messages || []]));
      snapshot.interests = (responses[3] && responses[3].interests) || [];
      if (!queue) queue = snapshot.attention.length ? "attention" : snapshot.unclaimed.length ? "unclaimed" : "recent";
      renderList();
    } catch (e) {
      if (token !== listRequest || sp !== sphereId()) return;
      listError = `${t("failed")} ${e.message}`; renderList();
    }
  }

  async function load() {
    if (!sphereSel.options.length) await loadSpheres();
    await loadStatus(true);
    await loadLists();
  }

  window.HubFacets.register("mailgate", {
    copilot:{context:() => ({active_objects:selected ? [{kind:'mail',object_id:String(selected.id),title:selected.subject,sphere:selected.sphere}] : [],view:{section:'mailgate',sphere:sphereId(),queue,category:cat},pending_forms:mailForm && !formEl.hidden && !selected
      ? [{kind:'mail_config',is_new:false,fields:mailForm.collect(),changes:window.HubFacets.ctx.changedFields(mailForm.before,mailForm.collect())}] : []}),refresh:load},
    placement: "tab", label: T.title, order: 40, mount,
    load: () => load().catch((e) => console.error("mail tab", e)),
    tick: async () => {
      try {
        const a = document.documentElement.dataset.sphere || "";
        if (a && a !== lastActive) { lastActive = a; followActive(); }
        const changed = await loadStatus(false);
        if (changed) await loadLists();
      } catch (e) { /* the hub is restarting */ }
    },
  });
})();

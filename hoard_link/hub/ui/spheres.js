/* Hoard Hub UI — spheres (header chip + configuration dialog).
   Facet "spheres": a chip in the top bar with the active sphere's name and colour; click → switch, or open the
   dialog that edits every sphere (form + JSON fallback). After a switch it sets <html data-sphere="…"> and fires a
   "sphere" CustomEvent ({detail: id}) on ctx.bus so other facets (Today, Mail…) follow. Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const ctx = H.ctx;
  const { el, api, toast, L } = ctx;

  const S = {
    sphere: { es: "Esfera", en: "Sphere" },
    switch_to: { es: "Cambiar de esfera", en: "Switch sphere" },
    configure: { es: "Configurar esferas…", en: "Configure spheres…" },
    title: { es: "Esferas", en: "Spheres" },
    close: { es: "Cerrar", en: "Close" },
    save: { es: "Guardar", en: "Save" },
    saved: { es: "Esferas guardadas", en: "Spheres saved" },
    add: { es: "+ Nueva esfera", en: "+ New sphere" },
    remove: { es: "Quitar esfera", en: "Remove sphere" },
    confirm_remove: { es: "¿Quitar esta esfera?", en: "Remove this sphere?" },
    json: { es: "Editar JSON", en: "Edit JSON" },
    form: { es: "Volver al formulario", en: "Back to the form" },
    id: { es: "Identificador", en: "Identifier" },
    id_hint: { es: "a-z, 0-9, - y _ (no se puede cambiar luego)", en: "a-z, 0-9, - and _ (cannot be changed later)" },
    name_es: { es: "Nombre (español)", en: "Name (Spanish)" },
    name_en: { es: "Nombre (inglés)", en: "Name (English)" },
    color: { es: "Color", en: "Colour" },
    mail_accounts: { es: "Cuentas de correo", en: "Mail accounts" },
    mail_hint: { es: "Una por línea: id, nombre o dirección de la cuenta de Faustus. * = las que nadie reclama.", en: "One per line: id, name or address of the Faustus account. * = every account nobody claims." },
    chat_sources: { es: "Fuentes de chat", en: "Chat sources" },
    chat_hint: { es: "Ids de las fuentes de la pestaña Chats.", en: "Ids of the sources in the Chats tab." },
    vip: { es: "VIP", en: "VIP" },
    vip_hint: { es: "Direcciones o @dominios: su correo y chats siempre piden atención.", en: "Addresses or @domains: their mail and chats always need attention." },
    keywords: { es: "Palabras clave", en: "Keywords" },
    keywords_hint: { es: "Un mensaje que las contenga pide atención.", en: "A message containing them needs attention." },
    mute: { es: "Silenciar", en: "Mute" },
    mute_hint: { es: "Direcciones, @dominios o palabras: prioridad baja.", en: "Addresses, @domains or words: low priority." },
    apps: { es: "Apps de esta esfera", en: "Apps of this sphere" },
    apps_hint: { es: "Ids de apps; * = todas. El correo de la esfera solo se ofrece a estas.", en: "App ids; * = all. The sphere's mail is only offered to these." },
    quiet: { es: "Horas de silencio", en: "Quiet hours" },
    from: { es: "Desde", en: "From" },
    to: { es: "Hasta", en: "To" },
    days: { es: "Días", en: "Days" },
    quiet_hint: { es: "Dentro de este horario solo suena lo urgente. Mismo inicio y fin = sin silencio.", en: "Inside this window only urgent things sound. Same start and end = no quiet hours." },
    daily: { es: "Todos los días", en: "Every day" },
    weekdays: { es: "Laborables", en: "Weekdays" },
    weekends: { es: "Fines de semana", en: "Weekends" },
    custom: { es: "Elegir días…", en: "Pick days…" },
    routing: { es: "Cómo se te avisa", en: "How you are told" },
    routing_hint: { es: "Canales por prioridad. «Resumen» = no avisar, dejarlo para el resumen diario.", en: "Channels per priority. “Digest” = do not push, keep it for the daily digest." },
    digest: { es: "Resumen diario", en: "Daily digest" },
    digest_on: { es: "Activado", en: "Enabled" },
    digest_at: { es: "Hora", en: "Time" },
    digest_sum: { es: "Resumir con el modelo local si ya está cargado", en: "Summarise with the local model when one is loaded" },
    digest_channels: { es: "Se envía por", en: "Sent through" },
    urgent: { es: "Urgente", en: "Urgent" }, high: { es: "Alta", en: "High" },
    normal: { es: "Normal", en: "Normal" }, low: { es: "Baja", en: "Low" },
    windows: { es: "Windows", en: "Windows" }, ntfy: { es: "ntfy", en: "ntfy" },
    telegram: { es: "Telegram", en: "Telegram" }, email: { es: "Correo", en: "Mail" },
    digest_ch: { es: "Resumen", en: "Digest" },
    new_name: { es: "Nueva esfera", en: "New sphere" },
    bad_json: { es: "JSON no válido: ", en: "Invalid JSON: " },
  };
  const t = (k) => L(S[k]);
  const PRIOS = ["urgent", "high", "normal", "low"];
  const PUSH = ["windows", "ntfy", "telegram", "email"];
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const DAY_LABEL = { mon: { es: "L", en: "M" }, tue: { es: "M", en: "T" }, wed: { es: "X", en: "W" }, thu: { es: "J", en: "T" }, fri: { es: "V", en: "F" }, sat: { es: "S", en: "S" }, sun: { es: "D", en: "S" } };

  let spheres = [];
  let active = "personal";
  let firstLoad = true;
  let chip, menu, slot;

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const nameOf = (s) => (s && s.name) ? (L(s.name) || s.id) : "";
  const toLines = (a) => (a || []).join("\n");
  const fromLines = (s) => String(s || "").split(/[\n,;]+/).map((x) => x.trim()).filter(Boolean);

  function injectCss() {
    if (document.getElementById("css-spheres")) return;
    const css = document.createElement("style");
    css.id = "css-spheres";
    css.textContent = `
      #facet-spheres { position: relative; display: inline-block; }
      .sphere-chip { display: inline-flex; align-items: center; gap: 7px; }
      .sphere-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; background: var(--sphere-color, #8a8f98); box-shadow: 0 0 8px var(--sphere-color, #8a8f98); flex: none; }
      .sphere-menu { position: absolute; top: calc(100% + 6px); left: 0; z-index: 30; min-width: 220px; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 6px; box-shadow: 0 10px 28px rgba(0,0,0,.45); display: grid; gap: 2px; }
      .sphere-menu button { display: flex; align-items: center; gap: 8px; text-align: left; width: 100%; background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 6px 9px; color: var(--text); font: inherit; font-size: 13px; cursor: pointer; }
      .sphere-menu button:hover { background: var(--card-2); }
      .sphere-menu button.on { border-color: var(--line); }
      .sphere-menu hr { border: 0; border-top: 1px solid var(--line); margin: 4px 0; width: 100%; }
      .sphere-menu .tick { margin-left: auto; color: var(--green); }
      #spheres-dialog { width: min(1000px, 96vw); }
      #spheres-dialog[open] { display: flex; flex-direction: column; max-height: 90vh; }
      #spheres-dialog .sp-body { flex: 1 1 auto; min-height: 0; display: grid; overflow: hidden; }
      #spheres-dialog .sp-body > .sp-main { overflow: auto; }
      #spheres-dialog .sp-wrap { display: grid; grid-template-columns: 190px 1fr; min-height: 0; overflow: hidden; }
      #spheres-dialog .sp-side { border-right: 1px solid var(--line); padding: 10px; display: grid; gap: 4px; align-content: start; overflow: auto; }
      #spheres-dialog .sp-side button { display: flex; align-items: center; gap: 8px; text-align: left; background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px 9px; color: var(--text); font: inherit; font-size: 13px; cursor: pointer; }
      #spheres-dialog .sp-side button.on { background: var(--card-2); border-color: var(--line); }
      #spheres-dialog .sp-main { padding: 12px 16px; overflow: auto; display: grid; gap: 12px; align-content: start; }
      #spheres-dialog fieldset { border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px 10px; margin: 0; min-width: 0; }
      #spheres-dialog legend { color: var(--muted); font-size: 12px; padding: 0 6px; }
      #spheres-dialog label.sp-f { display: grid; gap: 3px; font-size: 12.5px; }
      #spheres-dialog label.sp-f > span { color: var(--muted); font-size: 12px; }
      #spheres-dialog .sp-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
      #spheres-dialog textarea, #spheres-dialog input[type=text], #spheres-dialog input[type=time], #spheres-dialog select { width: 100%; box-sizing: border-box; background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font: inherit; font-size: 13px; }
      #spheres-dialog textarea { min-height: 52px; resize: vertical; font-family: var(--mono); font-size: 12px; }
      #spheres-dialog textarea.json { min-height: 340px; width: 100%; }
      #spheres-dialog input[type=color] { width: 44px; height: 30px; padding: 0; border: 1px solid var(--line); border-radius: 6px; background: transparent; }
      #spheres-dialog table.sp-route { border-collapse: collapse; font-size: 12.5px; }
      #spheres-dialog table.sp-route th, #spheres-dialog table.sp-route td { padding: 3px 12px 3px 0; text-align: left; font-weight: 500; }
      #spheres-dialog .sp-checks { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
      #spheres-dialog .sp-checks label { display: inline-flex; gap: 5px; align-items: center; font-size: 12.5px; }
      #spheres-dialog .dialog-head, #spheres-dialog .sp-foot { flex: none; }
      #spheres-dialog .sp-foot { display: flex; gap: 8px; align-items: center; padding: 10px 14px; border-top: 1px solid var(--line); }
      #spheres-dialog .sp-err { color: var(--red); font-size: 12px; flex: 1; }
      #spheres-dialog .sp-hint { color: var(--muted); font-size: 11.5px; }
      @media (max-width: 720px) { #spheres-dialog .sp-wrap { grid-template-columns: 1fr; } #spheres-dialog .sp-side { border-right: 0; border-bottom: 1px solid var(--line); grid-auto-flow: column; overflow-x: auto; } }
    `;
    document.head.appendChild(css);
  }

  // ---- header chip --------------------------------------------------------------------------------------
  function apply(announce) {
    const s = spheres.find((x) => x.id === active) || spheres[0];
    const root = document.documentElement;
    root.dataset.sphere = active;
    if (s) root.style.setProperty("--sphere-color", s.color || "#8a8f98");
    if (announce) ctx.bus.dispatchEvent(new CustomEvent("sphere", { detail: active }));
    renderChip();
  }

  function renderChip() {
    if (!chip) return;
    const s = spheres.find((x) => x.id === active);
    chip.innerHTML = "";
    const dot = el("i", "sphere-dot");
    if (s) dot.style.setProperty("--sphere-color", s.color || "#8a8f98");
    chip.appendChild(dot);
    chip.appendChild(el("b", "", s ? nameOf(s) : t("sphere")));
    chip.appendChild(el("span", "hint", "▾"));
    chip.title = t("switch_to");
    chip.setAttribute("aria-haspopup", "menu");
  }

  function closeMenu() {
    if (menu) { menu.remove(); menu = null; }
    if (chip) chip.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", outside, true);
    document.removeEventListener("keydown", onKey, true);
  }
  function outside(e) { if (menu && !slot.contains(e.target)) closeMenu(); }
  function onKey(e) { if (e.key === "Escape") closeMenu(); }

  function openMenu() {
    closeMenu();
    menu = el("div", "sphere-menu"); menu.setAttribute("role", "menu");
    for (const s of spheres) {
      const b = el("button", s.id === active ? "on" : ""); b.setAttribute("role", "menuitem"); b.dataset.sphere = s.id;
      const dot = el("i", "sphere-dot"); dot.style.setProperty("--sphere-color", s.color || "#8a8f98");
      b.appendChild(dot); b.appendChild(el("span", "", nameOf(s)));
      if (s.id === active) b.appendChild(el("span", "tick", "✓"));
      b.onclick = () => { closeMenu(); switchTo(s.id); };
      menu.appendChild(b);
    }
    menu.appendChild(el("hr"));
    const cfg = el("button", "", t("configure")); cfg.setAttribute("role", "menuitem"); cfg.dataset.action = "configure";
    cfg.onclick = () => { closeMenu(); openDialog(); };
    menu.appendChild(cfg);
    slot.appendChild(menu);
    chip.setAttribute("aria-expanded", "true");
    setTimeout(() => { document.addEventListener("click", outside, true); document.addEventListener("keydown", onKey, true); }, 0);
  }

  async function switchTo(id) {
    if (id === active) return;
    const r = await api("/api/spheres/active", { id });
    if (!r.ok) { toast(r.error || "error", "err"); return; }
    active = r.active || id;
    apply(true);
  }

  async function refresh(announceIfChanged) {
    let r;
    try { r = await api("/api/spheres"); } catch (e) { return; }
    if (!r || !r.ok) return;
    const changed = r.active !== active || firstLoad;
    spheres = r.spheres || [];
    active = r.active || "personal";
    apply(firstLoad || (announceIfChanged && changed));
    firstLoad = false;
  }

  // ---- the dialog ----------------------------------------------------------------------------------------
  let dlg = null, draft = [], sel = 0, jsonMode = false, newIds = new Set(), form = null;

  function daysControl(value) {
    const wrap = el("div", "sp-checks");
    const sel_ = el("select");
    const kinds = ["daily", "weekdays", "weekends", "custom"];
    for (const k of kinds) { const o = el("option", "", t(k)); o.value = k; sel_.appendChild(o); }
    const boxes = el("span", "sp-checks");
    const inputs = {};
    for (const d of DAYS) {
      const lab = el("label"); const cb = el("input"); cb.type = "checkbox"; inputs[d] = cb;
      lab.appendChild(cb); lab.appendChild(document.createTextNode(L(DAY_LABEL[d]))); boxes.appendChild(lab);
    }
    const isList = Array.isArray(value);
    sel_.value = isList ? "custom" : (["daily", "weekdays", "weekends"].includes(value) ? value : "daily");
    if (isList) for (const d of value) if (inputs[d]) inputs[d].checked = true;
    const sync = () => { boxes.hidden = sel_.value !== "custom"; };
    sel_.onchange = sync; sync();
    wrap.appendChild(sel_); wrap.appendChild(boxes);
    return { node: wrap, get: () => sel_.value === "custom" ? DAYS.filter((d) => inputs[d].checked) : sel_.value };
  }

  function checks(options, chosen) {
    const wrap = el("div", "sp-checks"); const inputs = {};
    for (const o of options) {
      const lab = el("label"); const cb = el("input"); cb.type = "checkbox"; cb.checked = chosen.includes(o.id); cb.dataset.id = o.id;
      inputs[o.id] = cb; lab.appendChild(cb); lab.appendChild(document.createTextNode(o.label)); wrap.appendChild(lab);
    }
    return { node: wrap, get: () => options.filter((o) => inputs[o.id].checked).map((o) => o.id) };
  }

  function textField(labelKey, value, hintKey, multiline = true) {
    const lab = el("label", "sp-f"); lab.appendChild(el("span", "", t(labelKey)));
    const inp = multiline ? el("textarea") : el("input"); if (!multiline) inp.type = "text";
    inp.value = multiline ? toLines(value) : (value || ""); lab.appendChild(inp);
    if (hintKey) lab.appendChild(el("span", "sp-hint", t(hintKey)));
    return { node: lab, input: inp };
  }

  function collect() {
    if (!form || !draft[sel]) return;
    const s = draft[sel];
    if (form.id) s.id = form.id.value.trim().toLowerCase();
    s.name = { es: form.name_es.value.trim(), en: form.name_en.value.trim() };
    s.color = form.color.value;
    for (const k of ["mail_accounts", "chat_sources", "vip", "keywords", "mute", "apps"]) s[k] = fromLines(form[k].value);
    s.quiet_hours = { start: form.q_start.value || "", end: form.q_end.value || "", days: form.q_days.get() };
    s.notify = {}; for (const p of PRIOS) s.notify[p] = form.route[p].get();
    s.digest = { enabled: form.d_on.checked, at: form.d_at.value || "08:30", days: form.d_days.get(), summarize: form.d_sum.checked, channels: form.d_ch.get() };
  }

  function renderForm(main) {
    main.innerHTML = "";
    const s = draft[sel];
    if (!s) return;
    form = {};
    const grid1 = el("div", "sp-grid");
    if (newIds.has(s.id) || !s.id) {
      const idf = textField("id", s.id, "id_hint", false); form.id = idf.input; grid1.appendChild(idf.node);
    }
    const nes = textField("name_es", (s.name || {}).es || "", null, false); form.name_es = nes.input; grid1.appendChild(nes.node);
    const nen = textField("name_en", (s.name || {}).en || "", null, false); form.name_en = nen.input; grid1.appendChild(nen.node);
    const col = el("label", "sp-f"); col.appendChild(el("span", "", t("color")));
    form.color = el("input"); form.color.type = "color"; form.color.value = /^#[0-9a-f]{6}$/i.test(s.color || "") ? s.color : "#8a8f98";
    col.appendChild(form.color); grid1.appendChild(col);
    main.appendChild(grid1);

    const grid2 = el("div", "sp-grid");
    for (const [k, hint] of [["mail_accounts", "mail_hint"], ["chat_sources", "chat_hint"], ["vip", "vip_hint"], ["keywords", "keywords_hint"], ["mute", "mute_hint"], ["apps", "apps_hint"]]) {
      const f = textField(k, s[k], hint); form[k] = f.input; grid2.appendChild(f.node);
    }
    main.appendChild(grid2);

    const qf = el("fieldset"); qf.appendChild(el("legend", "", t("quiet")));
    const qrow = el("div", "sp-grid");
    const q = s.quiet_hours || {};
    for (const [key, labelKey, val] of [["q_start", "from", q.start], ["q_end", "to", q.end]]) {
      const lab = el("label", "sp-f"); lab.appendChild(el("span", "", t(labelKey)));
      const inp = el("input"); inp.type = "time"; inp.value = val || ""; form[key] = inp; lab.appendChild(inp); qrow.appendChild(lab);
    }
    const dl = el("label", "sp-f"); dl.appendChild(el("span", "", t("days"))); form.q_days = daysControl(q.days || "daily"); dl.appendChild(form.q_days.node); qrow.appendChild(dl);
    qf.appendChild(qrow); qf.appendChild(el("div", "sp-hint", t("quiet_hint")));
    main.appendChild(qf);

    const rf = el("fieldset"); rf.appendChild(el("legend", "", t("routing")));
    const tbl = el("table", "sp-route"); form.route = {};
    const opts = [...PUSH.map((c) => ({ id: c, label: t(c) })), { id: "digest", label: t("digest_ch") }];
    for (const p of PRIOS) {
      const tr = el("tr"); tr.appendChild(el("th", "", t(p)));
      const td = el("td"); const c = checks(opts, (s.notify || {})[p] || []); form.route[p] = c; td.appendChild(c.node); tr.appendChild(td); tbl.appendChild(tr);
    }
    rf.appendChild(tbl); rf.appendChild(el("div", "sp-hint", t("routing_hint")));
    main.appendChild(rf);

    const df = el("fieldset"); df.appendChild(el("legend", "", t("digest")));
    const d = s.digest || {};
    const drow = el("div", "sp-grid");
    const on = el("label", "sp-f"); form.d_on = el("input"); form.d_on.type = "checkbox"; form.d_on.checked = d.enabled !== false;
    const onrow = el("span", "sp-checks"); onrow.appendChild(form.d_on); onrow.appendChild(document.createTextNode(t("digest_on"))); on.appendChild(onrow); drow.appendChild(on);
    const at = el("label", "sp-f"); at.appendChild(el("span", "", t("digest_at"))); form.d_at = el("input"); form.d_at.type = "time"; form.d_at.value = d.at || "08:30"; at.appendChild(form.d_at); drow.appendChild(at);
    const dd = el("label", "sp-f"); dd.appendChild(el("span", "", t("days"))); form.d_days = daysControl(d.days || "daily"); dd.appendChild(form.d_days.node); drow.appendChild(dd);
    df.appendChild(drow);
    const sm = el("label", "sp-checks"); form.d_sum = el("input"); form.d_sum.type = "checkbox"; form.d_sum.checked = d.summarize !== false;
    sm.appendChild(form.d_sum); sm.appendChild(document.createTextNode(t("digest_sum"))); df.appendChild(sm);
    const dc = el("div", "sp-f"); dc.appendChild(el("span", "sp-hint", t("digest_channels")));
    form.d_ch = checks(PUSH.map((c) => ({ id: c, label: t(c) })), d.channels || []); dc.appendChild(form.d_ch.node); df.appendChild(dc);
    main.appendChild(df);
  }

  function renderDialog() {
    const body = dlg.querySelector(".sp-body"); body.innerHTML = "";
    const err = dlg.querySelector(".sp-err"); err.textContent = "";
    dlg.querySelector(".sp-title").textContent = t("title");
    dlg.querySelector(".sp-json").textContent = jsonMode ? t("form") : t("json");
    dlg.querySelector(".sp-save").textContent = t("save");
    dlg.querySelector(".sp-close").textContent = t("close");
    const rm = dlg.querySelector(".sp-remove"); rm.textContent = t("remove");
    if (jsonMode) {
      rm.hidden = true;
      const ta = el("textarea", "json"); ta.value = JSON.stringify({ spheres: draft }, null, 2); ta.spellcheck = false; ta.id = "sp-json-body";
      const wrap = el("div", "sp-main"); wrap.appendChild(ta); body.appendChild(wrap);
      return;
    }
    const wrap = el("div", "sp-wrap");
    const side = el("div", "sp-side");
    draft.forEach((s, i) => {
      const b = el("button", i === sel ? "on" : ""); b.dataset.index = String(i);
      const dot = el("i", "sphere-dot"); dot.style.setProperty("--sphere-color", s.color || "#8a8f98");
      b.appendChild(dot); b.appendChild(el("span", "", L(s.name || {}) || s.id || t("new_name")));
      b.onclick = () => { collect(); sel = i; renderDialog(); };
      side.appendChild(b);
    });
    const add = el("button", "", t("add")); add.className = "sp-add";
    add.onclick = () => {
      collect();
      let n = 1; const ids = new Set(draft.map((x) => x.id)); while (ids.has("sphere" + n)) n++;
      const id = "sphere" + n;
      draft.push({ id, name: { es: S.new_name.es, en: S.new_name.en }, color: "#8a8f98", mail_accounts: [], chat_sources: [], vip: [], keywords: [], mute: [],
        quiet_hours: { start: "22:30", end: "08:00", days: "daily" }, notify: { urgent: ["windows"], high: ["windows"], normal: ["windows"], low: ["digest"] },
        digest: { enabled: true, at: "08:30", days: "daily", summarize: true, channels: ["windows"] }, apps: [] });
      newIds.add(id); sel = draft.length - 1; renderDialog();
    };
    side.appendChild(add);
    const main = el("div", "sp-main");
    wrap.appendChild(side); wrap.appendChild(main); body.appendChild(wrap);
    renderForm(main);
    rm.hidden = !draft[sel] || draft[sel].id === "personal";
  }

  async function save() {
    const err = dlg.querySelector(".sp-err"); err.textContent = "";
    let payload;
    if (jsonMode) {
      try { payload = JSON.parse(dlg.querySelector("#sp-json-body").value); } catch (e) { err.textContent = t("bad_json") + e.message; return; }
      if (Array.isArray(payload)) payload = { spheres: payload };
    } else { collect(); payload = { spheres: draft }; }
    const r = await api("/api/spheres", payload);
    if (!r.ok) { err.textContent = r.error || "error"; return; }
    const prev = active;
    spheres = r.spheres || spheres; active = r.active || active;
    apply(prev !== active);
    toast(t("saved"), "ok");
    dlg.close();
  }

  async function removeCurrent() {
    const s = draft[sel];
    if (!s || s.id === "personal") return;
    if (!window.confirm(t("confirm_remove"))) return;
    if (!newIds.has(s.id)) {
      const r = await api("/api/spheres/remove", { id: s.id });
      if (!r.ok) { dlg.querySelector(".sp-err").textContent = r.error || "error"; return; }
      const prev = active; spheres = r.spheres || spheres; active = r.active || active; apply(prev !== active);
    }
    newIds.delete(s.id); draft.splice(sel, 1); sel = Math.max(0, sel - 1); renderDialog();
  }

  function openDialog() {
    injectCss();
    if (!dlg) {
      dlg = document.createElement("dialog"); dlg.id = "spheres-dialog";
      dlg.innerHTML = `<div class="dialog-head"><b class="sp-title"></b><button class="ghost small sp-x">✕</button></div><div class="sp-body"></div>
        <div class="sp-foot"><span class="sp-err"></span><button class="ghost small sp-remove danger"></button><button class="ghost small sp-json"></button><button class="ghost small sp-close"></button><button class="primary small sp-save"></button></div>`;
      document.body.appendChild(dlg);
      dlg.querySelector(".sp-x").onclick = () => dlg.close();
      dlg.querySelector(".sp-close").onclick = () => dlg.close();
      dlg.querySelector(".sp-save").onclick = save;
      dlg.querySelector(".sp-remove").onclick = removeCurrent;
      dlg.querySelector(".sp-json").onclick = () => {
        const err = dlg.querySelector(".sp-err");
        if (jsonMode) {
          try { const p = JSON.parse(dlg.querySelector("#sp-json-body").value); draft = Array.isArray(p) ? p : (p.spheres || draft); } catch (e) { err.textContent = t("bad_json") + e.message; return; }
          sel = Math.min(sel, draft.length - 1); jsonMode = false;
        } else { collect(); jsonMode = true; }
        renderDialog();
      };
    }
    draft = clone(spheres); sel = Math.max(0, draft.findIndex((s) => s.id === active)); jsonMode = false; newIds = new Set();
    renderDialog();
    if (!dlg.open) dlg.showModal();
  }

  window.HubSpheres = { openDialog, active: () => active, list: () => clone(spheres), refresh };

  H.register("spheres", {
    placement: "header",
    label: S.sphere,
    order: 10,
    mount(container) {
      injectCss();
      slot = container;
      chip = el("button", "chip btn sphere-chip"); chip.id = "sphere-chip"; chip.setAttribute("aria-expanded", "false");
      chip.onclick = () => { if (menu) closeMenu(); else openMenu(); };
      container.appendChild(chip);
      renderChip();
    },
    load() { return refresh(true); },
    tick() { return refresh(true); },
  });
})();

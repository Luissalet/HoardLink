/* Hoard Hub UI — "Cerebro" / "Brain": the rules that write the family's events into Faustus's Brain.
   Rules (on/off, try with a sample event, see what it would write), the queue and the history with a link to each note.
   Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const { el, api, toast, L, fmtWhen } = H.ctx;

  const S = {
    label: { es: "Cerebro", en: "Brain" },
    hint: { es: "Lo que pasa en las apps queda en el Cerebro de Faustus: cada regla convierte un evento en una entidad o una nota y la enlaza.", en: "What happens in the apps lands in Faustus's Brain: each rule turns an event into an entity or a note and links it." },
    target: { es: "Destino", en: "Target" },
    t_fixed: { es: "dirección fija", en: "fixed address" }, t_placement: { es: "según la colocación", en: "from the placement" }, t_pc: { es: "este PC", en: "this PC" },
    moving: { es: "Faustus se está trasladando; se escribirá al terminar", en: "Faustus is moving; writes resume afterwards" },
    up: { es: "responde", en: "answers" }, down: { es: "no responde", en: "does not answer" },
    no_entities: { es: "sin ruta para crear entidades: solo notas por ahora", en: "no route to create entities: notes only for now" },
    queue: { es: "En espera", en: "Waiting" }, failed: { es: "fallidas", en: "failed" }, partial: { es: "sin entidad aún", en: "entity pending" }, written: { es: "escritas", en: "written" },
    retry: { es: "Reintentar ahora", en: "Retry now" }, check: { es: "Comprobar", en: "Check" }, change: { es: "Cambiar destino", en: "Change target" },
    ask_url: { es: "Dirección de Faustus (vacío = seguir la colocación PC/Spark):", en: "Faustus address (empty = follow the PC/Spark placement):" },
    rules: { es: "Reglas", en: "Rules" }, history: { es: "Lo escrito", en: "Written" },
    when: { es: "Cuando llega", en: "When" }, writes: { es: "Escribe", en: "Writes" },
    on: { es: "Activar", en: "Enable" }, off: { es: "Desactivar", en: "Disable" }, install: { es: "Instalar y activar", en: "Install and enable" },
    try: { es: "Probar", en: "Try" }, close: { es: "Cerrar", en: "Close" },
    is_on: { es: "activa", en: "on" }, is_off: { es: "desactivada", en: "off" }, not_installed: { es: "sin instalar", en: "not installed" },
    ev_type: { es: "Tipo de evento", en: "Event type" }, ev_data: { es: "Datos del evento (JSON)", en: "Event data (JSON)" },
    preview: { es: "Ver qué escribiría", en: "See what it would write" }, last_real: { es: "Usar el último evento real", en: "Use the last real event" },
    write: { es: "Escribir de verdad", en: "Write it" }, no_real: { es: "Aún no ha llegado ningún evento de ese tipo.", en: "No event of that type has arrived yet." },
    bad_json: { es: "Los datos no son JSON válido.", en: "The data is not valid JSON." },
    no_match: { es: "Ojo: este evento no encaja con el «cuando» de la regla; en real no se dispararía.", en: "Note: this event does not match the rule's «when»; it would not fire for real." },
    e_create: { es: "Crearía", en: "Would create" }, e_update: { es: "Actualizaría", en: "Would update" }, e_unchanged: { es: "No cambiaría nada (ya está escrito)", en: "Would change nothing (already written)" },
    skipped: { es: "No escribiría nada", en: "Would write nothing" },
    entity: { es: "Entidad", en: "Entity" }, note: { es: "Nota", en: "Note" }, links: { es: "Enlaza con", en: "Links to" },
    open: { es: "Abrir nota", en: "Open note" }, none: { es: "Todavía no se ha escrito nada.", en: "Nothing written yet." },
    st_done: { es: "escrito", en: "written" }, st_pending: { es: "en espera", en: "waiting" }, st_partial: { es: "nota escrita, entidad pendiente", en: "note written, entity pending" }, st_failed: { es: "falló", en: "failed" },
    wrote: { es: "Escrito", en: "Written" }, queued: { es: "En cola: se escribirá cuando Faustus responda", en: "Queued: it will be written when Faustus answers" },
    unchanged: { es: "Ya estaba escrito: nada que cambiar", en: "Already written: nothing to change" },
    times: { es: "escrituras", en: "writes" }, never: { es: "aún nada", en: "nothing yet" },
    mode_append: { es: "añade una línea por evento", en: "adds a line per event" }, mode_replace: { es: "mantiene el último texto", en: "keeps the latest text" },
  };
  const t = (k) => L(S[k]);
  const TYPES = { person: { es: "persona", en: "person" }, organization: { es: "organización", en: "organization" }, project: { es: "proyecto", en: "project" }, place: { es: "lugar", en: "place" }, tool: { es: "herramienta", en: "tool" }, concept: { es: "concepto", en: "concept" }, event: { es: "evento", en: "event" }, other: { es: "otro", en: "other" } };
  const typeName = (x) => L(TYPES[x] || TYPES.other);
  let box, head, rulesBox, histBox, state = null, openTest = null;

  function injectCss() {
    if (document.getElementById("css-brain")) return;
    const css = document.createElement("style");
    css.id = "css-brain";
    css.textContent = `
      .bb-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
      .bb-chip { padding: 3px 9px; border: 1px solid var(--line); border-radius: 999px; font-size: 12.5px; background: var(--card-2); }
      .bb-chip.ok { border-color: var(--green, #3fb27f); } .bb-chip.bad { border-color: var(--red, #d9534f); }
      .bb-chip code { font-family: var(--mono); font-size: 11.5px; }
      .bb-title { margin: 14px 0 6px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .4px; }
      .bb-rule { border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); padding: 9px 11px; margin-bottom: 7px; }
      .bb-rule.off { opacity: .72; }
      .bb-rule-top { display: flex; gap: 10px; align-items: flex-start; }
      .bb-rule-top > div:first-child { flex: 1; min-width: 0; }
      .bb-name { font-weight: 600; } .bb-state { font-size: 11.5px; color: var(--muted); margin-left: 8px; font-weight: 400; }
      .bb-line { font-size: 12.5px; color: var(--muted); margin-top: 2px; overflow-wrap: anywhere; }
      .bb-line code { font-family: var(--mono); font-size: 11.5px; color: var(--text); }
      .bb-actions { display: flex; gap: 6px; flex-wrap: wrap; }
      .bb-test { margin-top: 9px; border-top: 1px solid var(--line); padding-top: 9px; display: grid; gap: 7px; }
      .bb-test label { display: grid; gap: 3px; font-size: 12px; color: var(--muted); }
      .bb-test input, .bb-test textarea { font-family: var(--mono); font-size: 12px; width: 100%; box-sizing: border-box; }
      .bb-out { display: grid; gap: 7px; }
      .bb-plan { border: 1px dashed var(--line); border-radius: 8px; padding: 7px 9px; font-size: 12.5px; display: grid; gap: 4px; }
      .bb-plan pre { margin: 2px 0 0; white-space: pre-wrap; font-family: var(--mono); font-size: 11.5px; background: var(--hoard-sunken, transparent); padding: 6px 8px; border-radius: 6px; }
      .bb-effect { font-weight: 600; } .bb-warn { color: var(--amber, #c98a1b); font-size: 12.5px; }
      .bb-hist { display: grid; gap: 2px; font-size: 12.5px; }
      .bb-row { display: flex; gap: 9px; align-items: baseline; flex-wrap: wrap; padding: 4px 0; border-top: 1px solid var(--line); }
      .bb-row .bb-when { color: var(--muted); font-size: 11.5px; min-width: 96px; }
      .bb-row .bb-what { flex: 1; min-width: 180px; overflow-wrap: anywhere; }
      .bb-row .bb-st { font-size: 11.5px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); }
      .bb-row .bb-st.done { border-color: var(--green, #3fb27f); } .bb-row .bb-st.failed { border-color: var(--red, #d9534f); }
      .bb-row a { color: var(--blue); text-decoration: none; font-size: 12px; }
      .bb-err { color: var(--red, #d9534f); font-size: 11.5px; flex-basis: 100%; }
    `;
    document.head.appendChild(css);
  }

  const btn = (label, cls, fn) => { const b = el("button", cls, label); b.type = "button"; b.onclick = fn; return b; };

  function describe(a) {
    const parts = [];
    if (a.entity) parts.push(`${t("entity").toLowerCase()} ${typeName(a.entity.type)} ${a.entity.name}`);
    if (a.note) parts.push(`${t("note").toLowerCase()} ${a.note.folder || "Familia"}/${a.note.title} (${t(a.note.mode === "append" ? "mode_append" : "mode_replace")})`);
    const links = (a.links || []).map((l) => `${l.to} (${typeName(l.type)})`);
    if (links.length) parts.push(`${t("links").toLowerCase()} ${links.join(", ")}`);
    // ${event.data.merchant|event.data.shop} reads as ‹merchant›: the field of the event that fills that spot
    return parts.join(" · ").replace(/\$\{([^}]+)\}/g, (m, e) => `‹${e.split("|")[0].trim().replace(/^event\.data\./, "").replace(/^item(\.name)?$/, "…")}›`);
  }

  function renderHead() {
    head.innerHTML = "";
    const tg = state.target || {};
    const chip = el("span", "bb-chip" + (tg.reachable === true ? " ok" : tg.reachable === false ? " bad" : ""));
    chip.id = "bb-target";
    if (tg.moving) chip.textContent = `${t("target")}: ${t("moving")}`;
    else {
      chip.appendChild(document.createTextNode(`${t("target")}: `));
      chip.appendChild(el("code", "", tg.base || "?"));
      const how = tg.source === "fixed" ? t("t_fixed") : tg.source === "placement" ? `${t("t_placement")}${tg.node ? " · " + tg.node : ""}` : t("t_pc");
      chip.appendChild(document.createTextNode(` · ${how}` + (tg.reachable === true ? ` · ${t("up")}` : tg.reachable === false ? ` · ${t("down")}` : "")));
    }
    head.appendChild(chip);
    if (tg.reachable && tg.entities_api === false) head.appendChild(el("span", "bb-chip bad", t("no_entities")));
    const c = state.counts || {};
    const q = el("span", "bb-chip" + (c.failed ? " bad" : ""), `${t("queue")}: ${c.pending || 0} · ${t("failed")}: ${c.failed || 0} · ${t("partial")}: ${c.partial || 0} · ${t("written")}: ${c.done || 0}`);
    q.id = "bb-queue"; head.appendChild(q);
    head.appendChild(btn(t("retry"), "small", async () => { const r = await api("/api/brain/retry", {}); toast(r.ok ? `${t("retry")}: ${r.requeued}` : (r.error || "error"), r.ok ? "ok" : "err"); setTimeout(() => load(true), 1200); }));
    head.appendChild(btn(t("check"), "small ghost", () => load(true)));
    head.appendChild(btn(t("change"), "small ghost", async () => {
      const url = window.prompt(t("ask_url"), state.fixed_url || "");
      if (url === null) return;
      const r = await api("/api/brain/target", { url: url.trim() });
      if (!r.ok) toast(r.error || "error", "err");
      load(true);
    }));
    if (state.last_error) head.appendChild(el("div", "bb-err", state.last_error));
  }

  function renderRules() {
    rulesBox.innerHTML = "";
    rulesBox.appendChild(el("div", "bb-title", t("rules")));
    for (const r of state.rules || []) {
      const card = el("div", "bb-rule" + (r.enabled ? "" : " off")); card.dataset.rule = r.id;
      const top = el("div", "bb-rule-top"); const left = el("div");
      const name = el("div", "bb-name", r.name);
      name.appendChild(el("span", "bb-state", !r.installed ? t("not_installed") : r.enabled ? t("is_on") : t("is_off")));
      left.appendChild(name);
      const w = el("div", "bb-line", `${t("when")} `); w.appendChild(el("code", "", (r.when && r.when.type) || "*")); left.appendChild(w);
      for (const a of r.then || []) left.appendChild(el("div", "bb-line", `${t("writes")}: ${describe(a)}`));
      if (r.note) left.appendChild(el("div", "bb-line", r.note));
      left.appendChild(el("div", "bb-line", r.writes ? `${r.writes} ${t("times")} · ${fmtWhen(r.last_ts)}${r.pending ? ` · ${t("st_pending")}: ${r.pending}` : ""}${r.failed ? ` · ${t("failed")}: ${r.failed}` : ""}` : t("never")));
      top.appendChild(left);
      const acts = el("div", "bb-actions");
      const toggle = btn(!r.installed ? t("install") : r.enabled ? t("off") : t("on"), "small" + (r.enabled ? " ghost" : ""), async () => {
        const res = await api(`/api/brain/rules/${encodeURIComponent(r.id)}`, { enabled: !r.enabled });
        if (!res.ok) toast(res.error || "error", "err");
        load();
      });
      toggle.dataset.act = "toggle"; acts.appendChild(toggle);
      const tryB = btn(openTest === r.id ? t("close") : t("try"), "small ghost", () => { openTest = openTest === r.id ? null : r.id; renderRules(); });
      tryB.dataset.act = "try"; acts.appendChild(tryB);
      top.appendChild(acts); card.appendChild(top);
      if (openTest === r.id) card.appendChild(testPanel(r));
      rulesBox.appendChild(card);
    }
  }

  function testPanel(r) {
    const p = el("div", "bb-test");
    const sample = r.sample || { type: ((r.when && r.when.type) || "manual.test").split("|")[0], data: {} };
    const lt = el("label", "", t("ev_type")); const type = el("input"); type.value = sample.type || ""; type.name = "event_type"; lt.appendChild(type);
    const ld = el("label", "", t("ev_data")); const data = el("textarea"); data.rows = 7; data.spellcheck = false; data.name = "event_data"; data.value = JSON.stringify(sample.data || {}, null, 2); ld.appendChild(data);
    const out = el("div", "bb-out");
    const read = () => { try { return { type: type.value.trim(), data: JSON.parse(data.value || "{}") }; } catch (e) { toast(t("bad_json"), "err"); return null; } };
    const run = async (write) => {
      const ev = read(); if (!ev) return;
      const res = await api("/api/brain/test", { rule_id: r.id, event: ev, write });
      out.innerHTML = "";
      if (!res.ok) { out.appendChild(el("div", "bb-err", res.error || "error")); return; }
      if (!res.matches) out.appendChild(el("div", "bb-warn", t("no_match")));
      for (const w of res.writes || []) out.appendChild(planView(w, write));
      if (write) setTimeout(() => load(), 300);
    };
    const row = el("div", "bb-actions");
    const pv = btn(t("preview"), "small primary", () => run(false)); pv.dataset.act = "preview"; row.appendChild(pv);
    row.appendChild(btn(t("last_real"), "small ghost", async () => {
      const res = await api(`/api/events?type=${encodeURIComponent((r.when && r.when.type) || "*")}&limit=1`);
      const ev = res.ok && (res.events || [])[0];
      if (!ev) { toast(t("no_real"), "info"); return; }
      type.value = ev.type; data.value = JSON.stringify(ev.data || {}, null, 2);
    }));
    const wr = btn(t("write"), "small", () => run(true)); wr.dataset.act = "write"; row.appendChild(wr);
    for (const x of [lt, ld, row, out]) p.appendChild(x);
    return p;
  }

  function planView(w, wrote) {
    const v = el("div", "bb-plan");
    if (w.skipped) { v.appendChild(el("div", "bb-effect", `${t("skipped")}: ${w.reason || ""}`)); return v; }
    v.appendChild(el("div", "bb-effect", t(w.effect === "create" ? "e_create" : w.effect === "update" ? "e_update" : "e_unchanged")));
    if (w.entity) v.appendChild(el("div", "", `${t("entity")}: ${w.entity.name} (${typeName(w.entity.type)})${w.entity.summary ? " — " + w.entity.summary : ""}`));
    if (w.links && w.links.length) v.appendChild(el("div", "", `${t("links")}: ${w.links.map((l) => `${l.to} (${typeName(l.type)})`).join(", ")}`));
    if (w.note) { v.appendChild(el("div", "", `${t("note")}: ${w.note.path}${w.note.date ? " · " + w.note.date : ""}`)); v.appendChild(el("pre", "", w.note.text)); }
    if (wrote && w.written) {
      const x = w.written;
      const line = el("div", "bb-effect", x.unchanged ? t("unchanged") : x.status === "done" ? t("wrote") : x.status === "partial" ? `${t("wrote")} · ${t("st_partial")}` : x.status === "failed" ? `${t("st_failed")}: ${x.error || ""}` : t("queued"));
      if (x.url) { const a = el("a", "", ` ${t("open")}`); a.href = x.url; a.target = "_blank"; a.rel = "noopener"; line.appendChild(a); }
      v.appendChild(line);
    }
    return v;
  }

  async function renderHistory() {
    const r = await api("/api/brain/history?limit=40");
    histBox.innerHTML = "";
    histBox.appendChild(el("div", "bb-title", t("history")));
    const rows = (r.ok && r.writes) || [];
    if (!rows.length) { histBox.appendChild(el("div", "hint", t("none"))); return; }
    const list = el("div", "bb-hist");
    for (const w of rows) {
      const row = el("div", "bb-row"); row.dataset.write = w.id;
      row.appendChild(el("span", "bb-when", fmtWhen(w.done_ts || w.ts)));
      row.appendChild(el("span", "bb-what", `${w.rule_name || w.rule || "—"} ← ${w.event_type || "?"} · ${w.summary}`));
      row.appendChild(el("span", `bb-st ${w.status}`, t("st_" + w.status)));
      if (w.url) { const a = el("a", "", t("open")); a.href = w.url; a.target = "_blank"; a.rel = "noopener"; row.appendChild(a); }
      if (w.status === "failed" || w.status === "pending") row.appendChild(btn(t("retry"), "small ghost", async () => { await api("/api/brain/retry", { id: w.id }); setTimeout(() => load(), 1200); }));
      if (w.error) row.appendChild(el("div", "bb-err", w.error));
      list.appendChild(row);
    }
    histBox.appendChild(list);
  }

  async function load(probe) {
    const r = await api(`/api/brain${probe ? "?probe=1" : ""}`);
    if (!r.ok) return;
    if (!probe && state && state.target && r.target && state.target.base === r.target.base) { r.target.reachable = state.target.reachable; r.target.entities_api = state.target.entities_api; }
    state = r;
    renderHead();
    if (!openTest || !rulesBox.querySelector(".bb-test")) renderRules();
    renderHistory();
    const badge = document.getElementById("brain-badge");
    if (badge) { const n = (r.counts.pending || 0) + (r.counts.failed || 0); badge.textContent = n ? String(n) : ""; badge.className = "badge" + (r.counts.failed ? " bad" : ""); }
  }

  H.register("brain", {
    placement: "tab",
    label: S.label,
    order: 36,
    mount(container) {
      injectCss();
      box = container;
      box.appendChild(el("div", "hint", t("hint")));
      head = el("div", "bb-head"); box.appendChild(head);
      rulesBox = el("div", "bb-rules"); box.appendChild(rulesBox);
      histBox = el("div", "bb-history"); box.appendChild(histBox);
    },
    load() { openTest = openTest || null; load(true); },
    tick() { load(false); },
  });
})();

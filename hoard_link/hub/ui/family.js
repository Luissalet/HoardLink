/* Hoard Hub UI — the family panel: events (live), rules, jobs, backups, audit.
   Plain JS on the same JSON API as app.js. Loaded after app.js. */
(() => {
  "use strict";

  const I18N = {
    en: {
      f_events: "Events", f_rules: "Rules", f_jobs: "Jobs", f_backups: "Backups", f_audit: "Audit",
      f_emit_test: "Emit test event", f_new_rule: "New rule", f_new_job: "New job", f_templates: "Templates",
      f_rules_hint: "When an event matches, run actions. Templates below.",
      f_jobs_hint: "Actions on a clock: every 6h, or daily at 04:00.",
      f_backup_now: "Back up now", f_verify: "Verify latest", f_prune: "Prune (keep 14)", f_audit_run: "Run audit",
      f_save: "Save", edit: "Edit", run: "Run", remove: "Remove", enable: "Enable", disable: "Disable", use: "Use", install_defaults: "Install the recommended rules",
      restore: "Restore", verify: "Verify", detail: "Detail",
      no_events: "No events yet. Apps post to /api/events; the hub emits hub.* itself.",
      no_rules: "No rules. A rule runs actions when an event matches.", no_jobs: "No jobs. A job runs actions on a clock.",
      no_backups: "No snapshots yet.", live_off: "● disconnected", live_on: "● live",
      edit_rule_hint: "JSON. when: {type: glob, source?, where?}. then: [{kind: tool, app, tool, args} | {kind: hub, tool, args} | {kind: event, type, data} | {kind: start_app|stop_app|restart_app, app} | {kind: profile_start|profile_stop, name}]. ${event.data.x}, ${today}, ${now} in strings.",
      edit_job_hint: "JSON. every: '30m'|'6h'|'1d' or at: 'HH:MM' (+ days: [mon..sun|weekdays]). then: the same action list as rules.",
      confirm_remove: "Remove?", confirm_restore: (s, a) => `Restore ${a} from ${s} to a side folder next to its data?`,
      backup_summary: (s) => `${s.snapshots} snapshots · ${fmtBytes(s.store_bytes)} in store · ${s.objects} files${s.last ? " · last " + fmtTs(s.last.ts) : ""}`,
      backup_running: "backing up…", backup_done: (r) => `snapshot ${r.snapshot}: ${r.totals.files} files, ${fmtBytes(r.totals.new_bytes)} new`,
      restored: (r) => `restored ${r.files} files to ${r.dest}`, verified: (r) => r.ok ? `verified ${r.objects_checked} files` : `PROBLEM: ${r.corrupt.length} corrupt, ${r.missing.length} missing`,
      pruned: (r) => `dropped ${r.dropped_snapshots.length} snapshots, freed ${fmtBytes(r.bytes_freed)}`,
      audit_summary: (s) => `${s.apps} apps · ${s.running} running · shared contract ${s.shared_contract.length} · per-tool ${s.per_tool_contract.length} · no events ${s.no_events.length} · ${fmtBytes(s.data_bytes)} of data`,
      col: { app: "app", state: "state", contract: "contract", token: "token", events: "events", lib: "hoard_link", data: "data", git: ".gitignore", tools: "tools" },
      next: "next", last: "last", never: "never", at: "at", every: "every", ran: "ran", skipped: "skipped",
    },
    es: {
      f_events: "Eventos", f_rules: "Reglas", f_jobs: "Tareas", f_backups: "Copias", f_audit: "Auditoría",
      f_emit_test: "Emitir evento de prueba", f_new_rule: "Nueva regla", f_new_job: "Nueva tarea", f_templates: "Plantillas",
      f_rules_hint: "Cuando llega un evento que encaja, se ejecutan acciones. Plantillas abajo.",
      f_jobs_hint: "Acciones con reloj: cada 6h, o a diario a las 04:00.",
      f_backup_now: "Copiar ahora", f_verify: "Verificar la última", f_prune: "Limpiar (conservar 14)", f_audit_run: "Auditar",
      f_save: "Guardar", edit: "Editar", run: "Ejecutar", remove: "Quitar", enable: "Activar", disable: "Desactivar", use: "Usar", install_defaults: "Instalar las reglas recomendadas",
      restore: "Restaurar", verify: "Verificar", detail: "Detalle",
      no_events: "Aún no hay eventos. Las apps hacen POST a /api/events; el hub emite los hub.* por su cuenta.",
      no_rules: "Sin reglas. Una regla ejecuta acciones cuando un evento encaja.", no_jobs: "Sin tareas. Una tarea ejecuta acciones con reloj.",
      no_backups: "Aún no hay copias.", live_off: "● desconectado", live_on: "● en vivo",
      edit_rule_hint: "JSON. when: {type: patrón, source?, where?}. then: [{kind: tool, app, tool, args} | {kind: hub, tool, args} | {kind: event, type, data} | {kind: start_app|stop_app|restart_app, app} | {kind: profile_start|profile_stop, name}]. ${event.data.x}, ${today}, ${now} dentro de cadenas.",
      edit_job_hint: "JSON. every: '30m'|'6h'|'1d' o at: 'HH:MM' (+ days: [mon..sun|weekdays]). then: la misma lista de acciones que las reglas.",
      confirm_remove: "¿Quitar?", confirm_restore: (s, a) => `¿Restaurar ${a} desde ${s} en una carpeta aparte junto a sus datos?`,
      backup_summary: (s) => `${s.snapshots} copias · ${fmtBytes(s.store_bytes)} en el almacén · ${s.objects} ficheros${s.last ? " · última " + fmtTs(s.last.ts) : ""}`,
      backup_running: "copiando…", backup_done: (r) => `copia ${r.snapshot}: ${r.totals.files} ficheros, ${fmtBytes(r.totals.new_bytes)} nuevos`,
      restored: (r) => `restaurados ${r.files} ficheros en ${r.dest}`, verified: (r) => r.ok ? `verificados ${r.objects_checked} ficheros` : `PROBLEMA: ${r.corrupt.length} corruptos, ${r.missing.length} ausentes`,
      pruned: (r) => `borradas ${r.dropped_snapshots.length} copias, liberados ${fmtBytes(r.bytes_freed)}`,
      audit_summary: (s) => `${s.apps} apps · ${s.running} en marcha · contrato compartido ${s.shared_contract.length} · por herramienta ${s.per_tool_contract.length} · sin eventos ${s.no_events.length} · ${fmtBytes(s.data_bytes)} de datos`,
      col: { app: "app", state: "estado", contract: "contrato", token: "token", events: "eventos", lib: "hoard_link", data: "datos", git: ".gitignore", tools: "tools" },
      next: "próxima", last: "última", never: "nunca", at: "a las", every: "cada", ran: "ejecutada", skipped: "saltada",
    },
  };
  const langOf = () => { try { return localStorage.getItem("hub.lang") || ((navigator.language || "en").toLowerCase().startsWith("es") ? "es" : "en"); } catch (e) { return "en"; } };
  const t = (k, ...a) => { const L = I18N[langOf()] || I18N.en; const v = L[k] ?? I18N.en[k] ?? k; return typeof v === "function" ? v(...a) : v; };
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  function fmtBytes(n) { n = Number(n || 0); if (n < 1024) return `${n} B`; if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`; if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`; return `${(n / 1073741824).toFixed(2)} GB`; }
  function fmtTs(ts) { if (!ts) return t("never"); const d = new Date(ts * 1000); return d.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  function fmtTime(ts) { const d = new Date(ts * 1000); return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }

  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let data = null;
    try { data = await res.json(); } catch (e) { data = { ok: false, error: `HTTP ${res.status}` }; }
    if (data && data.ok === undefined) data.ok = res.ok;
    return data;
  }
  function toast(msg, kind = "info", detail = "") {
    const box = $("#toasts"); if (!box) return;
    const e = el("div", `toast ${kind}`, msg);
    if (detail) e.appendChild(el("small", "", detail));
    box.appendChild(e); setTimeout(() => e.remove(), kind === "err" ? 9000 : 4500);
  }
  function localize() { $("#family-panel").querySelectorAll("[data-i18n]").forEach((e) => { e.textContent = t(e.dataset.i18n); }); $("#edit-dialog").querySelectorAll("[data-i18n]").forEach((e) => { e.textContent = t(e.dataset.i18n); }); }

  // ---- tabs ------------------------------------------------------------------------------
  let tab = (() => { try { return localStorage.getItem("hub.ftab") || "events"; } catch (e) { return "events"; } })();
  const loaders = { events: loadEvents, rules: loadRules, jobs: loadJobs, backups: loadBackups, audit: () => {} };
  function showTab(name) {
    tab = name; try { localStorage.setItem("hub.ftab", name); } catch (e) { /* ignore */ }
    document.querySelectorAll("#family-tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    document.querySelectorAll(".tab-body").forEach((b) => { b.hidden = b.id !== `tab-${name}`; });
    (loaders[name] || (() => {}))();
  }
  document.querySelectorAll("#family-tabs .tab").forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });
  $("#family-toggle").onclick = () => {
    const body = $("#family-body"); const open = body.hidden; body.hidden = !open;
    $("#family-toggle").textContent = open ? "▾" : "▸"; $("#family-toggle").setAttribute("aria-expanded", String(open));
    try { localStorage.setItem("hub.fopen", open ? "1" : "0"); } catch (e) { /* ignore */ }
  };
  try { if (localStorage.getItem("hub.fopen") === "0") { $("#family-body").hidden = true; $("#family-toggle").textContent = "▸"; } } catch (e) { /* ignore */ }

  // ---- events ----------------------------------------------------------------------------------
  let events = [], lastId = 0;
  const MAX_ROWS = 300;
  function eventRow(ev) {
    const bad = ev.data && (ev.data.ok === false || ev.data.error);
    const frag = document.createDocumentFragment();
    frag.appendChild(el("span", "ev-ts", fmtTime(ev.ts)));
    frag.appendChild(el("span", "ev-type", ev.type));
    frag.appendChild(el("span", "ev-src", ev.source));
    const d = el("span", "ev-data" + (bad ? " bad" : ""), JSON.stringify(ev.data || {}));
    d.title = JSON.stringify(ev.data || {}, null, 1);
    frag.appendChild(d);
    return frag;
  }
  function renderEvents() {
    const list = $("#events-list"); list.innerHTML = "";
    const typ = $("#events-filter").value.trim(), src = $("#events-source").value.trim();
    const glob = typ ? new RegExp("^(" + typ.split("|").map((p) => p.trim().replace(/[.+^${}()[\]\\]/g, "\\$&").replace(/\*/g, ".*")).join("|") + ")$") : null;
    const rows = events.filter((e) => (!glob || glob.test(e.type)) && (!src || e.source === src));
    if (!rows.length) { list.appendChild(el("div", "hint", t("no_events"))); }
    for (const ev of rows) list.appendChild(eventRow(ev));
    $("#events-badge").textContent = lastId ? String(lastId) : "";
  }
  async function loadEvents() {
    const r = await api("/api/events?limit=150");
    if (!r.ok) return;
    events = r.events || []; lastId = r.last_id || 0;
    renderEvents(); connectSse();
  }
  // Live tail: the SSE stream names each event by its type, which EventSource
  // cannot subscribe to generically, so the page polls /api/events?since_id
  // every 2.5 s instead (cheap: an indexed id range). Other consumers
  // (Cassandra) use the stream.
  function connectSse() {
    if (window.__hubEventPoll) return;
    window.__hubEventPoll = setInterval(async () => {
      if (document.hidden) return;
      let r;
      try { r = await api(`/api/events?since_id=${lastId}&limit=200&order=asc`); } catch (e) { r = { ok: false }; }
      $("#events-live").textContent = r.ok ? t("live_on") : t("live_off");
      $("#events-live").classList.toggle("off", !r.ok);
      if (!r.ok || !r.events || !r.events.length) return;
      for (const ev of r.events) { events.unshift(ev); lastId = Math.max(lastId, ev.id); }
      events = events.slice(0, MAX_ROWS);
      if (tab === "events") renderEvents(); else $("#events-badge").textContent = String(lastId);
      if (r.events.some((e) => e.type.startsWith("hub.rule.") || e.type.startsWith("hub.job."))) { if (tab === "rules") loadRules(); if (tab === "jobs") loadJobs(); }
      if (r.events.some((e) => e.type.startsWith("hub.backup."))) { if (tab === "backups") loadBackups(); }
    }, 2500);
  }
  $("#events-filter").addEventListener("input", renderEvents);
  $("#events-source").addEventListener("input", renderEvents);
  $("#events-emit").onclick = async () => { const r = await api("/api/events", { type: "ui.test", data: { at: new Date().toISOString() } }); toast(r.ok ? `#${r.event.id} ui.test` : r.error, r.ok ? "ok" : "err"); };

  // ---- generic JSON editor dialog --------------------------------------------------------------------
  function editJson(title, hint, value, onSave) {
    const dlg = $("#edit-dialog");
    $("#edit-title").textContent = title; $("#edit-hint").textContent = hint; $("#edit-error").textContent = "";
    $("#edit-body").value = JSON.stringify(value, null, 2);
    $("#edit-save").onclick = async () => {
      let parsed;
      try { parsed = JSON.parse($("#edit-body").value); } catch (e) { $("#edit-error").textContent = String(e.message); return; }
      const r = await onSave(parsed);
      if (r && r.ok === false) { $("#edit-error").textContent = r.error || "error"; return; }
      dlg.close();
    };
    $("#edit-close").onclick = () => dlg.close();
    dlg.showModal();
  }
  function describeAction(a) {
    if (a.kind === "tool") return `${a.app}.${a.tool}${a.args && Object.keys(a.args).length ? "(" + Object.keys(a.args).join(", ") + ")" : ""}`;
    if (a.kind === "hub") return `hub.${a.tool}`;
    if (a.kind === "event") return `emit ${a.type}`;
    if (a.kind && a.kind.endsWith("_app")) return `${a.kind} ${a.app}`;
    if (a.kind && a.kind.startsWith("profile_")) return `${a.kind} ${a.name}`;
    return a.kind || "?";
  }
  function stripRuntime(r) { const c = { ...r }; for (const k of ["runs", "last", "created_ts", "skipped", "last_run_ts", "next_run_ts", "running", "id"]) delete c[k]; return c; }

  // ---- rules ---------------------------------------------------------------------------------------
  function renderList(kind, items, examples, history) {
    const list = $(`#${kind}-list`); list.innerHTML = "";
    if (!items.length) list.appendChild(el("div", "hint", t(kind === "rules" ? "no_rules" : "no_jobs")));
    for (const r of items) {
      const card = el("div", "rule" + (r.enabled === false ? " off" : ""));
      const left = el("div");
      left.appendChild(el("div", "r-name", `${r.name || r.id}  `)).appendChild(el("span", "hint", r.id));
      if (kind === "rules") left.appendChild(el("div", "r-when", `when ${JSON.stringify(r.when)}`));
      else left.appendChild(el("div", "r-when", (r.every ? `${t("every")} ${r.every}` : `${t("at")} ${r.at}${r.days ? " " + JSON.stringify(r.days) : ""}`) + (r.next_run_ts ? ` · ${t("next")} ${fmtTs(r.next_run_ts)}` : "")));
      left.appendChild(el("div", "r-then", "→ " + (r.then || []).map(describeAction).join(" ; ")));
      const last = r.last;
      left.appendChild(el("div", "r-last " + (last ? (last.ok ? "ok" : "bad") : ""), last ? `${t("last")} ${fmtTs(last.ts)} · ${last.ok ? "ok" : (last.error || "failed")} · ${last.ms} ms · ${t("ran")} ${r.runs}${r.skipped ? ` · ${t("skipped")} ${r.skipped}` : ""}` : `${t("last")}: ${t("never")}`));
      if (r.note) left.appendChild(el("div", "hint", r.note));
      card.appendChild(left);
      const acts = el("div", "r-actions");
      const b = (label, cls, fn) => { const x = el("button", cls, label); x.onclick = fn; acts.appendChild(x); };
      b(t("run"), "small", async () => {
        const res = await api(`/api/${kind}/${encodeURIComponent(r.id)}/run`, kind === "rules" ? { type: (r.when && r.when.type && !r.when.type.includes("*")) ? r.when.type : "manual.test", data: {} } : {});
        toast(res.ok ? `${r.name}: ok · ${res.ms} ms` : `${r.name}: ${res.error || (res.results || []).filter((x) => !x.ok).map((x) => x.error).join("; ")}`, res.ok ? "ok" : "err");
        loaders[kind]();
      });
      b(t("edit"), "small ghost", () => editJson(`${r.name || r.id}`, t(kind === "rules" ? "edit_rule_hint" : "edit_job_hint"), stripRuntime(r), async (v) => { const res = await api(`/api/${kind}/${encodeURIComponent(r.id)}/update`, v); if (res.ok) loaders[kind](); return res; }));
      b(r.enabled === false ? t("enable") : t("disable"), "small ghost", async () => { await api(`/api/${kind}/${encodeURIComponent(r.id)}/update`, { enabled: r.enabled === false }); loaders[kind](); });
      b(t("remove"), "small ghost danger", async () => { if (!confirm(t("confirm_remove"))) return; await api(`/api/${kind}/${encodeURIComponent(r.id)}/remove`, {}); loaders[kind](); });
      card.appendChild(acts);
      list.appendChild(card);
    }
    const ex = $(`#${kind}-examples`); ex.innerHTML = "";
    if (kind === "rules" && (examples || []).some((e) => !items.some((i) => i.id === e.id))) {
      const all = el("button", "small", t("install_defaults"));
      all.onclick = async () => { const res = await api("/api/rules/install-defaults", {}); if (res.ok) loaders.rules(); };
      ex.appendChild(el("div", "ex", "")).appendChild(all);
    }
    for (const e of examples || []) {
      const row = el("div", "ex");
      row.appendChild(el("span", "", `${e.name}: ${(e.then || []).map(describeAction).join(" ; ")}${e.note ? " — " + e.note : ""}`));
      const use = el("button", "small ghost", t("use"));
      use.onclick = () => editJson(e.name, t(kind === "rules" ? "edit_rule_hint" : "edit_job_hint"), e, async (v) => { const res = await api(`/api/${kind}`, v); if (res.ok) loaders[kind](); return res; });
      row.appendChild(use); ex.appendChild(row);
    }
    const h = $(`#${kind}-history`); h.innerHTML = "";
    for (const run of (history || []).slice().reverse().slice(0, 12)) {
      const line = el("div", run.ok ? "ok" : "bad", `${fmtTime(run.ts)} ${run.name || run.rule || run.job}${run.event_type ? " ← " + run.event_type : ""} · ${run.ok ? "ok" : "failed"} · ${run.ms} ms · ` + (run.results || []).map((x) => `${x.kind}${x.tool ? ":" + x.tool : ""}${x.type ? ":" + x.type : ""}=${x.ok ? "ok" : (x.error || "err")}`).join(", "));
      h.appendChild(line);
    }
    $(`#${kind}-badge`).textContent = items.length ? String(items.length) : "";
    $(`#${kind}-badge`).className = "badge" + (items.some((i) => i.last && !i.last.ok) ? " bad" : "");
  }
  async function loadRules() { const r = await api("/api/rules"); if (r.ok) renderList("rules", r.rules || [], r.examples, r.history); }
  async function loadJobs() { const r = await api("/api/jobs"); if (r.ok) renderList("jobs", r.jobs || [], r.examples, r.history); }
  $("#rule-new").onclick = () => editJson(t("f_new_rule"), t("edit_rule_hint"), { name: "", when: { type: "scribe.transcript.done" }, then: [{ kind: "event", type: "example.reaction", data: { from: "${event.type}" } }], cooldown_s: 5, enabled: true }, async (v) => { const res = await api("/api/rules", v); if (res.ok) loadRules(); return res; });
  $("#job-new").onclick = () => editJson(t("f_new_job"), t("edit_job_hint"), { name: "", at: "04:00", then: [{ kind: "hub", tool: "hub_backup_run", args: {} }], enabled: true }, async (v) => { const res = await api("/api/jobs", v); if (res.ok) loadJobs(); return res; });

  // ---- backups -----------------------------------------------------------------------------------------
  async function loadBackups() {
    const r = await api("/api/backups"); if (!r.ok) return;
    $("#backup-summary").textContent = t("backup_summary", r) + ` · ${r.root}`;
    const list = $("#backups-list"); list.innerHTML = "";
    const snaps = (r.snapshots || []).slice().reverse();
    if (!snaps.length) list.appendChild(el("div", "hint", t("no_backups")));
    for (const s of snaps) {
      const card = el("div", "rule" + (s.ok ? "" : " off"));
      const left = el("div");
      left.appendChild(el("div", "r-name", `${s.id}${s.label ? " · " + s.label : ""}`));
      left.appendChild(el("div", "r-then", `${fmtTs(s.ts)} · ${s.files} files · ${fmtBytes(s.bytes)} (${fmtBytes(s.new_bytes)} new) · ${s.skipped} skipped · ${s.apps.join(", ")}`));
      card.appendChild(left);
      const acts = el("div", "r-actions");
      const b = (label, cls, fn) => { const x = el("button", cls, label); x.onclick = fn; acts.appendChild(x); };
      b(t("detail"), "small ghost", async () => { const d = await api(`/api/backups/${encodeURIComponent(s.id)}`); $("#detail-title").textContent = s.id; $("#detail-body").textContent = JSON.stringify(d.snapshot, null, 1); $("#detail-dialog").showModal(); });
      b(t("verify"), "small ghost", async () => { const v = await api("/api/backups/verify", { snapshot: s.id }); toast(t("verified", v), v.ok ? "ok" : "err"); });
      const sel = el("select", "small"); for (const a of s.apps) { const o = el("option", "", a); o.value = a; sel.appendChild(o); }
      acts.appendChild(sel);
      b(t("restore"), "small ghost", async () => { const a = sel.value; if (!confirm(t("confirm_restore", s.id, a))) return; const v = await api("/api/backups/restore", { snapshot: s.id, app: a }); toast(v.ok ? t("restored", v) : v.error, v.ok ? "ok" : "err"); });
      card.appendChild(acts); list.appendChild(card);
    }
    $("#backups-badge").textContent = r.snapshots.length ? String(r.snapshots.length) : "";
  }
  $("#backup-run").onclick = async () => { const btn = $("#backup-run"); btn.disabled = true; toast(t("backup_running")); const r = await api("/api/backups/run", {}); btn.disabled = false; toast(r.ok ? t("backup_done", r) : (r.error || (r.errors || []).join("; ")), r.ok ? "ok" : "err"); loadBackups(); };
  $("#backup-verify").onclick = async () => { const v = await api("/api/backups/verify", {}); toast(v.ok ? t("verified", v) : (v.error || t("verified", v)), v.ok ? "ok" : "err"); };
  $("#backup-prune").onclick = async () => { const v = await api("/api/backups/prune", { keep: 14 }); toast(v.ok ? t("pruned", v) : v.error, v.ok ? "ok" : "err"); loadBackups(); };

  // ---- audit ----------------------------------------------------------------------------------------------
  async function runAudit() {
    $("#audit-summary").textContent = "…";
    const r = await api("/api/audit"); if (!r.ok) { $("#audit-summary").textContent = r.error || "error"; return; }
    const s = r.summary;
    $("#audit-summary").textContent = t("audit_summary", s) + ` · hoard_link ${s.library_version}`;
    const recs = $("#audit-recs"); recs.innerHTML = "";
    for (const rec of s.recommendations || []) recs.appendChild(el("div", "rec", rec));
    const tbl = el("table"); const thead = el("thead"); const tr = el("tr");
    for (const c of ["app", "state", "contract", "token", "events", "lib", "data", "git", "tools"]) tr.appendChild(el("th", "", t("col")[c]));
    thead.appendChild(tr); tbl.appendChild(thead);
    const tb = el("tbody");
    for (const a of r.apps) {
      const row = el("tr");
      row.appendChild(el("td", "", a.name));
      row.appendChild(el("td", a.state === "running" ? "ok" : "muted", a.state || "?"));
      row.appendChild(el("td", a.contract === "shared" ? "ok" : (a.contract === "per-tool" ? "warn" : "muted"), a.contract || "?"));
      row.appendChild(el("td", a.token_present ? "ok" : "bad", a.token_present ? "✓" : "✗"));
      row.appendChild(el("td", a.events ? "ok" : (a.state === "running" ? "warn" : "muted"), a.events ? "✓" : "—"));
      row.appendChild(el("td", a.vendored_lags ? "warn" : (a.vendored_hoard_link ? "ok" : "muted"), a.vendored_hoard_link ? a.vendored_hoard_link.version : (a.stack === "node" ? "node" : "—")));
      row.appendChild(el("td", a.data_dir_exists ? "" : "warn", a.data_size ? fmtBytes(a.data_size.bytes) : "—"));
      row.appendChild(el("td", a.data_gitignored === false ? "bad" : (a.data_gitignored ? "ok" : "muted"), a.data_gitignored === false ? "✗" : (a.data_gitignored ? "✓" : "?")));
      row.appendChild(el("td", "muted", a.tools ? String(a.tools.length) : "—"));
      tb.appendChild(row);
    }
    tbl.appendChild(tb); const box = $("#audit-table"); box.innerHTML = ""; box.appendChild(tbl);
    const problems = (s.per_tool_contract.length + s.no_token.length + s.no_events.length + s.vendored_lagging.length + s.data_not_gitignored.length);
    $("#audit-badge").textContent = problems ? String(problems) : "✓"; $("#audit-badge").className = "badge" + (problems ? " warn" : "");
  }
  $("#audit-run").onclick = runAudit;

  // ---- boot --------------------------------------------------------------------------------------------------
  localize();
  $("#btn-lang").addEventListener("click", () => setTimeout(() => { localize(); loaders[tab](); }, 0));
  showTab(tab);
  loadRules(); loadJobs(); loadBackups();
  connectSse();
})();

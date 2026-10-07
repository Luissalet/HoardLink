/* Hoard Hub UI — the family panel: events (live), rules, jobs, backups, audit, repos.
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
      f_repos: "Repos", f_repos_refresh: "Refresh", close: "Close",
      r_copy_push: "Copy push command", r_fetch: "Fetch", r_folder: "Open folder", r_github: "View on GitHub",
      r_f_all: "All", r_f_unpushed: "Unpushed", r_f_never: "Never pushed", r_f_dirty: "Uncommitted", r_f_drift: "Drift", r_f_ci: "CI",
      r_col: { repo: "repo", branch: "branch", sync: "↑ unpushed / ↓ behind", changes: "changes", branches: "extra branches", ci: "CI", drift: "drift", docs: "docs", issues: "issues" },
      r_chip_repos: (n) => `${n} repos`, r_chip_unpushed: (n, c) => `${n} with unpushed commits / ${c} commits`,
      r_chip_dirty: (n) => `${n} uncommitted`, r_chip_drift: (n) => `${n} drift`, r_chip_ci: (n) => `${n} CI failing`,
      r_chip_age: (s) => `scanned ${fmtAge(s)} ago`, r_scanning: "scanning…", r_ci_pending: "CI loading…",
      r_empty: "No git repositories found. They are looked up in the folders of the apps (and hub.json → repos.roots / extra).",
      r_never: "never pushed", r_none: "none", r_detail_issues: "Issues", r_detail_unpushed: "Unpushed commits", r_detail_dirty: "Uncommitted files",
      r_detail_branches: "Branches", r_detail_remotes: "Remotes", r_detail_commits: "Last commits", r_detail_ci: "CI",
      r_detail_drift: "Drift", r_more: (n) => `… and ${n} more`, r_no_issues: "Nothing to fix.", r_copied: "push command copied",
      r_copy_prompt: "Copy this command", r_fetching: "fetching…", r_fetched: "fetched", r_refreshing: "scanning repositories…",
      r_unpushed_unknown: "(commits not in the last 10 are not listed)", r_stray: "stray", r_current: "current",
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
      f_repos: "Repos", f_repos_refresh: "Actualizar", close: "Cerrar",
      r_copy_push: "Copiar comando de push", r_fetch: "Fetch", r_folder: "Abrir carpeta", r_github: "Ver en GitHub",
      r_f_all: "Todos", r_f_unpushed: "Con pendientes", r_f_never: "Sin push", r_f_dirty: "Cambios sin commitear", r_f_drift: "Deriva", r_f_ci: "CI",
      r_col: { repo: "repo", branch: "rama", sync: "↑ sin push / ↓ por detrás", changes: "cambios", branches: "ramas extra", ci: "CI", drift: "deriva", docs: "docs", issues: "avisos" },
      r_chip_repos: (n) => `${n} repos`, r_chip_unpushed: (n, c) => `${n} con commits pendientes / ${c} commits`,
      r_chip_dirty: (n) => `${n} sin commitear`, r_chip_drift: (n) => `${n} con deriva`, r_chip_ci: (n) => `${n} con CI fallando`,
      r_chip_age: (s) => `escaneado hace ${fmtAge(s)}`, r_scanning: "escaneando…", r_ci_pending: "cargando CI…",
      r_empty: "No hay repositorios git. Se buscan junto a las apps (y en hub.json → repos.roots / extra).",
      r_never: "nunca con push", r_none: "ninguna", r_detail_issues: "Avisos", r_detail_unpushed: "Commits sin push", r_detail_dirty: "Ficheros sin commitear",
      r_detail_branches: "Ramas", r_detail_remotes: "Remotos", r_detail_commits: "Últimos commits", r_detail_ci: "CI",
      r_detail_drift: "Deriva", r_more: (n) => `… y ${n} más`, r_no_issues: "Nada que arreglar.", r_copied: "comando de push copiado",
      r_copy_prompt: "Copia este comando", r_fetching: "trayendo el remoto…", r_fetched: "remoto actualizado", r_refreshing: "escaneando repositorios…",
      r_unpushed_unknown: "(solo se listan los últimos 10 commits)", r_stray: "suelta", r_current: "actual",
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
  function localize() { for (const root of ["#family-panel", "#edit-dialog", "#repo-dialog"]) $(root).querySelectorAll("[data-i18n]").forEach((e) => { e.textContent = t(e.dataset.i18n); }); }

  // ---- tabs ------------------------------------------------------------------------------
  let tab = (() => { try { return localStorage.getItem("hub.ftab") || "events"; } catch (e) { return "events"; } })();
  const loaders = { events: loadEvents, rules: loadRules, jobs: loadJobs, backups: loadBackups, audit: () => {}, repos: () => loadRepos() };
  function showTab(name, { openPanel = true } = {}) {
    const panel = document.getElementById(`tab-${name}`);
    if (!panel) return; // a saved facet may still be mounting asynchronously
    tab = name; try { localStorage.setItem("hub.ftab", name); } catch (e) { /* ignore */ }
    document.querySelectorAll("#family-tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    document.querySelectorAll("#family-body .tab-body").forEach((b) => { b.hidden = b.id !== `tab-${name}`; });
    if (openPanel) {
      $("#family-body").hidden = false;
      $("#family-toggle").textContent = "▾";
      $("#family-toggle").setAttribute("aria-expanded", "true");
      try { localStorage.setItem("hub.fopen", "1"); } catch (e) { /* ignore */ }
    }
    (loaders[name] || (() => {}))();
  }
  document.querySelectorAll("#family-tabs .tab").forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });
  window.hubShowTab = showTab;  // facets.js adds its own tabs and switches through this
  $("#family-toggle").onclick = () => {
    const body = $("#family-body"); const open = body.hidden; body.hidden = !open;
    $("#family-toggle").textContent = open ? "▾" : "▸"; $("#family-toggle").setAttribute("aria-expanded", String(open));
    try { localStorage.setItem("hub.fopen", open ? "1" : "0"); } catch (e) { /* ignore */ }
  };
  try { if (localStorage.getItem("hub.fopen") === "0") { $("#family-body").hidden = true; $("#family-toggle").textContent = "▸"; $("#family-toggle").setAttribute("aria-expanded", "false"); } } catch (e) { /* ignore */ }

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
  $("#rule-new").onclick = () => editJson(t("f_new_rule"), t("edit_rule_hint"), { name: "", when: { type: "links.watch.new" }, then: [{ kind: "event", type: "example.reaction", data: { from: "${event.type}" } }], cooldown_s: 5, enabled: true }, async (v) => { const res = await api("/api/rules", v); if (res.ok) loadRules(); return res; });
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

  // ---- repos --------------------------------------------------------------------------------------------------
  // The state of every git repository (read-only: the hub never pushes). GET /api/repos returns the cached scan at
  // once and starts a refresh in the background when it is stale, so the page polls while it says "refreshing".
  let reposSnap = null, reposFetchedAt = 0, reposFilter = "all", reposTick = 0;
  const REPO_FILTERS = {
    all: () => true,
    unpushed: (r) => r.unpushed > 0,
    never: (r) => r.never_pushed,
    dirty: (r) => r.dirty.total > 0,
    drift: (r) => r.drift.vendored || r.drift.theme || ["differs", "invalid", "missing_in_faustus"].includes(r.drift.manifest),
    ci: (r) => r.ci.state === "failing",
  };
  function fmtAge(s) { s = Math.max(0, Math.round(s)); if (s < 60) return `${s}s`; if (s < 3600) return `${Math.floor(s / 60)} min`; return `${Math.floor(s / 3600)} h`; }
  const issueText = (i) => (i.text && (i.text[langOf()] || i.text.en || i.text.es)) || i.kind;
  function repoChip(text, cls) { return el("span", "chip" + (cls ? " " + cls : ""), text); }
  function renderRepoSummary() {
    const box = $("#repos-summary"); box.innerHTML = "";
    if (!reposSnap) return;
    const s = reposSnap.summary;
    box.appendChild(repoChip(t("r_chip_repos", s.repos)));
    box.appendChild(repoChip(t("r_chip_unpushed", s.with_unpushed, s.unpushed_total), s.with_unpushed ? "warn" : ""));
    box.appendChild(repoChip(t("r_chip_dirty", s.dirty), s.dirty ? "info" : ""));
    box.appendChild(repoChip(t("r_chip_drift", s.drift), s.drift ? "warn" : ""));
    box.appendChild(repoChip(t("r_chip_ci", s.ci_failing), s.ci_failing ? "bad" : ""));
    const age = reposSnap.age_s == null ? null : reposSnap.age_s + (Date.now() - reposFetchedAt) / 1000;
    const status = reposSnap.refreshing ? t("r_scanning") : (reposSnap.ci_pending ? t("r_ci_pending") : (age == null ? "" : t("r_chip_age", age)));
    if (status) box.appendChild(el("span", "hint", status));
    $("#repos-refresh").disabled = !!reposSnap.refreshing;
  }
  function renderRepoFilters() {
    const box = $("#repos-filters"); box.innerHTML = "";
    for (const key of Object.keys(REPO_FILTERS)) {
      const n = reposSnap ? reposSnap.repos.filter(REPO_FILTERS[key]).length : 0;
      const btn = el("button", "chip btn" + (reposFilter === key ? " on" : ""), `${t("r_f_" + key)} ${key === "all" ? "" : n}`.trim());
      btn.onclick = () => { reposFilter = key; renderRepoFilters(); renderRepoTable(); };
      box.appendChild(btn);
    }
  }
  function sevClass(r) { return r.severity === "error" ? "bad" : (r.severity === "warn" ? "warn" : (r.severity ? "info" : "ok")); }
  function badge(text, cls, title) { const b = el("span", "rb" + (cls ? " " + cls : ""), text); if (title) b.title = title; return b; }
  function renderRepoTable() {
    const box = $("#repos-table"); box.innerHTML = "";
    if (!reposSnap) return;
    const q = $("#repos-filter").value.trim().toLowerCase();
    const rows = reposSnap.repos.filter(REPO_FILTERS[reposFilter] || REPO_FILTERS.all).filter((r) =>
      !q || [r.name, r.branch || "", r.github || "", r.issues.map((i) => i.kind).join(" ")].join(" ").toLowerCase().includes(q));
    if (!reposSnap.repos.length) { box.appendChild(el("div", "hint", reposSnap.refreshing ? t("r_refreshing") : t("r_empty"))); return; }
    const tbl = el("table"); const tr = el("tr");
    for (const c of ["repo", "branch", "sync", "changes", "branches", "ci", "drift", "docs", "issues"]) tr.appendChild(el("th", "", t("r_col")[c]));
    const thead = el("thead"); thead.appendChild(tr); tbl.appendChild(thead);
    const tb = el("tbody");
    for (const r of rows) {
      const row = el("tr", "repo-row"); row.tabIndex = 0;
      const nameTd = el("td", "repo-name");
      if (r.app) { const img = el("img", "repo-icon"); img.src = `/api/apps/${encodeURIComponent(r.app)}/icon`; img.alt = ""; img.width = 20; img.height = 20; nameTd.appendChild(img); }
      else nameTd.appendChild(el("span", "repo-icon ph", (r.name[0] || "?").toUpperCase()));
      nameTd.appendChild(document.createTextNode(r.name));
      row.appendChild(nameTd);
      row.appendChild(el("td", r.detached ? "warn" : "", r.error ? "—" : (r.detached ? "(detached)" : (r.branch || "—"))));
      const sync = el("td");
      if (r.error) sync.textContent = "—";
      else if (r.never_pushed) sync.appendChild(badge(t("r_never"), "warn", r.no_remote ? "no remote" : ""));
      else {
        sync.appendChild(badge(`↑${r.unpushed}`, r.unpushed ? "warn" : "dim", r.upstream ? r.upstream : ""));
        if (r.behind) sync.appendChild(badge(`↓${r.behind}`, "info"));
      }
      row.appendChild(sync);
      const ch = el("td", r.dirty.total ? "info" : "muted", r.dirty.total ? String(r.dirty.total) + (r.stash ? ` · ⚑${r.stash}` : "") : (r.stash ? `⚑${r.stash}` : "—"));
      ch.title = `${r.dirty.staged} staged · ${r.dirty.modified} modified · ${r.dirty.untracked} untracked${r.stash ? " · " + r.stash + " stash" : ""}`;
      row.appendChild(ch);
      const br = el("td", r.stray_branches.length ? "warn" : "muted", r.extra_branches ? String(r.extra_branches) + (r.stray_branches.length ? ` (${r.stray_branches.length} ${t("r_stray")})` : "") : "—");
      if (r.stray_branches.length) br.title = r.stray_branches.join(", ");
      row.appendChild(br);
      const ci = el("td"); const dot = el("span", "cidot " + (r.ci.state || "unknown")); dot.title = `CI: ${r.ci.state || "unknown"}${r.ci.workflow ? " · " + r.ci.workflow : ""}`;
      ci.appendChild(dot); row.appendChild(ci);
      const dr = el("td");
      if (r.drift.vendored) dr.appendChild(badge("lib", "warn", "hoard_link vendored"));
      if (r.drift.theme) dr.appendChild(badge("tema", "info", "hoard-theme.css"));
      if (["differs", "invalid"].includes(r.drift.manifest)) dr.appendChild(badge("manifest", "warn", "faustus-plugin.json"));
      else if (r.drift.manifest === "missing_in_faustus") dr.appendChild(badge("manifest", "info", "missing in Faustus"));
      if (!dr.childNodes.length) dr.textContent = "—", dr.className = "muted";
      row.appendChild(dr);
      const docs = el("td");
      docs.appendChild(badge("README", r.docs.readme ? "dim" : "bad")); docs.appendChild(badge("ES", r.docs.readme_es ? "dim" : "bad"));
      docs.appendChild(badge("LIC", r.docs.license ? "dim" : "bad"));
      row.appendChild(docs);
      row.appendChild(el("td", sevClass(r), r.error ? "!" : (r.issues.length ? String(r.issues.length) : "✓")));
      row.title = r.error || r.issues.map(issueText).join("\n");
      row.onclick = () => openRepo(r.name);
      row.onkeydown = (ev) => { if (ev.key === "Enter") openRepo(r.name); };
      tb.appendChild(row);
    }
    tbl.appendChild(tb); box.appendChild(tbl);
  }
  function renderRepos() {
    renderRepoSummary(); renderRepoFilters(); renderRepoTable();
    const s = reposSnap && reposSnap.summary;
    const bad = s ? s.errors + s.scan_errors : 0;
    $("#repos-badge").textContent = s ? (s.with_issues ? String(s.with_issues) : "✓") : "";
    $("#repos-badge").className = "badge" + (bad ? " bad" : (s && s.with_issues ? " warn" : ""));
  }
  async function loadRepos() {
    const r = await api("/api/repos"); if (!r.ok) return;
    reposSnap = r; reposFetchedAt = Date.now(); renderRepos();
  }
  $("#repos-refresh").onclick = async () => {
    $("#repos-refresh").disabled = true;
    const r = await api("/api/repos/refresh", { wait: false });
    if (r.ok === false) toast(r.error || "error", "err");
    await loadRepos();
  };
  $("#repos-filter").addEventListener("input", renderRepoTable);
  // Poll: quickly while a scan or the CI lookups run, every 30 s otherwise (reading also starts a refresh once the
  // scan is older than 5 minutes), and only while the page is visible.
  setInterval(async () => {
    if (document.hidden) return;
    reposTick += 1;
    const busy = !reposSnap || reposSnap.refreshing || reposSnap.ci_pending;
    if ((tab === "repos" && busy) || reposTick % 10 === 0) await loadRepos();
    else if (tab === "repos") renderRepoSummary();
  }, 3000);

  // ---- one repository: the detail dialog ---------------------------------------------------------------------------
  let openName = null, openPush = null;
  function section(title) { const d = el("div", "repo-sec"); d.appendChild(el("h3", "", title)); return d; }
  function lines(list, fmt, max = 50) {
    const box = el("div", "repo-lines");
    if (!list.length) box.appendChild(el("div", "hint", t("r_none")));
    for (const item of list.slice(0, max)) box.appendChild(el("div", "mono-line", fmt(item)));
    if (list.length > max) box.appendChild(el("div", "hint", t("r_more", list.length - max)));
    return box;
  }
  async function openRepo(name) {
    const res = await api(`/api/repos/${encodeURIComponent(name)}`);
    if (!res.ok) { toast(res.error || "error", "err"); return; }
    openName = res.repo.name;
    const r = res.repo, body = $("#repo-body"); body.innerHTML = "";
    $("#repo-title").textContent = `${r.name} — ${r.path}`;
    localize();
    const gh = $("#repo-github"); gh.hidden = !res.github_url; if (res.github_url) gh.href = res.github_url;
    $("#repo-folder").hidden = false;
    $("#repo-copy-push").disabled = !!r.error;
    if (r.error) { body.appendChild(el("div", "err", r.error)); $("#repo-dialog").showModal(); return; }
    let sec = section(t("r_detail_issues"));
    if (!r.issues.length) sec.appendChild(el("div", "hint", t("r_no_issues")));
    for (const i of r.issues) sec.appendChild(el("div", "rec " + (i.severity === "error" ? "bad" : (i.severity === "warn" ? "warn" : "info")), issueText(i)));
    body.appendChild(sec);
    const unpushed = r.commits.filter((c) => !c.pushed);
    if (r.unpushed) {
      sec = section(`${t("r_detail_unpushed")} (${r.unpushed})`);
      sec.appendChild(lines(unpushed, (c) => `${c.short}  ${c.subject}  — ${c.author}`));
      if (r.unpushed > unpushed.length) sec.appendChild(el("div", "hint", t("r_unpushed_unknown")));
      body.appendChild(sec);
    }
    if (r.dirty.total) {
      sec = section(`${t("r_detail_dirty")} (${r.dirty.total})`);
      sec.appendChild(lines(r.dirty.paths, (p) => `${p.xy}  ${p.path}`, 50));
      if (r.dirty.truncated) sec.appendChild(el("div", "hint", t("r_more", r.dirty.total - r.dirty.paths.length)));
      body.appendChild(sec);
    }
    sec = section(t("r_detail_branches"));
    sec.appendChild(lines(r.branches, (b) => `${b.current ? "* " : "  "}${b.name}${b.default ? "  (default)" : ""}${b.upstream ? "  → " + b.upstream : ""}${b.stray ? "  [" + t("r_stray") + ": " + b.stray + "]" : ""}`));
    body.appendChild(sec);
    sec = section(t("r_detail_remotes"));
    sec.appendChild(lines(r.remotes, (m) => `${m.name}  ${m.url}`));
    body.appendChild(sec);
    sec = section(t("r_detail_ci"));
    const ci = r.ci || {};
    const ciLine = el("div", "mono-line", `${ci.state || "unknown"}${ci.workflow ? " · " + ci.workflow : ""}${ci.conclusion ? " · " + ci.conclusion : ""}${ci.created_at ? " · " + ci.created_at : ""} `);
    if (ci.url) { const a = el("a", "", "↗"); a.href = ci.url; a.target = "_blank"; a.rel = "noopener noreferrer"; ciLine.appendChild(a); }
    sec.appendChild(ciLine); body.appendChild(sec);
    const dr = r.drift || {};
    const driftLines = [];
    for (const c of (dr.vendored || {}).copies || []) if (c.count) driftLines.push(`hoard_link ${c.path}: ${c.count} files (${c.stale.slice(0, 4).join(", ")}${c.count > 4 ? ", …" : ""})`);
    for (const p of (dr.theme || {}).stale || []) driftLines.push(`hoard-theme.css ${p}`);
    if (["differs", "invalid", "missing_in_faustus"].includes((dr.manifest || {}).state)) driftLines.push(`faustus-plugin.json: ${dr.manifest.state}${dr.manifest.keys ? " (" + dr.manifest.keys.join(", ") + ")" : ""}`);
    if (driftLines.length) { sec = section(t("r_detail_drift")); sec.appendChild(lines(driftLines, (x) => x)); body.appendChild(sec); }
    sec = section(t("r_detail_commits"));
    sec.appendChild(lines(r.commits, (c) => `${c.pushed ? "  " : "↑ "}${c.short}  ${c.subject}  — ${c.author}`));
    body.appendChild(sec);
    $("#repo-dialog").showModal();
  }
  $("#repo-close").onclick = () => $("#repo-dialog").close();
  $("#repo-copy-push").onclick = async () => {
    const r = await api(`/api/repos/${encodeURIComponent(openName)}/push-command`);
    if (!r.ok) { toast(r.error || "error", "err"); return; }
    try { await navigator.clipboard.writeText(r.command); toast(t("r_copied"), "ok", r.command); }
    catch (e) { window.prompt(t("r_copy_prompt"), r.command); }
  };
  $("#repo-fetch").onclick = async () => {
    const btn = $("#repo-fetch"); btn.disabled = true; toast(t("r_fetching"));
    const r = await api(`/api/repos/${encodeURIComponent(openName)}/fetch`, {});
    btn.disabled = false; toast(r.ok ? t("r_fetched") : (r.error || "error"), r.ok ? "ok" : "err");
    await loadRepos(); if ($("#repo-dialog").open) openRepo(openName);
  };
  $("#repo-folder").onclick = async () => { const r = await api(`/api/repos/${encodeURIComponent(openName)}/folder`, {}); if (!r.ok) toast(r.error || "error", "err"); };

  // ---- boot --------------------------------------------------------------------------------------------------
  localize();
  $("#btn-lang").addEventListener("click", () => setTimeout(() => { localize(); (loaders[tab] || (() => {}))(); }, 0));
  showTab(tab, { openPanel: false });
  loadRules(); loadJobs(); loadBackups(); loadRepos();
  connectSse();
})();

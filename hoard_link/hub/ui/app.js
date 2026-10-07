/* Hoard Hub UI — plain JS, no build step. Talks to the JSON API on the same origin. */
(() => {
  "use strict";

  const I18N = {
    en: {
      start_all: "Start all", stop_all: "Stop all", rescan: "Rescan", refresh: "Refresh", close: "Close",
      open_window: "Open window", open_browser: "Browser", start: "Start", stop: "Stop", restart: "Restart",
      close_windows: "Close windows", folder: "Folder", log: "Log", filter: "Filter apps…",
      counts: (c) => `${c.total} apps · ${c.running} running · ${c.windows} windows`,
      state: { running: "running", starting: "starting", foreign: "port busy", down: "stopped" },
      faustus_on: "Faustus reachable", faustus_off: "Faustus not running",
      faustus_starting: "Faustus starting…", faustus_stopping: "Faustus stopping…",
      unavailable: "unavailable", gpu_free: "free",
      no_apps: "No apps found. Put app folders (each with a faustus-plugin.json) next to this repository, or set roots in data/hub.json.",
      started: "started", stopped: "stopped", opened: "opened", closed: "closed", already: "already running",
      not_ready: "started but not ready yet", stopping: "Stopping…", stop_failed: "Some processes could not stop",
      stopped_all: "Apps, Faustus and managed servers stopped",
      windows: (n) => `${n} window${n === 1 ? "" : "s"}`, uptime: "up", mem: "mem",
      cannot_start: "Cannot start from here", hub: "hub", browser: "window engine", none: "none (tabs only)",
      psutil_missing: "psutil missing: no pid/stop",
      gpu_none: "No NVIDIA GPU found (nvidia-smi); leases are granted without a memory check.",
      gpu_probe_failed: "GPU detection failed. Memory could not be verified; new requests remain queued.",
      gpu_summary: (n, l, q) => `${n} GPU · ${l} lease${l === 1 ? "" : "s"} · ${q} queued`,
      used: "used", reserved: "reserved", available: "available", release: "Release", released: "lease released",
      queued: "queued", granted: "granted", no_leases: "No GPU leases.", expires: "expires in", owner: "owner",
      any_gpu: "any",
      profiles: "Groups", profile_started: "group started", profile_stopped: "group stopped",
      new_group: "New group", edit_group: "Edit group", group_name: "Group name", save_group: "Save group",
      cancel_group: "Cancel", delete_group: "Delete group", group_hint: "Choose apps to start together. Windows are optional.",
      group_window: "Open window", group_saved: "Group saved", group_deleted: "Group deleted",
      login_group: "Start at Windows login", login_on: "Starts at Windows login", login_off: "Disable Windows startup",
      login_none: "No group starts at Windows login", login_unavailable: "Windows startup is unavailable",
      login_foreign: "An unrelated Startup file exists; the Hub will preserve it.",
      delete_confirm: "Delete this group? Its apps and data will remain.", commands_kept: "Configured external commands are preserved.",
      pstate: { running: "running", partial: "partly running", stopped: "stopped", empty: "empty" },
      start_profile: "Start this profile", stop_profile: "Stop this profile",
      services: "Servers", start_service: "Start (no Faustus needed)", stop_service: "Stop",
      sstate: { running: "running", starting: "starting", down: "stopped", unavailable: "not installed" },
      started_by: "started by", started_elsewhere: "started outside the family",
    },
    es: {
      start_all: "Arrancar todo", stop_all: "Parar todo", rescan: "Reescanear", refresh: "Actualizar", close: "Cerrar",
      open_window: "Abrir ventana", open_browser: "Navegador", start: "Arrancar", stop: "Parar", restart: "Reiniciar",
      close_windows: "Cerrar ventanas", folder: "Carpeta", log: "Log", filter: "Filtrar apps…",
      counts: (c) => `${c.total} apps · ${c.running} en marcha · ${c.windows} ventanas`,
      state: { running: "en marcha", starting: "arrancando", foreign: "puerto ocupado", down: "parada" },
      faustus_on: "Faustus accesible", faustus_off: "Faustus apagado",
      faustus_starting: "Faustus arrancando…", faustus_stopping: "Parando Faustus…",
      unavailable: "no disponible", gpu_free: "libres",
      no_apps: "No hay apps. Pon las carpetas de las apps (cada una con su faustus-plugin.json) junto a este repositorio, o configura roots en data/hub.json.",
      started: "arrancada", stopped: "parada", opened: "abierta", closed: "cerradas", already: "ya estaba en marcha",
      not_ready: "arrancada pero aún no responde", stopping: "Parando…", stop_failed: "No se han podido parar algunos procesos",
      stopped_all: "Apps, Faustus y servidores gestionados parados",
      windows: (n) => `${n} ventana${n === 1 ? "" : "s"}`, uptime: "activa", mem: "mem",
      cannot_start: "No se puede arrancar desde aquí", hub: "hub", browser: "motor de ventanas", none: "ninguno (solo pestañas)",
      psutil_missing: "falta psutil: sin pid ni parar",
      gpu_none: "No se encontró GPU NVIDIA (nvidia-smi); las reservas se conceden sin comprobar memoria.",
      gpu_probe_failed: "Falló la detección de GPU. No se pudo verificar la memoria; las nuevas solicitudes siguen en cola.",
      gpu_summary: (n, l, q) => `${n} GPU · ${l} reserva${l === 1 ? "" : "s"} · ${q} en cola`,
      used: "usada", reserved: "reservada", available: "disponible", release: "Liberar", released: "reserva liberada",
      queued: "en cola", granted: "concedida", no_leases: "Sin reservas de GPU.", expires: "caduca en", owner: "dueño",
      any_gpu: "cualquiera",
      profiles: "Grupos", profile_started: "grupo arrancado", profile_stopped: "grupo parado",
      new_group: "Crear grupo", edit_group: "Editar grupo", group_name: "Nombre del grupo", save_group: "Guardar grupo",
      cancel_group: "Cancelar", delete_group: "Eliminar grupo", group_hint: "Elige las apps que arrancarán juntas. Abrir sus ventanas es opcional.",
      group_window: "Abrir ventana", group_saved: "Grupo guardado", group_deleted: "Grupo eliminado",
      login_group: "Iniciar con Windows", login_on: "Se inicia con Windows", login_off: "Desactivar inicio con Windows",
      login_none: "Ningún grupo se inicia con Windows", login_unavailable: "Inicio con Windows no disponible",
      login_foreign: "Existe un archivo de inicio ajeno; el Hub lo conservará.",
      delete_confirm: "¿Eliminar este grupo? Sus apps y datos se conservarán.", commands_kept: "Se conservan los comandos externos configurados.",
      pstate: { running: "en marcha", partial: "en marcha a medias", stopped: "parado", empty: "vacío" },
      start_profile: "Arrancar este perfil", stop_profile: "Parar este perfil",
      services: "Servidores", start_service: "Arrancar (sin Faustus)", stop_service: "Parar",
      sstate: { running: "en marcha", starting: "arrancando", down: "parado", unavailable: "no instalado" },
      started_by: "arrancado por", started_elsewhere: "arrancado fuera de la familia",
    },
  };

  let lang = (() => {
    try { const saved = localStorage.getItem("hub.lang"); if (saved) return saved; } catch (e) { /* ignore */ }
    return (navigator.language || "en").toLowerCase().startsWith("es") ? "es" : "en";
  })();
  const t = (k, ...a) => { const v = I18N[lang][k] ?? I18N.en[k] ?? k; return typeof v === "function" ? v(...a) : v; };

  const $ = (s, r = document) => r.querySelector(s);
  const grid = $("#grid"), empty = $("#empty"), counts = $("#counts"), foot = $("#foot");
  const tpl = $("#card-tpl");
  let snapshot = null, busy = new Set(), filter = "", timer = null, backendsTimer = null;

  // ---- API ----------------------------------------------------------------
  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let data = null;
    try { data = await res.json(); } catch (e) { data = { ok: false, error: `HTTP ${res.status}` }; }
    if (data && data.ok === undefined) data.ok = res.ok;
    return data;
  }

  function toast(msg, kind = "info", detail = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    if (detail) { const s = document.createElement("small"); s.textContent = detail; el.appendChild(s); }
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 9000 : 4500);
  }

  // ---- rendering ------------------------------------------------------------
  function fmtUptime(s) {
    if (s == null) return "";
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  }

  function localize(root) {
    root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  }
  function applyI18n() {
    localize(document);
    $("#search").placeholder = t("filter");
    $("#btn-lang").textContent = lang === "es" ? "EN" : "ES";
    document.documentElement.lang = lang;
  }

  function render() {
    if (!snapshot) return;
    const apps = snapshot.apps.filter((a) => {
      if (!filter) return true;
      const hay = `${a.name} ${a.id} ${a.purpose} ${(a.capabilities || []).join(" ")}`.toLowerCase();
      return hay.includes(filter);
    });
    counts.textContent = t("counts", snapshot.counts);
    empty.hidden = apps.length > 0;
    empty.textContent = snapshot.apps.length ? "" : t("no_apps");

    const existing = new Map([...grid.children].map((el) => [el.dataset.id, el]));
    const order = [];
    for (const a of apps) {
      let el = existing.get(a.id);
      if (!el) { el = tpl.content.firstElementChild.cloneNode(true); el.dataset.id = a.id; localize(el); bindCard(el); }
      updateCard(el, a);
      order.push(el);
      existing.delete(a.id);
    }
    existing.forEach((el) => el.remove());
    order.forEach((el) => grid.appendChild(el));

    renderProfiles();

    // Faustus + hub footer
    const f = snapshot.faustus || {};
    $("#faustus").innerHTML = "";
    const chip = document.createElement("span");
    chip.className = `chip ${f.reachable ? "on" : "off"}`;
    chip.innerHTML = `<span class="dot"></span>`;
    const label = document.createElement("b");
    label.textContent = faustusPending.has("stop") ? t("faustus_stopping") :
      (faustusPending.has("start") ? t("faustus_starting") : (f.reachable ? t("faustus_on") : t("faustus_off")));
    chip.appendChild(label);
    if (f.reachable && f.url) { const a = document.createElement("a"); a.href = f.url; a.target = "_blank"; a.textContent = f.url.replace("http://", ""); chip.appendChild(a); }
    const startFaustus = document.createElement("button");
    startFaustus.id = "btn-faustus-start"; startFaustus.className = "ghost small";
    startFaustus.textContent = t("start"); startFaustus.title = `${t("start")} Faustus`;
    startFaustus.setAttribute("aria-label", startFaustus.title);
    startFaustus.disabled = faustusPending.size > 0 || batchBusy || !f.startable;
    startFaustus.onclick = () => runFaustus("start");
    const stopFaustus = document.createElement("button");
    stopFaustus.id = "btn-faustus-stop"; stopFaustus.className = "ghost small danger";
    stopFaustus.textContent = t("stop"); stopFaustus.title = `${t("stop")} Faustus`;
    stopFaustus.setAttribute("aria-label", stopFaustus.title);
    stopFaustus.disabled = faustusPending.has("stop") || batchBusy || !f.stoppable;
    stopFaustus.onclick = () => runFaustus("stop");
    chip.append(startFaustus, stopFaustus);
    $("#faustus").appendChild(chip);
    const sep = document.createElement("span"); sep.className = "strip-sep"; $("#faustus").appendChild(sep);

    const h = snapshot.hub || {};
    foot.innerHTML = "";
    const bits = [
      `${t("hub")} ${h.url || ""} · v${snapshot.version}`,
      `${t("browser")}: ${h.window_engine === "shell" ? "Hoard Window" : (h.browser ? h.browser.split(/[\\/]/).pop() : t("none"))}`,
      `${(snapshot.roots || []).join(" ; ")}`,
    ];
    if (h.psutil === false) bits.push("⚠ " + t("psutil_missing"));
    bits.forEach((b) => { const s = document.createElement("span"); s.textContent = b; foot.appendChild(s); });
  }

  const faustusPending = new Set();
  let batchBusy = false;
  async function runFaustus(action) {
    if (batchBusy || faustusPending.has(action) || (action === "start" && faustusPending.size)) return;
    faustusPending.add(action); render();
    try {
      const r = await api(`/api/faustus/${action}`, {});
      if (!r.cancelled) toast(`Faustus: ${r.ok ? t(action === "start" ? "started" : "stopped") : (r.error || "error")}`, r.ok ? "ok" : "err", r.model_error || "");
    } catch (e) { toast(String(e), "err"); }
    finally {
      faustusPending.delete(action);
      await refresh(); refreshBackends(true); refreshServices();
    }
  }

  let profileBusy = new Set(), loginState = null, editingGroup = null, loginPending = false;
  function renderProfiles() {
    const box = $("#profiles");
    const profiles = snapshot.profiles || [];
    box.hidden = false;
    box.innerHTML = "";
    const label = document.createElement("span"); label.className = "profiles-label"; label.textContent = t("profiles");
    box.appendChild(label);
    const add = document.createElement("button"); add.className = "ghost small"; add.textContent = t("new_group");
    add.onclick = () => editGroup(null); box.appendChild(add);
    for (const p of profiles) {
      const chip = document.createElement("span");
      const on = p.state === "running", some = p.state === "partial";
      chip.className = `chip profile ${on ? "on" : (some ? "some" : "off")}${profileBusy.has(p.name) ? " busy" : ""}`;
      chip.title = `${t("pstate")[p.state] || p.state}\n` + p.members.map((m) => `${m.state === "running" ? "●" : "○"} ${m.name}${m.kind === "command" ? " (cmd)" : ""}${m.desktop ? " ▣" : ""} — ${t("state")[m.state] || m.state}`).join("\n");
      chip.innerHTML = `<span class="dot"></span><b></b><span class="pcount"></span>`;
      $("b", chip).textContent = p.name;
      $(".pcount", chip).textContent = `${p.running}/${p.total}`;
      const start = document.createElement("button"); start.className = "ghost small pbtn"; start.textContent = "▶"; start.title = t("start_profile");
      const stop = document.createElement("button"); stop.className = "ghost small pbtn danger"; stop.textContent = "■"; stop.title = t("stop_profile");
      start.hidden = on; stop.hidden = p.state === "stopped" || p.state === "empty";
      start.disabled = stop.disabled = profileBusy.has(p.name);
      start.onclick = () => runProfile(p.name, "start");
      stop.onclick = () => runProfile(p.name, "stop");
      chip.append(start, stop);
      const edit = document.createElement("button"); edit.className = "ghost small"; edit.textContent = t("edit_group");
      edit.onclick = () => editGroup(p); edit.disabled = profileBusy.has(p.name); chip.appendChild(edit);
      const login = document.createElement("button"); login.className = "ghost small";
      const selected = loginState?.installed && loginState.ours && loginState.profile === p.name;
      login.textContent = t(selected ? "login_on" : "login_group");
      login.setAttribute("aria-pressed", String(!!selected));
      login.disabled = loginPending || !loginState?.supported || (loginState.installed && !loginState.ours) || profileBusy.has(p.name) || !p.members.some(m => m.kind === "app" && m.state !== "unknown");
      login.onclick = () => setLogin(p.name, !selected); chip.appendChild(login);
      box.appendChild(chip);
    }
    const note = document.createElement("span"); note.className = "hint login-note";
    note.textContent = loginState?.installed ? (loginState.ours ? `${t("login_on")}: ${loginState.profile || "Hoard Hub"}` : t("login_foreign")) : t(loginState?.supported ? "login_none" : "login_unavailable");
    box.appendChild(note);
    if (loginState?.installed && loginState.ours) {
      const off = document.createElement("button"); off.className = "ghost small"; off.textContent = t("login_off");
      off.disabled = loginPending; off.onclick = () => setLogin(null, false); box.appendChild(off);
    }
  }
  async function setLogin(name, enabled) {
    if (loginPending) return;
    loginPending = true;
    if (name) profileBusy.add(name); renderProfiles();
    try {
      const r = await api("/api/autostart", {profile: name, enabled});
      if (!r.ok) toast(r.error, "err");
      loginState = await api("/api/autostart");
    } catch (e) { toast(String(e), "err"); }
    finally { loginPending = false; profileBusy.delete(name); renderProfiles(); }
  }
  function editGroup(p) {
    editingGroup = p?.name || null;
    $("#group-editor").hidden = false;
    $("#group-heading").textContent = t(p ? "edit_group" : "new_group");
    $("#group-name").value = p?.name || "";
    $("#group-name").readOnly = !!p;
    $("#group-error").textContent = p?.commands?.length ? t("commands_kept") : "";
    $("#group-remove").hidden = !p;
    const members = $("#group-members"); members.replaceChildren();
    for (const app of snapshot.apps) {
      const row = document.createElement("div"); row.className = "group-member";
      const label = document.createElement("label"), check = document.createElement("input");
      check.type = "checkbox"; check.value = app.id; check.className = "group-choice";
      check.checked = !!p?.members.some(m => m.kind === "app" && m.id === app.id);
      label.append(check, document.createTextNode(app.name));
      const windowLabel = document.createElement("label"), windowCheck = document.createElement("input");
      windowCheck.type = "checkbox"; windowCheck.className = "group-window"; windowCheck.checked = !!p?.desktop.includes(app.id);
      windowCheck.disabled = !check.checked;
      check.onchange = () => { windowCheck.disabled = !check.checked; if (!check.checked) windowCheck.checked = false; };
      windowLabel.append(windowCheck, document.createTextNode(t("group_window")));
      row.append(label, windowLabel); members.appendChild(row);
    }
    $("#group-name").focus();
    $("#group-editor").scrollIntoView({block: "nearest"});
  }
  $("#group-cancel").onclick = () => { $("#group-editor").hidden = true; };
  $("#group-form").onsubmit = async (event) => {
    event.preventDefault();
    const apps = [], desktop = [];
    document.querySelectorAll(".group-member").forEach(row => {
      const choice = $(".group-choice", row); if (choice.checked) apps.push(choice.value);
      if (choice.checked && $(".group-window", row).checked) desktop.push(choice.value);
    });
    const name = $("#group-name").value.trim();
    $("#group-save").disabled = true; $("#group-error").textContent = "";
    try {
      const r = await api(`/api/profiles/${encodeURIComponent(name)}/save`, {apps, desktop, create: !editingGroup});
      if (!r.ok) { $("#group-error").textContent = r.error; return; }
      $("#group-editor").hidden = true; toast(t("group_saved"), "ok"); await refresh();
    } catch (e) { $("#group-error").textContent = String(e); }
    finally { $("#group-save").disabled = false; }
  };
  $("#group-remove").onclick = async () => {
    if (!editingGroup || !confirm(t("delete_confirm"))) return;
    $("#group-remove").disabled = true;
    try {
      const r = await api(`/api/profiles/${encodeURIComponent(editingGroup)}/remove`, {});
      if (!r.ok) { $("#group-error").textContent = r.error; return; }
      $("#group-editor").hidden = true; toast(t("group_deleted"), "ok"); await refresh();
    } catch (e) { $("#group-error").textContent = String(e); }
    finally { $("#group-remove").disabled = false; }
  };
  async function runProfile(name, action) {
    profileBusy.add(name); renderProfiles();
    try {
      const r = await api(`/api/profiles/${encodeURIComponent(name)}/${action}`, {});
      const msg = action === "start" ? t("profile_started") : t("profile_stopped");
      const errs = [...(r.apps || []), ...(r.commands || []), ...(r.desktop || [])].filter((x) => !x.ok).map((x) => `${x.app || x.command}: ${x.error}`);
      toast(`${name}: ${r.ok ? msg : (r.error || "error")}`, r.ok ? "ok" : "err", errs.slice(0, 6).join("\n"));
    } catch (e) { toast(String(e), "err"); }
    profileBusy.delete(name);
    await refresh();
    if (action === "start") { setTimeout(refresh, 2000); setTimeout(refresh, 7000); }
  }

  function updateCard(el, a) {
    const busyNow = busy.has(a.id);
    el.className = `card ${a.state}${busyNow ? " busy" : ""}`;
    const img = $(".icon", el);
    // Keep a cached fallback separate from an icon found by a later rescan.
    const src = `/api/apps/${encodeURIComponent(a.id)}/icon?available=${a.has_icon ? 1 : 0}`;
    if (img.dataset.src !== src) { img.src = src; img.dataset.src = src; }
    $("h2", el).textContent = a.name;
    $("h2", el).title = a.id;
    const pill = $(".pill.state", el);
    pill.className = `pill state ${a.state}`;
    pill.textContent = t("state")[a.state] || a.state;
    $(".port", el).textContent = a.port ? `:${a.port}` : "";
    const p = a.process;
    $(".pid", el).textContent = p ? `pid ${p.pid}` : "";
    $(".purpose", el).textContent = a.purpose;
    $(".purpose", el).title = a.purpose;
    const reason = $(".reason", el);
    if (!a.launchable) { reason.hidden = false; reason.textContent = `${t("cannot_start")}: ${a.launch_reason}`; } else { reason.hidden = true; }
    const proc = $(".proc", el);
    if (p) {
      proc.hidden = false;
      proc.innerHTML = "";
      const parts = [];
      if (p.name) parts.push(["", p.name]);
      if (p.rss_mb != null) parts.push([t("mem"), `${p.rss_mb} MB`]);
      if (p.uptime_s != null) parts.push([t("uptime"), fmtUptime(p.uptime_s)]);
      if (p.children) parts.push(["+", `${p.children} proc`]);
      if (a.health && a.health.state === "foreign") parts.push(["", a.health.detail]);
      for (const [k, v] of parts) { const s = document.createElement("span"); s.innerHTML = `${k ? k + " " : ""}<b></b>`; $("b", s).textContent = v; proc.appendChild(s); }
    } else { proc.hidden = true; }
    let badge = $(".win-badge", el);
    const wins = (a.windows || []).length;
    if (wins) { if (!badge) { badge = document.createElement("span"); badge.className = "win-badge"; el.appendChild(badge); } badge.textContent = t("windows", wins); }
    else if (badge) badge.remove();

    const running = a.state === "running";
    $(".act-window", el).disabled = !(running || a.launchable);
    $(".act-browser", el).disabled = !(running || a.launchable);
    $(".act-start", el).hidden = running || a.state === "starting";
    $(".act-start", el).disabled = !a.launchable || (a.state === "foreign" && !a.port_adaptable);
    $(".act-stop", el).hidden = !(running || a.state === "starting");
    $(".act-stop", el).disabled = !a.stoppable && a.state !== "starting";
    $(".act-restart", el).hidden = !running;
    $(".act-restart", el).disabled = !a.launchable;
    $(".act-close", el).hidden = !wins;
  }

  function bindCard(el) {
    const id = () => el.dataset.id;
    const app = () => snapshot.apps.find((a) => a.id === id());
    const run = async (action, body, okMsg) => {
      busy.add(id()); render();
      try {
        const res = await api(`/api/apps/${encodeURIComponent(id())}/${action}`, body || {});
        const name = app()?.name || id();
        if (res.ok) {
          const extra = res.already ? t("already") : (res.ready === false ? t("not_ready") : (res.mode === "browser-tab" && action === "open" && body?.mode !== "browser" ? "tab" : ""));
          toast(`${name}: ${okMsg}${extra ? " (" + extra + ")" : ""}`, "ok", res.error || "");
        } else {
          toast(`${name}: ${res.error || "error"}`, "err", (res.log_tail || []).slice(-6).join("\n"));
        }
      } catch (e) { toast(String(e), "err"); }
      busy.delete(id());
      await refresh();
    };
    $(".act-window", el).onclick = () => run("open", { mode: "window" }, t("opened"));
    $(".act-browser", el).onclick = () => run("open", { mode: "browser" }, t("opened"));
    $(".act-start", el).onclick = () => run("start", {}, t("started"));
    $(".act-stop", el).onclick = () => run("stop", {}, t("stopped"));
    $(".act-restart", el).onclick = () => run("restart", {}, t("started"));
    $(".act-close", el).onclick = () => run("close-windows", {}, t("closed"));
    $(".act-folder", el).onclick = () => api(`/api/apps/${encodeURIComponent(id())}/folder`, {}).then((r) => { if (!r.ok) toast(r.error, "err"); });
    $(".act-log", el).onclick = () => openLog(id());
    $(".act-detail", el).onclick = () => { const a = app(); $("#detail-title").textContent = a.name; $("#detail-body").textContent = JSON.stringify(a, null, 2); $("#detail-dialog").showModal(); };
  }

  // ---- log dialog -------------------------------------------------------------
  let logApp = null;
  async function loadLog() {
    if (!logApp) return;
    const r = await api(`/api/apps/${encodeURIComponent(logApp)}/log?lines=300`);
    $("#log-body").textContent = r.ok ? ((r.lines || []).join("\n") || "(empty)") : (r.error || "error");
    const pre = $("#log-body"); pre.scrollTop = pre.scrollHeight;
  }
  function openLog(id) {
    logApp = id;
    const a = snapshot.apps.find((x) => x.id === id);
    $("#log-title").textContent = `${a ? a.name : id} — ${a ? a.log : ""}`;
    $("#log-dialog").showModal();
    loadLog();
  }
  $("#log-refresh").onclick = loadLog;
  $("#log-close").onclick = () => $("#log-dialog").close();
  $("#detail-close").onclick = () => $("#detail-dialog").close();

  // ---- backends strip ---------------------------------------------------------
  async function refreshBackends(force = false) {
    try {
      const b = await api(`/api/backends${force ? "?force=1" : ""}`);
      const box = $("#backends");
      box.innerHTML = "";
      const caps = b.capabilities || {};
      for (const cap of Object.keys(caps)) {
        const r = caps[cap];
        const chip = document.createElement("span");
        const on = r.state === "resolved";
        chip.className = `chip ${on ? "on" : "off"}`;
        chip.title = r.reason || "";
        const where = on ? `${r.provider || ""}${r.model ? " · " + r.model : ""}` : t("unavailable");
        chip.innerHTML = `<span class="dot"></span><b>${cap}</b> <span></span>`;
        chip.lastElementChild.textContent = where;
        box.appendChild(chip);
      }
      for (const g of b.gpus || []) {
        const chip = document.createElement("span");
        chip.className = "chip gpu";
        chip.innerHTML = `<b>GPU ${g.index}</b> <span></span>`;
        chip.lastElementChild.textContent = `${(g.free_mb / 1024).toFixed(1)} / ${(g.total_mb / 1024).toFixed(1)} GB ${t("gpu_free")}`;
        box.appendChild(chip);
      }
      if (b.error) { const s = document.createElement("span"); s.className = "chip off"; s.textContent = b.error; box.appendChild(s); }
    } catch (e) { /* strip is decorative */ }
  }

  // ---- local servers (ComfyUI, Ollama, backends.json commands) -----------------------
  let serviceBusy = new Set(), serviceData = null;
  async function refreshServices() {
    try { serviceData = await api("/api/services"); } catch (e) { return; }
    renderServices();
    if ((serviceData.items || []).some((s) => s.state === "starting")) setTimeout(refreshServices, 2000);
  }
  function renderServices() {
    const box = $("#services");
    const items = (serviceData && serviceData.items) || [];
    box.hidden = !items.length;
    box.innerHTML = "";
    if (!items.length) return;
    const label = document.createElement("span"); label.className = "profiles-label"; label.textContent = t("services");
    box.appendChild(label);
    for (const s of items) {
      const chip = document.createElement("span");
      const on = s.state === "running", some = s.state === "starting";
      chip.className = `chip profile ${on ? "on" : (some ? "some" : "off")}${serviceBusy.has(s.id) ? " busy" : ""}`;
      chip.title = [`${t("sstate")[s.state] || s.state} · ${s.url}`, (s.capabilities || []).join(", "),
        on ? (s.started_by ? `${t("started_by")} ${s.started_by}` : t("started_elsewhere")) : "",
        s.state !== "running" && s.problem ? s.problem : "", s.gpu != null ? `GPU ${s.gpu}` : "", s.log || ""].filter(Boolean).join("\n");
      chip.innerHTML = `<span class="dot"></span><b></b><span class="pcount"></span>`;
      $("b", chip).textContent = s.label;
      $(".pcount", chip).textContent = t("sstate")[s.state] || s.state;
      const start = document.createElement("button"); start.className = "ghost small pbtn"; start.textContent = "▶"; start.title = t("start_service");
      const stop = document.createElement("button"); stop.className = "ghost small pbtn danger"; stop.textContent = "■"; stop.title = t("stop_service");
      start.hidden = !s.startable; stop.hidden = !s.stoppable;
      start.disabled = stop.disabled = serviceBusy.has(s.id);
      start.onclick = () => runService(s, "start");
      stop.onclick = () => runService(s, "stop");
      chip.append(start, stop);
      box.appendChild(chip);
    }
  }
  async function runService(s, action) {
    serviceBusy.add(s.id); renderServices();
    try {
      const r = await api(`/api/services/${action}`, { id: s.id, gpu: "auto" });
      toast(`${s.label}: ${r.ok ? (action === "start" ? (r.already ? t("already") : t("started")) : t("stopped")) : (r.error || "error")}`, r.ok ? "ok" : "err");
    } catch (e) { toast(String(e), "err"); }
    serviceBusy.delete(s.id);
    await refreshServices();
    refreshBackends(true);
  }

  // ---- GPU leases panel ----------------------------------------------------------
  let leaseData = null;
  const gb = (mb) => `${(mb / 1024).toFixed(1)} GB`;
  function renderLeases() {
    const d = leaseData;
    if (!d) return;
    const leases = d.leases || [], queue = d.queue || [];
    const probeFailed = d.inventory_status === "error";
    $("#gpu-summary").textContent = t("gpu_summary", probeFailed ? "?" : (d.gpus || []).length, leases.length, queue.length);
    const cards = $("#gpu-cards");
    cards.innerHTML = "";
    if (!d.inventory) {
      const p = document.createElement("div"); p.className = "gpu-none";
      p.textContent = probeFailed ? `${t("gpu_probe_failed")} ${d.inventory_error || ""}` : t("gpu_none");
      cards.appendChild(p);
    }
    for (const g of d.gpus || []) {
      const card = document.createElement("div");
      card.className = "gpu-card";
      const usedPct = Math.min(100, (g.used_mb / g.total_mb) * 100);
      const effUsed = g.total_mb - g.available_mb - (d.headroom_mb || 0);
      const resPct = Math.max(0, Math.min(100 - usedPct, ((effUsed - g.used_mb) / g.total_mb) * 100));
      card.innerHTML = `<div class="gpu-title"><b></b><span></span></div>
        <div class="bar"><span class="u"></span><span class="r"></span></div>
        <div class="gpu-nums"></div>`;
      $(".gpu-title b", card).textContent = `GPU ${g.index}`;
      $(".gpu-title span", card).textContent = gb(g.total_mb);
      $(".bar .u", card).style.width = `${usedPct}%`;
      $(".bar .r", card).style.width = `${resPct}%`;
      $(".gpu-nums", card).textContent = `${t("used")} ${gb(g.used_mb)} · ${t("reserved")} ${gb(g.reserved_mb)} · ${t("available")} ${gb(g.available_mb)}`;
      cards.appendChild(card);
    }
    const box = $("#gpu-leases");
    box.innerHTML = "";
    const rows = [...leases, ...queue];
    if (!rows.length) { const p = document.createElement("div"); p.className = "gpu-none"; p.textContent = t("no_leases"); box.appendChild(p); return; }
    for (const l of rows) {
      const row = document.createElement("div");
      row.className = `lease-row ${l.state}`;
      row.innerHTML = `<span class="pill"></span><b class="who"></b><span class="what"></span><span class="mono amount"></span><span class="mono where"></span><span class="mono ttl"></span><button class="ghost small danger"></button>`;
      $(".pill", row).textContent = l.state === "queued" ? `${t("queued")} #${l.position}` : t("granted");
      $(".pill", row).className = `pill ${l.state === "queued" ? "starting" : "running"}`;
      $(".who", row).textContent = l.owner;
      $(".what", row).textContent = l.purpose || "";
      $(".amount", row).textContent = gb(l.vram_mb);
      $(".where", row).textContent = l.gpu != null ? `GPU ${l.gpu}` : (l.gpu_request === "any" ? t("any_gpu") : `GPU ${[].concat(l.gpu_request).join(" / ")}`);
      $(".ttl", row).textContent = `${t("expires")} ${fmtUptime(l.expires_in_s)}`;
      row.title = `${l.lease_id}${l.pid ? " · pid " + l.pid : ""}${l.note ? " · " + l.note : ""}`;
      const btn = $("button", row);
      btn.textContent = t("release");
      btn.onclick = async () => {
        btn.disabled = true;
        const r = await api("/api/lease/release", { lease_id: l.lease_id });
        toast(`${l.owner}: ${r.ok ? t("released") : (r.error || "error")}`, r.ok ? "ok" : "err");
        await refreshLeases();
      };
      box.appendChild(row);
    }
  }
  async function refreshLeases() {
    try { leaseData = await api("/api/lease"); renderLeases(); } catch (e) { /* panel is best-effort */ }
  }
  $("#gpu-toggle").onclick = () => {
    const body = $("#gpu-body"); body.hidden = !body.hidden;
    $("#gpu-toggle").setAttribute("aria-expanded", String(!body.hidden));
    try { localStorage.setItem("hub.gpu.collapsed", body.hidden ? "1" : "0"); } catch (e) { /* ignore */ }
  };
  try { if (localStorage.getItem("hub.gpu.collapsed") === "1") { $("#gpu-body").hidden = true; $("#gpu-toggle").setAttribute("aria-expanded", "false"); } } catch (e) { /* ignore */ }

  // ---- refresh loop -------------------------------------------------------------
  let refreshing = null;
  function refresh() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        snapshot = await api("/api/apps");
        loginState = await api("/api/autostart");
        render();
        refreshLeases();
      } catch (e) { counts.textContent = String(e); }
      refreshing = null;
    })();
    return refreshing;
  }
  function schedule() {
    clearInterval(timer); clearInterval(backendsTimer);
    timer = setInterval(() => { if (!document.hidden) refresh(); }, 5000);
    backendsTimer = setInterval(() => { if (!document.hidden) { refreshBackends(); refreshServices(); } }, 20000);
  }

  // ---- top bar --------------------------------------------------------------------
  $("#search").addEventListener("input", (e) => { filter = e.target.value.trim().toLowerCase(); render(); });
  $("#btn-rescan").onclick = async () => { const r = await api("/api/apps/rescan", {}); toast(`${t("rescan")}: ${(r.apps || []).length} apps`, "ok"); await refresh(); await refreshBackends(true); };
  async function runBatch(action) {
    if (batchBusy || (action === "start" && faustusPending.size)) return;
    batchBusy = true;
    const start = $("#btn-start-all"), stop = $("#btn-stop-all");
    start.disabled = stop.disabled = true;
    if (action === "stop") stop.textContent = t("stopping");
    render();
    try {
      const r = await api(`/api/apps/${action}-all`, {});
      const outcomes = [...(r.results || []), ...(r.commands || []), ...(r.services || []), ...(r.faustus ? [{...r.faustus, app: "Faustus"}] : [])];
      const errors = outcomes.filter((x) => !x.ok).map((x) => `${x.app || x.service || x.command || ""}: ${x.error || "error"}`);
      const message = action === "stop" ? t(r.ok ? "stopped_all" : "stop_failed") : `${t("start_all")}: ${(r.results || []).filter((x) => x.ok).length}`;
      toast(message, r.ok ? "ok" : "err", errors.join("\n"));
    } catch (e) { toast(String(e), "err"); }
    finally {
      batchBusy = false; start.disabled = stop.disabled = false; stop.textContent = t("stop_all");
      await refresh(); refreshBackends(true); refreshServices();
      setTimeout(refresh, 2000); setTimeout(refresh, 7000);
    }
  }
  $("#btn-start-all").onclick = () => runBatch("start");
  $("#btn-stop-all").onclick = () => runBatch("stop");
  $("#btn-lang").onclick = () => { lang = lang === "es" ? "en" : "es"; try { localStorage.setItem("hub.lang", lang); } catch (e) { /* ignore */ } applyI18n(); render(); renderLeases(); refreshBackends(); renderServices(); };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

  applyI18n();
  refresh().then(() => { refreshBackends(); refreshServices(); });
  schedule();
})();

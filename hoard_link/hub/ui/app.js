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
      unavailable: "unavailable", gpu_free: "free",
      no_apps: "No apps found. Put app folders (each with a faustus-plugin.json) next to this repository, or set roots in data/hub.json.",
      started: "started", stopped: "stopped", opened: "opened", closed: "closed", already: "already running",
      not_ready: "started but not ready yet", confirm_stop_all: "Stop every running app?",
      windows: (n) => `${n} window${n === 1 ? "" : "s"}`, uptime: "up", mem: "mem",
      cannot_start: "Cannot start from here", hub: "hub", browser: "window engine", none: "none (tabs only)",
      psutil_missing: "psutil missing: no pid/stop",
    },
    es: {
      start_all: "Arrancar todo", stop_all: "Parar todo", rescan: "Reescanear", refresh: "Actualizar", close: "Cerrar",
      open_window: "Abrir ventana", open_browser: "Navegador", start: "Arrancar", stop: "Parar", restart: "Reiniciar",
      close_windows: "Cerrar ventanas", folder: "Carpeta", log: "Log", filter: "Filtrar apps…",
      counts: (c) => `${c.total} apps · ${c.running} en marcha · ${c.windows} ventanas`,
      state: { running: "en marcha", starting: "arrancando", foreign: "puerto ocupado", down: "parada" },
      faustus_on: "Faustus accesible", faustus_off: "Faustus apagado",
      unavailable: "no disponible", gpu_free: "libres",
      no_apps: "No hay apps. Pon las carpetas de las apps (cada una con su faustus-plugin.json) junto a este repositorio, o configura roots en data/hub.json.",
      started: "arrancada", stopped: "parada", opened: "abierta", closed: "cerradas", already: "ya estaba en marcha",
      not_ready: "arrancada pero aún no responde", confirm_stop_all: "¿Parar todas las apps en marcha?",
      windows: (n) => `${n} ventana${n === 1 ? "" : "s"}`, uptime: "activa", mem: "mem",
      cannot_start: "No se puede arrancar desde aquí", hub: "hub", browser: "motor de ventanas", none: "ninguno (solo pestañas)",
      psutil_missing: "falta psutil: sin pid ni parar",
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

    // Faustus + hub footer
    const f = snapshot.faustus || {};
    $("#faustus").innerHTML = "";
    const chip = document.createElement("span");
    chip.className = `chip ${f.reachable ? "on" : "off"}`;
    chip.innerHTML = `<span class="dot"></span>`;
    const label = document.createElement("b");
    label.textContent = f.reachable ? t("faustus_on") : t("faustus_off");
    chip.appendChild(label);
    if (f.reachable && f.url) { const a = document.createElement("a"); a.href = f.url; a.target = "_blank"; a.textContent = f.url.replace("http://", ""); chip.appendChild(a); }
    $("#faustus").appendChild(chip);
    const sep = document.createElement("span"); sep.className = "strip-sep"; $("#faustus").appendChild(sep);

    const h = snapshot.hub || {};
    foot.innerHTML = "";
    const bits = [
      `${t("hub")} ${h.url || ""} · v${snapshot.version}`,
      `${t("browser")}: ${h.browser ? h.browser.split(/[\\/]/).pop() : t("none")}`,
      `${(snapshot.roots || []).join(" ; ")}`,
    ];
    if (h.psutil === false) bits.push("⚠ " + t("psutil_missing"));
    bits.forEach((b) => { const s = document.createElement("span"); s.textContent = b; foot.appendChild(s); });
  }

  function updateCard(el, a) {
    const busyNow = busy.has(a.id);
    el.className = `card ${a.state}${busyNow ? " busy" : ""}`;
    const img = $(".icon", el);
    const src = `/api/apps/${encodeURIComponent(a.id)}/icon`;
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
    $(".act-start", el).disabled = !a.launchable || a.state === "foreign";
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

  // ---- refresh loop -------------------------------------------------------------
  let refreshing = null;
  function refresh() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        snapshot = await api("/api/apps");
        render();
      } catch (e) { counts.textContent = String(e); }
      refreshing = null;
    })();
    return refreshing;
  }
  function schedule() {
    clearInterval(timer); clearInterval(backendsTimer);
    timer = setInterval(() => { if (!document.hidden) refresh(); }, 5000);
    backendsTimer = setInterval(() => { if (!document.hidden) refreshBackends(); }, 20000);
  }

  // ---- top bar --------------------------------------------------------------------
  $("#search").addEventListener("input", (e) => { filter = e.target.value.trim().toLowerCase(); render(); });
  $("#btn-rescan").onclick = async () => { const r = await api("/api/apps/rescan", {}); toast(`${t("rescan")}: ${(r.apps || []).length} apps`, "ok"); await refresh(); await refreshBackends(true); };
  $("#btn-start-all").onclick = async () => { const r = await api("/api/apps/start-all", {}); const n = (r.results || []).filter((x) => x.ok).length; toast(`${t("start_all")}: ${n}`, "ok"); setTimeout(refresh, 1500); setTimeout(refresh, 6000); };
  let stopAllArmed = null;
  $("#btn-stop-all").onclick = async () => {
    const btn = $("#btn-stop-all");
    if (!stopAllArmed) { stopAllArmed = setTimeout(() => { stopAllArmed = null; btn.textContent = t("stop_all"); }, 4000); btn.textContent = t("confirm_stop_all"); return; }
    clearTimeout(stopAllArmed); stopAllArmed = null; btn.textContent = t("stop_all");
    const r = await api("/api/apps/stop-all", {}); const n = (r.results || []).filter((x) => x.ok && x.pid).length; toast(`${t("stop_all")}: ${n}`, "ok"); await refresh();
  };
  $("#btn-lang").onclick = () => { lang = lang === "es" ? "en" : "es"; try { localStorage.setItem("hub.lang", lang); } catch (e) { /* ignore */ } applyI18n(); render(); refreshBackends(); };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

  applyI18n();
  refresh().then(() => refreshBackends());
  schedule();
})();

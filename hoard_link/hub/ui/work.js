/* Hoard Hub UI — jobs across apps ("Trabajos" / "Work"): what the family is doing right now.
   Active jobs with progress bar, GPU chip and elapsed time; finished ones below (failed only filter).
   The badge counts active jobs. Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const ctx = H.ctx;
  const { el, api, L, fmtWhen } = ctx;

  const S = {
    label: { es: "Trabajos", en: "Work" },
    active: { es: "En marcha", en: "Active" },
    finished: { es: "Terminados", en: "Finished" },
    failed_only: { es: "Solo fallidos", en: "Failed only" },
    none_active: { es: "Nada en marcha ahora mismo.", en: "Nothing running right now." },
    none_finished: { es: "Aún no hay trabajos terminados.", en: "No finished jobs yet." },
    queued: { es: "en cola", en: "queued" }, running: { es: "en marcha", en: "running" },
    done: { es: "hecho", en: "done" }, failed: { es: "fallido", en: "failed" }, cancelled: { es: "cancelado", en: "cancelled" },
    stale: { es: "sin noticias", en: "stale" },
    eta: { es: "faltan", en: "left" },
    all_apps: { es: "Todas las apps", en: "All apps" },
    hint: { es: "Los trabajos largos que anuncian las apps (<app>.job.*).", en: "Long jobs the apps announce (<app>.job.*)." },
    open: { es: "Abrir", en: "Open" },
  };
  const t = (k) => L(S[k]);
  let box, activeBox, finishedBox, appSel, failedOnly, badge;

  function injectCss() {
    if (document.getElementById("css-work")) return;
    const css = document.createElement("style");
    css.id = "css-work";
    css.textContent = `
      .wk-list { display: grid; gap: 6px; margin-bottom: 8px; }
      .wk-row { display: grid; grid-template-columns: 22px 1fr auto; gap: 4px 12px; align-items: center; padding: 7px 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); }
      .wk-row.stale { opacity: .6; }
      .wk-row.failed { border-color: #5a2b2f; }
      .wk-row img { width: 22px; height: 22px; border-radius: 6px; object-fit: cover; background: var(--hoard-sunken); grid-row: span 2; align-self: start; }
      .wk-title { font-weight: 600; display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
      .wk-title a { color: var(--text); text-decoration: none; } .wk-title a:hover { color: var(--accent-2); }
      .wk-sub { color: var(--muted); font-size: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
      .wk-bar { height: 8px; width: 160px; border-radius: 999px; background: var(--hoard-elevated); overflow: hidden; }
      .wk-bar i { display: block; height: 100%; background: var(--accent); }
      .wk-bar.idle i { width: 30%; background: var(--blue); opacity: .6; animation: wk-slide 1.6s ease-in-out infinite; }
      @keyframes wk-slide { 0% { margin-left: 0; } 50% { margin-left: 70%; } 100% { margin-left: 0; } }
      .wk-right { display: flex; gap: 8px; align-items: center; justify-content: flex-end; flex-wrap: wrap; font-family: var(--mono); font-size: 12px; color: var(--muted); }
      .wk-err { color: var(--red); font-size: 12px; grid-column: 2 / 4; }
      .wk-head { margin: 10px 0 4px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .4px; }
    `;
    document.head.appendChild(css);
  }

  function fmtDur(s) {
    s = Math.max(0, Math.round(s || 0));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  }

  function row(j) {
    const r = el("div", `wk-row ${j.status}${j.stale ? " stale" : ""}`);
    const img = el("img"); img.src = `/api/apps/${encodeURIComponent(j.app)}/icon`; img.alt = ""; img.onerror = () => { img.style.visibility = "hidden"; };
    r.appendChild(img);
    const main = el("div");
    const title = el("div", "wk-title");
    if (j.url) { const a = el("a", "", j.title); a.href = j.url; a.target = "_blank"; a.rel = "noopener"; title.appendChild(a); } else title.appendChild(el("span", "", j.title));
    title.appendChild(el("span", "hint", j.app));
    if (j.kind) title.appendChild(el("span", "chip", j.kind));
    main.appendChild(title);
    const sub = el("div", "wk-sub");
    if (j.status === "running" || j.status === "queued") {
      const bar = el("div", "wk-bar" + (j.progress == null ? " idle" : ""));
      const fill = el("i"); if (j.progress != null) fill.style.width = `${Math.round(j.progress * 100)}%`; bar.appendChild(fill);
      sub.appendChild(bar);
      if (j.progress != null) sub.appendChild(el("span", "", `${Math.round(j.progress * 100)}%`));
      if (j.eta_s) sub.appendChild(el("span", "", `${t("eta")} ${fmtDur(j.eta_s)}`));
    } else sub.appendChild(el("span", "", fmtWhen(j.finished_ts)));
    main.appendChild(sub);
    r.appendChild(main);
    const right = el("div", "wk-right");
    if (j.gpu != null && j.gpu !== "") { const g = el("span", "chip gpu", `GPU${j.gpu === true ? "" : " " + j.gpu}${j.lease && j.lease.vram_mb ? " · " + Math.round(j.lease.vram_mb / 102.4) / 10 + " GB" : ""}`); right.appendChild(g); }
    right.appendChild(el("span", `chip ${j.status === "failed" ? "bad" : (j.stale ? "warn" : "info")}`, j.stale ? t("stale") : t(j.status) || j.status));
    right.appendChild(el("span", "", fmtDur(j.elapsed_s)));
    r.appendChild(right);
    if (j.error) r.appendChild(el("div", "wk-err", j.error));
    return r;
  }

  async function load() {
    const app = appSel ? appSel.value : "";
    const q = `/api/work?limit=50${app ? "&app=" + encodeURIComponent(app) : ""}${failedOnly && failedOnly.checked ? "&failed=1" : ""}`;
    const r = await api(q);
    if (!r.ok) return;
    activeBox.innerHTML = ""; finishedBox.innerHTML = "";
    if (!r.active.length) activeBox.appendChild(el("div", "hint", t("none_active")));
    for (const j of r.active) activeBox.appendChild(row(j));
    if (!(r.finished || []).length) finishedBox.appendChild(el("div", "hint", t("none_finished")));
    for (const j of r.finished || []) finishedBox.appendChild(row(j));
    // the app filter lists every app that has appeared
    const seen = new Set([...r.active, ...(r.finished || [])].map((j) => j.app));
    for (const a of seen) if (appSel && ![...appSel.options].some((o) => o.value === a)) { const o = el("option", "", a); o.value = a; appSel.appendChild(o); }
    setBadge(r.counts ? r.counts.active : r.active.length);
  }

  function setBadge(n) {
    if (!badge) badge = document.getElementById("work-badge");
    if (!badge) return;
    badge.textContent = n > 0 ? String(n) : "";
    badge.classList.toggle("warn", n > 0);
  }
  async function pollBadge() { const r = await api("/api/work?active=1"); if (r && r.ok) setBadge(r.counts ? r.counts.active : r.active.length); }

  H.register("work", {
    placement: "tab",
    label: S.label,
    order: 70,
    mount(container) {
      injectCss();
      box = container;
      const tools = el("div", "tab-tools");
      appSel = el("select"); const all = el("option", "", t("all_apps")); all.value = ""; appSel.appendChild(all); appSel.onchange = load;
      const lab = el("label", "hint"); failedOnly = el("input"); failedOnly.type = "checkbox"; failedOnly.onchange = load;
      lab.appendChild(failedOnly); lab.appendChild(document.createTextNode(" " + t("failed_only")));
      tools.appendChild(appSel); tools.appendChild(lab); tools.appendChild(el("span", "hint", t("hint")));
      box.appendChild(tools);
      box.appendChild(el("div", "wk-head", t("active")));
      activeBox = el("div", "wk-list"); box.appendChild(activeBox);
      box.appendChild(el("div", "wk-head", t("finished")));
      finishedBox = el("div", "wk-list"); box.appendChild(finishedBox);
      pollBadge(); setInterval(pollBadge, 10000);
    },
    load,
    tick: load,
  });
})();

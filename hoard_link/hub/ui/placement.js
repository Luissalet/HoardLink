// "Ejecutar en: PC | Sparks" — where Faustus, Hermes and Prometheus run (never on both). The control shows on the Home
// Faustus tile and in System › Connections; its panel explains the move before it starts, follows it while it runs and
// shows the tailnet links while the three live on the Spark.
(() => {
  "use strict";
  const es = () => (document.documentElement.lang || "es").startsWith("es");
  const T = {
    run_on: ["Ejecutar en", "Run on"], pc: ["PC", "PC"], spark_target: ["Spark de destino", "Target Spark"],
    moving: ["Trasladando:", "Moving:"], on_spark: ["Faustus, Hermes y Prometheus corren en {node}. Puedes apagar el PC.", "Faustus, Hermes and Prometheus run on {node}. You can switch the PC off."],
    to_sparks_title: ["Pasar Faustus, Hermes y Prometheus a {node}", "Move Faustus, Hermes and Prometheus to {node}"],
    to_sparks_body: ["Los paro aquí, copio su código y sus datos a {node} y los arranco allí; nunca corren en los dos a la vez. Tarda unos minutos (la primera vez, más). Después puedes apagar el PC.",
                     "I stop them here, copy their code and data to {node} and start them there; they never run on both at once. It takes a few minutes (longer the first time). Then you can switch the PC off."],
    to_pc_title: ["Volver al PC", "Back to the PC"],
    to_pc_body: ["Los paro en la Spark, traigo sus datos a este PC y los arranco aquí. Lo que estaba en pausa vuelve a funcionar.",
                 "I stop them on the Spark, bring their data back to this PC and start them here. What was paused works again."],
    paused: ["En pausa mientras corren en la Spark:", "Paused while they run on the Spark:"],
    go: ["Mover ahora", "Move now"], cancel: ["Cancelar", "Cancel"], log: ["Registro del traslado", "Move log"],
    failed: ["El último traslado no terminó:", "The last move did not finish:"], links: ["Desde cualquier dispositivo de tu Tailscale:", "From any device on your Tailscale:"],
    no_links: ["Tailscale aún no está activo en la Spark: de momento, solo desde la red de casa.", "Tailscale is not active on the Spark yet: for now, home network only."],
    done_sparks: ["Listo: ya corren en la Spark. Puedes apagar el PC.", "Done: they run on the Spark. You can switch the PC off."],
    done_pc: ["Listo: vuelven a correr en el PC.", "Done: they run on the PC again."],
    update: ["Llevar el código nuevo a la Spark", "Send the new code to the Spark"],
    update_hint: ["Copia el código que cambió en el PC y reinicia solo esas apps; sus datos no se tocan.",
                  "Copies the code that changed on the PC and restarts only those apps; their data is left alone."],
    h_running: ["en marcha", "running"], h_starting: ["arrancando", "starting"], h_down: ["parado o sin respuesta", "stopped or not answering"],
    h_checking: ["comprobando…", "checking…"], h_unreachable: ["No puedo consultar {node}:", "Cannot reach {node}:"],
    h_checked: ["comprobado hace {s} s", "checked {s} s ago"],
  };
  const t = (k, vars = {}) => (T[k] ? T[k][es() ? 0 : 1] : k).replace(/\{(\w+)\}/g, (_, v) => vars[v] ?? "");
  let state = null, confirmTo = null, confirmNode = "", polling = null, lastJobRunning = null;

  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function list(items) { const ul = el("ul"); (items || []).forEach((p) => ul.appendChild(el("li", null, p))); return ul; }

  async function fetchState() {
    try { const r = await fetch("/api/placement"); state = await r.json(); } catch (_) { /* keep the last one */ }
    render();
  }

  async function move(to, node = "") {
    confirmTo = null;
    try {
      const r = await fetch("/api/placement/switch", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(node ? { to, node } : { to }) });
      const d = await r.json();
      if (!d.ok && window.hubToast) window.hubToast(d.error || "error", "err");
    } catch (e) { if (window.hubToast) window.hubToast(String(e), "err"); }
    await fetchState();
    startPolling();
  }

  function startPolling() {
    if (polling) return;
    polling = setInterval(async () => {
      await fetchState();
      if (!(state && state.job && state.job.running)) { clearInterval(polling); polling = null; window.hubRefresh?.(); }
    }, 2000);
  }

  // The two-way toggle.
  function toggle() {
    const job = state.job || {};
    const wrap = el("span", "placement-toggle");
    wrap.appendChild(el("b", null, t("run_on")));
    const seg = el("span", "placement-seg");
    seg.setAttribute("role", "group"); seg.setAttribute("aria-label", t("run_on"));
    for (const to of ["pc", "sparks"]) {
      const b = el("button", "ghost small" + (state.location === to ? " active" : ""), to === "pc" ? t("pc") : state.node);
      b.type = "button"; b.dataset.placementTo = to;
      b.setAttribute("aria-pressed", String(state.location === to));
      b.disabled = !!job.running || (state.location === to && job.ok !== false);
      b.onclick = () => { confirmTo = to; confirmNode = state.node; render(); };
      seg.appendChild(b);
    }
    wrap.appendChild(seg);
    if (job.running) wrap.appendChild(el("span", "placement-step", `${t("moving")} ${job.step || ""}`));
    return wrap;
  }

  // The quick switch: "PC ← Spark3" / "PC → Spark3". One click moves Faustus, Hermes and Prometheus to the other side
  // (no confirmation); the arrow points where they go and pulses while they move.
  function arrow() {
    const wrap = el("span", "placement-arrow");
    if (!state) return wrap;
    const job = state.job || {};
    const node = state.node || "Spark";
    const here = job.running ? job.to : state.location;
    const to = job.running ? job.to : (state.location === "sparks" ? "pc" : "sparks");
    const pc = el("span", "pa-end" + (here === "pc" ? " is-here" : ""), t("pc"));
    const sp = el("span", "pa-end" + (here === "sparks" ? " is-here" : ""), node);
    const b = el("button", "pa-go" + (job.running ? " is-moving" : ""), to === "pc" ? "\u2190" : "\u2192");
    b.type = "button"; b.dataset.placementArrow = to; b.disabled = !!job.running;
    b.title = job.running ? `${t("moving")} ${job.step || ""}` : (to === "sparks" ? t("to_sparks_title", { node }) : t("to_pc_title"));
    b.setAttribute("aria-label", b.title);
    b.onclick = (ev) => { ev.stopPropagation(); move(to); };
    wrap.append(pc, b, sp);
    return wrap;
  }

  // Confirmation, progress, links, paused features, last error.
  function panel() {
    const job = state.job || {};
    const box = el("div", "placement-panel");
    if (confirmTo && !job.running) {
      box.classList.add("is-confirm");
      const title = el("h3");
      const body = el("p");
      const updateCopy = () => {
        title.textContent = confirmTo === "sparks" ? t("to_sparks_title", { node: confirmNode }) : t("to_pc_title");
        body.textContent = confirmTo === "sparks" ? t("to_sparks_body", { node: confirmNode }) : t("to_pc_body");
      };
      updateCopy();
      box.append(title);
      if (confirmTo === "sparks" && state.location === "pc" && state.nodes?.length > 1) {
        const label = el("label", null, t("spark_target") + " ");
        const select = el("select");
        select.dataset.placementNode = "";
        for (const node of state.nodes) {
          const option = el("option", null, node);
          option.value = node;
          select.append(option);
        }
        select.value = state.nodes.includes(confirmNode) ? confirmNode : state.node;
        select.onchange = () => {
          confirmNode = select.value;
          updateCopy();
        };
        label.append(select);
        box.append(label);
      }
      box.append(body);
      if (confirmTo === "sparks") { box.appendChild(el("p", "muted", t("paused"))); box.appendChild(list(state.paused_on_spark)); }
      const row = el("div", "placement-actions");
      const go = el("button", "hub-btn hub-btn-primary primary", t("go")); go.type = "button"; go.dataset.placementConfirm = confirmTo;
      go.onclick = () => move(confirmTo, confirmTo === "sparks" ? confirmNode : "");
      const no = el("button", "hub-btn ghost", t("cancel")); no.type = "button"; no.onclick = () => { confirmTo = null; render(); };
      row.append(go, no); box.appendChild(row);
      return box;
    }
    if (state.location === "sparks" && !job.running) {
      box.appendChild(el("p", null, t("on_spark", { node: state.node })));
      box.appendChild(health());
      const urls = state.urls || {};
      if (Object.keys(urls).length) {
        box.appendChild(el("p", "muted", t("links")));
        const ul = el("ul", "placement-links");
        for (const [name, url] of Object.entries(urls)) {
          const li = el("li"); li.appendChild(el("b", null, `${name[0].toUpperCase()}${name.slice(1)}: `));
          const a = el("a", null, url.replace(/^https:\/\//, "")); a.href = url; a.target = "_blank"; a.rel = "noopener"; li.appendChild(a); ul.appendChild(li);
        }
        box.appendChild(ul);
      } else box.appendChild(el("p", "muted", t("no_links")));
      box.appendChild(el("p", "muted", t("paused"))); box.appendChild(list(state.paused_on_spark));
      const row = el("div", "placement-actions");
      const upd = el("button", "hub-btn ghost", t("update")); upd.type = "button"; upd.title = t("update_hint");
      upd.dataset.placementUpdate = "1"; upd.onclick = () => move("sparks");
      row.appendChild(upd); box.appendChild(row);
    }
    if (job.ok === false && !job.running) box.appendChild(el("p", "placement-error", `${t("failed")} ${job.error || ""}`));
    if ((job.log || []).length && (job.running || job.ok === false || state.location === "sparks")) {
      const det = el("details", "placement-log"); det.open = !!job.running;
      det.appendChild(el("summary", null, t("log")));
      const pre = el("pre"); pre.textContent = job.log.join("\n"); det.appendChild(pre);
      box.appendChild(det);
      setTimeout(() => { pre.scrollTop = pre.scrollHeight; }, 0);
    }
    return box.childElementCount ? box : null;
  }

  // Is each one actually up on the Spark? (its user service and its HTTP answer, asked by the Hub over SSH)
  function health() {
    const remote = state.remote || {};
    const wrap = el("div", "placement-health-box");
    if (remote.checked && remote.ok === false) {
      wrap.appendChild(el("p", "placement-error", `${t("h_unreachable", { node: state.node })} ${remote.error || ""}`));
      return wrap;
    }
    const ul = el("ul", "placement-health");
    for (const app of state.apps || []) {
      const h = (remote.apps || {})[app];
      const s = !remote.checked ? "starting" : h && h.healthy ? "running" : h && ["active", "activating", "reloading"].includes(h.service) ? "starting" : "down";
      const li = el("li", "is-" + s);
      li.dataset.app = app; li.dataset.state = s;
      li.title = h ? `systemd: ${h.service} · HTTP ${h.http}` : "";
      li.append(el("span", "hub-dot is-" + s), el("b", null, app[0].toUpperCase() + app.slice(1)),
                el("span", "muted", !remote.checked ? t("h_checking") : t("h_" + s)));
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
    if (remote.checked) wrap.appendChild(el("p", "muted placement-checked", t("h_checked", { s: Math.max(0, Math.round(Date.now() / 1000 - remote.checked)) })));
    return wrap;
  }

  // One block: toggle + panel. The Home tile and the System strip both use it.
  function controls() {
    const box = el("div", "placement-controls");
    if (!state) return box;
    box.appendChild(toggle());
    const p = panel(); if (p) box.appendChild(p);
    return box;
  }

  function render() {
    if (!state) return;
    const job = state.job || {};
    if (lastJobRunning && !job.running && window.hubToast) {
      window.hubToast(job.ok ? t(state.location === "sparks" ? "done_sparks" : "done_pc") : `${t("failed")} ${job.error || ""}`, job.ok ? "ok" : "err");
    }
    lastJobRunning = !!job.running;
    if (job.running) startPolling();
    const strip = document.getElementById("placement");
    if (strip) strip.replaceChildren(controls());
    document.querySelectorAll("[data-placement-slot]").forEach((slot) => slot.replaceChildren(controls()));
    document.querySelectorAll("[data-placement-arrow-slot]").forEach((slot) => slot.replaceChildren(arrow()));
  }

  window.HubPlacement = { state: () => state, controls, arrow };
  window.addEventListener("hub:snapshot", (ev) => {
    const snap = ev.detail && ev.detail.snapshot;
    if (snap && snap.placement && !polling) {
      state = snap.placement;
      if (confirmTo && !state.job?.running && document.activeElement?.closest(".placement-panel")) return;
      render();
    }
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fetchState); else fetchState();
})();

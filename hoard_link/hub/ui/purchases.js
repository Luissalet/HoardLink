/* Hoard Hub UI — purchases ("Compras" / "Purchases"): one row per purchase with the stage it has reached
   (paid -> shipped -> delivered -> filed -> stored) and chips linking to the record in each app. Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const ctx = H.ctx;
  const { el, api, toast, L, fmtWhen } = ctx;

  const STEPS = ["paid", "shipped", "delivered", "filed", "stored"];
  const S = {
    label: { es: "Compras", en: "Purchases" },
    paid: { es: "pagado", en: "paid" }, shipped: { es: "enviado", en: "shipped" }, delivered: { es: "entregado", en: "delivered" },
    filed: { es: "archivado", en: "filed" }, stored: { es: "guardado", en: "stored" }, closed: { es: "cerrado", en: "closed" },
    all: { es: "Todas", en: "All" },
    ph: { es: "Comercio, artículo o nº de pedido…", en: "Merchant, item or order no…" },
    none: { es: "Aún no hay compras. Aparecen solas cuando Ledger lee un pago o Phileas un envío.", en: "No purchases yet. They appear by themselves when Ledger reads a payment or Phileas a parcel." },
    close: { es: "Cerrar", en: "Close" }, reopen: { es: "Reabrir", en: "Reopen" },
    confirm_close: { es: "¿Dar esta compra por cerrada?", en: "Mark this purchase as closed?" },
    closed_ok: { es: "Compra cerrada", en: "Purchase closed" }, reopened: { es: "Compra reabierta", en: "Purchase reopened" },
    hint: { es: "El camino de cada compra por las apps.", en: "Each purchase's path through the apps." },
    order: { es: "pedido", en: "order" },
    c_ledger: { es: "Pago", en: "Payment" }, c_phileas: { es: "Envío", en: "Parcel" }, c_invoice: { es: "Factura", en: "Invoice" },
    c_receipt: { es: "Recibo", en: "Receipt" }, c_document: { es: "Documento", en: "Document" }, c_warranty: { es: "Garantía", en: "Warranty" },
    c_homehoard: { es: "Casa", en: "Home" }, c_tantalus: { es: "Vigilante", en: "Watcher" },
    until: { es: "hasta", en: "until" },
  };
  const t = (k) => L(S[k]);
  let box, list, stageSel, input, summary, badge;

  function injectCss() {
    if (document.getElementById("css-purchases")) return;
    const css = document.createElement("style");
    css.id = "css-purchases";
    css.textContent = `
      .pc-list { display: grid; gap: 6px; }
      .pc-row { display: grid; grid-template-columns: 1fr auto; gap: 6px 12px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); }
      .pc-row.closed { opacity: .6; }
      .pc-title { font-weight: 600; }
      .pc-sub { color: var(--muted); font-size: 12px; }
      .pc-track { display: flex; align-items: center; gap: 0; margin-top: 4px; grid-column: 1 / 3; }
      .pc-step { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--muted); }
      .pc-step i { width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--hoard-border-hover); background: transparent; display: inline-block; }
      .pc-step.done i { background: var(--green); border-color: var(--green); }
      .pc-step.now i { background: var(--accent); border-color: var(--accent); box-shadow: 0 0 8px var(--accent); }
      .pc-step.now { color: var(--text); font-weight: 600; }
      .pc-link { width: 26px; height: 2px; background: var(--line); margin: 0 6px; }
      .pc-link.done { background: var(--green); }
      .pc-chips { display: flex; gap: 6px; flex-wrap: wrap; grid-column: 1 / 3; }
      .pc-chips a.chip { color: var(--blue); text-decoration: none; }
      .pc-chips a.chip:hover { border-color: var(--hoard-border-hover); }
      .pc-actions { display: flex; gap: 6px; align-items: start; justify-content: end; }
    `;
    document.head.appendChild(css);
  }

  function money(p) { return p.amount == null ? "" : `${p.amount.toFixed(2)} ${p.currency || ""}`.trim(); }

  function row(p) {
    const r = el("div", "pc-row" + (p.stage === "closed" ? " closed" : ""));
    const left = el("div");
    left.appendChild(el("div", "pc-title", p.title));
    const bits = [p.merchant && p.merchant !== p.title ? p.merchant : "", money(p), p.date, p.order_ref ? `${t("order")} ${p.order_ref}` : ""].filter(Boolean);
    if (p.meta && p.meta.warranty_until) bits.push(`${t("c_warranty")} ${t("until")} ${p.meta.warranty_until}`);
    left.appendChild(el("div", "pc-sub", bits.join(" · ")));
    r.appendChild(left);
    const acts = el("div", "pc-actions");
    if (p.stage === "closed") {
      const b = el("button", "ghost small", t("reopen")); b.onclick = async () => { const x = await api(`/api/purchases/${p.id}/reopen`, {}); if (x.ok) { toast(t("reopened"), "ok"); load(); } else toast(x.error || "error", "err"); }; acts.appendChild(b);
    } else {
      const b = el("button", "ghost small", t("close")); b.onclick = async () => { if (!confirm(t("confirm_close"))) return; const x = await api(`/api/purchases/${p.id}/close`, {}); if (x.ok) { toast(t("closed_ok"), "ok"); load(); } else toast(x.error || "error", "err"); }; acts.appendChild(b);
    }
    r.appendChild(acts);
    const track = el("div", "pc-track");
    const at = STEPS.indexOf(p.stage);          // closed -> -1: every step greyed out
    const ms = p.milestones || null;             // what really happened (a filed invoice does not mean it arrived)
    const reached = (s, i) => ms ? !!ms[s] : i <= at;
    STEPS.forEach((s, i) => {
      if (i) track.appendChild(el("span", "pc-link" + (reached(s, i) && reached(STEPS[i - 1], i - 1) ? " done" : "")));
      const st = el("span", "pc-step" + (ms ? (ms[s] ? (i === at ? " now" : " done") : "") : (i < at ? " done" : (i === at ? " now" : ""))));
      st.appendChild(el("i")); st.appendChild(el("span", "", t(s)));
      track.appendChild(st);
    });
    if (p.stage === "closed") track.appendChild(el("span", "chip", t("closed")));
    r.appendChild(track);
    const keys = Object.keys(p.refs || {});
    if (keys.length) {
      const chips = el("div", "pc-chips");
      for (const k of keys) {
        const ref = p.refs[k];
        const label = `${S["c_" + k] ? t("c_" + k) : k} · ${ref.app}`;
        let c;
        if (ref.app_url) { c = el("a", "chip", label); c.href = ref.app_url; c.target = "_blank"; c.rel = "noopener"; } else c = el("span", "chip", label);
        c.title = ref.uri;
        chips.appendChild(c);
      }
      r.appendChild(chips);
    }
    return r;
  }

  async function load() {
    const q = `/api/purchases?limit=200${stageSel && stageSel.value ? "&stage=" + stageSel.value : ""}${input && input.value.trim() ? "&q=" + encodeURIComponent(input.value.trim()) : ""}`;
    const r = await api(q);
    if (!r.ok) return;
    list.innerHTML = "";
    if (!r.purchases.length) list.appendChild(el("div", "hint", t("none")));
    for (const p of r.purchases) list.appendChild(row(p));
    summary.textContent = STEPS.concat(["closed"]).map((s) => `${t(s)} ${r.counts[s] || 0}`).join(" · ");
    setBadge(r.counts || {});
  }

  function setBadge(counts) {
    const pending = ["paid", "shipped", "delivered"].reduce((n, s) => n + (counts[s] || 0), 0);
    if (!badge) badge = document.getElementById("purchases-badge");
    if (badge) { badge.textContent = pending > 0 ? String(pending) : ""; badge.classList.toggle("warn", pending > 0); }
  }
  async function pollBadge() { const r = await api("/api/purchases?limit=1"); if (r && r.ok) setBadge(r.counts || {}); }

  H.register("purchases", {
    placement: "tab",
    label: S.label,
    order: 80,
    mount(container) {
      injectCss();
      box = container;
      const tools = el("div", "tab-tools");
      stageSel = el("select"); for (const s of ["", ...STEPS, "closed"]) { const o = el("option", "", s ? t(s) : t("all")); o.value = s; stageSel.appendChild(o); }
      stageSel.onchange = load;
      input = el("input", "narrow"); input.type = "search"; input.placeholder = t("ph");
      let timer = null; input.oninput = () => { clearTimeout(timer); timer = setTimeout(load, 250); };
      summary = el("span", "hint");
      for (const x of [stageSel, input, summary]) tools.appendChild(x);
      box.appendChild(tools);
      list = el("div", "pc-list"); box.appendChild(list);
      pollBadge(); setInterval(pollBadge, 20000);
    },
    load() { if (input) input.placeholder = t("ph"); return load(); },
    tick: load,
  });
})();

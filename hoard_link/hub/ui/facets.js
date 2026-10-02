// Hoard Hub — facet loader (0.7).
// Asks /api/facets which UI scripts the facets ship, loads them in order, and gives them one small API:
//   HubFacets.register(id, {placement: "tab"|"top"|"header", label: {es, en}, mount(container, ctx), load(ctx), order})
// A "tab" facet gets a button in the family panel and a body next to Events/Rules/…; a "top" facet a section
// above the family panel (Today); a "header" facet a slot in the top bar (the sphere selector).
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
  const lang = () => { try { return localStorage.getItem("hub.lang") || ((navigator.language || "en").toLowerCase().startsWith("es") ? "es" : "en"); } catch (e) { return "en"; } };
  const L = (o) => (o && typeof o === "object") ? (o[lang()] ?? o.en ?? o.es ?? "") : String(o ?? "");
  async function api(path, body, method) {
    const opts = body === undefined && !method ? {} : { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) };
    const res = await fetch(path, opts);
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
  function fmtWhen(v) {
    if (!v) return "";
    const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
    if (isNaN(d)) return String(v);
    return d.toLocaleString(lang() === "es" ? "es-ES" : undefined, { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  const registry = [];
  const ctx = { $, el, api, toast, lang, L, fmtWhen, bus: new EventTarget() };
  window.HubFacets = {
    ctx,
    register(id, spec) { registry.push({ id, ...spec }); },
  };

  function mountTab(f) {
    const tabs = $("#family-tabs"); const body = $("#family-body");
    if (!tabs || !body) return;
    const btn = el("button", "tab"); btn.dataset.tab = f.id;
    const b = el("b", "", L(f.label)); btn.appendChild(b);
    const badge = el("span", "badge"); badge.id = `${f.id}-badge`; btn.appendChild(badge);
    tabs.appendChild(btn);
    const box = el("div", "tab-body facet-body"); box.id = `tab-${f.id}`; box.hidden = true;
    body.appendChild(box);
    f.mount && f.mount(box, ctx);
    btn.onclick = () => {
      if (window.hubShowTab) window.hubShowTab(f.id);
      document.querySelectorAll("#family-tabs .tab").forEach((x) => x.classList.toggle("on", x.dataset.tab === f.id));
      document.querySelectorAll("#family-body .tab-body").forEach((x) => { x.hidden = x.id !== `tab-${f.id}`; });
      try { localStorage.setItem("hub.ftab", f.id); } catch (e) { /* ignore */ }
      f.load && f.load(ctx);
    };
    f._btn = btn; f._label = b;
  }
  function mountTop(f) {
    const anchor = $("#family-panel") || $("main");
    const sec = el("section", "facet-top"); sec.id = `facet-${f.id}`;
    anchor.parentNode.insertBefore(sec, anchor);
    f.mount && f.mount(sec, ctx);
    f.load && f.load(ctx);
  }
  function mountHeader(f) {
    const bar = $(".top-actions") || $("header");
    const slot = el("span", "facet-slot"); slot.id = `facet-${f.id}`;
    bar.insertBefore(slot, bar.firstChild);
    f.mount && f.mount(slot, ctx);
    f.load && f.load(ctx);
  }
  function relabel() { for (const f of registry) if (f._label) f._label.textContent = L(f.label); }

  async function boot() {
    let info;
    try { info = await api("/api/facets"); } catch (e) { return; }
    if (!info || !info.ok) return;
    const scripts = [];
    for (const f of info.facets || []) for (const s of f.ui_scripts || []) if (!scripts.includes(s)) scripts.push(s);
    for (const s of scripts) {
      await new Promise((resolve) => { const tag = document.createElement("script"); tag.src = `/ui/${s}`; tag.onload = resolve; tag.onerror = resolve; document.body.appendChild(tag); });
    }
    registry.sort((a, b) => (a.order || 50) - (b.order || 50));
    for (const f of registry) {
      try {
        if (f.placement === "top") mountTop(f);
        else if (f.placement === "header") mountHeader(f);
        else mountTab(f);
      } catch (e) { console.error("facet", f.id, e); }
    }
    let saved = "";
    try { saved = localStorage.getItem("hub.ftab") || ""; } catch (e) { /* ignore */ }
    const mine = registry.find((f) => f.id === saved && f._btn);
    if (mine) mine._btn.click();
    const langBtn = $("#btn-lang");
    if (langBtn) langBtn.addEventListener("click", () => setTimeout(() => { relabel(); ctx.bus.dispatchEvent(new Event("lang")); for (const f of registry) if (f.placement !== "tab" || (f._btn && f._btn.classList.contains("on"))) f.load && f.load(ctx); }, 0));
    // facets poll on their own clock (ctx.every); one shared timer keeps it cheap
    setInterval(() => { for (const f of registry) if (f.tick && (f.placement !== "tab" || (f._btn && f._btn.classList.contains("on")))) { try { f.tick(ctx); } catch (e) { /* ignore */ } } }, 5000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();

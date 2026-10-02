// Hoard Hub — Mail tab (facet "mailgate"): the family's single inbox pass.
// Status line (accounts → sphere, last and next pass, "Leer ahora"), then three lists: what needs you, mail nobody claimed
// ("sin dueño", with category chips) and recent mail (who claimed each one). Filtered by sphere (defaults to the active one).
(() => {
  "use strict";
  if (!window.HubFacets) return;
  const { $, el, api, toast, L, fmtWhen, bus } = window.HubFacets.ctx;

  const T = {
    title: { es: "Correo", en: "Mail" },
    on: { es: "Activar lectura", en: "Turn reading on" }, off: { es: "Desactivar", en: "Turn off" },
    now: { es: "Leer ahora", en: "Read now" }, check: { es: "Comprobar cuentas", en: "Check accounts" },
    disabled: { es: "La lectura del correo está desactivada: el hub no lee nada.", en: "Mail reading is off: the hub reads nothing." },
    not_configured: { es: "No encuentro la carpeta de Faustus (hub.json → faustus_dir, o HOARD_FAUSTUS_DIR).", en: "Faustus folder not found (hub.json faustus_dir, or HOARD_FAUSTUS_DIR)." },
    accounts: { es: "Cuentas", en: "Accounts" }, no_accounts: { es: "sin cuentas leídas todavía", en: "no accounts read yet" },
    last: { es: "Última lectura", en: "Last pass" }, next: { es: "Próxima", en: "Next" }, never: { es: "nunca", en: "never" },
    new_n: { es: "nuevos", en: "new" }, running: { es: "leyendo…", en: "reading…" }, manual: { es: "solo manual", en: "manual only" },
    sphere: { es: "Esfera", en: "Sphere" }, all: { es: "Todas", en: "All" },
    attention: { es: "Necesita tu atención", en: "Needs you" }, unclaimed: { es: "Sin dueño", en: "Unclaimed" }, recent: { es: "Recientes", en: "Recent" },
    nothing: { es: "Nada por aquí.", en: "Nothing here." },
    dismiss: { es: "Descartar", en: "Dismiss" }, dismiss_all: { es: "Descartar la categoría", en: "Dismiss category" }, open: { es: "Abrir", en: "Open" },
    close: { es: "Cerrar", en: "Close" }, confirm_all: { es: "¿Descartar todos los de esta categoría?", en: "Dismiss every one in this category?" },
    settings: { es: "Ajustes", en: "Settings" }, interval: { es: "Cada (min, 0 = solo manual)", en: "Every (min, 0 = manual only)" },
    retention: { es: "Guardar el texto (días)", en: "Keep the text (days)" }, owner: { es: "Usuario de Faustus (si hay varios)", en: "Faustus user (when several)" },
    save: { es: "Guardar", en: "Save" }, saved: { es: "guardado", en: "saved" },
    pass_ok: (r) => ({ es: `${r.new} nuevos de ${r.seen} leídos`, en: `${r.new} new of ${r.seen} read` }),
    claimed_by: { es: "reclamado por", en: "claimed by" }, attachments: { es: "Adjuntos", en: "Attachments" }, links: { es: "Enlaces", en: "Links" },
    c_all: { es: "todo", en: "all" }, c_promo: { es: "promo", en: "promo" }, c_social: { es: "social", en: "social" },
    c_security: { es: "seguridad", en: "security" }, c_dev: { es: "dev", en: "dev" }, c_other: { es: "otros", en: "other" },
    interests: { es: "Apps con interés registrado", en: "Apps with a registered interest" },
    sent_to: { es: "para", en: "for" }, why: { es: "por qué", en: "why" },
  };
  const t = (k, ...a) => { const v = T[k]; return L(typeof v === "function" ? v(...a) : v || { es: k, en: k }); };

  if (!document.getElementById("css-mailgate")) {
    const st = document.createElement("style"); st.id = "css-mailgate";
    st.textContent = `
      .mg-status { display: flex; gap: 8px 14px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; font-size: 12.5px; }
      .mg-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 12px; align-items: start; }
      .mg-col h4 { margin: 0 0 6px; font-size: 13px; display: flex; gap: 8px; align-items: center; }
      .mg-row { padding: 7px 9px; border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); display: grid; gap: 3px; }
      .mg-row + .mg-row { margin-top: 6px; }
      .mg-row.attention { border-left: 3px solid var(--amber); }
      .mg-top { display: flex; gap: 8px; justify-content: space-between; align-items: baseline; }
      .mg-subject { font-weight: 600; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .mg-meta { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .mg-snippet { color: var(--muted); font-size: 12px; max-height: 2.6em; overflow: hidden; }
      .mg-chips { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
      .mg-chips .chip { padding: 1px 8px; font-size: 11.5px; }
      .mg-detail { margin-top: 4px; padding: 6px 8px; border-radius: 8px; background: var(--bg-2); font-size: 12.5px; }
      .mg-detail pre { white-space: pre-wrap; word-break: break-word; margin: 0 0 6px; font-family: inherit; max-height: 280px; overflow: auto; }
      .mg-detail a { color: var(--blue); }
      .mg-act { display: flex; gap: 6px; flex-wrap: wrap; }
      .mg-form { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; margin: 6px 0 10px; }
      .mg-form label { display: grid; gap: 2px; font-size: 12px; color: var(--muted); }
      .mg-form input { width: 130px; }
    `;
    document.head.appendChild(st);
  }

  let box, statusEl, formEl, colsEl, sphereSel, cat = "all", lastSig = "", lastStatus = null, lastActive = "";
  const open = new Set();

  const ago = (ts) => {
    if (!ts) return t("never");
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return L({ es: "hace un momento", en: "just now" });
    if (s < 5400) return L({ es: `hace ${Math.round(s / 60)} min`, en: `${Math.round(s / 60)} min ago` });
    if (s < 129600) return L({ es: `hace ${Math.round(s / 3600)} h`, en: `${Math.round(s / 3600)} h ago` });
    return fmtWhen(ts);
  };
  const inFuture = (ts) => {
    const s = ts - Date.now() / 1000;
    if (s <= 0) return L({ es: "en breve", en: "soon" });
    return s < 5400 ? L({ es: `en ${Math.max(1, Math.round(s / 60))} min`, en: `in ${Math.max(1, Math.round(s / 60))} min` }) : fmtWhen(ts);
  };
  const sphereId = () => (sphereSel && sphereSel.value) || "";
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v !== undefined && v !== null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const chip = (text, cls = "") => el("span", `chip ${cls}`.trim(), text);

  function mount(container) {
    box = container;
    const tools = el("div", "tab-tools");
    const sphereLabel = el("span", "hint", t("sphere") + ":");
    tools.appendChild(sphereLabel);
    sphereSel = el("select", "small");
    sphereSel.onchange = () => { lastSig = ""; loadLists(); };
    tools.appendChild(sphereSel);
    const settingsBtn = el("button", "ghost small", t("settings"));
    settingsBtn.onclick = () => { formEl.hidden = !formEl.hidden; };
    tools.appendChild(settingsBtn);
    box.appendChild(tools);
    statusEl = el("div", "mg-status"); box.appendChild(statusEl);
    formEl = el("div", "mg-form"); formEl.hidden = true; box.appendChild(formEl);
    colsEl = el("div", "mg-cols"); box.appendChild(colsEl);
    bus.addEventListener("sphere", () => { followActive(); });
    bus.addEventListener("lang", () => {
      sphereLabel.textContent = t("sphere") + ":"; settingsBtn.textContent = t("settings");
      loadSpheres().then(() => { lastSig = ""; renderStatus(); buildForm(); loadLists(); });
    });
  }

  async function loadSpheres() {
    let spheres = [], active = "";
    try { const r = await api("/api/spheres"); if (r && r.ok) { spheres = r.spheres || []; active = r.active || ""; } } catch (e) { /* the spheres facet may be absent */ }
    const keep = sphereSel.value;
    sphereSel.innerHTML = "";
    const all = el("option", "", t("all")); all.value = ""; sphereSel.appendChild(all);
    for (const s of spheres) { const o = el("option", "", L(s.name) || s.id); o.value = s.id; sphereSel.appendChild(o); }
    if (!spheres.length) { const o = el("option", "", "personal"); o.value = "personal"; sphereSel.appendChild(o); }
    sphereSel.value = keep && [...sphereSel.options].some((o) => o.value === keep) ? keep : (active || document.documentElement.dataset.sphere || "");
    if (![...sphereSel.options].some((o) => o.value === sphereSel.value)) sphereSel.value = "";
    lastActive = active;
  }
  function followActive() {
    const a = document.documentElement.dataset.sphere || "";
    if (a && [...sphereSel.options].some((o) => o.value === a)) { sphereSel.value = a; lastSig = ""; loadLists(); }
  }

  function renderStatus() {
    const s = lastStatus; statusEl.innerHTML = "";
    if (!s) return;
    const toggle = el("button", s.enabled ? "ghost small" : "primary small", s.enabled ? t("off") : t("on"));
    toggle.onclick = async () => {
      const r = await api("/api/mail/config", { enabled: !s.enabled });
      toast(r.ok ? (r.config.enabled ? t("on") + " ✓" : t("off") + " ✓") : r.error, r.ok ? "ok" : "err");
      await loadStatus(true);
    };
    statusEl.appendChild(toggle);
    if (s.enabled) {
      const now = el("button", "small", s.running ? t("running") : t("now")); now.disabled = !!s.running;
      now.onclick = async () => {
        now.disabled = true; now.textContent = t("running");
        const r = await api("/api/mail/fetch", {});
        toast(r.ok ? t("pass_ok", r) : (r.error || "error"), r.ok ? "ok" : "err");
        lastSig = ""; await loadStatus(true); loadLists();
      };
      statusEl.appendChild(now);
    }
    const chk = el("button", "ghost small", t("check"));
    chk.onclick = async () => { chk.disabled = true; await loadStatus(true, true); chk.disabled = false; };
    statusEl.appendChild(chk);
    if (!s.enabled) statusEl.appendChild(el("span", "hint", t("disabled")));
    else if (!s.configured) statusEl.appendChild(el("span", "chip bad", t("not_configured")));
    statusEl.appendChild(el("span", "hint", t("accounts") + ":"));
    if (!(s.accounts || []).length) statusEl.appendChild(el("span", "hint", t("no_accounts")));
    for (const a of s.accounts || []) statusEl.appendChild(chip(`${a.account} → ${a.sphere}`, "info"));
    const lp = s.last_pass;
    const last = el("span", "hint");
    last.textContent = `${t("last")}: ${lp ? ago(lp.ts) + (lp.ok ? ` · ${L(T.pass_ok(lp))}` : "") : t("never")}`;
    statusEl.appendChild(last);
    if (lp && !lp.ok && lp.error) statusEl.appendChild(chip(lp.error, "bad"));
    if (s.enabled) statusEl.appendChild(el("span", "hint", `${t("next")}: ${s.next_pass_ts ? inFuture(s.next_pass_ts) : t("manual")}`));
    const badge = document.getElementById("mailgate-badge");
    if (badge) { const n = (s.counts || {}).attention || 0; badge.textContent = n ? String(n) : ""; badge.className = "badge" + (n ? " warn" : ""); }
  }

  function buildForm() {
    formEl.innerHTML = "";
    if (!lastStatus) return;
    api("/api/mail/config").then((r) => {
      if (!r || !r.ok) return;
      const c = r.config; formEl.innerHTML = "";
      const mk = (label, key, type, width) => {
        const inp = el("input"); inp.type = type; inp.value = c[key]; if (width) inp.style.width = width;
        const lab = el("label", "", label); lab.appendChild(inp); formEl.appendChild(lab); return inp;
      };
      const iv = mk(t("interval"), "interval_min", "number"), rt = mk(t("retention"), "retention_days", "number"), ow = mk(t("owner"), "owner", "text", "180px");
      const save = el("button", "primary small", t("save"));
      save.onclick = async () => {
        const res = await api("/api/mail/config", { interval_min: Number(iv.value), retention_days: Number(rt.value), owner: ow.value });
        toast(res.ok ? t("saved") : res.error, res.ok ? "ok" : "err"); loadStatus(true);
      };
      formEl.appendChild(save);
    }).catch(() => {});
  }

  async function loadStatus(force, refresh) {
    const r = await api("/api/mail/status" + (refresh ? "?refresh=1" : ""));
    if (!r || !r.ok) { statusEl.textContent = (r && r.error) || "error"; return; }
    const changed = !lastStatus || r.enabled !== lastStatus.enabled || (r.counts || {}).total !== (lastStatus.counts || {}).total ||
      JSON.stringify(r.last_pass) !== JSON.stringify(lastStatus.last_pass);
    lastStatus = r; renderStatus();
    if (force || changed) { lastSig = ""; if (!formEl.hidden || !formEl.children.length) buildForm(); }
    return changed;
  }

  function detailBox(msg) {
    const d = el("div", "mg-detail");
    d.appendChild(el("pre", "", msg.text || msg.snippet || "(…)"));
    if ((msg.links || []).length) {
      d.appendChild(el("div", "hint", t("links")));
      for (const l of msg.links.slice(0, 8)) { const a = el("a", "", l.label || l.url); a.href = l.url; a.target = "_blank"; a.rel = "noopener noreferrer"; d.appendChild(a); d.appendChild(document.createElement("br")); }
    }
    if ((msg.attachments || []).length) {
      d.appendChild(el("div", "hint", t("attachments")));
      for (const a of msg.attachments) {
        if (a.url) { const l = el("a", "", `${a.name} (${Math.round((a.size || 0) / 1024)} KB)`); l.href = a.url; l.target = "_blank"; d.appendChild(l); }
        else d.appendChild(el("span", "", a.name));
        d.appendChild(document.createElement("br"));
      }
    }
    if ((msg.reasons || []).length) d.appendChild(el("div", "hint", `${t("why")}: ${msg.reasons.join(", ")}`));
    return d;
  }

  function row(m, opts) {
    const r = el("div", "mg-row" + (m.priority === "attention" ? " attention" : ""));
    const top = el("div", "mg-top");
    const subj = el("div", "mg-subject", m.subject || "(sin asunto)"); subj.title = m.subject;
    top.appendChild(subj);
    top.appendChild(el("span", "hint", ago(m.date_ts)));
    r.appendChild(top);
    r.appendChild(el("div", "mg-meta", `${m.from_name ? m.from_name + " · " : ""}${m.from_addr} · ${m.sphere}`));
    if (m.snippet) r.appendChild(el("div", "mg-snippet", m.snippet));
    const chips = el("div", "mg-chips");
    if (opts.category && m.category) chips.appendChild(chip(t("c_" + m.category), "info"));
    for (const why of m.reasons || []) if (m.priority === "attention") chips.appendChild(chip(why, "warn"));
    for (const c of m.claims || []) chips.appendChild(chip(`${c.app}${c.kind ? " · " + c.kind : ""}`, "on"));
    if (m.n_attachments) chips.appendChild(chip(`📎 ${m.n_attachments}`));
    if (chips.children.length) r.appendChild(chips);
    const acts = el("div", "mg-act");
    const detail = el("div"); detail.hidden = true;
    const openBtn = el("button", "ghost small", t("open"));
    const toggle = async () => {
      if (!detail.hidden) { detail.hidden = true; openBtn.textContent = t("open"); open.delete(m.id); return; }
      const full = await api(`/api/mail/messages/${m.id}`);
      detail.innerHTML = ""; if (full.ok) detail.appendChild(detailBox(full.message)); else detail.textContent = full.error;
      detail.hidden = false; openBtn.textContent = t("close"); open.add(m.id);
    };
    openBtn.onclick = toggle; subj.onclick = toggle;
    acts.appendChild(openBtn);
    if (opts.dismiss) {
      const dis = el("button", "ghost small", t("dismiss"));
      dis.onclick = async () => { const res = await api("/api/mail/dismiss", { ids: [m.id] }); if (res.ok) { lastSig = ""; loadStatus(true); loadLists(); } else toast(res.error, "err"); };
      acts.appendChild(dis);
    }
    r.appendChild(acts); r.appendChild(detail);
    if (open.has(m.id)) toggle();
    return r;
  }

  function column(title, count, rows, extra) {
    const col = el("div", "mg-col");
    const h = el("h4", "", title); h.appendChild(el("span", "badge", String(count))); col.appendChild(h);
    if (extra) col.appendChild(extra);
    if (!rows.length) col.appendChild(el("div", "hint", t("nothing")));
    for (const r of rows) col.appendChild(r);
    return col;
  }

  async function loadLists() {
    const sp = sphereId();
    const [att, un, rec, ints] = await Promise.all([
      api("/api/mail/attention?" + qs({ sphere: sp, days: 7, limit: 40 })),
      api("/api/mail/unclaimed?" + qs({ sphere: sp, days: 7, limit: 100 })),
      api("/api/mail/messages?" + qs({ sphere: sp, kind: "mail", limit: 40 })),
      api("/api/mail/interests"),
    ]);
    const sig = JSON.stringify([att.messages, un.messages, rec.messages, cat, (ints.interests || []).map((i) => i.app)]);
    if (sig === lastSig) return;
    lastSig = sig;
    colsEl.innerHTML = "";
    colsEl.appendChild(column(t("attention"), (att.messages || []).length, (att.messages || []).map((m) => row(m, { dismiss: true }))));
    // unclaimed with category chips
    const all = un.messages || [];
    const counts = { all: all.length };
    for (const m of all) counts[m.category] = (counts[m.category] || 0) + 1;
    const bar = el("div", "mg-chips");
    for (const c of ["all", "promo", "social", "security", "dev", "other"]) {
      if (c !== "all" && !counts[c]) continue;
      const b = el("button", "chip btn" + (cat === c ? " on" : ""), `${t("c_" + c)} ${counts[c] || 0}`);
      b.onclick = () => { cat = c; lastSig = ""; loadLists(); };
      bar.appendChild(b);
    }
    const shown = all.filter((m) => cat === "all" || m.category === cat);
    if (cat !== "all" && shown.length) {
      const dis = el("button", "ghost small", t("dismiss_all"));
      dis.onclick = async () => {
        if (!confirm(t("confirm_all"))) return;
        const res = await api("/api/mail/dismiss", { ids: shown.map((m) => m.id) });
        if (res.ok) { lastSig = ""; loadStatus(true); loadLists(); } else toast(res.error, "err");
      };
      bar.appendChild(dis);
    }
    colsEl.appendChild(column(t("unclaimed"), all.length, shown.map((m) => row(m, { dismiss: true, category: true })), bar));
    const recent = column(t("recent"), (rec.messages || []).length, (rec.messages || []).map((m) => row(m, {})));
    if ((ints.interests || []).length) recent.appendChild(el("div", "hint", `${t("interests")}: ${ints.interests.map((i) => i.app).join(", ")}`));
    colsEl.appendChild(recent);
  }

  async function load() {
    if (!sphereSel.options.length) await loadSpheres();
    await loadStatus(true);
    await loadLists();
  }

  window.HubFacets.register("mailgate", {
    placement: "tab", label: T.title, order: 40, mount,
    load: () => load().catch((e) => console.error("mail tab", e)),
    tick: async () => {
      try {
        const a = document.documentElement.dataset.sphere || "";
        if (a && a !== lastActive) { lastActive = a; followActive(); }
        const changed = await loadStatus(false);
        if (changed) await loadLists();
      } catch (e) { /* the hub is restarting */ }
    },
  });
})();

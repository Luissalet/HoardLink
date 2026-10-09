/* Hoard Hub UI — "Sesiones de agente" / "Agent sessions".
   What the agents wrote in the family apps (their write journals), grouped by agent and session, with "Deshacer sesión"
   (a dry run first, then the confirmation) and the minting / revoking of the per-agent tokens of each app.
   Spanish and English; text goes in through textContent only. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const { el, api, toast, L, fmtWhen } = H.ctx;

  const S = {
    label: { es: "Sesiones de agente", en: "Agent sessions" },
    refresh: { es: "Actualizar", en: "Refresh" },
    filter: { es: "agente, app, sesión o razón…", en: "agent, app, session or reason…" },
    hint: { es: "Lo que han escrito los agentes en las apps que llevan diario. Cada fila es una sesión: puedes deshacerla entera.",
            en: "What agents wrote in the apps that keep a journal. Each row is a session: you can undo all of it." },
    none: { es: "Todavía no hay escrituras de agentes en ninguna app.", en: "No agent writes in any app yet." },
    no_agent: { es: "(agente sin identificar)", en: "(unidentified agent)" },
    no_session: { es: "(sin sesión)", en: "(no session)" },
    writes: { es: "escrituras", en: "writes" },
    failed: { es: "fallidas", en: "failed" },
    undoable: { es: "se pueden deshacer", en: "can be undone" },
    undone: { es: "deshechas", en: "undone" },
    detail: { es: "Detalle", en: "Detail" },
    undo: { es: "Deshacer sesión", en: "Undo session" },
    checking: { es: "Comprobando qué se puede deshacer…", en: "Checking what can be undone…" },
    will_undo: { es: "Se deshará", en: "Will be undone" },
    cannot: { es: "No se puede deshacer", en: "Cannot be undone" },
    conflicts: { es: "En conflicto (alguien tocó lo mismo después)", en: "Conflicts (someone touched the same thing later)" },
    nothing: { es: "No hay nada que deshacer en esta sesión.", en: "There is nothing to undo in this session." },
    confirm: { es: "Confirmar y deshacer", en: "Confirm and undo" },
    cancel: { es: "Cancelar", en: "Cancel" },
    done: { es: "Sesión deshecha", en: "Session undone" },
    partial: { es: "Deshecho en parte: quedan conflictos o cosas sin deshacer", en: "Partly undone: conflicts or irreversible writes remain" },
    apps: { es: "Apps", en: "Apps" },
    st_ok: { es: "con diario", en: "journal on" },
    st_not_adopted: { es: "sin diario todavía", en: "no journal yet" },
    st_down: { es: "apagada", en: "not running" },
    st_unauthorized: { es: "token rechazado", en: "token refused" },
    tokens: { es: "Tokens por agente", en: "Per-agent tokens" },
    tokens_hint: { es: "Un token con perfil limita lo que puede hacer un agente en esa app: solo leer, leer y escribir borradores, o todo. Solo se enseña al crearlo.",
                   en: "A token with a profile limits what an agent can do in that app: read only, read and write drafts, or everything. It is shown only when minted." },
    agent: { es: "Agente", en: "Agent" },
    profile: { es: "Perfil", en: "Profile" },
    label_ph: { es: "nota (opcional)", en: "note (optional)" },
    mint: { es: "Crear token", en: "Mint token" },
    revoke: { es: "Revocar", en: "Revoke" },
    revoke_q: { es: "¿Revocar este token? El agente dejará de poder usarlo.", en: "Revoke this token? The agent will stop being able to use it." },
    revoked: { es: "Token revocado", en: "Token revoked" },
    minted: { es: "Token creado. Cópialo ahora: no se vuelve a mostrar.", en: "Token minted. Copy it now: it is not shown again." },
    copy: { es: "Copiar", en: "Copy" },
    copied: { es: "Copiado", en: "Copied" },
    p_read_only: { es: "solo lectura", en: "read only" },
    p_drafts: { es: "borradores", en: "drafts" },
    p_all: { es: "todo", en: "everything" },
    no_tokens: { es: "Sin tokens de agente.", en: "No agent tokens." },
    unreachable: { es: "No se pudo consultar el Hub.", en: "Could not reach the Hub." },
    reason: { es: "Razón", en: "Reason" },
    why: { es: "con", en: "with" },
  };
  const t = (k) => L(S[k]);
  const PROFILES = ["read_only", "drafts", "all"];
  let box, listEl, appsEl, tokensEl, filterEl, data = null, tokenData = null, lastTokenShown = null;
  const open = new Set();         // session keys with their detail open
  const panels = new Map();       // session key -> undo preview state

  function injectCss() {
    if (document.getElementById("css-agent-sessions")) return;
    const css = document.createElement("style");
    css.id = "css-agent-sessions";
    css.textContent = `
      .as-apps { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; align-items: center; }
      .as-chip { padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); background: var(--card-2); font-size: 11.5px; color: var(--muted); }
      .as-chip.ok { color: var(--green); }
      .as-agent { margin: 12px 0 4px; display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
      .as-agent b { font-size: 14px; }
      .as-sess { border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); padding: 8px 10px; margin-bottom: 6px; display: grid; gap: 4px; min-width: 0; }
      .as-head { display: flex; gap: 8px 12px; align-items: baseline; flex-wrap: wrap; min-width: 0; }
      .as-id { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
      .as-meta { color: var(--muted); font-size: 12px; }
      .as-actions { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; }
      .as-reasons { font-size: 12.5px; color: var(--text); overflow-wrap: anywhere; }
      .as-entry { display: grid; grid-template-columns: auto auto 1fr; gap: 2px 10px; font-size: 12px; padding: 3px 0; border-top: 1px solid var(--line); min-width: 0; }
      .as-entry .as-sum { color: var(--muted); font-family: var(--mono); font-size: 11.5px; overflow-wrap: anywhere; grid-column: 1 / -1; }
      .as-entry.bad .as-tool { color: var(--red); } .as-entry.undone { opacity: .55; }
      .as-plan { border: 1px solid var(--accent); border-radius: 10px; padding: 8px 10px; display: grid; gap: 6px; background: var(--card); }
      .as-plan h4 { margin: 4px 0 0; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }
      .as-plan ul { margin: 0; padding-left: 18px; font-size: 12.5px; overflow-wrap: anywhere; }
      .as-plan .conf { color: var(--amber); }
      .as-tok { display: grid; grid-template-columns: auto 1fr auto auto; gap: 8px; align-items: baseline; padding: 4px 0; border-top: 1px solid var(--line); font-size: 12.5px; }
      .as-form { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin: 8px 0; }
      .as-form input, .as-form select { background: var(--hoard-sunken); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font: inherit; min-width: 0; }
      .as-token { font-family: var(--mono); width: min(100%, 460px); }
      @media (max-width: 600px) { .as-actions { margin-left: 0; } .as-tok { grid-template-columns: 1fr auto; } }
    `;
    document.head.appendChild(css);
  }

  const agentName = (a) => a || t("no_agent");
  const sessionName = (s) => s || t("no_session");
  const matches = (s, q) => !q || [s.agent, s.app, s.app_name, s.session, ...(s.reasons || []), ...Object.keys(s.tools || {})].join(" ").toLowerCase().includes(q);

  function toolsLine(s) {
    return Object.entries(s.tools).sort((a, b) => b[1] - a[1]).map(([k, n]) => (n > 1 ? `${k} ×${n}` : k)).join(" · ");
  }

  function entryRow(e) {
    const row = el("div", "as-entry" + (e.ok ? "" : " bad") + (e.undone ? " undone" : ""));
    row.appendChild(el("span", "as-meta", fmtWhen(e.ts)));
    row.appendChild(el("span", "as-tool", e.tool + (e.undone ? " ↺" : "")));
    row.appendChild(el("span", "", e.reason || (e.error ? e.error : "")));
    if (e.summary) row.appendChild(el("span", "as-sum", e.summary));
    return row;
  }

  function renderPlan(s, plan, dry) {
    const wrap = el("div", "as-plan");
    const undoable = plan.would_undo || [];
    const conflicts = plan.conflicts || [];
    const stuck = plan.not_undoable || [];
    if (!undoable.length && !conflicts.length && !stuck.length) wrap.appendChild(el("div", "", t("nothing")));
    const section = (title, items, line, cls) => {
      if (!items.length) return;
      wrap.appendChild(el("h4", "", `${title} (${items.length})`));
      const ul = el("ul", cls || "");
      for (const it of items) ul.appendChild(el("li", "", line(it)));
      wrap.appendChild(ul);
    };
    section(t("will_undo"), undoable, (i) => `${i.tool}${i.summary ? " — " + i.summary : ""}`);
    section(t("conflicts"), conflicts, (i) => `${i.tool}: ${i.with ? `${agentName(i.with.agent)} / ${sessionName(i.with.session)} (${i.with.tool})` : i.message || i.reason}`, "conf");
    section(t("cannot"), stuck, (i) => `${i.tool}: ${i.reason}${i.message ? " — " + i.message : ""}`);
    const bar = el("div", "as-actions");
    bar.style.marginLeft = "0";
    if (undoable.length) {
      const go = el("button", "primary small", t("confirm"));
      go.onclick = async () => {
        go.disabled = true;
        const r = await api("/api/agent-sessions/undo", { app: s.app, session: s.session, agent: s.agent || undefined, dry_run: false, confirm: true });
        if (!r.ok) { toast(r.error || t("unreachable"), "err", r.hint || ""); go.disabled = false; return; }
        toast(r.complete ? t("done") : t("partial"), r.complete ? "info" : "err",
          `${(r.undone || []).length} ✓ · ${(r.conflicts || []).length} ⚠ · ${(r.not_undoable || []).length} ✗`);
        panels.delete(s.key);
        await load(true);
      };
      bar.appendChild(go);
    }
    const no = el("button", "ghost small", t("cancel"));
    no.onclick = () => { panels.delete(s.key); draw(); };
    bar.appendChild(no);
    wrap.appendChild(bar);
    return wrap;
  }

  function sessionCard(s) {
    const card = el("div", "as-sess");
    const head = el("div", "as-head");
    head.appendChild(el("strong", "", s.app_name));
    head.appendChild(el("span", "as-id", sessionName(s.session)));
    const counts = [`${s.writes} ${t("writes")}`];
    if (s.failed) counts.push(`${s.failed} ${t("failed")}`);
    if (s.undone) counts.push(`${s.undone} ${t("undone")}`);
    if (s.undoable) counts.push(`${s.undoable} ${t("undoable")}`);
    head.appendChild(el("span", "as-meta", counts.join(" · ") + " · " + fmtWhen(s.last_ts)));
    const actions = el("span", "as-actions");
    const det = el("button", "ghost small", t("detail"));
    det.onclick = () => { open.has(s.key) ? open.delete(s.key) : open.add(s.key); draw(); };
    actions.appendChild(det);
    if (s.can_undo) {
      const undo = el("button", "small danger", t("undo"));
      undo.onclick = async () => {
        undo.disabled = true;
        panels.set(s.key, { loading: true }); draw();
        const r = await api("/api/agent-sessions/undo", { app: s.app, session: s.session, agent: s.agent || undefined, dry_run: true });
        if (!r.ok) { panels.delete(s.key); toast(r.error || t("unreachable"), "err", r.hint || ""); draw(); return; }
        panels.set(s.key, { plan: r }); draw();
      };
      actions.appendChild(undo);
    }
    head.appendChild(actions);
    card.appendChild(head);
    if (s.reasons.length) card.appendChild(el("div", "as-reasons", s.reasons.join(" · ")));
    card.appendChild(el("div", "as-meta", toolsLine(s)));
    const panel = panels.get(s.key);
    if (panel) card.appendChild(panel.loading ? el("div", "as-meta", t("checking")) : renderPlan(s, panel.plan, true));
    if (open.has(s.key)) for (const e of s.entries) card.appendChild(entryRow(e));
    return card;
  }

  function draw() {
    if (!listEl) return;
    listEl.replaceChildren();
    appsEl.replaceChildren();
    if (!data) return;
    appsEl.appendChild(el("span", "as-meta", t("apps") + ":"));
    for (const a of data.apps) appsEl.appendChild(el("span", "as-chip" + (a.state === "ok" ? " ok" : ""), `${a.name} · ${t("st_" + a.state)}`));
    const q = (filterEl.value || "").trim().toLowerCase();
    let shown = 0;
    for (const a of data.agents) {
      const sessions = a.sessions.filter((s) => matches(s, q));
      if (!sessions.length) continue;
      const head = el("div", "as-agent");
      head.appendChild(el("b", "", agentName(a.agent)));
      head.appendChild(el("span", "as-meta", `${sessions.length} · ${a.writes} ${t("writes")}`));
      listEl.appendChild(head);
      for (const s of sessions) { listEl.appendChild(sessionCard(s)); shown++; }
    }
    if (!shown) listEl.appendChild(el("p", "hint", t("none")));
    const badge = document.getElementById("agent_sessions-badge");
    if (badge) badge.textContent = data.sessions.some((s) => s.undoable) ? String(data.sessions.filter((s) => s.undoable).length) : "";
  }

  async function load(refresh) {
    const r = await api("/api/agent-sessions" + (refresh ? "?refresh=1" : ""));
    if (!r || !r.ok) { listEl.replaceChildren(el("p", "hint", (r && r.error) || t("unreachable"))); return; }
    data = r;
    draw();
    if (tokensEl.open) loadTokens();
  }

  // ---- tokens
  async function loadTokens() {
    const r = await api("/api/agent-sessions/tokens");
    if (!r || !r.ok) return;
    tokenData = r;
    drawTokens();
  }

  function drawTokens() {
    const body = tokensEl.querySelector(".as-tokens-body");
    body.replaceChildren();
    body.appendChild(el("p", "hint", t("tokens_hint")));
    const adopted = (tokenData ? tokenData.apps : []).filter((a) => !a.state);
    const form = el("form", "as-form");
    const appSel = el("select"); appSel.setAttribute("aria-label", t("apps"));
    for (const a of adopted) { const o = el("option", "", a.name); o.value = a.app; appSel.appendChild(o); }
    const agent = el("input"); agent.placeholder = t("agent"); agent.required = true; agent.maxLength = 80; agent.setAttribute("aria-label", t("agent"));
    const prof = el("select"); prof.setAttribute("aria-label", t("profile"));
    for (const p of PROFILES) { const o = el("option", "", t("p_" + p)); o.value = p; if (p === "drafts") o.selected = true; prof.appendChild(o); }
    const label = el("input"); label.placeholder = t("label_ph"); label.maxLength = 120;
    const go = el("button", "primary small", t("mint")); go.type = "submit";
    form.append(appSel, agent, prof, label, go);
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const r = await api("/api/agent-sessions/tokens", { app: appSel.value, agent: agent.value.trim(), profile: prof.value, label: label.value.trim() });
      if (!r.ok) { toast(r.error || t("unreachable"), "err"); return; }
      lastTokenShown = { app: appSel.value, token: r.token };
      toast(t("minted"), "info");
      await loadTokens();
    };
    if (!adopted.length) { form.hidden = true; }
    body.appendChild(form);
    if (lastTokenShown) {
      const row = el("div", "as-form");
      const field = el("input", "as-token"); field.readOnly = true; field.value = lastTokenShown.token; field.setAttribute("aria-label", "token");
      field.onfocus = () => field.select();
      const copy = el("button", "small", t("copy"));
      copy.onclick = async () => { try { await navigator.clipboard.writeText(field.value); toast(t("copied")); } catch (e) { field.select(); } };
      const close = el("button", "ghost small", "✕"); close.setAttribute("aria-label", t("cancel"));
      close.onclick = () => { lastTokenShown = null; drawTokens(); };
      row.append(el("strong", "", lastTokenShown.app), field, copy, close);
      body.appendChild(row);
    }
    let any = false;
    for (const a of adopted) for (const tk of a.tokens) {
      any = true;
      const row = el("div", "as-tok");
      row.appendChild(el("strong", "", a.name));
      row.appendChild(el("span", "", `${tk.agent} · ${t("p_" + tk.profile)}${tk.label ? " · " + tk.label : ""}`));
      row.appendChild(el("span", "as-meta", fmtWhen(tk.created)));
      const rev = el("button", "ghost small danger", t("revoke"));
      rev.onclick = async () => {
        if (!window.confirm(t("revoke_q"))) return;
        const r = await api("/api/agent-sessions/tokens/revoke", { app: a.app, id: tk.id });
        if (!r.ok) { toast(r.error || t("unreachable"), "err"); return; }
        toast(t("revoked"));
        await loadTokens();
      };
      row.appendChild(rev);
      body.appendChild(row);
    }
    if (!any) body.appendChild(el("p", "hint", t("no_tokens")));
  }

  H.register("agent_sessions", {
    placement: "tab", label: S.label, order: 16,
    mount(container) {
      injectCss();
      box = container;
      const bar = el("div", "tab-tools");
      const refresh = el("button", "primary small", t("refresh"));
      refresh.onclick = () => load(true);
      filterEl = el("input"); filterEl.type = "search"; filterEl.placeholder = t("filter"); filterEl.className = "narrow"; filterEl.setAttribute("aria-label", t("filter"));
      filterEl.oninput = draw;
      bar.append(refresh, filterEl, el("span", "hint", t("hint")));
      box.appendChild(bar);
      appsEl = el("div", "as-apps");
      box.appendChild(appsEl);
      listEl = el("div", "as-list");
      box.appendChild(listEl);
      tokensEl = el("details", "as-tokens");
      tokensEl.appendChild(el("summary", "", t("tokens")));
      tokensEl.appendChild(el("div", "as-tokens-body"));
      tokensEl.addEventListener("toggle", () => { if (tokensEl.open) loadTokens(); });
      box.appendChild(tokensEl);
    },
    load: () => load(false),
    tick: () => { if (!panels.size) load(false); },
  });
})();

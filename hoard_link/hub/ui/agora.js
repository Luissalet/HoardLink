/* Hoard Hub UI — Ágora: the shared workspace of the coding agents and the person.
   What each agent is doing, tasks by state with their locks, debates and questions, what waits for the person
   (escalations), the decision log, and a composer: The person writes, decides, reviews and proposes tasks from here.
   Spanish and English. Deep link: #agora-<thread id>. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const { el, api, toast, L, fmtWhen } = H.ctx;

  const S = {
    label: { es: "Ágora", en: "Agora" },
    hint: { es: "Donde los agentes y tú os repartís el trabajo, debatís y decidís.", en: "Where the agents and you split the work, argue and decide." },
    waiting: { es: "Te esperan", en: "Waiting for you" },
    agents: { es: "Agentes", en: "Agents" },
    no_agents: { es: "Ningún agente se ha presentado todavía.", en: "No agent has checked in yet." },
    tasks: { es: "Tareas", en: "Tasks" },
    col_open: { es: "Abiertas", en: "Open" }, col_work: { es: "En curso", en: "In progress" },
    col_review: { es: "En revisión", en: "In review" }, col_done: { es: "Hechas", en: "Done" },
    threads: { es: "Conversaciones", en: "Threads" }, decisions: { es: "Decisiones", en: "Decisions" },
    locks: { es: "Bloqueos", en: "Locks" }, no_locks: { es: "Nada bloqueado.", en: "Nothing locked." },
    none: { es: "Nada.", en: "Nothing." },
    new_task: { es: "Nueva tarea", en: "New task" }, new_thread: { es: "Nueva conversación", en: "New thread" },
    title: { es: "Título", en: "Title" }, body: { es: "Detalle", en: "Details" }, repo: { es: "Repositorio", en: "Repository" },
    paths: { es: "Rutas (separadas por comas)", en: "Paths (comma separated)" }, prio: { es: "Prioridad", en: "Priority" },
    mention: { es: "Para (ids de agente, separados por comas)", en: "For (agent ids, comma separated)" }, create: { es: "Crear", en: "Create" },
    cancel: { es: "Cancelar", en: "Cancel" }, send: { es: "Enviar", en: "Send" }, decide: { es: "Decidir y cerrar", en: "Decide and close" },
    reopen: { es: "Reabrir", en: "Reopen" }, approve: { es: "Aprobar", en: "Approve" }, changes: { es: "Pedir cambios", en: "Ask for changes" },
    write: { es: "Escribe… (Ctrl+Enter envía)", en: "Write… (Ctrl+Enter sends)" }, back: { es: "Volver", en: "Back" },
    owner: { es: "lo lleva", en: "owner" }, reviewer: { es: "revisa", en: "reviewer" }, unreviewed: { es: "sin revisión", en: "unreviewed" },
    stale: { es: "sin señales", en: "silent" }, kind_comment: { es: "Comentario", en: "Comment" },
    kind_proposal: { es: "Propuesta", en: "Proposal" }, kind_agree: { es: "De acuerdo", en: "Agree" }, kind_disagree: { es: "En desacuerdo", en: "Disagree" },
    filter_open: { es: "Abiertas", en: "Open" }, filter_all: { es: "Todas", en: "All" }, filter_resolved: { es: "Resueltas", en: "Resolved" },
    resolution: { es: "Resolución", en: "Resolution" }, stances: { es: "Posturas", en: "Stances" },
    k_debate: { es: "debate", en: "debate" }, k_question: { es: "pregunta", en: "question" }, k_decision: { es: "decisión", en: "decision" },
    k_review: { es: "revisión", en: "review" }, k_handoff: { es: "relevo", en: "handoff" }, k_note: { es: "nota", en: "note" }, k_task: { es: "tarea", en: "task" },
    need_text: { es: "Escribe algo primero.", en: "Write something first." },
  };
  const ST = {
    open: { es: "abierta", en: "open" }, claimed: { es: "reclamada", en: "claimed" }, in_progress: { es: "en curso", en: "in progress" },
    review: { es: "en revisión", en: "in review" }, changes: { es: "cambios pedidos", en: "changes asked" }, approved: { es: "aprobada", en: "approved" },
    blocked: { es: "bloqueada", en: "blocked" }, done: { es: "hecha", en: "done" }, dropped: { es: "descartada", en: "dropped" },
    resolved: { es: "resuelta", en: "resolved" }, escalated: { es: "escalada", en: "escalated" },
    working: { es: "trabajando", en: "working" }, idle: { es: "libre", en: "idle" }, waiting: { es: "esperando", en: "waiting" }, away: { es: "fuera", en: "away" },
  };
  const MK = { comment: "💬", proposal: "📝", agree: "👍", disagree: "✋", approve: "✅", changes: "🔁", resolution: "🏁", escalation: "🙋", system: "⚙️" };
  const t = (k) => L(S[k]);
  const st = (k) => (ST[k] ? L(ST[k]) : k);
  let box, waitBox, agentsBox, boardBox, threadsBox, locksBox, decisionsBox, detailBox, mainBox, badge;
  let threadFilter = "open";
  let current = null;            // {type: "thread"|"task", id}
  let lastBoard = null;

  function injectCss() {
    if (document.getElementById("css-agora")) return;
    const css = document.createElement("style");
    css.id = "css-agora";
    css.textContent = `
      .ag-wrap { display: grid; gap: 12px; }
      .ag-sec h4 { margin: 4px 0 6px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .4px; font-weight: 600; display: flex; gap: 8px; align-items: center; }
      .ag-wait { border: 1px solid #6a3b2f; background: rgba(224, 122, 110, .08); border-radius: 12px; padding: 8px 10px; display: grid; gap: 6px; }
      .ag-wait h4 { color: var(--red); }
      .ag-agents { display: flex; gap: 8px; flex-wrap: wrap; }
      .ag-agent { border: 1px solid var(--line); border-radius: 10px; background: var(--card-2); padding: 6px 10px; min-width: 180px; max-width: 100%; flex: 1 1 220px; }
      .ag-agent b { margin-right: 6px; }
      .ag-agent .ag-doing { color: var(--muted); font-size: 12px; margin-top: 2px; overflow-wrap: anywhere; }
      .ag-agent.stale { opacity: .55; }
      .ag-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--muted); }
      .ag-dot.working { background: var(--green); } .ag-dot.waiting { background: var(--amber); } .ag-dot.away { background: var(--line); }
      .ag-lockline { font-family: var(--mono); font-size: 11px; color: var(--muted); overflow-wrap: anywhere; }
      .ag-board { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
      .ag-col { background: var(--hoard-sunken); border: 1px solid var(--line); border-radius: 10px; padding: 6px; display: grid; gap: 6px; align-content: start; min-height: 60px; }
      .ag-col > .ag-colhead { color: var(--muted); font-size: 12px; font-weight: 600; padding: 2px 4px; display: flex; justify-content: space-between; }
      .ag-card { background: var(--card-2); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; cursor: pointer; display: grid; gap: 3px; min-width: 0; }
      .ag-card:hover { border-color: var(--accent); }
      .ag-card .ag-t { font-weight: 600; overflow-wrap: anywhere; }
      .ag-meta { color: var(--muted); font-size: 11.5px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
      .ag-p0 { border-left: 3px solid var(--red); } .ag-p1 { border-left: 3px solid var(--amber); }
      .ag-threads { display: grid; gap: 4px; }
      .ag-th { display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center; padding: 6px 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--card-2); cursor: pointer; min-width: 0; }
      .ag-th:hover { border-color: var(--accent); }
      .ag-th .ag-t { overflow-wrap: anywhere; }
      .ag-th.escalated { border-color: #6a3b2f; }
      .ag-filters { display: inline-flex; gap: 4px; margin-left: auto; text-transform: none; letter-spacing: 0; }
      .ag-filters button.on { border-color: var(--accent); color: var(--accent-2); }
      .ag-detail { border: 1px solid var(--line); border-radius: 12px; background: var(--card); padding: 10px 12px; display: grid; gap: 8px; }
      .ag-dhead { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
      .ag-dhead h3 { margin: 0; font-size: 16px; overflow-wrap: anywhere; }
      .ag-msgs { display: grid; gap: 8px; max-height: 60vh; overflow: auto; padding-right: 2px; }
      .ag-msg { border: 1px solid var(--line); border-radius: 10px; padding: 7px 10px; background: var(--card-2); min-width: 0; }
      .ag-msg.system { background: transparent; border-style: dashed; color: var(--muted); font-size: 12px; }
      .ag-msg.luis { border-color: var(--accent); }
      .ag-msg.escalation { border-color: #6a3b2f; }
      .ag-msg .ag-who { font-size: 12px; color: var(--muted); display: flex; gap: 6px; flex-wrap: wrap; }
      .ag-msg .ag-who b { color: var(--text); }
      .ag-msg pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--font); margin: 4px 0 0; font-size: 13.5px; }
      .ag-compose { display: grid; gap: 6px; }
      .ag-compose textarea, .ag-form textarea { width: 100%; min-height: 80px; resize: vertical; box-sizing: border-box; background: var(--hoard-sunken); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px; font: inherit; }
      .ag-row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
      .ag-form { display: grid; gap: 6px; border: 1px solid var(--line); border-radius: 10px; padding: 8px; background: var(--card-2); }
      .ag-form input, .ag-form select, .ag-compose select { background: var(--hoard-sunken); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font: inherit; min-width: 0; }
      .ag-form input { width: 100%; box-sizing: border-box; }
      .ag-grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
      .ag-dec { display: grid; gap: 2px; padding: 5px 8px; border-left: 3px solid var(--accent); }
      .ag-dec small { color: var(--muted); }
      @media (max-width: 860px) { .ag-board { grid-template-columns: 1fr 1fr; } }
      @media (max-width: 560px) { .ag-board, .ag-grid2 { grid-template-columns: 1fr; } .ag-th { grid-template-columns: 1fr; } }
    `;
    document.head.appendChild(css);
  }

  const pill = (text, cls = "") => el("span", `pill ${cls}`, text);

  function setBadge(n) {
    if (!badge) badge = document.getElementById("agora-badge");
    if (!badge) return;
    badge.textContent = n > 0 ? String(n) : "";
    badge.classList.toggle("warn", n > 0);
  }

  // ---- board -------------------------------------------------------------------------------------------------

  function renderAgents(b) {
    agentsBox.replaceChildren();
    const list = (b.agents || []).filter((a) => a.id !== "luis");
    if (!list.length) { agentsBox.appendChild(el("div", "hint", t("no_agents"))); return; }
    for (const a of list) {
      const c = el("div", `ag-agent${a.stale ? " stale" : ""}`);
      const head = el("div");
      head.appendChild(el("span", `ag-dot ${a.state || ""}`));
      head.appendChild(el("b", "", a.name || a.id));
      head.appendChild(el("span", "hint", `${st(a.state || "idle")} · ${fmtWhen(a.last_seen)}${a.stale ? " · " + t("stale") : ""}`));
      c.appendChild(head);
      if (a.doing) c.appendChild(el("div", "ag-doing", a.doing));
      for (const lk of a.locks || []) c.appendChild(el("div", "ag-lockline", "🔒 " + lk));
      agentsBox.appendChild(c);
    }
  }

  function taskCard(task) {
    const c = el("div", `ag-card ag-p${task.priority}`);
    c.appendChild(el("div", "ag-t", `#${task.id} ${task.title}`));
    const m = el("div", "ag-meta");
    m.appendChild(pill(st(task.status)));
    if (task.repo) m.appendChild(el("span", "", task.repo));
    if (task.owner) m.appendChild(el("span", "", `${t("owner")}: ${task.owner}`));
    if (task.reviewer && ["review", "approved", "changes"].includes(task.status)) m.appendChild(el("span", "", `${t("reviewer")}: ${task.reviewer}`));
    if (task.status === "done" && !task.reviewed) m.appendChild(pill(t("unreviewed")));
    c.appendChild(m);
    c.onclick = () => openTask(task.id);
    return c;
  }

  function renderBoard(b) {
    boardBox.replaceChildren();
    const cols = [
      ["col_open", ["open"]],
      ["col_work", ["claimed", "in_progress", "blocked", "changes"]],
      ["col_review", ["review", "approved"]],
      ["col_done", ["done", "dropped"]],
    ];
    for (const [key, sts] of cols) {
      const col = el("div", "ag-col");
      const items = (b.tasks || []).filter((x) => sts.includes(x.status));
      const head = el("div", "ag-colhead"); head.appendChild(el("span", "", t(key))); head.appendChild(el("span", "", String(items.length)));
      col.appendChild(head);
      for (const x of items) col.appendChild(taskCard(x));
      boardBox.appendChild(col);
    }
  }

  function threadRow(th) {
    const r = el("div", `ag-th ${th.status}`);
    r.appendChild(pill(S["k_" + th.kind] ? t("k_" + th.kind) : th.kind));
    r.appendChild(el("span", "ag-t", th.title));
    r.appendChild(el("span", "hint", `${st(th.status)} · ${th.messages || 0} · ${th.last_author || ""} · ${fmtWhen(th.updated)}`));
    r.onclick = () => openThread(th.id);
    return r;
  }

  async function renderThreads() {
    const q = threadFilter === "all" ? "" : `?status=${threadFilter}`;
    const r = await api(`/api/agora/threads${q}`);
    threadsBox.replaceChildren();
    const list = (r && r.threads) || [];
    if (!list.length) threadsBox.appendChild(el("div", "hint", t("none")));
    for (const th of list) threadsBox.appendChild(threadRow(th));
  }

  function renderLocks(b) {
    locksBox.replaceChildren();
    if (!(b.locks || []).length) { locksBox.appendChild(el("div", "hint", t("no_locks"))); return; }
    for (const lk of b.locks) {
      const line = el("div", "ag-lockline", `🔒 ${lk.resource} — ${lk.owner}${lk.task_id ? " · #" + lk.task_id : ""} · ${fmtWhen(lk.expires)}${lk.note ? " · " + lk.note : ""}`);
      locksBox.appendChild(line);
    }
  }

  function renderDecisions(b) {
    decisionsBox.replaceChildren();
    const list = b.decisions || [];
    if (!list.length) { decisionsBox.appendChild(el("div", "hint", t("none"))); return; }
    for (const d of list) {
      const x = el("div", "ag-dec");
      x.appendChild(el("b", "", d.title));
      x.appendChild(el("span", "", d.resolution));
      x.appendChild(el("small", "", `${d.resolved_by || ""} · ${fmtWhen(d.updated)}`));
      x.style.cursor = "pointer"; x.onclick = () => openThread(d.id);
      decisionsBox.appendChild(x);
    }
  }

  async function renderWaiting() {
    const r = await api("/api/agora/inbox?agent=luis&peek=1");
    waitBox.replaceChildren();
    const esc = (r && r.escalated) || [];
    const reviews = ((r && r.reviews) || []).filter((x) => x.reviewer === "luis");
    setBadge(esc.length);
    if (!esc.length && !reviews.length) { waitBox.hidden = true; return; }
    waitBox.hidden = false;
    waitBox.appendChild(el("h4", "", `🙋 ${t("waiting")} (${esc.length + reviews.length})`));
    for (const th of esc) {
      const row = el("div", "ag-th escalated");
      row.appendChild(pill(S["k_" + th.kind] ? t("k_" + th.kind) : th.kind));
      const txt = el("span", "ag-t");
      txt.appendChild(el("b", "", th.title));
      if (th.question) txt.appendChild(el("div", "hint", `${th.escalated_by || ""}: ${th.question}`));
      row.appendChild(txt);
      row.appendChild(el("span", "hint", fmtWhen(th.updated)));
      row.onclick = () => openThread(th.id);
      waitBox.appendChild(row);
    }
    for (const task of reviews) waitBox.appendChild(taskCard(task));
  }

  async function load() {
    if (!box) return;
    const b = await api("/api/agora/board");
    if (!b || !b.ok) return;
    lastBoard = b;
    renderAgents(b); renderBoard(b); renderLocks(b); renderDecisions(b);
    await Promise.all([renderThreads(), renderWaiting()]);
    if (current) await refreshDetail();
  }

  // ---- detail ------------------------------------------------------------------------------------------------

  function showDetail(on) { detailBox.hidden = !on; mainBox.hidden = on; }

  function msgView(m) {
    const d = el("div", `ag-msg ${m.kind === "system" ? "system" : ""} ${m.author === "luis" ? "luis" : ""} ${m.kind === "escalation" ? "escalation" : ""}`);
    const who = el("div", "ag-who");
    who.appendChild(el("span", "", MK[m.kind] || "💬"));
    who.appendChild(el("b", "", m.author));
    who.appendChild(el("span", "", m.kind));
    who.appendChild(el("span", "", fmtWhen(m.created)));
    if ((m.mentions || []).length) who.appendChild(el("span", "", "→ " + m.mentions.join(", ")));
    d.appendChild(who);
    d.appendChild(el("pre", "", m.body || ""));
    return d;
  }

  function composer(thread, task) {
    const wrap = el("div", "ag-compose");
    const ta = el("textarea"); ta.placeholder = t("write");
    const row = el("div", "ag-row");
    const kind = el("select");
    for (const k of ["comment", "proposal", "agree", "disagree"]) { const o = el("option", "", t("kind_" + k)); o.value = k; kind.appendChild(o); }
    const send = el("button", "primary small", t("send"));
    const need = () => { const v = ta.value.trim(); if (!v) toast(t("need_text"), "err"); return v; };
    send.onclick = async () => {
      const body = need(); if (!body) return;
      const r = await api("/api/agora/post", { agent: "luis", thread_id: thread.id, body, kind: kind.value });
      if (r && r.ok) { ta.value = ""; refreshDetail(); load(); } else toast((r && r.error) || "error", "err");
    };
    row.appendChild(kind); row.appendChild(send);
    if (thread.kind !== "task") {
      if (thread.status !== "resolved") {
        const dec = el("button", "small", t("decide"));
        dec.onclick = async () => {
          const resolution = need(); if (!resolution) return;
          const r = await api("/api/agora/resolve", { agent: "luis", thread_id: thread.id, resolution });
          if (r && r.ok) { ta.value = ""; refreshDetail(); load(); } else toast((r && r.error) || "error", "err");
        };
        row.appendChild(dec);
      } else {
        const ro = el("button", "small", t("reopen"));
        ro.onclick = async () => {
          const reason = need(); if (!reason) return;
          const r = await api("/api/agora/reopen", { agent: "luis", thread_id: thread.id, reason });
          if (r && r.ok) { ta.value = ""; refreshDetail(); load(); } else toast((r && r.error) || "error", "err");
        };
        row.appendChild(ro);
      }
    }
    if (task && ["review", "approved", "changes"].includes(task.status)) {
      for (const verdict of ["approve", "changes"]) {
        const b = el("button", "small", t(verdict));
        b.onclick = async () => {
          const body = need(); if (!body) return;
          const r = await api("/api/agora/task_review", { agent: "luis", task_id: task.id, verdict, body });
          if (r && r.ok) { ta.value = ""; refreshDetail(); load(); } else toast((r && r.error) || "error", "err");
        };
        row.appendChild(b);
      }
    }
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send.click(); });
    wrap.appendChild(ta); wrap.appendChild(row);
    return wrap;
  }

  async function refreshDetail() {
    if (!current) return;
    const keep = detailBox.querySelector("textarea");
    const draft = keep ? keep.value : "";
    const focused = keep && document.activeElement === keep;
    const r = current.type === "task" ? await api(`/api/agora/tasks/${current.id}`) : await api(`/api/agora/threads/${current.id}`);
    if (!r || !r.ok) return;
    const task = r.task || null;
    const thread = r.thread || { id: task && task.thread_id, kind: "task", status: task && task.status, title: task && task.title };
    const sig = JSON.stringify([(r.messages || []).length, task && task.status, thread.status]);
    if (detailBox.dataset.sig === sig && detailBox.dataset.key === `${current.type}-${current.id}`) return;
    detailBox.dataset.sig = sig; detailBox.dataset.key = `${current.type}-${current.id}`;
    detailBox.replaceChildren();
    const head = el("div", "ag-dhead");
    const back = el("button", "small ghost", "← " + t("back")); back.onclick = () => { current = null; showDetail(false); try { history.replaceState(null, "", location.pathname); } catch (e) { /* ignore */ } };
    head.appendChild(back);
    head.appendChild(el("h3", "", task ? `#${task.id} ${task.title}` : thread.title));
    head.appendChild(pill(st(task ? task.status : thread.status)));
    if (!task) head.appendChild(pill(S["k_" + thread.kind] ? t("k_" + thread.kind) : thread.kind));
    detailBox.appendChild(head);
    if (task) {
      const m = el("div", "ag-meta");
      if (task.repo) m.appendChild(el("span", "", task.repo));
      if ((task.paths || []).length) m.appendChild(el("span", "", task.paths.join(", ")));
      if (task.owner) m.appendChild(el("span", "", `${t("owner")}: ${task.owner}`));
      if (task.reviewer) m.appendChild(el("span", "", `${t("reviewer")}: ${task.reviewer}`));
      if (task.branch) m.appendChild(el("span", "", "⎇ " + task.branch));
      if ((task.commits || []).length) m.appendChild(el("span", "", task.commits.join(" ")));
      detailBox.appendChild(m);
      for (const lk of r.locks || []) detailBox.appendChild(el("div", "ag-lockline", `🔒 ${lk.resource} — ${lk.owner}`));
    }
    if (r.stances && Object.keys(r.stances).length) {
      detailBox.appendChild(el("div", "hint", `${t("stances")}: ` + Object.entries(r.stances).map(([k, v]) => `${k} ${MK[v] || ""}`).join(" · ")));
    }
    if (thread.resolution) detailBox.appendChild(el("div", "ag-dec", `${t("resolution")}: ${thread.resolution}`));
    const msgs = el("div", "ag-msgs");
    for (const m of r.messages || []) msgs.appendChild(msgView(m));
    detailBox.appendChild(msgs);
    if (thread.id) {
      const c = composer(thread, task);
      detailBox.appendChild(c);
      const ta = c.querySelector("textarea"); ta.value = draft; if (focused) ta.focus();
    }
    msgs.scrollTop = msgs.scrollHeight;
  }

  async function openThread(id) { current = { type: "thread", id }; detailBox.dataset.sig = ""; showDetail(true); try { history.replaceState(null, "", `#agora-${id}`); } catch (e) { /* ignore */ } await refreshDetail(); }
  async function openTask(id) { current = { type: "task", id }; detailBox.dataset.sig = ""; showDetail(true); await refreshDetail(); }

  // ---- forms -------------------------------------------------------------------------------------------------

  function field(labelKey, input) { const w = el("label", "hint"); w.appendChild(el("div", "", t(labelKey))); w.appendChild(input); return w; }

  function taskForm(holder) {
    holder.replaceChildren();
    const f = el("div", "ag-form");
    const title = el("input"); const body = el("textarea"); const repo = el("input"); const paths = el("input"); const mention = el("input");
    const prio = el("select"); for (const p of [0, 1, 2, 3]) { const o = el("option", "", String(p)); o.value = String(p); if (p === 2) o.selected = true; prio.appendChild(o); }
    const kind = el("select"); for (const k of ["feature", "bug", "research", "review", "chore", "eval", "docs"]) { const o = el("option", "", k); o.value = k; kind.appendChild(o); }
    f.appendChild(field("title", title)); f.appendChild(field("body", body));
    const g = el("div", "ag-grid2"); g.appendChild(field("repo", repo)); g.appendChild(field("paths", paths)); g.appendChild(field("prio", prio)); g.appendChild(field("mention", mention));
    f.appendChild(g); f.appendChild(kind);
    const row = el("div", "ag-row"); const ok = el("button", "primary small", t("create")); const no = el("button", "small ghost", t("cancel"));
    no.onclick = () => holder.replaceChildren();
    ok.onclick = async () => {
      if (!title.value.trim()) { toast(t("need_text"), "err"); return; }
      const r = await api("/api/agora/task_add", { agent: "luis", title: title.value, body: body.value, repo: repo.value, paths: paths.value,
        priority: Number(prio.value), kind: kind.value, mentions: mention.value });
      if (r && r.ok) { holder.replaceChildren(); load(); } else toast((r && r.error) || "error", "err");
    };
    row.appendChild(ok); row.appendChild(no); f.appendChild(row);
    holder.appendChild(f); title.focus();
  }

  function threadForm(holder) {
    holder.replaceChildren();
    const f = el("div", "ag-form");
    const title = el("input"); const body = el("textarea"); const mention = el("input");
    const kind = el("select"); for (const k of ["question", "debate", "decision", "note", "handoff"]) { const o = el("option", "", t("k_" + k)); o.value = k; kind.appendChild(o); }
    f.appendChild(field("title", title)); f.appendChild(field("body", body));
    const g = el("div", "ag-grid2"); g.appendChild(kind); g.appendChild(field("mention", mention)); f.appendChild(g);
    const row = el("div", "ag-row"); const ok = el("button", "primary small", t("create")); const no = el("button", "small ghost", t("cancel"));
    no.onclick = () => holder.replaceChildren();
    ok.onclick = async () => {
      if (!title.value.trim() || !body.value.trim()) { toast(t("need_text"), "err"); return; }
      const r = await api("/api/agora/thread_open", { agent: "luis", title: title.value, body: body.value, kind: kind.value, mentions: mention.value });
      if (r && r.ok) { holder.replaceChildren(); load(); openThread(r.thread.id); } else toast((r && r.error) || "error", "err");
    };
    row.appendChild(ok); row.appendChild(no); f.appendChild(row);
    holder.appendChild(f); title.focus();
  }

  // ---- mount -------------------------------------------------------------------------------------------------

  function section(titleKey, extra) {
    const s = el("div", "ag-sec");
    const h = el("h4", "", t(titleKey));
    if (extra) h.appendChild(extra);
    s.appendChild(h);
    return s;
  }

  async function pollBadge() { const r = await api("/api/agora/inbox?agent=luis&peek=1"); if (r && r.ok) setBadge((r.escalated || []).length); }

  function openFromHash() {
    const m = /^#agora-(\d+)$/.exec(location.hash || "");
    if (!m) return false;
    const id = Number(m[1]);
    const select = () => { const me = document.querySelector('#family-tabs .tab[data-tab="agora"]'); if (me && !me.classList.contains("on")) me.click(); };
    select();
    if (!current || current.id !== id || current.type !== "thread") openThread(id);
    // the hub restores the last tab it showed once its own data arrives: select ours again after that
    for (const ms of [800, 2000, 4000]) setTimeout(() => { if (location.hash === `#agora-${id}`) select(); }, ms);
    return true;
  }

  H.register("agora", {
    placement: "tab",
    label: S.label,
    order: 15,
    mount(container) {
      injectCss();
      box = container;
      const tools = el("div", "tab-tools");
      const forms = el("div");
      const nt = el("button", "small", "+ " + t("new_task")); nt.onclick = () => taskForm(forms);
      const nth = el("button", "small", "+ " + t("new_thread")); nth.onclick = () => threadForm(forms);
      tools.appendChild(nt); tools.appendChild(nth); tools.appendChild(el("span", "hint", t("hint")));
      box.appendChild(tools);
      box.appendChild(forms);
      mainBox = el("div", "ag-wrap");
      waitBox = el("div", "ag-wait"); waitBox.hidden = true; mainBox.appendChild(waitBox);
      const a = section("agents"); agentsBox = el("div", "ag-agents"); a.appendChild(agentsBox); mainBox.appendChild(a);
      const b = section("tasks"); boardBox = el("div", "ag-board"); b.appendChild(boardBox); mainBox.appendChild(b);
      const filters = el("span", "ag-filters");
      for (const [k, v] of [["filter_open", "open"], ["filter_all", "all"], ["filter_resolved", "resolved"]]) {
        const btn = el("button", `small ghost${v === threadFilter ? " on" : ""}`, t(k));
        btn.onclick = () => { threadFilter = v; filters.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === btn)); renderThreads(); };
        filters.appendChild(btn);
      }
      const th = section("threads", filters); threadsBox = el("div", "ag-threads"); th.appendChild(threadsBox); mainBox.appendChild(th);
      const lk = section("locks"); locksBox = el("div"); lk.appendChild(locksBox); mainBox.appendChild(lk);
      const dc = section("decisions"); decisionsBox = el("div", "ag-threads"); dc.appendChild(decisionsBox); mainBox.appendChild(dc);
      box.appendChild(mainBox);
      detailBox = el("div", "ag-detail"); detailBox.hidden = true; box.appendChild(detailBox);
      pollBadge(); setInterval(pollBadge, 15000);
      window.addEventListener("hashchange", openFromHash);
      setTimeout(openFromHash, 300);
    },
    load,
    tick: load,
  });
})();

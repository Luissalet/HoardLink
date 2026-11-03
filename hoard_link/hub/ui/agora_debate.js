/* Hoard Hub UI — Ágora debates (companion to agora.js, like agora_blocks.js).
   Paints, inside a thread or task conversation, the findings board of its debates: columns by status
   (proposed / challenged / verified / rejected), the consensus (who verifies what), the frozen snapshot, and the
   person's controls: open a debate, file a finding, verify / challenge / reject one, close the debate (outcome into
   the decision log, tasks from the adopted findings). Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H || !H.ctx) return;
  const { el, toast, L, fmtWhen } = H.ctx;

  const S = {
    debates: { es: "Debates", en: "Debates" },
    open_debate: { es: "Abrir debate", en: "Open debate" },
    title: { es: "Título (por defecto, el del hilo)", en: "Title (defaults to the thread's)" },
    question: { es: "Pregunta (por defecto, el primer mensaje)", en: "Question (defaults to the first message)" },
    participants: { es: "Participantes (ids separados por comas)", en: "Participants (comma-separated ids)" },
    files: { es: "Ficheros enlazados, uno por línea (ruta#L10-L20)", en: "Linked files, one per line (path#L10-L20)" },
    commits: { es: "Commits (separados por comas)", en: "Commits (comma-separated)" },
    start: { es: "Empezar debate", en: "Start debate" },
    col_proposed: { es: "Propuestos", en: "Proposed" }, col_challenged: { es: "Cuestionados", en: "Challenged" },
    col_verified: { es: "Verificados", en: "Verified" }, col_rejected: { es: "Rechazados", en: "Rejected" },
    open: { es: "abierto", en: "open" }, closed: { es: "cerrado", en: "closed" },
    snapshot: { es: "Instantánea congelada", en: "Frozen snapshot" },
    consensus: { es: "Consenso", en: "Consensus" },
    verify: { es: "Verificar", en: "Verify" }, challenge: { es: "Cuestionar", en: "Challenge" },
    reject: { es: "Rechazar", en: "Reject" }, note: { es: "Nota (qué has comprobado o qué falla)", en: "Note (what you checked or what is wrong)" },
    need_note: { es: "Escribe una nota para cuestionar o rechazar.", en: "Write a note to challenge or reject." },
    verified_by: { es: "verifican", en: "verified by" }, challenged_by: { es: "cuestionan", en: "challenged by" },
    rejected_by: { es: "rechazan", en: "rejected by" }, evidence: { es: "Evidencia", en: "Evidence" },
    add_finding: { es: "Añadir hallazgo", en: "Add finding" }, claim: { es: "Afirmación", en: "Claim" },
    evidence_lines: { es: "Evidencia, una por línea (ruta#L1-L9, commit, URL, $ comando)", en: "Evidence, one per line (path#L1-L9, commit, URL, $ command)" },
    file_it: { es: "Archivar hallazgo", en: "File finding" },
    close: { es: "Cerrar debate", en: "Close debate" }, outcome: { es: "Resultado (entra en el registro de decisiones)", en: "Outcome (goes into the decision log)" },
    spawn: { es: "Crear tareas de los hallazgos verificados con adopt/adapt", en: "Open tasks from verified adopt/adapt findings" },
    close_it: { es: "Cerrar y registrar decisión", en: "Close and record decision" },
    outcome_label: { es: "Resultado", en: "Outcome" }, decision: { es: "Decisión en el hilo", en: "Decision in thread" },
    tasks: { es: "Tareas", en: "Tasks" }, none: { es: "Sin hallazgos todavía.", en: "No findings yet." },
    d_adopt: { es: "adoptar", en: "adopt" }, d_adapt: { es: "adaptar", en: "adapt" }, d_drop: { es: "descartar", en: "drop" },
    d_needs_luis: { es: "decide Luis", en: "needs Luis" }, round: { es: "ronda", en: "round" },
    filed: { es: "archivados", en: "filed" }, done: { es: "Hecho", en: "Done" },
    files_n: { es: "fichero(s)", en: "file(s)" }, commits_n: { es: "commit(s)", en: "commit(s)" },
    notes_n: { es: "nota(s)", en: "note(s)" },
  };
  const t = (k) => L(S[k] || { es: k, en: k });
  const STATUSES = ["proposed", "challenged", "verified", "rejected"];
  const DISPOSITIONS = ["adopt", "adapt", "drop", "needs_luis"];
  const cache = new Map();      // detail key -> last debates payload (repaint without flicker)
  const drafts = new Map();     // draft id -> text typed by the person (survives detail rebuilds)
  const inflight = new Set();
  const formsOpen = new Set();

  function injectCss() {
    if (document.getElementById("css-agora-debate")) return;
    const css = document.createElement("style");
    css.id = "css-agora-debate";
    css.textContent = `
      .ag-debates { display: grid; gap: 10px; margin: 6px 0 10px; }
      .ag-deb-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .ag-deb-head h4 { margin: 0; font-size: 12px; text-transform: uppercase; letter-spacing: .4px; color: var(--muted); }
      .ag-deb { border: 1px solid var(--line, #333); border-radius: 10px; padding: 8px 10px; display: grid; gap: 8px; }
      .ag-deb-title { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
      .ag-deb-title b { font-size: 14px; }
      .ag-deb-pill { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line, #444); color: var(--muted); }
      .ag-deb-pill.open { color: var(--green, #3fb950); border-color: currentColor; }
      .ag-deb-cons { font-size: 12px; color: var(--muted); display: flex; gap: 10px; flex-wrap: wrap; }
      .ag-deb-cons span b { color: var(--text, inherit); }
      .ag-deb-board { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 8px; }
      .ag-deb-col { border-radius: 8px; padding: 6px; display: grid; gap: 6px; align-content: start; min-height: 60px;
                    background: color-mix(in srgb, var(--card, #1a1a1a) 85%, transparent); border-top: 3px solid var(--line, #444); }
      .ag-deb-col.challenged { border-top-color: var(--amber, #d29922); }
      .ag-deb-col.verified { border-top-color: var(--green, #3fb950); }
      .ag-deb-col.rejected { border-top-color: var(--red, #f85149); }
      .ag-deb-col h5 { margin: 0; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; color: var(--muted); }
      .ag-fnd { border: 1px solid var(--line, #333); border-radius: 7px; padding: 6px 7px; display: grid; gap: 5px; font-size: 13px;
                background: var(--card, #161616); }
      .ag-fnd-top { display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; font-size: 11px; color: var(--muted); }
      .ag-fnd-top b { color: var(--text, inherit); font-size: 12px; }
      .ag-fnd-claim { white-space: pre-wrap; word-break: break-word; }
      .ag-fnd ul { margin: 0; padding-left: 16px; font-size: 12px; }
      .ag-fnd li code { font-size: 11px; word-break: break-all; }
      .ag-fnd pre, .ag-deb pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 11px; max-height: 180px; overflow: auto; }
      .ag-fnd-who { font-size: 11px; color: var(--muted); }
      .ag-fnd-act { display: grid; gap: 4px; }
      .ag-fnd-act textarea, .ag-deb-form textarea, .ag-deb-form input { width: 100%; box-sizing: border-box; }
      .ag-fnd-act textarea { min-height: 34px; resize: vertical; font-size: 12px; }
      .ag-fnd-btns { display: flex; gap: 4px; flex-wrap: wrap; }
      .ag-deb-form { display: grid; gap: 6px; padding: 8px; border: 1px dashed var(--line, #444); border-radius: 8px; }
      .ag-deb-form .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .ag-deb details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
    `;
    document.head.appendChild(css);
  }

  const detailNow = () => document.querySelector(".ag-detail");
  const spaceOf = (detail) => (detail && detail.dataset.spaceId) || "ecosystem";

  function get(detail, path) {
    const sid = spaceOf(detail);
    return H.ctx.api(sid === "ecosystem" ? path : path + (path.includes("?") ? "&" : "?") + "space_id=" + encodeURIComponent(sid));
  }

  async function post(detail, op, body) {
    const r = await H.ctx.api("/api/agora/" + op, { agent: "luis", space_id: spaceOf(detail), ...body });
    if (!r || !r.ok) { toast((r && r.error) || "error", "err"); return null; }
    return r;
  }

  function draft(id, tag = "textarea", placeholder = "") {
    const node = el(tag);
    node.placeholder = placeholder;
    node.dataset.draft = id;
    node.value = drafts.get(id) || "";
    node.addEventListener("input", () => drafts.set(id, node.value));
    return node;
  }

  const clear = (...ids) => ids.forEach((id) => drafts.delete(id));
  const lines = (text) => String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const csvList = (text) => String(text || "").split(/[,\n]/).map((s) => s.trim()).filter(Boolean);

  function keyOf(detail) {
    const m = /^(thread|task)-(\d+)$/.exec((detail && detail.dataset.key) || "");
    return m ? { type: m[1], id: Number(m[2]), key: `${spaceOf(detail)}:${m[1]}-${m[2]}` } : null;
  }

  function query(k) {
    return `/api/agora/debates?full=1&${k.type === "task" ? "task_id" : "thread_id"}=${k.id}`;
  }

  function evidenceItem(e) {
    const li = el("li");
    li.appendChild(el("span", "", `[${e.kind}] `));
    if (e.kind === "url" && /^https?:\/\//i.test(e.ref || "")) {
      const a = el("a", "", e.ref); a.href = e.ref; a.target = "_blank"; a.rel = "noopener"; li.appendChild(a);
    } else {
      li.appendChild(el("code", "", e.ref || e.note || ""));
    }
    if (e.output) {
      const d = el("details"); d.appendChild(el("summary", "", "output")); d.appendChild(el("pre", "", e.output)); li.appendChild(d);
    }
    if (e.note && e.ref) li.appendChild(el("div", "ag-fnd-who", e.note));
    return li;
  }

  function stanceBox(detail, debate, f, rerender) {
    const box = el("div", "ag-fnd-act");
    const id = `deb-${debate.id}-f${f.id}`;
    const ta = draft(id, "textarea", t("note"));
    const row = el("div", "ag-fnd-btns");
    for (const op of ["verify", "challenge", "reject"]) {
      const b = el("button", op === "verify" ? "small primary" : "small", t(op));
      b.dataset.op = op; b.dataset.finding = String(f.id);
      b.onclick = async () => {
        const note = ta.value.trim();
        if (op !== "verify" && !note) { toast(t("need_note"), "err"); ta.focus(); return; }
        b.disabled = true;
        const r = await post(detail, "debate_" + op, { finding_id: f.id, debate_id: debate.id, note });
        b.disabled = false;
        if (r) { clear(id); rerender(r); }
      };
      row.appendChild(b);
    }
    box.appendChild(ta); box.appendChild(row);
    return box;
  }

  function findingCard(detail, debate, f, rerender) {
    const card = el("div", "ag-fnd");
    card.dataset.finding = String(f.id);
    const top = el("div", "ag-fnd-top");
    top.appendChild(el("b", "", `F${f.id}`));
    top.appendChild(el("span", "", f.author));
    top.appendChild(el("span", "ag-deb-pill", t("d_" + f.disposition)));
    top.appendChild(el("span", "", `${t("round")} ${f.round}`));
    card.appendChild(top);
    card.appendChild(el("div", "ag-fnd-claim", f.claim));
    if ((f.evidence || []).length) {
      const ul = el("ul");
      for (const e of f.evidence) ul.appendChild(evidenceItem(e));
      card.appendChild(ul);
    }
    const who = [];
    for (const k of ["verified_by", "challenged_by", "rejected_by"]) if ((f[k] || []).length) who.push(`${t(k)}: ${f[k].join(", ")}`);
    if (who.length) card.appendChild(el("div", "ag-fnd-who", who.join(" · ")));
    const notes = (f.events || []).filter((e) => ["verify", "challenge", "reject"].includes(e.kind) && e.note);
    if (notes.length) {
      const d = el("details");
      d.appendChild(el("summary", "", `${notes.length} ${t("notes_n")}`));
      for (const e of notes) d.appendChild(el("div", "ag-fnd-who", `#${e.seq} ${e.author} · ${t(e.kind)} · ${e.note}`));
      card.appendChild(d);
    }
    if (debate.status === "open" && f.author !== "luis") card.appendChild(stanceBox(detail, debate, f, rerender));
    return card;
  }

  function snapshotView(snap) {
    const d = el("details");
    const files = snap.files || [];
    d.appendChild(el("summary", "", `${t("snapshot")} · ${files.length} ${t("files_n")} · ${(snap.commits || []).length} ${t("commits_n")}`
      + (snap.head ? ` · ${String(snap.head).slice(0, 10)}` : "") + (snap.digest ? ` · sha256 ${snap.digest.slice(0, 12)}` : "")));
    d.appendChild(el("pre", "", snap.question || ""));
    for (const f of files) {
      d.appendChild(el("div", "ag-fnd-who", f.ref + (f.lines ? ` (L${f.lines[0]}-L${f.lines[1]})` : "")));
      if (f.text) d.appendChild(el("pre", "", f.text));
    }
    if ((snap.commits || []).length) d.appendChild(el("div", "ag-fnd-who", snap.commits.join(" ")));
    return d;
  }

  function consensusView(view) {
    const c = view.consensus || {};
    const row = el("div", "ag-deb-cons");
    const head = el("span"); head.appendChild(el("b", "", t("consensus") + ": ")); head.appendChild(document.createTextNode(c.summary || ""));
    row.appendChild(head);
    for (const [who, n] of Object.entries(c.participants || {})) {
      row.appendChild(el("span", "", `${who}: ${n.filed} ${t("filed")} · ${n.verify}✓ ${n.challenge}? ${n.reject}✗`));
    }
    return row;
  }

  function boardView(detail, view, rerender) {
    const board = el("div", "ag-deb-board");
    for (const s of STATUSES) {
      const col = el("div", "ag-deb-col " + s);
      col.dataset.status = s;
      const items = view.findings.filter((f) => f.status === s);
      col.appendChild(el("h5", "", `${t("col_" + s)} · ${items.length}`));
      for (const f of items) col.appendChild(findingCard(detail, view.debate, f, rerender));
      board.appendChild(col);
    }
    return board;
  }

  function findingForm(detail, debate, rerender) {
    const form = el("div", "ag-deb-form");
    const base = `deb-${debate.id}-new`;
    const claim = draft(base + "-claim", "textarea", t("claim"));
    const ev = draft(base + "-ev", "textarea", t("evidence_lines"));
    const disp = el("select");
    for (const d of DISPOSITIONS) { const o = el("option", "", t("d_" + d)); o.value = d; disp.appendChild(o); }
    disp.value = drafts.get(base + "-disp") || "adopt";
    disp.onchange = () => drafts.set(base + "-disp", disp.value);
    const go = el("button", "small primary", t("file_it"));
    go.onclick = async () => {
      if (!claim.value.trim()) { claim.focus(); return; }
      go.disabled = true;
      const r = await post(detail, "debate_finding", { debate_id: debate.id, claim: claim.value.trim(),
                                                       evidence: lines(ev.value), disposition: disp.value });
      go.disabled = false;
      if (r) { clear(base + "-claim", base + "-ev"); rerender(r); }
    };
    const row = el("div", "row"); row.appendChild(disp); row.appendChild(go);
    form.appendChild(claim); form.appendChild(ev); form.appendChild(row);
    return form;
  }

  function closeForm(detail, debate, rerender) {
    const form = el("div", "ag-deb-form");
    const id = `deb-${debate.id}-outcome`;
    const outcome = draft(id, "textarea", t("outcome"));
    const lab = el("label", "hint"); const cb = el("input"); cb.type = "checkbox"; cb.checked = true;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(" " + t("spawn")));
    const go = el("button", "small primary", t("close_it"));
    go.onclick = async () => {
      if (!outcome.value.trim()) { outcome.focus(); return; }
      go.disabled = true;
      const r = await post(detail, "debate_close", { debate_id: debate.id, outcome: outcome.value.trim(), spawn_tasks: cb.checked });
      go.disabled = false;
      if (r) { clear(id); toast(`${t("done")} · ${t("decision")} ${r.decision_thread_id}` + ((r.tasks || []).length ? ` · ${t("tasks")} #${r.tasks.join(", #")}` : "")); rerender(r); }
    };
    form.appendChild(outcome); const row = el("div", "row"); row.appendChild(lab); row.appendChild(go); form.appendChild(row);
    return form;
  }

  function toggle(label, key, build) {
    const wrap = el("div");
    const b = el("button", "small", label);
    b.dataset.toggle = key;
    const holder = el("div");
    const show = () => { holder.replaceChildren(); if (formsOpen.has(key)) holder.appendChild(build()); };
    b.onclick = () => { if (formsOpen.has(key)) formsOpen.delete(key); else formsOpen.add(key); show(); };
    show();
    wrap.appendChild(b); wrap.appendChild(holder);
    return wrap;
  }

  function debateCard(detail, view, rerender) {
    const d = view.debate;
    const card = el("section", "ag-deb");
    card.dataset.debate = String(d.id);
    const title = el("div", "ag-deb-title");
    title.appendChild(el("b", "", `#${d.id} ${d.title}`));
    title.appendChild(el("span", "ag-deb-pill " + d.status, t(d.status)));
    title.appendChild(el("span", "ag-fnd-who", `${d.opened_by} · ${fmtWhen(d.created)} · ${(d.participants || []).join(", ")}`));
    card.appendChild(title);
    card.appendChild(snapshotView(d.snapshot || {}));
    card.appendChild(consensusView(view));
    if (view.findings.length) card.appendChild(boardView(detail, view, rerender));
    else card.appendChild(el("div", "hint", t("none")));
    if (d.status === "open") {
      const row = el("div", "ag-deb-head");
      row.appendChild(toggle(t("add_finding"), `deb-${d.id}-add`, () => findingForm(detail, d, rerender)));
      row.appendChild(toggle(t("close"), `deb-${d.id}-close`, () => closeForm(detail, d, rerender)));
      card.appendChild(row);
    } else {
      card.appendChild(el("div", "ag-dec", `${t("outcome_label")}: ${d.outcome || ""}`));
      const meta = [`${t("decision")} ${d.decision_thread_id}`];
      if ((d.spawned_tasks || []).length) meta.push(`${t("tasks")}: #${d.spawned_tasks.join(", #")}`);
      card.appendChild(el("div", "ag-fnd-who", meta.join(" · ")));
    }
    return card;
  }

  function openForm(detail, k, rerender) {
    const form = el("div", "ag-deb-form");
    const base = `deb-open-${k.key}`;
    const title = draft(base + "-title", "input", t("title"));
    const question = draft(base + "-q", "textarea", t("question"));
    const people = draft(base + "-p", "input", t("participants"));
    if (!people.value) people.value = "claude, codex";
    const files = draft(base + "-files", "textarea", t("files"));
    const commits = draft(base + "-commits", "input", t("commits"));
    const go = el("button", "small primary", t("start"));
    go.onclick = async () => {
      go.disabled = true;
      const body = { participants: csvList(people.value), files: lines(files.value), commits: csvList(commits.value) };
      body[k.type === "task" ? "task_id" : "thread_id"] = k.id;
      if (title.value.trim()) body.title = title.value.trim();
      if (question.value.trim()) body.question = question.value.trim();
      const r = await post(detail, "debate_open", body);
      go.disabled = false;
      if (r) { clear(base + "-title", base + "-q", base + "-p", base + "-files", base + "-commits"); formsOpen.delete(base); rerender(r); }
    };
    for (const n of [title, question, people, files, commits]) form.appendChild(n);
    const row = el("div", "row"); row.appendChild(go); form.appendChild(row);
    return form;
  }

  function render(detail, k, data) {
    const wrap = el("section", "ag-debates");
    wrap.dataset.key = k.key;
    const rerender = () => refresh(detail, k, true);
    const head = el("div", "ag-deb-head");
    head.appendChild(el("h4", "", `${t("debates")} · ${(data.debates || []).length}`));
    head.appendChild(toggle(t("open_debate"), `deb-open-${k.key}`, () => openForm(detail, k, rerender)));
    wrap.appendChild(head);
    for (const view of data.debates || []) wrap.appendChild(debateCard(detail, view, rerender));
    return wrap;
  }

  function paint(detail, k, data) {
    injectCss();
    const old = detail.querySelector(".ag-debates");
    const active = document.activeElement;
    const focused = active && active.dataset ? active.dataset.draft : null;
    const caret = focused && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null;
    const wrap = render(detail, k, data);
    if (old) old.replaceWith(wrap);
    else {
      const msgs = detail.querySelector(".ag-msgs");
      const anchor = msgs && msgs.previousElementSibling && msgs.previousElementSibling.tagName === "H4" ? msgs.previousElementSibling : msgs;
      if (anchor) detail.insertBefore(wrap, anchor); else detail.appendChild(wrap);
    }
    if (focused) {
      const n = wrap.querySelector(`[data-draft="${CSS.escape(focused)}"]`);
      if (n) { n.focus(); if (caret) try { n.setSelectionRange(...caret); } catch (e) { /* inputs without a caret */ } }
    }
  }

  const again = new Set();
  async function refresh(detail, k, force) {
    if (inflight.has(k.key)) { if (force) again.add(k.key); return; }
    inflight.add(k.key);
    try {
      const r = await get(detail, query(k));
      if (!r || !r.ok) return;
      const now = detailNow(); const nk = keyOf(now);
      if (!now || !nk || nk.key !== k.key) return;
      const sig = JSON.stringify(r.debates);
      const prev = cache.get(k.key);
      cache.set(k.key, { sig, data: r });
      const shown = now.querySelector(".ag-debates");
      if (!force && shown && shown.dataset.key === k.key && prev && prev.sig === sig) return;
      paint(now, k, r);
    } catch (e) {
      /* the board must never break the conversation */
    } finally {
      inflight.delete(k.key);
      if (again.delete(k.key)) refresh(detailNow(), k, true);
    }
  }

  function check() {
    const detail = detailNow();
    if (!detail || detail.hidden) return;
    const k = keyOf(detail);
    if (!k) return;
    const shown = detail.querySelector(".ag-debates");
    if (shown && shown.dataset.key === k.key) return;
    const c = cache.get(k.key);
    if (c) paint(detail, k, c.data);   // the conversation was rebuilt: put the board back at once
    refresh(detail, k, false);
  }

  const obs = new MutationObserver(check);
  const start = () => obs.observe(document.body, { childList: true, subtree: true });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
  // agents file findings from elsewhere: refresh the open board now and then (cheap: one GET, repaint on change)
  setInterval(() => {
    const detail = detailNow();
    const k = keyOf(detail);
    if (detail && !detail.hidden && k && !document.hidden) refresh(detail, k, false);
  }, 8000);
})();

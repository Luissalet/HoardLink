/* Hoard Hub UI — Ágora delivery review (companion to agora.js, like agora_debate.js).
   In the task of a delivery that lists commits: a «Revisión de la entrega» section with a button that opens the real
   diff of those commits (files, hunks, lines) where the person comments on any line; the comments are sent in one go
   and arrive in the task's thread, mentioning whoever delivered. «Revisión guiada» asks the family's language model
   to order the diff in chapters, shown beside the diff. Spanish and English. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H || !H.ctx) return;
  const { el, toast, L } = H.ctx;

  const S = {
    title: { es: "Revisión de la entrega", en: "Delivery review" },
    open: { es: "Revisar el diff", en: "Review the diff" },
    guided: { es: "Revisión guiada", en: "Guided review" },
    close: { es: "Cerrar", en: "Close" },
    commits_n: { es: "commit(s)", en: "commit(s)" }, files_n: { es: "fichero(s)", en: "file(s)" },
    comments_n: { es: "comentario(s)", en: "comment(s)" },
    unreadable: { es: "No se puede leer este commit en ningún repositorio registrado", en: "No registered checkout can read this commit" },
    all_commits: { es: "Todos los commits", en: "All commits" },
    pending: { es: "por enviar", en: "to send" },
    send: { es: "Enviar comentarios", en: "Send comments" },
    general: { es: "Nota general (opcional)", en: "General note (optional)" },
    sent: { es: "Comentarios enviados", en: "Comments sent" },
    comment_line: { es: "Comentar la línea", en: "Comment on the line" },
    write: { es: "Escribe el comentario", en: "Write the comment" },
    add: { es: "Añadir", en: "Add" }, cancel: { es: "Cancelar", en: "Cancel" }, remove: { es: "Quitar", en: "Remove" },
    outdated: { es: "de una entrega anterior", en: "from an earlier delivery" },
    binary: { es: "Fichero binario", en: "Binary file" },
    truncated: { es: "Diff recortado: se muestran las primeras líneas", en: "Diff cut: the first lines are shown" },
    renamed_from: { es: "antes", en: "was" },
    st_added: { es: "nuevo", en: "added" }, st_deleted: { es: "borrado", en: "deleted" },
    st_modified: { es: "modificado", en: "modified" }, st_renamed: { es: "renombrado", en: "renamed" },
    guide_wait: { es: "El modelo está ordenando el diff en capítulos…", en: "The model is ordering the diff into chapters…" },
    guide_again: { es: "Rehacer la guía", en: "Redo the guide" },
    guide_by: { es: "Guía de", en: "Guide by" },
    chapters: { es: "Capítulos", en: "Chapters" }, check: { es: "Qué comprobar", en: "What to check" },
    all_files: { es: "Todos los ficheros", en: "All files" }, prev: { es: "Anterior", en: "Previous" },
    next: { es: "Siguiente", en: "Next" }, files: { es: "Ficheros", en: "Files" },
    loading: { es: "Leyendo el diff…", en: "Reading the diff…" },
    stale: { es: "La entrega cambió mientras revisabas: se ha recargado.", en: "The delivery changed while you reviewed: it was reloaded." },
    no_changes: { es: "Este commit no cambia ficheros.", en: "This commit changes no files." },
    side_old: { es: "antes", en: "old" },
    nothing: { es: "Escribe algún comentario o una nota general.", en: "Write a comment or a general note." },
    owner_gets: { es: "Llegará a", en: "It goes to" },
  };
  const t = (k) => L(S[k] || { es: k, en: k });
  const states = new Map();          // key -> { data, guide, pending[], general, chapter, commit, open }
  const inflight = new Set();
  const missing = new Map();         // key -> time when the task turned out to have no readable delivery

  const detailNow = () => document.querySelector(".ag-detail");
  const spaceOf = (d) => (d && d.dataset.spaceId) || "ecosystem";
  const keyOf = (d) => {
    const m = /^task-(\d+)$/.exec((d && d.dataset.key) || "");
    return m ? { id: Number(m[1]), key: `${spaceOf(d)}:task-${m[1]}` } : null;
  };
  const withSpace = (path, sid) => (sid === "ecosystem" ? path : path + (path.includes("?") ? "&" : "?") + "space_id=" + encodeURIComponent(sid));
  const state = (key) => { if (!states.has(key)) states.set(key, { data: null, guide: null, pending: [], general: "", chapter: -1, commit: "", open: false }); return states.get(key); };

  function injectCss() {
    if (document.getElementById("css-agora-review")) return;
    const css = document.createElement("style");
    css.id = "css-agora-review";
    css.textContent = `
      .ag-review { display: grid; gap: 6px; margin: 6px 0 10px; }
      .ag-rv-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .ag-rv-head h4 { margin: 0; font-size: 12px; text-transform: uppercase; letter-spacing: .4px; color: var(--muted); }
      .ag-rv-overlay { position: fixed; inset: 0; z-index: 60; background: var(--bg, #111);
                       display: grid; grid-template-rows: auto 1fr; }
      .ag-rv-top { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 8px 14px; border-bottom: 1px solid var(--line, #333);
                   background: var(--card, #1a1a1a); }
      .ag-rv-top h3 { margin: 0; font-size: 15px; flex: 1 1 240px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .ag-rv-body { display: grid; grid-template-columns: minmax(0, 300px) minmax(0, 1fr); min-height: 0; }
      .ag-rv-body.single { grid-template-columns: minmax(0, 1fr); }
      .ag-rv-side { overflow: auto; border-right: 1px solid var(--line, #333); padding: 10px; display: grid; gap: 8px; align-content: start; }
      .ag-rv-main { overflow: auto; padding: 10px 14px 96px; display: grid; gap: 10px; align-content: start; min-width: 0; }
      .ag-rv-chap { text-align: left; display: grid; gap: 2px; padding: 6px 8px; border: 1px solid var(--line, #333); border-radius: 8px; cursor: pointer; }
      .ag-rv-chap.on { border-color: var(--accent, #d29922); }
      .ag-rv-chap b { font-size: 13px; } .ag-rv-chap span { font-size: 12px; color: var(--muted); }
      .ag-rv-note { font-size: 13px; line-height: 1.45; border-left: 3px solid var(--accent, #d29922); padding: 4px 10px; }
      .ag-rv-commit { display: grid; gap: 8px; }
      .ag-rv-commit > h4 { margin: 0; font-size: 13px; display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
      .ag-rv-commit > h4 code { color: var(--muted); }
      .ag-rv-file { border: 1px solid var(--line, #333); border-radius: 8px; background: var(--card, #161616); }
      .ag-rv-file > summary { cursor: pointer; padding: 6px 10px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; font-size: 13px; }
      .ag-rv-file > summary code { font-size: 12.5px; word-break: break-all; }
      .ag-rv-badge { font-size: 11px; padding: 0 7px; border-radius: 999px; border: 1px solid var(--line, #444); color: var(--muted); }
      .ag-rv-add-n { color: var(--green, #3fb950); font-size: 12px; } .ag-rv-del-n { color: var(--red, #f85149); font-size: 12px; }
      .ag-rv-table { width: 100%; border-collapse: collapse; font: 12px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; table-layout: fixed; }
      .ag-rv-table td { padding: 0 6px; vertical-align: top; }
      .ag-rv-table td.n { width: 46px; text-align: right; color: var(--muted); user-select: none; padding-right: 6px; }
      .ag-rv-table td.c { white-space: pre-wrap; word-break: break-all; }
      .ag-rv-table td.b { width: 26px; padding: 0; text-align: center; }
      .ag-rv-table tr.add td { background: color-mix(in srgb, var(--green, #3fb950) 14%, transparent); }
      .ag-rv-table tr.del td { background: color-mix(in srgb, var(--red, #f85149) 14%, transparent); }
      .ag-rv-table tr.hunk td { color: var(--muted); background: color-mix(in srgb, var(--accent, #d29922) 8%, transparent); padding: 2px 8px; }
      .ag-rv-table button.plus { display: block; margin: 0 auto; width: 22px; height: 17px; min-height: 0; padding: 0; line-height: 1; font-size: 13px; opacity: .35; border-radius: 4px; }
      .ag-rv-table tr:hover button.plus, .ag-rv-table button.plus:focus-visible { opacity: 1; }
      .ag-rv-bubble td { padding: 4px 10px 8px 78px; background: var(--card-2, #1d1d1d); }
      .ag-rv-bubble .box { border: 1px solid var(--line, #444); border-radius: 8px; padding: 6px 8px; display: grid; gap: 4px; font: 13px/1.4 var(--font, sans-serif);
                           white-space: pre-wrap; background: var(--card, #161616); }
      .ag-rv-bubble .box.mine { border-style: dashed; border-color: var(--accent, #d29922); }
      .ag-rv-bubble .box.old { opacity: .6; }
      .ag-rv-bubble .who { font-size: 11px; color: var(--muted); display: flex; gap: 8px; flex-wrap: wrap; }
      .ag-rv-bubble textarea { width: 100%; min-height: 56px; resize: vertical; }
      .ag-rv-bubble .row { display: flex; gap: 6px; }
      .ag-rv-send { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .ag-rv-send textarea { flex: 1 1 260px; min-height: 34px; height: 34px; resize: vertical; }
      .ag-rv-wait { color: var(--muted); font-size: 13px; }
      @media (max-width: 820px) {
        .ag-rv-body { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto 1fr; }
        .ag-rv-side { border-right: 0; border-bottom: 1px solid var(--line, #333); max-height: 30vh; }
        .ag-rv-table col:nth-child(1), .ag-rv-table col:nth-child(2) { width: 34px !important; }
        .ag-rv-bubble td { padding-left: 10px; }
        .ag-rv-table td.n { width: 34px; }
      }
    `;
    document.head.appendChild(css);
  }

  // ---- data -------------------------------------------------------------------------------------------------

  async function fetchDiff(sid, id, summary) {
    return H.ctx.api(withSpace(`/api/agora/review_diff?task_id=${id}${summary ? "&summary_only=1" : ""}`, sid));
  }

  const totals = (data) => {
    let files = 0, add = 0, del = 0;
    for (const c of data.commits || []) for (const f of c.files || []) { files += 1; add += f.additions; del += f.deletions; }
    return { files, add, del };
  };

  // ---- the section inside the task -----------------------------------------------------------------------------

  function renderSection(detail, k, st) {
    const d = st.data;
    const sec = el("section", "ag-review");
    sec.dataset.key = k.key;
    const head = el("div", "ag-rv-head");
    head.appendChild(el("h4", "", `${t("title")} r${d.submission_revision}`));
    const sum = totals(d);
    head.appendChild(el("span", "hint", `${(d.commits || []).length} ${t("commits_n")} · ${sum.files} ${t("files_n")} · +${sum.add} −${sum.del} · ${(d.comments || []).length} ${t("comments_n")}`));
    const open = el("button", "small primary", t("open"));
    open.dataset.action = "open-review";
    open.onclick = () => openOverlay(detail, k, false);
    head.appendChild(open);
    const guided = el("button", "small", t("guided") + (d.has_guide ? " ✓" : ""));
    guided.dataset.action = "open-guide";
    guided.onclick = () => openOverlay(detail, k, true);
    head.appendChild(guided);
    sec.appendChild(head);
    return sec;
  }

  function paintSection(detail, k, st) {
    injectCss();
    const old = detail.querySelector(".ag-review");
    const sec = renderSection(detail, k, st);
    if (old) { old.replaceWith(sec); return; }
    const meta = detail.querySelector(".ag-meta");
    const anchor = meta || detail.querySelector(".ag-dhead");
    if (anchor) anchor.after(sec); else detail.appendChild(sec);
  }

  // ---- the overlay -------------------------------------------------------------------------------------------

  let overlay = null, returnFocus = null;

  function closeOverlay() {
    if (!overlay) return;
    overlay.remove();
    overlay = null;
    document.removeEventListener("keydown", onKey, true);
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
    returnFocus = null;
  }

  function onKey(e) {
    if (e.key === "Escape" && overlay) { e.stopPropagation(); closeOverlay(); }
  }

  function openOverlay(detail, k, askGuide) {
    injectCss();
    closeOverlay();
    returnFocus = document.activeElement;
    const st = state(k.key);
    st.open = true;
    overlay = el("div", "ag-rv-overlay");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", t("title"));
    overlay.dataset.key = k.key;
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey, true);
    paintOverlay(detail, k);
    const close = overlay.querySelector('[data-action="close-review"]');
    if (close) close.focus();
    if (askGuide && !st.guide) generateGuide(detail, k, false);
    else if (!st.guide && st.data && st.data.has_guide) loadGuide(detail, k);
  }

  async function loadGuide(detail, k) {
    const st = state(k.key);
    const r = await H.ctx.api("/api/agora/review_guide", { agent: "luis", space_id: spaceOf(detail), task_id: k.id, only_cached: true });
    if (r && r.ok && r.guide) { st.guide = r.guide; if (overlay && overlay.dataset.key === k.key) paintOverlay(detail, k); }
  }

  async function generateGuide(detail, k, refresh) {
    const st = state(k.key);
    st.waiting = true;
    paintOverlay(detail, k);
    let r = null;
    try { r = await H.ctx.api("/api/agora/review_guide", { agent: "luis", space_id: spaceOf(detail), task_id: k.id, refresh: Boolean(refresh) }); }
    catch (e) { r = { ok: false, error: String(e) }; }
    st.waiting = false;
    if (!r || !r.ok) toast((r && r.error) || "error", "err");
    else { st.guide = r.guide; st.chapter = -1; }
    if (overlay && overlay.dataset.key === k.key) paintOverlay(detail, k);
    refreshData(detail, k);
  }

  function paintOverlay(detail, k) {
    if (!overlay || overlay.dataset.key !== k.key) return;
    const st = state(k.key);
    const d = st.data;
    const keep = overlay.querySelector(".ag-rv-main");
    const scroll = keep ? keep.scrollTop : 0;
    const openFiles = new Set([...overlay.querySelectorAll("details.ag-rv-file[open]")].map((n) => n.dataset.file));
    overlay.replaceChildren();
    const top = el("div", "ag-rv-top");
    top.appendChild(el("h3", "", `${t("title")} r${d.submission_revision} · #${d.task_id} ${d.title || ""}`));
    if ((d.commits || []).length > 1) {
      const sel = el("select");
      sel.setAttribute("aria-label", t("all_commits"));
      sel.appendChild(Object.assign(el("option", "", t("all_commits")), { value: "" }));
      for (const c of d.commits) sel.appendChild(Object.assign(el("option", "", `${c.short} ${c.subject || ""}`.slice(0, 70)), { value: c.sha }));
      sel.value = st.commit;
      sel.onchange = () => { st.commit = sel.value; paintOverlay(detail, k); };
      top.appendChild(sel);
    }
    const guide = el("button", "small", st.guide ? t("guide_again") : t("guided"));
    guide.dataset.action = "guide";
    guide.disabled = Boolean(st.waiting);
    guide.onclick = () => generateGuide(detail, k, Boolean(st.guide));
    top.appendChild(guide);
    const close = el("button", "small", t("close") + " ✕");
    close.dataset.action = "close-review";
    close.onclick = closeOverlay;
    top.appendChild(close);
    overlay.appendChild(top);

    const body = el("div", "ag-rv-body" + (st.guide || st.waiting ? "" : " single"));
    if (st.guide || st.waiting) body.appendChild(sidePane(detail, k, st));
    const main = el("div", "ag-rv-main");
    mainPane(main, detail, k, st, openFiles);
    body.appendChild(main);
    overlay.appendChild(body);
    main.scrollTop = scroll;
  }

  function sidePane(detail, k, st) {
    const side = el("aside", "ag-rv-side");
    if (st.waiting) { side.appendChild(el("div", "ag-rv-wait", t("guide_wait"))); return side; }
    const g = st.guide;
    side.appendChild(el("h4", "", `${t("chapters")} · ${g.chapters.length}`));
    if (g.summary) side.appendChild(el("div", "ag-rv-note", g.summary));
    const all = el("button", "small ag-rv-chap" + (st.chapter < 0 ? " on" : ""), t("all_files"));
    all.onclick = () => { st.chapter = -1; paintOverlay(detail, k); };
    side.appendChild(all);
    g.chapters.forEach((c, i) => {
      const b = el("button", "ag-rv-chap" + (st.chapter === i ? " on" : ""));
      b.dataset.chapter = String(i);
      b.appendChild(el("b", "", `${i + 1}. ${c.title}`));
      b.appendChild(el("span", "", `${c.files.length} ${t("files_n")}`));
      b.onclick = () => { st.chapter = i; paintOverlay(detail, k); };
      side.appendChild(b);
    });
    if (g.model) side.appendChild(el("div", "hint", `${t("guide_by")} ${g.model}`));
    return side;
  }

  function mainPane(main, detail, k, st, openFiles) {
    const d = st.data;
    const g = st.guide;
    const chapter = g && st.chapter >= 0 ? g.chapters[st.chapter] : null;
    if (chapter) {
      const note = el("div", "ag-rv-note");
      note.appendChild(el("b", "", chapter.title));
      note.appendChild(el("div", "", chapter.summary));
      if (chapter.check) note.appendChild(el("div", "hint", `${t("check")}: ${chapter.check}`));
      const nav = el("div", "row");
      const prev = el("button", "small", "← " + t("prev")); prev.disabled = st.chapter <= 0;
      prev.onclick = () => { st.chapter -= 1; paintOverlay(detail, k); };
      const next = el("button", "small", t("next") + " →"); next.disabled = st.chapter >= g.chapters.length - 1;
      next.onclick = () => { st.chapter += 1; paintOverlay(detail, k); };
      nav.appendChild(prev); nav.appendChild(next); note.appendChild(nav);
      main.appendChild(note);
    }
    const shownFiles = new Set(chapter ? chapter.files : []);
    for (const c of d.commits || []) {
      if (st.commit && c.sha !== st.commit) continue;
      const sec = el("section", "ag-rv-commit");
      const h = el("h4");
      h.appendChild(el("code", "", c.short));
      h.appendChild(el("span", "", c.subject || ""));
      sec.appendChild(h);
      if (c.error) { sec.appendChild(el("div", "hint", `${t("unreadable")}: ${c.error}`)); main.appendChild(sec); continue; }
      const files = (c.files || []).filter((f) => !chapter || shownFiles.has(f.path));
      if (!(c.files || []).length) sec.appendChild(el("div", "hint", t("no_changes")));
      for (const f of files) {
        sec.appendChild(fileBlock(detail, k, st, c, f, openFiles.has(`${c.sha}:${f.path}`) || (openFiles.size === 0 && (files.length <= 3 || Boolean(chapter)))));
      }
      if (files.length || !(c.files || []).length) main.appendChild(sec);
    }
    main.appendChild(sendBar(detail, k, st));
    paintBubbles(main, st);
  }

  function fileBlock(detail, k, st, commit, f, open) {
    const det = el("details", "ag-rv-file");
    det.dataset.file = `${commit.sha}:${f.path}`;
    const sum = el("summary");
    sum.appendChild(el("span", "ag-rv-badge", t("st_" + f.status)));
    sum.appendChild(el("code", "", f.path));
    if (f.old_path && f.status === "renamed") sum.appendChild(el("span", "hint", `${t("renamed_from")}: ${f.old_path}`));
    sum.appendChild(el("span", "ag-rv-add-n", `+${f.additions}`));
    sum.appendChild(el("span", "ag-rv-del-n", `−${f.deletions}`));
    det.appendChild(sum);
    let painted = false;
    const paint = () => {
      if (painted) return;
      painted = true;
      if (f.binary) { det.appendChild(el("div", "hint", t("binary"))); return; }
      det.appendChild(diffTable(detail, k, st, commit, f));
      if (f.truncated) det.appendChild(el("div", "hint", t("truncated")));
      paintBubbles(det, st);
    };
    det.addEventListener("toggle", () => { if (det.open) paint(); });
    if (open) { det.open = true; paint(); }
    return det;
  }

  function diffTable(detail, k, st, commit, f) {
    const table = el("table", "ag-rv-table");
    const cols = el("colgroup");   // a fixed layout takes its widths from the first row, which is a full-width hunk header
    for (const w of ["46px", "46px", "26px", ""]) { const c = el("col"); if (w) c.style.width = w; cols.appendChild(c); }
    table.appendChild(cols);
    const tb = el("tbody");
    for (const h of f.hunks) {
      const hr = el("tr", "hunk");
      const td = el("td", "", h.header); td.colSpan = 4; hr.appendChild(td); tb.appendChild(hr);
      for (const ln of h.lines) {
        const tr = el("tr", ln.t);
        tr.dataset.commit = commit.sha; tr.dataset.path = f.path;
        tr.dataset.old = ln.old == null ? "" : String(ln.old); tr.dataset.new = ln.new == null ? "" : String(ln.new);
        const side = ln.t === "del" ? "old" : "new";
        const num = side === "old" ? ln.old : ln.new;
        const o = el("td", "n", ln.old == null ? "" : String(ln.old));
        const n = el("td", "n", ln.new == null ? "" : String(ln.new));
        const bt = el("td", "b");
        const plus = el("button", "ghost plus", "+");
        plus.type = "button";
        plus.title = t("comment_line");
        plus.setAttribute("aria-label", `${t("comment_line")} ${f.path}:${num}`);
        plus.dataset.action = "comment";
        plus.onclick = () => composer(detail, k, st, tr, { commit: commit.sha, short: commit.short, path: f.path, side, line: num, code: ln.text });
        bt.appendChild(plus);
        const code = el("td", "c", (ln.t === "add" ? "+" : ln.t === "del" ? "-" : " ") + ln.text);
        tr.appendChild(o); tr.appendChild(n); tr.appendChild(bt); tr.appendChild(code);
        tb.appendChild(tr);
      }
    }
    table.appendChild(tb);
    return table;
  }

  const anchorOf = (c) => `${c.commit}|${c.path}|${c.side}|${c.line}`;

  function rowFor(root, c) {
    const rows = root.querySelectorAll(`tr[data-commit="${CSS.escape(c.commit)}"]`);
    for (const r of rows) {
      if (r.dataset.path !== c.path) continue;
      if ((c.side === "old" ? r.dataset.old : r.dataset.new) === String(c.line)) return r;   // a deleted row has no new number, an added one no old
    }
    return null;
  }

  function bubbleRow(cls, box) {
    const tr = el("tr", "ag-rv-bubble " + cls);
    const td = el("td"); td.colSpan = 4; td.appendChild(box); tr.appendChild(td);
    return tr;
  }

  function paintBubbles(root, st) {
    root.querySelectorAll("tr.ag-rv-bubble.saved, tr.ag-rv-bubble.queued").forEach((r) => r.remove());
    const list = (st.data && st.data.comments) || [];
    for (const c of list) {
      const row = rowFor(root, c);
      if (!row) continue;
      const box = el("div", "box" + (c.outdated ? " old" : ""));
      const who = el("div", "who");
      who.appendChild(el("b", "", c.author));
      if (c.end_line) who.appendChild(el("span", "", `${c.line}-${c.end_line}`));
      if (c.outdated) who.appendChild(el("span", "", t("outdated") + ` (r${c.submission_revision})`));
      box.appendChild(who); box.appendChild(el("div", "", c.body));
      row.after(bubbleRow("saved", box));
    }
    st.pending.forEach((c, i) => {
      const row = rowFor(root, c);
      if (!row) return;
      const box = el("div", "box mine");
      const who = el("div", "who");
      who.appendChild(el("b", "", "luis"));
      who.appendChild(el("span", "", t("pending")));
      const rm = el("button", "small ghost", t("remove"));
      rm.onclick = () => { st.pending.splice(i, 1); refreshOverlayBubbles(); updateSendBar(st); };
      who.appendChild(rm);
      box.appendChild(who); box.appendChild(el("div", "", c.body));
      row.after(bubbleRow("queued", box));
    });
  }

  function refreshOverlayBubbles() {
    if (!overlay) return;
    const st = states.get(overlay.dataset.key);
    if (st) paintBubbles(overlay, st);
  }

  function composer(detail, k, st, row, anchor) {
    for (let n = row.nextElementSibling; n && n.classList.contains("ag-rv-bubble"); n = n.nextElementSibling) {
      if (n.classList.contains("composer")) { const open = n.querySelector("textarea"); if (open) open.focus(); return; }
    }
    const box = el("div", "box");
    const ta = el("textarea");
    ta.placeholder = t("write");
    ta.setAttribute("aria-label", `${t("comment_line")} ${anchor.path}:${anchor.line}`);
    const rowBtns = el("div", "row");
    const add = el("button", "small primary", t("add"));
    add.dataset.action = "add-comment";
    const cancel = el("button", "small", t("cancel"));
    const done = () => tr.remove();
    add.onclick = () => {
      const body = ta.value.trim();
      if (!body) { ta.focus(); return; }
      st.pending.push({ commit: anchor.commit, path: anchor.path, side: anchor.side, line: anchor.line, code: anchor.code, body });
      done(); refreshOverlayBubbles(); updateSendBar(st);
    };
    cancel.onclick = done;
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); add.click(); }
      if (e.key === "Escape") { e.stopPropagation(); done(); }
    });
    rowBtns.appendChild(add); rowBtns.appendChild(cancel);
    box.appendChild(ta); box.appendChild(rowBtns);
    const tr = bubbleRow("composer", box);
    row.after(tr);
    ta.focus();
  }

  // ---- sending ------------------------------------------------------------------------------------------------

  function sendBar(detail, k, st) {
    const bar = el("div", "ag-rv-send");
    bar.dataset.role = "send";
    const general = el("textarea");
    general.placeholder = t("general");
    general.setAttribute("aria-label", t("general"));
    general.value = st.general;
    general.addEventListener("input", () => { st.general = general.value; updateSendBar(st); });
    const owner = st.data.owner;
    const go = el("button", "small primary");
    go.dataset.action = "send";
    go.onclick = async () => {
      if (!st.pending.length && !st.general.trim()) { toast(t("nothing"), "err"); return; }
      go.disabled = true;
      const body = { agent: "luis", space_id: spaceOf(detail), task_id: k.id, expected_submission_revision: st.data.submission_revision,
                     comments: st.pending.map((c) => ({ commit: c.commit, path: c.path, side: c.side, line: c.line, body: c.body })) };
      if (st.general.trim()) body.body = st.general.trim();
      const r = await H.ctx.api("/api/agora/review_comment_send", body);
      go.disabled = false;
      if (!r || !r.ok) {
        toast((r && r.error) || "error", "err");
        if (r && r.error_code === "stale_submission") { toast(t("stale"), "info"); st.pending = []; await refreshData(detail, k, true); }
        return;
      }
      st.pending = []; st.general = "";
      refreshOverlayBubbles(); updateSendBar(st);
      toast(`${t("sent")}: ${r.count}`, "ok", owner ? `${t("owner_gets")} ${owner}` : "");
      await refreshData(detail, k, true);
    };
    bar.appendChild(general);
    bar.appendChild(go);
    bar._go = go;
    queueMicrotask(() => updateSendBar(st));
    return bar;
  }

  function updateSendBar(st) {
    if (!overlay) return;
    const go = overlay.querySelector('[data-action="send"]');
    if (!go) return;
    const n = st.pending.length;
    go.textContent = `${t("send")}${n ? ` (${n})` : ""}`;
  }

  // ---- loading and live ---------------------------------------------------------------------------------------

  const again = new Map();   // key -> force: a refresh asked while another is in flight runs right after it
  async function refreshData(detail, k, force) {
    if (inflight.has(k.key)) { again.set(k.key, Boolean(again.get(k.key) || force)); return; }
    inflight.add(k.key);
    try {
      const st = state(k.key);
      const r = await fetchDiff(spaceOf(detail), k.id, false);
      if (!r || !r.ok) { if (!st.data) missing.set(k.key, Date.now()); return; }
      missing.delete(k.key);
      const rsig = JSON.stringify([r.submission_revision, (r.commits || []).map((c) => c.sha)]);   // which delivery this is
      const sig = rsig + String(r.has_guide);
      const csig = JSON.stringify((r.comments || []).map((c) => c.id));
      const changed = !st.data || st.sig !== sig;
      const cchanged = !st.data || st.csig !== csig;
      if (st.rsig && st.rsig !== rsig) { st.guide = null; st.pending = []; st.chapter = -1; }   // resubmitted: what was read or queued is of the old one
      st.data = r; st.sig = sig; st.rsig = rsig; st.csig = csig;
      const now = detailNow();
      const nk = keyOf(now);
      if (now && nk && nk.key === k.key && (changed || cchanged || force || !now.querySelector(".ag-review"))) paintSection(now, k, st);
      if (overlay && overlay.dataset.key === k.key) {
        if (changed || force) paintOverlay(detail, k);
        else if (cchanged) { paintBubbles(overlay, st); }
      }
    } catch (e) {
      /* the review must never break the conversation */
    } finally {
      inflight.delete(k.key);
      if (again.has(k.key)) { const f = again.get(k.key); again.delete(k.key); refreshData(detailNow() || detail, k, f); }
    }
  }

  function check() {
    const detail = detailNow();
    if (!detail || detail.hidden) return;
    const k = keyOf(detail);
    if (!k) return;
    const st = states.get(k.key);
    if (st && st.data && !detail.querySelector(".ag-review")) paintSection(detail, k, st);
    if (st && st.data) return;
    const gone = missing.get(k.key);
    if (gone && Date.now() - gone < 30000) return;
    refreshData(detail, k, false);
  }

  const obs = new MutationObserver(check);
  const start = () => obs.observe(document.body, { childList: true, subtree: true });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();

  // Somebody else commented or resubmitted: read it again while the task (or the review) is open. Every 10 s is
  // cheap, the commits' diffs are cached by the hub.
  setInterval(() => {
    if (document.hidden) return;
    const detail = detailNow();
    const k = detail && !detail.hidden ? keyOf(detail) : null;
    if (!k) return;
    const st = states.get(k.key);
    if (st && st.data) refreshData(detail, k, false);
  }, 10000);
})();

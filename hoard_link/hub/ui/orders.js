/* Hoard Hub UI — Órdenes: production orders, their plan and pieces, and the gates waiting for the person.
   Reads /api/agora/orders and /api/agora/order; the person decides a gate with /api/agora/gate_decide
   (only this page may), splits a piece off with gate_split and writes an order with order_create. */
(() => {
  "use strict";
  if (!window.HubFacets) return;
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = "/ui/orders.css";
  document.head.appendChild(css);

  const ctx = window.HubFacets.ctx;
  const es = () => ctx.lang() === "es";
  const tr = (a, b) => (es() ? a : b);
  const el = (tag, cls, text) => ctx.el(tag, cls, text);
  const KIND = {
    brief: ["Expediente", "Brief"], text: ["Texto", "Text"], image: ["Imagen", "Image"],
    character_sheet: ["Hoja de personaje", "Character sheet"], video: ["Vídeo", "Video"], audio: ["Audio", "Audio"],
    mesh: ["Malla", "Mesh"], rigged_model: ["Modelo articulado", "Rigged model"], set: ["Escenario", "Set"],
    render: ["Render", "Render"], playable_scene: ["Escena jugable", "Playable scene"], figure: ["Figura", "Figure"],
    document: ["Documento", "Document"], slides: ["Diapositivas", "Slides"], web: ["Web", "Web"],
    code_branch: ["Rama de código", "Code branch"], feature: ["Función nueva", "New feature"],
    report: ["Informe", "Report"], data: ["Datos", "Data"],
  };
  const ORDER_STATE = {
    open: ["Abierta", "Open"], producing: ["En producción", "In production"], waiting: ["Esperando tu decisión", "Waiting for you"],
    delivered: ["Entregada", "Delivered"], cancelled: ["Cancelada", "Cancelled"],
  };
  const PIECE_STATE = {
    provisional: ["Provisional", "Provisional"], checked: ["Comprobada", "Checked"], kept: ["Conservada", "Kept"],
    cancelled: ["Cancelada", "Cancelled"], stale: ["Caduca", "Stale"],
  };
  const TASK_STATE = {
    open: ["Sin empezar", "Not started"], claimed: ["Reclamada", "Claimed"], in_progress: ["En curso", "In progress"],
    review: ["En revisión", "In review"], changes: ["Devuelta", "Sent back"], approved: ["Aprobada", "Approved"],
    blocked: ["Bloqueada", "Blocked"], done: ["Hecha", "Done"], dropped: ["Descartada", "Dropped"],
  };
  const label = (map, key) => (map[key] ? tr(map[key][0], map[key][1]) : key);
  const fileUrl = (piece, i) => `/api/agora/piece_file/${piece.id}` + (i === undefined ? "" : `?preview=${i}`);
  const baseName = p => String(p || "").split(/[\\/]/).pop();

  let root = null, data = null, measures = null, lastSig = "", spaces = [], busy = false, opened = new Set(), details = new Map(), timers = [];
  const drafts = new Map();   // gate id → note being written

  function stopTimers() { for (const t of timers) clearInterval(t); timers = []; }

  // ---- previews ------------------------------------------------------------------------------------------
  function preview(piece, big) {
    const box = el("div", "ord-media ord-media-" + piece.view + (big ? " big" : ""));
    const missing = () => { box.replaceChildren(el("p", "ord-media-note", tr("El fichero no está en su sitio: ", "The file is not where it was: ") + baseName(piece.path))); };
    if (piece.path && !piece.exists && piece.view !== "web") { missing(); return box; }
    const img = (src, alt) => { const i = el("img"); i.src = src; i.alt = alt || piece.title; i.loading = "lazy"; i.onerror = missing; return i; };
    switch (piece.view) {
      case "image":
        box.append(img(piece.previews?.length && !/\.(png|jpe?g|webp|gif|svg|bmp)$/i.test(piece.path) ? fileUrl(piece, 0) : fileUrl(piece)));
        break;
      case "turntable": {
        const frames = piece.previews.map((_, i) => img(fileUrl(piece, i), `${piece.title} · ${i + 1}`));
        frames.forEach((f, i) => { f.hidden = i !== 0; box.append(f); });
        const bar = el("div", "ord-turn");
        let at = 0, auto = big;
        const show = i => { at = (i + frames.length) % frames.length; frames.forEach((f, k) => { f.hidden = k !== at; }); [...bar.children].forEach((b, k) => b.setAttribute("aria-pressed", String(k === at))); };
        frames.forEach((_, i) => { const b = el("button", "ghost small", String(i + 1)); b.type = "button"; b.title = tr("Vista ", "View ") + (i + 1); b.onclick = () => { auto = false; show(i); }; bar.append(b); });
        box.append(bar); show(0);
        if (big && frames.length > 1) timers.push(setInterval(() => { if (auto) show(at + 1); }, 1100));
        break;
      }
      case "video": { const v = el("video"); v.src = fileUrl(piece); v.controls = true; v.preload = "metadata"; v.muted = true; box.append(v); break; }
      case "audio": { const a = el("audio"); a.src = fileUrl(piece); a.controls = true; a.preload = "none"; box.append(a); break; }
      case "web": {
        // The page runs in a sandboxed frame (it never acts as this page); with screenshots, they show first.
        // A local page arrives as one document (its styles, scripts and images written in) and runs in a frame
        // without this page's origin; a page with its own address is framed from there.
        const live = async () => {
          const f = el("iframe"); f.title = piece.title;
          f.setAttribute("sandbox", "allow-scripts allow-forms");
          if (piece.url) f.src = piece.url;
          else {
            const r = await fetch(fileUrl(piece) + "?inline=1").catch(() => null);
            if (!r || !r.ok) { box.replaceChildren(el("p", "ord-media-note", tr("No se puede abrir la página.", "The page cannot be opened."))); return; }
            f.srcdoc = await r.text();
          }
          box.replaceChildren(f);
        };
        if (piece.previews?.length) {
          box.append(img(fileUrl(piece, 0)));
          const b = el("button", "ghost small ord-live", tr("Ver funcionando", "See it working"));
          b.type = "button"; b.onclick = live; box.append(b);
        } else live();
        break;
      }
      case "pdf": {
        // A browser without a PDF viewer (headless Chromium builds, some embedded views) leaves the frame blank: say so
        // and give the file to open, instead of an empty box.
        if (navigator.pdfViewerEnabled === false) {
          const a = el("a", "ghost small", tr("Abrir el PDF", "Open the PDF")); a.href = fileUrl(piece); a.target = "_blank"; a.rel = "noopener";
          box.append(el("p", "ord-media-note", tr("Este navegador no muestra PDF dentro de la página.", "This browser does not show PDFs inside the page.")), a);
          break;
        }
        const f = el("iframe"); f.title = piece.title; f.loading = "lazy"; f.src = fileUrl(piece); box.append(f); break;
      }
      case "text": {
        const pre = el("pre", "ord-text", tr("Cargando…", "Loading…")); box.append(pre);
        fetch(fileUrl(piece)).then(r => r.ok ? r.text() : Promise.reject(r.status)).then(t => { pre.textContent = t.length > 6000 ? t.slice(0, 6000) + "\n…" : t; })
          .catch(() => { pre.textContent = tr("No se puede leer el fichero.", "The file cannot be read."); });
        break;
      }
      case "mesh":
        box.append(el("p", "ord-media-note", tr("Sin vista previa: ", "No preview: ") + baseName(piece.path)));
        break;
      case "object":
        box.append(el("p", "ord-media-note", [piece.app, piece.object, piece.place].filter(Boolean).join(" · ")));
        break;
      default:
        if (piece.previews?.length) box.append(img(fileUrl(piece, 0)));
        else box.append(el("p", "ord-media-note", baseName(piece.path)));
    }
    return box;
  }

  function where(piece) {
    if (piece.url) return piece.url;
    if (piece.app) return [piece.app, piece.object, piece.place].filter(Boolean).join(" · ");
    return piece.path || "";
  }

  function keepList(piece) {
    if (!piece.keep?.length) return null;
    const ul = el("ul", "ord-keep");
    ul.setAttribute("aria-label", tr("Detalles a conservar", "Details to keep"));
    for (const k of piece.keep) {
      const li = el("li", "ord-keep-" + (k.state || "pending"));
      const mark = { ok: tr("comprobado", "checked"), missing: tr("falta", "missing"), pending: tr("sin comprobar", "not checked"),
        not_visible: tr("no se ve aquí", "not visible here") }[k.state || "pending"];
      const detail = el("span", "", k.detail);
      li.append(el("span", "ord-keep-dot"), detail, el("small", "", mark));
      const origin = k.source === "order" ? tr("de la orden", "from the order")
        : k.from ? tr("de la pieza #", "from piece #") + k.from : "";
      const why = [origin, k.how, k.evidence, k.note].filter(Boolean).join(" · ");
      if (why) li.title = why;
      if (k.anchor) {
        const a = k.anchor;
        const reference = el("a", "", tr("Referencia fijada", "Pinned reference") + ` #${a.piece} · r${a.revision}`);
        reference.href = `/api/agora/piece_file/${a.piece}` + (a.preview === undefined ? "" : `?preview=${a.preview}`);
        reference.target = "_blank"; reference.rel = "noopener";
        reference.title = [a.view, a.region?.join(", "), a.palette?.join(", "),
          a.measure && `${a.measure.value} ${a.measure.unit}`, a.sha256].filter(Boolean).join(" · ");
        detail.append(el("br"), reference);
      }
      ul.append(li);
    }
    return ul;
  }

  function pill(text, cls) { return el("span", "ord-pill " + (cls || ""), text); }

  // ---- gates waiting for the person ------------------------------------------------------------------
  async function decide(gate, decision, pieceId, note, buttons) {
    if (busy) return;
    busy = true; buttons.forEach(b => { b.disabled = true; });
    const body = { space_id: gate.space_id, gate_id: gate.id, decision, note };
    if (pieceId) body.piece_id = pieceId;
    const res = await ctx.api("/api/agora/gate_decide", body).catch(e => ({ ok: false, error: String(e) }));
    busy = false; buttons.forEach(b => { b.disabled = false; });
    if (!res?.ok && res?.error_code === "piece_changed") { ctx.toast(tr("No se ha decidido: el fichero de una pieza ha cambiado desde que se registró", "Not decided: a piece's file changed since it was registered"), "err", tr("Su puesto tiene que registrarla de nuevo.", "Its station has to register it again.")); return; }
    if (!res?.ok) { ctx.toast(tr("No se ha guardado la decisión", "The decision was not saved"), "err", res?.error || ""); return; }
    drafts.delete(gate.id);
    const stale = (res.decided || []).reduce((n, d) => n + (d.stale?.length || 0), 0);
    ctx.toast(decision === "keep" ? tr("Conservado", "Kept")
      : tr("Cancelado: vuelve a su puesto", "Cancelled: back to its station") + (stale ? tr(`; ${stale} pieza(s) derivadas quedan caducas`, `; ${stale} derived piece(s) are now stale`) : ""), "ok");
    details.delete(gate.order_id); lastSig = "";
    await load();
  }

  async function split(gate, piece) {
    const res = await ctx.api("/api/agora/gate_split", { space_id: gate.space_id, gate_id: gate.id, piece_id: piece.id });
    if (!res?.ok) { ctx.toast(tr("No se ha podido separar", "Could not split it off"), "err", res?.error || ""); return; }
    lastSig = ""; await load();
  }

  function gateCard(gate) {
    const card = el("article", "ord-gate");
    card.dataset.gate = gate.id;
    const head = el("header", "ord-gate-head");
    const h = el("h3", "", gate.title);
    const sub = el("p", "ord-gate-sub", (gate.order?.title || "") + (gate.order?.space_id ? " · " + gate.order.space_id : "") +
      " · " + tr(`${gate.pieces.length} pieza(s)`, `${gate.pieces.length} piece(s)`) + (gate.opened_by ? tr(" · la abre ", " · opened by ") + gate.opened_by : ""));
    head.append(h, sub);
    const hold = window.HubOrderMetrics?.reason(gate.budget_hold);
    if (hold) head.append(el("p", "ord-warn", hold));
    if (gate.note) head.append(el("p", "ord-gate-note", gate.note));
    for (const check of gate.technical_advisories || []) {
      const note = check.note || tr(`Comprobación automática pendiente para «${check.title}».`,
        `Technical check pending for “${check.title}”.`);
      head.append(el("p", "ord-gate-note", note));
    }
    card.append(head);
    const strip = el("div", "ord-gate-pieces");
    for (const piece of gate.pieces) {
      const fig = el("figure", "ord-piece");
      fig.dataset.piece = piece.id;
      fig.append(preview(piece, true));
      const cap = el("figcaption");
      const line = el("div", "ord-piece-line");
      line.append(el("b", "", piece.title), pill(label(KIND, piece.kind), "kind"));
      cap.append(line);
      const meta = [piece.station, piece.agent, piece.model].filter(Boolean).join(" · ");
      if (meta) cap.append(el("small", "ord-muted", meta));
      const kl = keepList(piece); if (kl) cap.append(kl);
      if (piece.defects_open) cap.append(el("small", "ord-warn", tr(`${piece.defects_open} defecto(s) abiertos`, `${piece.defects_open} open defect(s)`)));
      if (piece.bytes === "unverified") cap.append(el("small", "ord-warn", tr("Sin copia de esta revisión: se comprueba su huella al decidir.", "No copy of this revision: its print is checked when you decide.")));
      if (gate.pieces.length > 1) {
        const alone = el("button", "ghost small", tr("Decidir aparte", "Decide separately"));
        alone.type = "button"; alone.onclick = () => split(gate, piece);
        cap.append(alone);
      }
      fig.append(cap);
      strip.append(fig);
    }
    card.append(strip);
    const foot = el("div", "ord-gate-decide");
    const note = el("textarea"); note.rows = 2; note.placeholder = tr("Nota opcional: qué falta, qué cambiar…", "Optional note: what is missing, what to change…");
    note.setAttribute("aria-label", tr("Nota de la decisión", "Decision note"));
    note.value = drafts.get(gate.id) || ""; note.oninput = () => drafts.set(gate.id, note.value);
    const keep = el("button", "primary", gate.pieces.length > 1 ? tr("Conservar todo", "Keep all") : tr("Conservar", "Keep"));
    const cancel = el("button", "danger", gate.pieces.length > 1 ? tr("Cancelar todo", "Cancel all") : tr("Cancelar", "Cancel"));
    keep.type = cancel.type = "button";
    const buttons = [keep, cancel];
    keep.onclick = () => decide(gate, "keep", null, note.value.trim(), buttons);
    cancel.onclick = () => decide(gate, "cancel", null, note.value.trim(), buttons);
    const acts = el("div", "ord-gate-buttons"); acts.append(cancel, keep);
    foot.append(note, acts);
    card.append(foot);
    return card;
  }

  // ---- writing an order ------------------------------------------------------------------------------
  function composer() {
    const form = el("form", "ord-compose");
    form.append(el("h2", "", tr("Nueva orden", "New order")));
    const words = el("textarea"); words.rows = 3; words.name = "words";
    words.placeholder = tr("Qué quieres, con tus palabras. Por ejemplo: la cartógrafa sube tres escalones bajo la lluvia…",
      "What you want, in your words. For example: the cartographer climbs three steps in the rain…");
    words.setAttribute("aria-label", tr("La orden", "The order"));
    const grid = el("div", "ord-compose-grid");
    const field = (labelText, input) => { const l = el("label"); l.append(el("span", "", labelText), input); return l; };
    const title = el("input"); title.name = "title"; title.maxLength = 200; title.placeholder = tr("Se toma de la primera línea", "Taken from the first line");
    const keep = el("textarea"); keep.rows = 2; keep.name = "keep"; keep.placeholder = tr("Uno por línea: tubo a la izquierda…", "One per line: tube on the left…");
    const deliv = el("textarea"); deliv.rows = 2; deliv.name = "deliverables"; deliv.placeholder = tr("Uno por línea: lámina, vídeo de 8 s…", "One per line: concept sheet, 8 s video…");
    const folder = el("input"); folder.name = "folder"; folder.placeholder = "E:\\…";
    const audience = el("input"); audience.name = "audience"; audience.maxLength = 2000;
    const constraints = el("textarea"); constraints.rows = 2; constraints.name = "constraints";
    constraints.placeholder = tr("Una por línea", "One per line");
    const materials = el("textarea"); materials.rows = 2; materials.name = "materials";
    materials.placeholder = tr("Un fichero, carpeta o enlace por línea", "One file, folder or link per line");
    const space = el("select"); space.name = "space";
    const preferred = spaces.find(s => s.id === "estudio") ? "estudio" : "ecosystem";
    for (const s of spaces) { const o = el("option", "", s.title || s.id); o.value = s.id; if (s.id === preferred) o.selected = true; space.append(o); }
    grid.append(field(tr("Título", "Title"), title), field(tr("Proyecto", "Project"), space),
      field(tr("Detalles que no pueden perderse", "Details that must not be lost"), keep),
      field(tr("Entregables", "Deliverables"), deliv), field(tr("Carpeta de la orden", "Order folder"), folder),
      field(tr("Público destinatario", "Audience"), audience), field(tr("Restricciones", "Constraints"), constraints),
      field(tr("Materiales de partida", "Starting materials"), materials));
    const send = el("button", "primary", tr("Crear orden", "Create order")); send.type = "submit";
    const row = el("div", "ord-compose-foot");
    row.append(el("small", "ord-muted", tr("Quien dirige la parte en trabajos y te trae aquí lo que haya que decidir.",
      "Whoever leads splits it into jobs and brings here what needs your decision.")), send);
    form.append(words, grid, row);
    form.onsubmit = async ev => {
      ev.preventDefault();
      if (!words.value.trim() && !title.value.trim()) {
        ctx.toast(tr("Escribe la orden o ponle un título", "Enter the request or give it a title"), "err");
        words.focus(); return;
      }
      send.disabled = true;
      const lines = t => t.value.split("\n").map(s => s.trim()).filter(Boolean);
      const refs = lines(materials).map(value => /^https?:\/\//i.test(value) ? { url: value } : { path: value });
      const res = await ctx.api("/api/agora/order_create", { space_id: space.value, words: words.value.trim(), title: title.value.trim(),
        keep: lines(keep), deliverables: lines(deliv), folder: folder.value.trim(), refs,
        dossier: { audience: audience.value.trim(), constraints: lines(constraints) } }).catch(e => ({ ok: false, error: String(e) }));
      send.disabled = false;
      if (!res?.ok) { ctx.toast(tr("No se ha creado la orden", "The order was not created"), "err", res?.error || ""); return; }
      ctx.toast(tr("Orden creada", "Order created"), "ok");
      form.reset(); opened.add(res.order.id); lastSig = ""; await load();
    };
    return form;
  }

  // ---- orders ------------------------------------------------------------------------------------------
  function planTable(order) {
    const plan = (order.plan || []).filter(j => !j.removed);
    if (!plan.length) return el("p", "ord-muted", tr("Aún sin plan: quien dirige lo compone con hub_agora_plan_set.", "No plan yet: whoever leads composes it with hub_agora_plan_set."));
    const titleOf = Object.fromEntries(plan.map(j => [j.task_id, j.task?.title || "#" + j.task_id]));
    const table = el("table", "ord-plan");
    const head = el("tr");
    for (const h of [tr("Trabajo", "Job"), tr("Puesto", "Station"), tr("Entrega", "Delivers"), tr("Espera a", "Waits for"), tr("Para", "For"), tr("Estado", "State")]) head.append(el("th", "", h));
    const thead = el("thead"); thead.append(head); table.append(thead);
    const body = el("tbody");
    for (const j of plan) {
      const tr_ = el("tr");
      const cell = (t, cls) => { const td = el("td", cls || "", t); tr_.append(td); return td; };
      const name = cell(""); name.append(el("b", "", j.task?.title || "#" + j.task_id), el("small", "ord-muted", " #" + j.task_id));
      if (j.requirements?.length) {
        const requirements = el("details");
        requirements.append(el("summary", "", tr("Exigencias de entrada", "Input requirements")));
        const items = el("ul");
        for (const k of j.requirements) {
          const item = el("li", "", tr(k.es, k.en));
          const from = k.from;
          item.append(el("small", "ord-muted", ` · ${from.title || from.station} #${from.task_id}`));
          items.append(item);
        }
        requirements.append(items);
        name.append(requirements);
      }
      cell([j.station, j.skill].filter(Boolean).join(" · "));
      cell((j.produces || []).map(k => label(KIND, k)).join(", "));
      cell((j.after || []).map(id => titleOf[id] || "#" + id).join(", "));
      cell([j.assignee?.agent || j.task?.owner, j.assignee?.model, j.assignee?.machine].filter(Boolean).join(" · "));
      const st = cell(""); st.append(pill(label(TASK_STATE, j.task?.status), "task-" + (j.task?.status || "")));
      if (j.defects_open) st.append(" ", pill(tr(`Devuelta · ${j.defects_open} defecto(s)`, `Sent back · ${j.defects_open} defect(s)`), "task-changes"));
      body.append(tr_);
    }
    table.append(body);
    return table;
  }

  function pieceGrid(order) {
    const list = order.piece_list || [];
    if (!list.length) return el("p", "ord-muted", tr("Sin piezas todavía.", "No pieces yet."));
    const grid = el("div", "ord-pieces");
    const byId = Object.fromEntries(list.map(p => [p.id, p]));
    for (const p of list) {
      const card = el("figure", "ord-piece small state-" + p.status);
      card.dataset.piece = p.id;
      card.append(preview(p, false));
      const cap = el("figcaption");
      const line = el("div", "ord-piece-line");
      line.append(el("b", "", p.title), pill(label(PIECE_STATE, p.status), "state-" + p.status));
      cap.append(line, el("small", "ord-muted", label(KIND, p.kind) + (p.revision > 1 ? tr(` · revisión ${p.revision}`, ` · revision ${p.revision}`) : "") +
        (p.station ? " · " + p.station : "") + (p.model ? " · " + p.model : "")));
      const named = role => (p.derivation || []).filter(d => d.role === role).map(d => byId[d.piece]?.title || "#" + d.piece).join(", ");
      if (named("material")) cap.append(el("small", "ord-muted", tr("Hecha a partir de ", "Made from ") + named("material")));
      if (named("reference")) cap.append(el("small", "ord-muted", tr("Mirando ", "Looking at ") + named("reference")));
      if (p.provenance === "unspecified") cap.append(el("small", "ord-muted", tr("Procedencia sin precisar", "Provenance not given")));
      if (p.stale_because) cap.append(el("small", "ord-warn", p.stale_because));
      for (const w of p.warnings || []) cap.append(el("small", "ord-warn", w));
      const kl = keepList(p); if (kl) cap.append(kl);
      for (const d of (p.defects || []).filter(d => d.status === "open")) {
        const pos = [d.view && tr("vista ", "view ") + d.view, d.pose && "pose " + d.pose, d.at && tr("segundo ", "second ") + d.at, d.step && tr("paso ", "step ") + d.step].filter(Boolean).join(", ");
        const line = el("small", "ord-defect", (d.source === "gate" ? tr("Cancelada por ti: ", "Cancelled by you: ") : tr("Defecto: ", "Defect: ")) + d.note + (pos ? ` (${pos})` : ""));
        const more = [d.expected && tr("esperado: ", "expected: ") + d.expected, d.observed && tr("observado: ", "observed: ") + d.observed, d.evidence && tr("prueba: ", "evidence: ") + d.evidence].filter(Boolean).join(" · ");
        if (more) line.title = more;
        cap.append(line);
      }
      const loc = where(p); if (loc) { const s = el("small", "ord-path", baseName(loc) || loc); s.title = loc; cap.append(s); }
      card.append(cap);
      grid.append(card);
    }
    return grid;
  }

  function decisionList(order) {
    const ds = order.decisions || [];
    if (!ds.length) return null;
    const ul = el("ul", "ord-decisions");
    for (const d of ds) {
      const li = el("li");
      li.append(pill(d.decision === "keep" ? tr("Conservada", "Kept") : tr("Cancelada", "Cancelled"), d.decision === "keep" ? "state-kept" : "state-cancelled"),
        el("span", "", " " + (d.piece_title || "#" + d.piece_id)), el("small", "ord-muted", " · " + ctx.fmtWhen(d.created)));
      if (d.note) li.append(el("q", "", d.note));
      ul.append(li);
    }
    return ul;
  }

  function orderBody(order) {
    const box = el("div", "ord-order-body");
    const dossier = order.dossier || {};
    box.append(el("h4", "", tr("Expediente", "Dossier")));
    if (dossier.request || order.words) box.append(el("p", "ord-words", dossier.request || order.words));
    const facts = el("dl", "ord-facts");
    const fact = (k, v) => { if (!v) return; facts.append(el("dt", "", k), el("dd", "", v)); };
    fact(tr("Origen", "Comes from"), (order.refs || []).map(r => [r.app, r.object, r.place, r.path, r.url, r.note].filter(Boolean).join(" · ")).join("; "));
    fact(tr("Público destinatario", "Audience"), dossier.audience);
    fact(tr("Restricciones", "Constraints"), (dossier.constraints || []).join(" · "));
    if (dossier.materials?.length) {
      const entries = el("ul");
      const state = { unavailable: tr("No disponible", "Unavailable"), unrecorded: tr("Sin huella", "No digest"),
        changed: tr("Huella distinta de la declarada", "Digest differs from the declaration"),
        declared: tr("Huella declarada", "Declared digest") };
      for (const material of dossier.materials) {
        const name = material.path || material.url || [material.app, material.object, material.place, material.note].filter(Boolean).join(" · ");
        const item = el("li", "", [name, material.source_rev, state[material.state]].filter(Boolean).join(" · "));
        if (material.sha256) item.append(" · ", el("code", "", material.sha256));
        entries.append(item);
      }
      const value = el("dd"); value.append(entries);
      facts.append(el("dt", "", tr("Materiales de partida", "Starting materials")), value);
    }
    if (dossier.native) {
      const native = dossier.native;
      fact(tr("Expediente de origen", "Source dossier"), [native.app, native.object, native.source_rev, native.sha256].filter(Boolean).join(" · "));
    }
    fact(tr("Entregables", "Deliverables"), (order.deliverables || []).join(", "));
    fact(tr("No puede perderse", "Must not be lost"), (order.keep || []).join(" · "));
    fact(tr("Plantilla", "Template"), order.template);
    fact(tr("Carpeta", "Folder"), order.folder);
    if (facts.children.length) box.append(facts);
    box.append(el("h4", "", tr("Plan", "Plan")), planTable(order));
    box.append(el("h4", "", tr("Piezas", "Pieces")), pieceGrid(order));
    const dl = decisionList(order);
    if (window.HubOrderReceipts) box.append(window.HubOrderReceipts(order));
    if (dl) box.append(el("h4", "", tr("Tus decisiones", "Your decisions")), dl);
    return box;
  }

  async function openOrder(summary, slot) {
    slot.replaceChildren(el("p", "ord-muted", tr("Cargando…", "Loading…")));
    const res = await ctx.api(`/api/agora/order?order_id=${summary.id}&space_id=${encodeURIComponent(summary.space_id)}`).catch(() => null);
    if (!res?.ok) { slot.replaceChildren(el("p", "ord-warn", tr("No se puede leer la orden.", "The order cannot be read.") + " " + (res?.error || ""))); return; }
    details.set(summary.id, res.order);
    slot.replaceChildren(orderBody(res.order));
  }

  function orderRow(o) {
    const row = el("details", "ord-order state-" + o.status);
    row.dataset.order = o.id;
    const sum = el("summary");
    const main = el("div", "ord-order-main");
    main.append(el("b", "", o.title), el("small", "ord-muted", `${o.space_id} · #${o.id}`));
    const stats = el("div", "ord-order-stats");
    stats.append(pill(label(ORDER_STATE, o.status), "order-" + o.status),
      el("small", "", tr(`${o.jobs.done}/${o.jobs.total} trabajos`, `${o.jobs.done}/${o.jobs.total} jobs`)),
      el("small", "", tr(`${o.pieces.kept} conservadas · ${o.pieces.provisional + o.pieces.checked} en curso`, `${o.pieces.kept} kept · ${o.pieces.provisional + o.pieces.checked} in progress`)));
    const thumbs = el("div", "ord-thumbs");
    for (const t of (o.thumbs || []).filter(t => t.preview != null || t.view === "image" || t.view === "turntable").slice(0, 4)) {
      const i = el("img"); i.alt = t.title; i.loading = "lazy";
      i.src = fileUrl(t, t.preview ?? (t.view === "turntable" ? 0 : undefined));
      i.onerror = () => i.remove();
      thumbs.append(i);
    }
    sum.append(main, thumbs, stats);
    row.append(sum);
    const slot = el("div", "ord-order-slot");
    row.append(slot);
    row.addEventListener("toggle", () => {
      if (row.open) { opened.add(o.id); const cached = details.get(o.id); if (cached) slot.replaceChildren(orderBody(cached)); openOrder(o, slot); }
      else opened.delete(o.id);
    });
    if (opened.has(o.id)) row.open = true;
    return row;
  }

  // ---- page ----------------------------------------------------------------------------------------------
  function render() {
    if (!root) return;
    stopTimers();
    const page = el("div", "ord-page");
    const waiting = data?.waiting || [];
    const wait = el("section", "ord-waiting");
    wait.setAttribute("aria-labelledby", "ord-waiting-title");
    const wh = el("h2", "", waiting.length ? tr("Te esperan", "Waiting for you") : tr("Nada espera tu decisión", "Nothing waits for your decision"));
    wh.id = "ord-waiting-title";
    wait.append(wh);
    if (waiting.length) {
      wait.append(el("p", "ord-muted", tr("Conservar fija la pieza como referencia para lo que venga después. Cancelar la devuelve a su puesto con tu nota, y lo que derive de ella queda caduco.",
        "Keep fixes the piece as the reference for what comes next. Cancel sends it back to its station with your note, and what derives from it becomes stale.")));
      for (const g of waiting) wait.append(gateCard(g));
    }
    page.append(wait, composer());
    const list = el("section", "ord-list");
    list.append(el("h2", "", tr("Órdenes", "Orders")));
    const orders = data?.orders || [];
    if (!orders.length) list.append(el("p", "ord-muted", tr("Todavía no hay órdenes.", "There are no orders yet.")));
    for (const o of orders) list.append(orderRow(o));
    page.append(list);
    const stats = window.HubOrderMetrics?.render(measures);
    if (stats) page.append(stats);
    root.replaceChildren(page);
    const badge = document.getElementById("orders-badge");
    if (badge) badge.textContent = waiting.length ? String(waiting.length) : "";
  }

  async function load() {
    if (!root) return;
    const [res, sp, nextMeasures] = await Promise.all([ctx.api("/api/agora/orders?all_spaces=1").catch(e => ({ ok: false, error: String(e) })),
      spaces.length ? null : ctx.api("/api/agora/spaces").catch(() => null), window.HubOrderMetrics?.load()]);
    if (sp?.ok) spaces = sp.spaces || [];
    if (!res?.ok) { root.replaceChildren(el("p", "ord-warn", tr("No se pueden leer las órdenes: ", "Orders cannot be read: ") + (res?.error || ""))); return; }
    const sig = JSON.stringify([res, nextMeasures, ctx.lang()]);
    if (sig === lastSig && root.childElementCount) return;   // nothing changed: keep players and open rows as they are
    lastSig = sig; data = res; measures = nextMeasures;
    render();
  }

  function typing() {
    const a = document.activeElement;
    return !!(root && a && root.contains(a) && /^(TEXTAREA|INPUT|SELECT)$/.test(a.tagName)) || [...drafts.values()].some(Boolean);
  }

  let ticks = 0;
  window.HubFacets.register("orders", {
    placement: "tab", order: 5, label: { es: "Órdenes", en: "Orders" },
    mount(container) { root = container; root.classList.add("ord-root"); },
    load() { return load(); },
    tick() { if (++ticks % 3 === 0 && !busy && !typing()) load(); },
    copilot: { context: () => ({ active_objects: [], view: { section: "orders", waiting: (data?.waiting || []).length } }) },
  });
})();

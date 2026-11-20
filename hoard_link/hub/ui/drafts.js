/* Hoard Hub UI — Pasada 0 (draft pass): the early gate on the Orders page.
   Reads /api/agora/draft_passes?all_spaces=1 and, while a draft pass waits for the person, shows its variants side by
   side at the top of the Orders page, with decision media first and production details on demand.
   The person keeps one variant or discards one, with a note, through /api/agora/draft_decide (only this
   page may). It does not change orders.js: it adds its own section into the Orders page whenever that page renders. */
(() => {
  "use strict";
  if (!window.HubFacets) return;
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = "/ui/drafts.css";
  document.head.appendChild(css);

  const ctx = window.HubFacets.ctx;
  const es = () => ctx.lang() === "es";
  const tr = (a, b) => (es() ? a : b);
  const el = (tag, cls, text) => ctx.el(tag, cls, text);
  const ANCHOR = {
    blocking: ["Bloqueo", "Blocking"], blocking_color: ["Color del bloqueo", "Blocking colour"], parts: ["Partes", "Parts"],
    depth: ["Profundidad", "Depth"], normals: ["Normales", "Normals"], composition: ["Imagen de borrador", "Draft image"],
    keyframe_start: ["Fotograma inicial", "First key frame"], keyframe: ["Fotograma clave", "Key frame"],
    keyframe_end: ["Fotograma final", "Last key frame"], animatic: ["Toma corta", "Short take"], outline: ["Esquema", "Outline"],
  };
  const decisionAnchors = ["keyframe_start", "keyframe_end", "animatic", "keyframe", "composition"];
  const anchorLabel = a => (ANCHOR[a] ? tr(ANCHOR[a][0], ANCHOR[a][1]) : (a || tr("Pieza", "Piece")));
  const fileUrl = (piece, i) => `/api/agora/piece_file/${piece.id}` + (i === undefined ? "" : `?preview=${i}`);
  const isVideo = piece => /\.(mp4|webm|mov|m4v)$/i.test(piece.path || "");
  const secs = s => (s === null || s === undefined ? "–" : s >= 90 ? `${(s / 60).toFixed(1)} min` : `${Math.round(s)} s`);

  let passes = [], lastSig = "", busy = false, force = false;
  const notes = new Map();   // variant id → note being written
  const changed = new Set(); // variants that changed while the person was looking at them
  const expandedDetails = new Set();

  function figureControls(pass, variant, piece, video) {
    const controls = el("div", "ord-gate-buttons");
    const request = el("button", "ghost small", tr("Hacer figura de este fotograma", "Make a figure from this frame"));
    const undo = el("button", "ghost small", tr("Deshacer encargo", "Undo request"));
    const status = el("small", "ord-muted");
    status.setAttribute("role", "status");
    request.type = undo.type = "button";
    undo.hidden = true;
    controls.append(request, undo, status);
    let pending = false, token = null;
    const refresh = () => { request.disabled = pending || !video.paused || video.seeking || video.readyState < 2; };
    for (const event of ["loadeddata", "play", "pause", "seeking", "seeked", "emptied"]) {
      video.addEventListener(event, refresh);
    }
    refresh();
    request.onclick = async () => {
      if (request.disabled) return;
      pending = true;
      refresh();
      const result = await ctx.api("/api/agora/draft_figure_request", {
        space_id: pass.space_id, variant_id: variant.id, take_piece: piece.id,
        signature: variant.signature, seconds: video.currentTime,
      }).catch(error => ({ok: false, error: String(error)}));
      if (result?.ok) {
        token = result.undo || (result.existing ? token : null);
        undo.hidden = !token;
        status.textContent = tr(`Figura encargada · tarea #${result.task_id} · f${result.frame}`,
          `Figure requested · task #${result.task_id} · f${result.frame}`);
      } else {
        status.textContent = tr("No se ha encargado la figura.", "The figure was not requested.") + " " + (result?.error || "");
      }
      pending = false;
      refresh();
    };
    undo.onclick = async () => {
      undo.disabled = true;
      const result = await ctx.api("/api/agora/order_undo", {space_id: pass.space_id, token})
        .catch(error => ({ok: false, error: String(error)}));
      undo.disabled = false;
      if (result?.ok) {
        token = null;
        undo.hidden = true;
        status.textContent = tr("Encargo deshecho.", "Request undone.");
      } else {
        status.textContent = tr("No se ha podido deshacer.", "The request could not be undone.") + " " + (result?.error || "");
      }
    };
    return controls;
  }

  function media(piece) {
    const box = el("div", "drf-media");
    const isImage = /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(piece.path || "");
    const preview = piece.previews?.length ? fileUrl(piece, 0) : null;
    const src = isImage ? fileUrl(piece) : preview;
    if (isVideo(piece)) {
      const v = el("video"); v.src = fileUrl(piece); v.controls = true; v.muted = true; v.playsInline = true; v.preload = "metadata";
      if (preview) v.poster = preview;
      box.append(v);
    } else if (src) {
      const img = el("img"); img.src = src; img.alt = piece.title; img.loading = "lazy";
      img.onerror = () => box.replaceChildren(el("p", "ord-media-note", tr("No se puede mostrar.", "It cannot be shown.")));
      const a = el("a", "drf-image-link"); a.href = src; a.target = "_blank"; a.rel = "noopener"; a.title = tr("Abrir a tamaño completo", "Open full size");
      a.append(img); box.append(a);
    } else {
      const a = el("a", "ghost small", tr("Abrir ", "Open ") + String(piece.path || piece.url || piece.title).split(/[\\/]/).pop());
      a.href = piece.url || fileUrl(piece); a.target = "_blank"; a.rel = "noopener";
      box.append(a);
    }
    return box;
  }

  function pieceStrip(pass, variant, pieces, cls) {
    const strip = el("div", cls);
    const blocking = variant.pieces.filter(p => p.anchor === "blocking" && /\.glb$/i.test(p.path || "") && p.sha256);
    for (const p of pieces) {
      const fig = el("figure", "drf-piece" + (p.anchor === "animatic" ? " drf-wide" : ""));
      const contents = media(p);
      fig.append(contents);
      const cap = el("figcaption");
      cap.append(el("b", "", anchorLabel(p.anchor)), el("small", "ord-muted", p.title));
      if (isVideo(p) && p.previews?.length) {
        const preview = el("a", "", tr("Abrir imagen de vista previa", "Open preview image"));
        preview.href = fileUrl(p, 0); preview.target = "_blank"; preview.rel = "noopener";
        cap.append(preview);
      }
      const also = pass.variants.filter(o => o.id !== variant.id && o.status !== "discarded" && o.pieces.some(q => q.id === p.id)).map(o => o.key.toUpperCase());
      if (also.length) cap.append(el("small", "drf-shared", tr(`También en ${also.join(", ")}: se conserva si se conserva cualquiera de ellas`, `Also in ${also.join(", ")}: kept if any of them is kept`)));
      if (isVideo(p) && p.anchor === "animatic" && p.sha256 && blocking.length === 1) {
        cap.append(figureControls(pass, variant, p, contents.querySelector("video")));
      }
      fig.append(cap);
      strip.append(fig);
    }
    return strip;
  }

  async function decide(pass, variant, decision, note, buttons) {
    if (busy) return;
    busy = true; buttons.forEach(b => { b.disabled = true; });
    // the signature says what this page showed: a variant changed since then has to be seen again before deciding
    const res = await ctx.api("/api/agora/draft_decide", { space_id: pass.space_id, variant_id: variant.id, decision, note,
                                                           signature: variant.signature })
      .catch(e => ({ ok: false, error: String(e) }));
    busy = false; buttons.forEach(b => { b.disabled = false; });
    if (!res?.ok && res?.error_code === "variant_changed") {
      changed.add(variant.id);
      ctx.toast(tr(`No se ha decidido: «${variant.title}» ha cambiado mientras la mirabas`, `Not decided: «${variant.title}» changed while you were looking at it`),
        "err", tr("Ya se muestra como está ahora. Mírala y vuelve a decidir.", "It now shows as it is. Look at it and decide again."));
      lastSig = ""; force = true; await load(); return;
    }
    if (!res?.ok && res?.error_code === "piece_changed") {
      ctx.toast(tr("No se ha decidido: el fichero de una pieza ha cambiado desde que se registró", "Not decided: a piece's file changed since it was registered"), "err",
        tr("Su puesto tiene que registrarla de nuevo.", "Its station has to register it again.")); return;
    }
    if (!res?.ok) { ctx.toast(tr("No se ha guardado la decisión", "The decision was not saved"), "err", res?.error || ""); return; }
    notes.delete(variant.id); changed.delete(variant.id); expandedDetails.delete(variant.id);
    ctx.toast(decision === "keep" ? tr(`Conservada «${variant.title}»: la pasada cara partirá de ella`, `Kept «${variant.title}»: the full pass starts from it`)
      : tr(`Descartada «${variant.title}»: queda guardada con tu nota`, `Discarded «${variant.title}»: it stays, with your note`), "ok");
    lastSig = ""; await load();
  }

  function variantCard(pass, v) {
    const card = el("article", "drf-variant");
    card.dataset.variant = v.id;
    const head = el("header", "drf-variant-head");
    head.append(el("span", "drf-key", v.key.toUpperCase()), el("h4", "", v.title));
    card.append(head);
    if (changed.has(v.id)) card.append(el("p", "drf-changed", tr("Ha cambiado mientras la mirabas: esto es lo que hay ahora.", "It changed while you were looking at it: this is what it is now.")));
    const decisionPieces = decisionAnchors.flatMap(anchor => v.pieces.filter(p => p.anchor === anchor));
    const technicalPieces = v.pieces.filter(p => !decisionAnchors.includes(p.anchor));
    if (decisionPieces.length) card.append(pieceStrip(pass, v, decisionPieces, "drf-decision-media"));
    if (v.idea) card.append(el("p", "drf-idea", v.idea));
    if (v.note) card.append(el("p", "drf-note", v.note));

    const details = el("details", "drf-technical");
    details.open = expandedDetails.has(v.id);
    details.ontoggle = () => {
      if (details.open) expandedDetails.add(v.id);
      else expandedDetails.delete(v.id);
    };
    details.append(el("summary", "", tr("Ver cómo se hizo", "See how it was made")));
    const m = v.measures || {};
    const facts = [tr("Tiempo ", "Time ") + secs(m.seconds), "GPU " + secs(m.gpu_seconds)];
    if (m.gpu) facts.push(m.gpu);
    if (m.engine) facts.push(m.engine);
    details.append(el("p", "ord-muted", facts.join(" · ")));
    if (technicalPieces.length) details.append(pieceStrip(pass, v, technicalPieces, "drf-strip"));
    card.append(details);

    const foot = el("div", "drf-decide");
    const note = el("textarea"); note.rows = 2;
    note.placeholder = tr("Nota opcional: qué te gusta o qué cambiar…", "Optional note: what you like or what to change…");
    note.setAttribute("aria-label", tr(`Nota sobre «${v.title}»`, `Note on «${v.title}»`));
    note.value = notes.get(v.id) || ""; note.oninput = () => notes.set(v.id, note.value);
    const keep = el("button", "primary", tr("Conservar esta", "Keep this one"));
    const drop = el("button", "danger", tr("Descartar", "Discard"));
    keep.type = drop.type = "button";
    const buttons = [keep, drop];
    keep.onclick = () => decide(pass, v, "keep", note.value.trim(), buttons);
    drop.onclick = () => decide(pass, v, "cancel", note.value.trim(), buttons);
    const acts = el("div", "ord-gate-buttons"); acts.append(drop, keep);
    foot.append(note, acts);
    card.append(foot);
    return card;
  }

  function passCard(pass) {
    const card = el("article", "ord-gate drf-pass");
    card.dataset.draftPass = pass.id;
    const head = el("header", "ord-gate-head");
    head.append(el("h3", "", pass.title));
    const t = pass.totals || {}, b = pass.baseline || {};
    let line = (pass.order?.title || "") + (pass.space_id ? " · " + pass.space_id : "") + " · " +
      tr(`${t.variants} variantes en ${secs(t.seconds)} (GPU ${secs(t.gpu_seconds)})`, `${t.variants} variants in ${secs(t.seconds)} (GPU ${secs(t.gpu_seconds)})`);
    if (b.seconds) line += tr(` · la pasada completa tardó ${secs(b.seconds)}`, ` · the full pass took ${secs(b.seconds)}`) +
      (b.gpu_seconds ? tr(` (GPU ${secs(b.gpu_seconds)})`, ` (GPU ${secs(b.gpu_seconds)})`) : "");
    head.append(el("p", "ord-gate-sub", line));
    if (pass.note) head.append(el("p", "ord-gate-note", pass.note));
    card.append(head);
    const grid = el("div", "drf-variants");
    for (const v of pass.variants) if (v.status !== "kept" && v.status !== "discarded") grid.append(variantCard(pass, v));
    card.append(grid);
    const done = pass.variants.filter(v => v.status === "discarded");
    if (done.length) card.append(el("p", "ord-muted", tr("Ya descartadas: ", "Already discarded: ") + done.map(v => v.title).join(", ")));
    return card;
  }

  function paint() {
    const page = document.querySelector(".ord-root .ord-page");
    if (!page) return;
    let section = page.querySelector(":scope > .drf-early");
    const badge = document.getElementById("orders-badge");
    // orders.js writes the open gates into the badge on every repaint; this section adds the passes it shows
    const recount = (before, after) => { if (badge) { const n = (parseInt(badge.textContent, 10) || 0) - before + after; badge.textContent = n ? String(n) : ""; } };
    if (!passes.length) { if (section) { recount(+section.dataset.count || 0, 0); section.remove(); } return; }
    if (section && section.dataset.sig === lastSig) return;
    const fresh = el("section", "drf-early");
    fresh.dataset.sig = lastSig;
    fresh.dataset.count = String(passes.length);
    fresh.setAttribute("aria-labelledby", "drf-title");
    const h = el("h2", "", tr("Pasada 0: elige la estructura", "Draft pass: choose the structure")); h.id = "drf-title";
    fresh.append(h, el("p", "ord-muted", tr(
      "Cada variante es la cadena entera en borrador. Conservar una fija su encuadre, sus partes y sus fotogramas clave para la pasada cara; las demás quedan guardadas como descartadas con tu nota.",
      "Each variant is the whole chain in draft. Keeping one fixes its framing, parts and key frames for the full pass; the others stay as discarded, with your note.")));
    for (const p of passes) fresh.append(passCard(p));
    const focus = document.activeElement && section?.contains(document.activeElement);
    if (focus && !force) return;   // never repaint under the person's hands, unless what they see is out of date
    force = false;
    recount(section ? +section.dataset.count || 0 : 0, passes.length);
    if (section) section.replaceWith(fresh); else page.prepend(fresh);
  }

  async function load() {
    const res = await ctx.api("/api/agora/draft_passes?all_spaces=1").catch(() => null);
    if (!res?.ok) return;
    const sig = JSON.stringify(res.draft_passes);
    if (sig !== lastSig) { lastSig = sig; passes = res.draft_passes || []; }
    paint();
  }

  // The Orders page repaints its whole body; put the early gate back each time it does.
  const watch = new MutationObserver(() => paint());
  const attach = () => {
    const root = document.querySelector(".ord-root");
    if (!root) return false;
    watch.observe(root, { childList: true });
    load();
    return true;
  };
  if (!attach()) {
    const wait = new MutationObserver(() => { if (attach()) wait.disconnect(); });
    wait.observe(document.body, { childList: true, subtree: true });
  }
  setInterval(() => { if (document.querySelector(".ord-root") && !busy && ![...notes.values()].some(Boolean)) load(); }, 6000);
})();

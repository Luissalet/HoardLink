/* Hoard Hub UI — Pasada 0 (draft pass): the early gate on the Orders page.
   Reads /api/agora/draft_passes?all_spaces=1 and, while a draft pass waits for the person, shows its variants side by
   side at the top of the Orders page, each end to end (blocking, depth, normals, key frames, draft image…) with its
   time and GPU. The person keeps one variant or discards one, with a note, through /api/agora/draft_decide (only this
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
    keyframe_end: ["Fotograma final", "Last key frame"], animatic: ["Animática", "Animatic"], outline: ["Esquema", "Outline"],
  };
  const anchorLabel = a => (ANCHOR[a] ? tr(ANCHOR[a][0], ANCHOR[a][1]) : (a || tr("Pieza", "Piece")));
  const fileUrl = (piece, i) => `/api/agora/piece_file/${piece.id}` + (i === undefined ? "" : `?preview=${i}`);
  const secs = s => (s === null || s === undefined ? "–" : s >= 90 ? `${(s / 60).toFixed(1)} min` : `${Math.round(s)} s`);

  let passes = [], lastSig = "", busy = false;
  const notes = new Map();   // variant id → note being written

  function media(piece) {
    const box = el("div", "drf-media");
    const isImage = /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(piece.path || "");
    const src = isImage ? fileUrl(piece) : (piece.previews?.length ? fileUrl(piece, 0) : null);
    if (src) {
      const img = el("img"); img.src = src; img.alt = piece.title; img.loading = "lazy";
      img.onerror = () => box.replaceChildren(el("p", "ord-media-note", tr("No se puede mostrar.", "It cannot be shown.")));
      const a = el("a"); a.href = src; a.target = "_blank"; a.rel = "noopener"; a.title = tr("Abrir a tamaño completo", "Open full size");
      a.append(img); box.append(a);
    } else if (/\.(mp4|webm|mov|m4v)$/i.test(piece.path || "")) {
      const v = el("video"); v.src = fileUrl(piece); v.controls = true; v.muted = true; v.preload = "metadata"; box.append(v);
    } else {
      const a = el("a", "ghost small", tr("Abrir ", "Open ") + String(piece.path || piece.url || piece.title).split(/[\\/]/).pop());
      a.href = piece.url || fileUrl(piece); a.target = "_blank"; a.rel = "noopener";
      box.append(a);
    }
    return box;
  }

  async function decide(pass, variant, decision, note, buttons) {
    if (busy) return;
    busy = true; buttons.forEach(b => { b.disabled = true; });
    const res = await ctx.api("/api/agora/draft_decide", { space_id: pass.space_id, variant_id: variant.id, decision, note })
      .catch(e => ({ ok: false, error: String(e) }));
    busy = false; buttons.forEach(b => { b.disabled = false; });
    if (!res?.ok) { ctx.toast(tr("No se ha guardado la decisión", "The decision was not saved"), "err", res?.error || ""); return; }
    notes.delete(variant.id);
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
    if (v.idea) card.append(el("p", "drf-idea", v.idea));
    const m = v.measures || {};
    const facts = [tr("Tiempo ", "Time ") + secs(m.seconds), "GPU " + secs(m.gpu_seconds)];
    if (m.gpu) facts.push(m.gpu);
    if (m.engine) facts.push(m.engine);
    card.append(el("p", "ord-muted", facts.join(" · ")));
    const strip = el("div", "drf-strip");
    for (const p of v.pieces) {
      const fig = el("figure", "drf-piece" + (p.anchor === "animatic" ? " drf-wide" : ""));
      fig.append(media(p));
      const cap = el("figcaption");
      cap.append(el("b", "", anchorLabel(p.anchor)), el("small", "ord-muted", p.title));
      fig.append(cap);
      strip.append(fig);
    }
    card.append(strip);
    if (v.note) card.append(el("p", "drf-note", v.note));
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
    if (focus) return;   // never repaint under the person's hands
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

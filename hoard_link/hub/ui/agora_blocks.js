/* Hoard Hub UI — Ágora submission blocks (companion to agora.js).
   Paints task.submission_blocks and collects per-block review comments without editing agora.js
   (that path may be locked by another task). Loads as a second ui_script on the agora facet. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H || !H.ctx) return;
  const { el, api, toast, L } = H.ctx;

  const S = {
    blocks: { es: "Bloques de la entrega", en: "Submission blocks" },
    peek: { es: "Vista de código", en: "Code peek" },
    verified: { es: "verificado", en: "verified" },
    comment_block: { es: "Nota de este bloque", en: "Note for this block" },
    paths: { es: "Rutas", en: "Paths" },
    steps: { es: "Pasos", en: "Steps" },
  };
  const t = (k) => L(S[k] || { es: k, en: k });

  function injectCss() {
    if (document.getElementById("css-agora-blocks")) return;
    const css = document.createElement("style");
    css.id = "css-agora-blocks";
    css.textContent = `
      .ag-blocks { display: grid; gap: 8px; margin: 8px 0 12px; padding: 8px; border: 1px solid var(--border, #333); border-radius: 8px; }
      .ag-blocks h4 { margin: 0 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: .4px; color: var(--muted); }
      .ag-block { border: 1px solid var(--border, #333); border-radius: 6px; padding: 8px; display: grid; gap: 6px; background: color-mix(in srgb, var(--panel, #1a1a1a) 92%, transparent); }
      .ag-block-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; font-size: 13px; }
      .ag-block-kind { font-family: ui-monospace, monospace; font-size: 11px; color: var(--muted); }
      .ag-block pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; max-height: 220px; overflow: auto; }
      .ag-block textarea { width: 100%; min-height: 52px; resize: vertical; }
    `;
    document.head.appendChild(css);
  }

  function blockView(b, withComments) {
    const card = el("div", "ag-block");
    card.dataset.blockId = b.id || "";
    const head = el("div", "ag-block-head");
    head.appendChild(el("b", "", b.title || b.id || b.kind));
    head.appendChild(el("span", "ag-block-kind", `[${b.id}] ${b.kind}`));
    card.appendChild(head);
    if (b.kind === "code_peek" && b.peek) {
      const p = b.peek;
      card.appendChild(el("div", "hint", `${t("peek")}: ${p.ref} · ${p.verified ? t("verified") : "?"}`));
      if (p.checkout) card.appendChild(el("div", "hint", p.checkout));
      const pre = el("pre", "");
      pre.textContent = p.text || "";
      card.appendChild(pre);
    } else if (b.kind === "paths") {
      card.appendChild(el("div", "hint", t("paths")));
      card.appendChild(el("pre", "", (b.paths || []).join("\n")));
    } else if (b.kind === "flow" || b.kind === "sequence") {
      card.appendChild(el("div", "hint", t("steps")));
      card.appendChild(el("pre", "", (b.steps || []).map((s) => "→ " + s).join("\n")));
    } else if (b.body) {
      card.appendChild(el("pre", "", b.body));
    }
    if (withComments) {
      const ta = el("textarea");
      ta.placeholder = t("comment_block");
      ta.dataset.blockComment = b.id || "";
      card.appendChild(ta);
    }
    return card;
  }

  function collectBlockComments(root) {
    const out = [];
    for (const ta of root.querySelectorAll("textarea[data-block-comment]")) {
      const body = ta.value.trim();
      if (!body) continue;
      out.push({ block_id: ta.dataset.blockComment, body });
    }
    return out;
  }

  function paint(detail, task) {
    injectCss();
    const blocks = task && task.submission_blocks;
    if (!detail || !Array.isArray(blocks) || !blocks.length) return;
    if (detail.querySelector(".ag-blocks")) return;
    const wrap = el("section", "ag-blocks");
    wrap.appendChild(el("h4", "", t("blocks")));
    const canComment = task && ["review", "approved", "changes"].includes(task.status);
    for (const b of blocks) wrap.appendChild(blockView(b, canComment));
    const msgs = detail.querySelector(".ag-msgs");
    if (msgs) detail.insertBefore(wrap, msgs);
    else {
      const compose = detail.querySelector(".ag-compose");
      if (compose) detail.insertBefore(wrap, compose);
      else detail.appendChild(wrap);
    }
  }

  // Wrap shared api so we can paint after task loads and attach block_comments on review.
  const original = api;
  if (original.__agoraBlocksWrapped) return;
  async function wrapped(url, body, opts) {
    const path = String(url || "");
    let payload = body;
    try {
      if (payload && payload.task_id != null && path.includes("/api/agora/task_review") && !payload.block_comments) {
        const detail = document.querySelector(".ag-detail");
        const notes = detail ? collectBlockComments(detail) : [];
        if (notes.length) payload = { ...payload, block_comments: notes };
      }
    } catch (e) {
      /* collecting notes must never break review */
    }
    const result = await original(url, payload, opts);
    try {
      if (result && result.ok && /^\/api\/agora\/tasks\/\d+/.test(path.split("?")[0]) && result.task) {
        const detail = document.querySelector(".ag-detail");
        if (detail && !detail.hidden) {
          requestAnimationFrame(() => paint(detail, result.task));
        }
      }
    } catch (e) {
      /* painting must never break the API */
    }
    return result;
  }
  wrapped.__agoraBlocksWrapped = true;
  H.ctx.api = wrapped;

  // Observe detail rebuilds from agora.js polling.
  const obs = new MutationObserver(() => {
    const detail = document.querySelector(".ag-detail");
    if (!detail || detail.hidden) return;
    if (detail.querySelector(".ag-blocks")) return;
    const key = detail.dataset.key || "";
    const m = /^task-(\d+)$/.exec(key);
    if (!m) return;
    original(`/api/agora/tasks/${m[1]}`).then((r) => {
      if (r && r.ok && r.task) paint(detail, r.task);
    }).catch(() => {});
  });
  const start = () => {
    const host = document.querySelector(".ag-detail") || document.body;
    obs.observe(host.parentElement || document.body, { childList: true, subtree: true });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();

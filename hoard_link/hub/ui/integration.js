(() => {
  "use strict";

  const api = "/api/integration";
  const labels = {
    queued: ["En cola", "Queued"],
    waiting_for_suite_slot: ["Esperando turno de pruebas", "Waiting for test slot"],
    testing_main: ["Probando la rama principal", "Testing main"],
    testing_candidate: ["Probando la entrega", "Testing the delivery"],
    testing_inference: ["Probando inferencia", "Testing inference"],
    integrating: ["Fusionando", "Integrating"],
    checking_live_case: ["Comprobando el caso de origen", "Checking the original case"],
    integrated: ["Integrada", "Integrated"],
    checked: ["Comprobada", "Checked"],
    failed: ["Fallida", "Failed"],
    regressions: ["Con fallos nuevos", "New failures"],
    inference_failed: ["Falló la inferencia", "Inference failed"],
    needs_attention: ["Requiere atención", "Needs attention"],
    interrupted: ["Interrumpida", "Interrupted"],
    rolled_back: ["Fusión revertida", "Merge rolled back"],
    cancelled: ["Cancelada", "Cancelled"],
    undone: ["Deshecha", "Undone"],
  };
  const t = (ctx, es, en) => ctx.L({es, en});
  function state(ctx, value) {
    return t(ctx, ...(labels[value] || labels.integrating));
  }

  function renderReceipt(ctx, target, receipt) {
    const {el} = ctx;
    const content = [];
    if (receipt.comparison) {
      const {main, candidate, known, new: regressions} = receipt.comparison;
      content.push(el("p", "", t(ctx,
        `Principal: ${main.counts.passed} correctas. Entrega: ${candidate.counts.passed} correctas.`,
        `Main: ${main.counts.passed} passed. Delivery: ${candidate.counts.passed} passed.`)));
      content.push(el("p", "", t(ctx,
        `${Object.keys(known).length} fallos conocidos · ${Object.keys(regressions).length} fallos nuevos`,
        `${Object.keys(known).length} known failures · ${Object.keys(regressions).length} new failures`)));
      if (Object.keys(regressions).length) {
        const list = el("ul");
        for (const test of Object.keys(regressions)) list.append(el("li", "", test));
        content.push(list);
      }
    }
    if (receipt.live) {
      const passed = receipt.live.counts.passed;
      content.push(el("p", "", passed
        ? t(ctx, "Caso de origen comprobado tras la fusión", "Original case passed after integration")
        : t(ctx, "Falló el caso de origen", "Original case failed")));
    }
    if (receipt.merge_commit) {
      const commit = el("p", "", t(ctx, "Commit de fusión ", "Merge commit "));
      commit.append(el("code", "", receipt.merge_commit.slice(0, 12)));
      content.push(commit);
    }
    if (receipt.cleanup) {
      if (receipt.cleanup.retained) content.push(el("p", "",
        t(ctx, "Árbol de entrega conservado porque contiene cambios", "Delivery worktree kept because it contains changes")));
      if (receipt.cleanup.removed_branch && receipt.state !== "undone") content.push(el("p", "",
        t(ctx, "Rama de entrega retirada", "Delivery branch removed")));
    }
    if (receipt.error) {
      const error = el("p", "muted", receipt.error);
      error.style.overflowWrap = "anywhere";
      content.push(error);
    }
    target.replaceChildren(...content);
  }

  let root;
  let lastRows;
  let generation = 0;
  async function load(ctx, quiet = false) {
    const {el} = ctx;
    const id = ++generation;
    if (!quiet) {
      const loading = el("p", "muted", t(ctx, "Cargando integraciones…", "Loading integrations…"));
      loading.setAttribute("role", "status");
      root.replaceChildren(loading);
      lastRows = undefined;
    }
    try {
      const result = await ctx.api(api);
      if (id !== generation) return;
      if (!result.ok) throw new Error();
      const rows = JSON.stringify(result.receipts);
      if (rows === lastRows) return;
      lastRows = rows;
      const open = new Set([...root.querySelectorAll("details[open]")].map(row => row.dataset.id));
      if (!result.receipts.length) {
        root.replaceChildren(el("p", "muted", t(ctx, "Todavía no hay integraciones.", "No integrations yet.")));
        return;
      }
      const list = el("div", "hub-project-list");
      for (const row of result.receipts) {
        const details = el("details", "hub-project-row");
        details.style.display = "block";
        details.dataset.id = row.id;
        details.open = open.has(row.id);
        const summary = el("summary", "", `#${row.task_id} · ${row.repo} · ${state(ctx, row.state)}`);
        const body = el("div");
        details.append(summary, body);
        const show = async () => {
          if (!details.open || body.hasChildNodes()) return;
          body.append(el("p", "muted", t(ctx, "Cargando recibo…", "Loading receipt…")));
          try {
            const data = await ctx.api(`${api}?id=${encodeURIComponent(row.id)}`);
            if (!data.ok) throw new Error();
            renderReceipt(ctx, body, data.receipt);
          } catch {
            body.replaceChildren(el("p", "", t(ctx, "No se pudo leer el recibo.", "Could not read the receipt.")));
          }
        };
        details.addEventListener("toggle", show);
        if (details.open) show();
        list.append(details);
      }
      root.replaceChildren(list);
    } catch {
      if (id !== generation) return;
      lastRows = undefined;
      const error = el("p", "", t(ctx, "No se pudieron cargar las integraciones.", "Could not load integrations."));
      error.setAttribute("role", "alert");
      const retry = el("button", "ghost small", t(ctx, "Reintentar", "Retry"));
      retry.type = "button";
      retry.onclick = () => load(ctx);
      root.replaceChildren(error, retry);
    }
  }

  window.HubFacets.register("integration", {
    placement: "tab",
    label: {es: "Integraciones", en: "Integrations"},
    mount(container) { root = container; },
    load,
    tick(ctx) { load(ctx, true); },
  });
})();

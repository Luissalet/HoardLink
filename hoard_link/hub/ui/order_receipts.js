/* Tool receipts and skill checks inside an order. */
(() => {
  "use strict";
  const ctx = window.HubFacets?.ctx;
  if (!ctx) return;
  const t = (es, en) => ctx.lang() === "es" ? es : en;
  const el = (tag, text) => ctx.el(tag, "", text);
  const api = "/api/production/";
  const pretty = data => JSON.stringify(data, null, 2);
  const css = el("link");
  css.rel = "stylesheet";
  css.href = "/ui/order_receipts.css";
  document.head.append(css);

  window.HubOrderReceipts = order => {
    const scope = { order_id: order.id, space_id: order.space_id };
    const query = new URLSearchParams(scope);
    const panel = el("details");
    panel.className = "ord-receipts";
    const body = el("div");
    const status = el("p");
    status.setAttribute("role", "status");
    panel.append(el("summary", t("Recibos y pruebas de skills", "Receipts and skill tests")), status, body);
    let generation = 0;

    async function request(path, data) {
      const answer = await ctx.api(api + path, data);
      if (!answer?.ok) throw new Error(t("El Hub no ha completado la operación.", "The Hub did not complete the operation.")
        + (answer?.error ? " " + answer.error : ""));
      return answer;
    }

    function button(label, action) {
      const node = el("button", label);
      node.type = "button";
      node.addEventListener("click", async () => {
        status.textContent = "";
        node.disabled = true;
        try { await action(); }
        catch (err) { status.textContent = err.message; }
        finally { node.disabled = false; }
      });
      return node;
    }

    function expansion(title, load) {
      const item = el("details");
      const content = el("div");
      item.append(el("summary", title), content);
      item.addEventListener("toggle", async () => {
        if (!item.open || content.children.length) return;
        content.textContent = "";
        try { await load(content); }
        catch (err) { content.textContent = err.message; }
      });
      return item;
    }

    function field(parent, title, tag) {
      const label = el("label", title + " ");
      const input = el(tag);
      label.append(input);
      parent.append(label);
      return input;
    }

    function receipts(answer, container) {
      for (const row of answer.receipts) {
        const name = [row.app || row.station, row.tool, `#${row.id}`].join(" · ");
        const verdict = row.ok === null ? t("Sin respuesta registrada", "No recorded reply")
          : row.ok ? t("Correcta", "Succeeded") : t("Fallida", "Failed");
        container.append(expansion(`${name} · ${verdict}`, async content => {
          const result = await request("order_receipt?" + query + "&receipt_id=" + row.id);
          content.append(el("h5", t("Argumentos", "Arguments")), el("pre", pretty(result.receipt.arguments)),
            el("h5", t("Respuesta", "Reply")), el("pre", pretty(result.receipt.reply)));
          if (row.task_id) {
            content.append(button(t("Crear borrador de la skill", "Draft the skill"), async () => {
              const result = await request("order_skill_draft", { ...scope, task_id: row.task_id });
              await refresh();
              status.textContent = t("Borrador guardado", "Draft saved") + " · " + result.draft.slug;
            }));
          }
          if (row.ok) content.append(benchForm(row, answer.drafts));
        }));
      }
      if (answer.next_before) {
        const more = button(t("Ver recibos anteriores", "Show older receipts"), async () => {
          const older = await request("order_receipts?" + query + "&before=" + answer.next_before);
          more.remove();
          receipts(older, container);
        });
        container.append(more);
      }
    }

    function benchForm(row, drafts) {
      const form = el("details");
      form.className = "ord-receipts-bench";
      form.append(el("summary", t("Probar una skill con este recibo", "Test a skill against this receipt")));
      const draft = field(form, t("Borrador", "Draft"), "select");
      const published = el("option", t("Skill publicada", "Published skill"));
      published.value = "";
      draft.append(published);
      for (const item of drafts.filter(item => item.task_id === row.task_id)) {
        const option = el("option", item.slug + " · " + ctx.fmtWhen(item.created));
        option.value = item.id;
        draft.append(option);
      }
      const slug = field(form, t("Identificador de la skill publicada", "Published skill slug"), "input");
      const step = field(form, t("Paso e indicaciones para el trabajador", "Step and worker instructions"), "textarea");
      const compare = field(form, t("Campos JSON a comparar, uno por línea (opcional)", "JSON comparison paths, one per line (optional)"), "textarea");
      form.append(button(t("Iniciar prueba", "Start test"), async () => {
        const data = { ...scope, receipt_id: row.id, step: step.value.trim() };
        if (draft.value) data.draft_id = draft.value;
        else data.slug = slug.value.trim();
        const fields = compare.value.split("\n").map(value => value.trim()).filter(Boolean);
        if (fields.length) data.compare = fields;
        await request("skill_bench", data);
        await refresh();
      }));
      return form;
    }

    const labels = {
      running: ["En marcha", "Running"], passed: ["Coincide", "Matches"],
      different: ["Salida distinta", "Different output"], failed: ["Fallida", "Failed"],
    };

    async function refresh() {
      const current = ++generation;
      const answer = await request("order_receipts?" + query);
      if (current !== generation) return;
      body.replaceChildren(button(t("Actualizar", "Refresh"), refresh), el("h5", t("Llamadas", "Calls")));
      if (answer.receipts.length) receipts(answer, body);
      else body.append(el("p", t("Aún no hay recibos de esta orden.", "This order has no receipts yet.")));
      if (answer.drafts.length) body.append(el("h5", t("Borradores", "Drafts")));
      for (const draft of answer.drafts) {
        body.append(expansion(draft.slug, async content => {
          const result = await request("order_skill_draft?" + query + "&draft_id=" + draft.id);
          content.append(el("pre", result.draft.text));
          content.append(button(t("Quitar borrador", "Remove draft"), async () => {
            await request("order_receipt_undo", { draft_id: draft.id });
            await refresh();
          }));
        }));
      }
      if (answer.benchmarks.length) body.append(el("h5", t("Resultados de pruebas", "Test results")));
      for (const run of answer.benchmarks) {
        const state = labels[run.state] ? t(...labels[run.state]) : run.state;
        body.append(expansion(run.slug + " · " + state, async content => {
          content.append(el("p", t("Recibo de referencia", "Reference receipt") + " #" + run.reference_receipt));
          if (run.result?.actual_receipt) {
            content.append(expansion(t("Recibo del trabajador", "Worker receipt") + " #" + run.result.actual_receipt,
              async receipt => {
                const result = await request("order_receipt?" + query + "&receipt_id=" + run.result.actual_receipt);
                receipt.append(el("pre", pretty(result.receipt.reply)));
              }));
          }
          if (run.result?.worker?.detail) content.append(el("p", run.result.worker.detail));
          if (run.result?.differences?.length) content.append(el("pre", pretty(run.result.differences)));
          if (run.state === "running") content.append(button(t("Detener prueba", "Stop test"), async () => {
            await request("skill_bench_undo", { run_id: run.id });
            await refresh();
          }));
        }));
      }
    }

    panel.addEventListener("toggle", async () => {
      if (!panel.open) return;
      try { await refresh(); }
      catch (err) { status.textContent = err.message; }
    });
    return panel;
  };
})();

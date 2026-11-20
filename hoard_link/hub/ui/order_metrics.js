/* Measures and budget pauses embedded in Orders. */
(() => {
  "use strict";
  const { el, api, lang } = window.HubFacets.ctx;
  const tr = (es, en) => lang() === "es" ? es : en;
  const labels = {
    retries: ["Reintentos agotados", "Retry limit reached"],
    gpu: ["Presupuesto de GPU agotado", "GPU budget reached"],
    returns: ["Límite de devoluciones alcanzado", "Return limit reached"],
  };
  function minutes(seconds) {
    if (seconds == null) return "—";
    const format = new Intl.NumberFormat(lang(), { maximumFractionDigits: 2 });
    return seconds > 0 && seconds < 0.6 ? "< " + format.format(0.01) : format.format(seconds / 60);
  }

  function table(headers, rows, responsive = false) {
    const t = el("table", responsive ? "ord-plan ord-task-metrics" : "ord-plan");
    const head = el("thead"), body = el("tbody");
    const heading = el("tr");
    for (const label of headers) heading.append(el("th", "", label));
    head.append(heading);
    for (const values of rows) {
      const row = el("tr");
      values.forEach((value, index) => {
        const cell = el("td", "", String(value));
        if (responsive) cell.dataset.heading = headers[index];
        row.append(cell);
      });
      body.append(row);
    }
    t.append(head, body);
    const wrap = el("div", "ord-plan-wrap");
    wrap.append(t);
    return wrap;
  }

  window.HubOrderMetrics = {
    load: () => api("/api/work-admission/metrics?all_spaces=1").catch(() => null),
    reason: key => labels[key] ? tr(...labels[key]) : "",
    render(data) {
      if (!data?.ok || !(data.chains.length || data.holds.length || data.task_stations?.length)) return null;
      const box = el("section", "ord-list");
      box.append(el("h2", "", tr("Medidas de producción", "Production measures")));
      for (const hold of data.holds) {
        const reason = this.reason(hold.reason);
        const pending = hold.gates ? "" : tr(" · sin piezas pendientes de decisión", " · no pieces awaiting a decision");
        box.append(el("p", "ord-warn", `#${hold.id} · ${hold.title} · ${reason}${pending}`));
      }
      if (data.chains.length) box.append(table(
        [tr("Cadena", "Pipeline"), tr("Órdenes a la primera", "First-pass orders"), tr("Piezas a la primera", "First-pass pieces")],
        data.chains.map(row => [row.chain || tr("Sin plantilla", "No template"),
          `${row.orders_kept_first}/${row.orders_decided}`, `${row.pieces_kept_first}/${row.pieces_decided}`]),
      ));
      if (data.stations.length) box.append(table(
        [tr("Puesto", "Station"), tr("Devoluciones", "Returns"), tr("Avance (min)", "Advance (min)"), tr("Fallos (min)", "Failed work (min)"),
          tr("GPU (min)", "GPU (min)"), tr("GPU en fallos (min)", "Failed GPU (min)")],
        data.stations.map(row => [row.station || "—", row.returns || 0, minutes(row.advance_seconds), minutes(row.failed_seconds),
          minutes(row.gpu_seconds), minutes(row.failed_gpu_seconds)]),
      ));
      if (data.task_stations?.length) {
        box.append(el("h3", "", tr("Tareas por puesto y repositorio", "Tasks by station and repository")),
          el("p", "ord-muted", tr("Las reentregas se cuentan por revisión de las tareas cerradas. Los tiempos son medianas en minutos.",
            "Resubmissions are counted by revision for closed tasks. Times are medians in minutes.")),
          table([
            tr("Puesto", "Station"), tr("Repositorio", "Repository"), tr("Cerradas", "Closed"),
            tr("Devueltas", "Returned"), tr("Reentregas", "Resubmissions"),
            tr("Creación a cierre · 1.ª / devuelta", "Creation to close · first / returned"),
            tr("Reclamación a entrega · 1.ª / devuelta", "Claim to submission · first / returned"),
          ], data.task_stations.map(row => [
            row.station || tr("Sin puesto", "No station"), row.repo || tr("Sin repositorio", "No repository"),
            `${row.closed_tasks}/${row.tasks}`, row.returned_tasks, row.resubmissions,
            `${minutes(row.median_close_first_seconds)} / ${minutes(row.median_close_returned_seconds)}`,
            `${minutes(row.median_submit_first_seconds)} / ${minutes(row.median_submit_returned_seconds)}`,
          ]), true));
      }
      return box;
    },
  };
})();

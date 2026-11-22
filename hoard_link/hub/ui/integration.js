(() => {
  "use strict";

  const api = "/api/integration";
  const stylesheet = document.createElement("link");
  stylesheet.rel = "stylesheet";
  stylesheet.href = "/ui/integration.css";
  document.head.append(stylesheet);
  const labels = {
    queued: ["En cola", "Queued"],
    waiting_for_merge_lock: ["Esperando el bloqueo de fusión", "Waiting for the merge lock"],
    waiting_for_suite_slot: ["Esperando turno de pruebas", "Waiting for test slot"],
    testing_main: ["Probando la rama principal", "Testing main"],
    testing_candidate: ["Probando la entrega", "Testing the delivery"],
    testing_inference: ["Probando inferencia", "Testing inference"],
    integrating: ["Fusionando", "Integrating"],
    checking_dependencies: ["Comprobando dependencias", "Checking dependencies"],
    installing_dependencies: ["Actualizando dependencias", "Updating dependencies"],
    processing_repositories: ["Integrando repositorios", "Integrating repositories"],
    checking_live_case: ["Comprobando el caso de origen", "Checking the original case"],
    publishing_frontend: ["Publicando la interfaz web", "Publishing the web interface"],
    cleaning_delivery: ["Limpiando la entrega", "Cleaning the delivery"],
    delivery_retained: ["Árbol de entrega conservado", "Delivery worktree kept"],
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

  function correctionNote(ctx, correction) {
    return ctx.el("p", "", t(ctx,
      `Corrección verificada por ${correction.agent} (${correction.repo}). ${correction.reason}`,
      `Correction verified by ${correction.agent} (${correction.repo}). ${correction.reason}`));
  }

  function dependencyFailure(receipt) {
    return receipt.dependencies?.some(row => ["failed", "unverified"].includes(row.install?.state));
  }

  function dependencyNotes(ctx, rows, heading) {
    const states = {
      stale: ["Hay dependencias por actualizar", "Dependencies need updating"],
      current: ["Dependencias al día", "Dependencies are current"],
      unverified: ["No se pudieron comprobar todas las dependencias", "Some dependencies could not be checked"],
      installed: ["Dependencias actualizadas", "Dependencies updated"],
      failed: ["Falló la actualización de dependencias", "Dependency update failed"],
      running: ["Actualizando dependencias", "Updating dependencies"],
    };
    const visible = rows?.filter(row => row.changed.length || row.status !== "current" || row.install);
    if (!visible?.length) return [];
    const list = ctx.el("ul");
    for (const row of visible) {
      const install = row.install;
      const deferred = install?.state === "deferred";
      const label = deferred
        ? (install.reason === "app_running"
          ? t(ctx, "Actualización aplazada mientras la aplicación está en marcha", "Update deferred while the app is running")
          : t(ctx, "Se ha aplazado la actualización de dependencias", "Dependency update deferred"))
        : t(ctx, ...states[install?.state ?? row.status]);
      const packages = row.issues?.map(item => item.package).join(", ");
      const details = [...row.changed, ...(packages ? [packages] : [])].join(" · ");
      const item = ctx.el("li", "", `${row.manager} · ${label}`);
      if (details) item.append(ctx.el("p", "muted", details));
      list.append(item);
    }
    return [ctx.el("h4", "", t(ctx, ...heading)), list];
  }

  function renderReceipt(ctx, target, receipt) {
    if (receipt.repositories?.length) {
      const sections = receipt.repositories.map(row => {
        const section = ctx.el("section");
        const body = ctx.el("div");
        section.append(ctx.el("h3", "", `${row.plan.repo} · ${state(ctx, row.state)}`), body);
        renderReceipt(ctx, body, row);
        return section;
      });
      if (receipt.correction?.agent) sections.push(correctionNote(ctx, receipt.correction));
      if (receipt.error && !receipt.repositories.some(row => (row.test_timeout || dependencyFailure(row)) &&
        receipt.error === `${row.plan.repo}: ${row.error}`)) {
        sections.push(ctx.el("p", "muted", receipt.error));
      }
      target.replaceChildren(...sections);
      return;
    }
    const {el} = ctx;
    const content = [];
    if (receipt.correction?.agent) content.push(correctionNote(ctx, receipt.correction));
    if (receipt.comparison) {
      const {main, candidate, known, new: regressions} = receipt.comparison;
      content.push(el("p", "", t(ctx,
        `Pruebas superadas · Principal ${main.counts.passed} · Entrega ${candidate.counts.passed}`,
        `Main: ${main.counts.passed} passed. Delivery: ${candidate.counts.passed} passed.`)));
      const knownCount = Object.keys(known).length;
      const regressionCount = Object.keys(regressions).length;
      const knownLabel = knownCount === 1 ? t(ctx, "1 fallo conocido", "1 known failure")
        : t(ctx, `${knownCount} fallos conocidos`, `${knownCount} known failures`);
      const regressionLabel = regressionCount === 1 ? t(ctx, "1 fallo nuevo", "1 new failure")
        : t(ctx, `${regressionCount} fallos nuevos`, `${regressionCount} new failures`);
      content.push(el("p", "", `${knownLabel} · ${regressionLabel}`));
      const otherRegressions = Object.keys(regressions).filter(test =>
        !receipt.closure_baseline?.new.some(focal => test === focal || test.endsWith(`/${focal}`)));
      if (otherRegressions.length) {
        const list = el("ul");
        for (const test of otherRegressions) list.append(el("li", "", test));
        content.push(list);
      }
    }
    if (receipt.closure_baseline) {
      const {known, new: regressions} = receipt.closure_baseline;
      for (const [cases, label] of [
        [known, t(ctx, "Fallos de la línea base", "Baseline failures")],
        [regressions, t(ctx, "Fallos nuevos en las pruebas focales", "New failures in focused tests")],
      ]) {
        if (!cases.length) continue;
        const list = el("ul");
        for (const test of cases) list.append(el("li", "", test));
        content.push(el("h4", "", label), list);
      }
    }
    content.push(...dependencyNotes(ctx, receipt.dependencies,
      ["Dependencias de la copia principal", "Main checkout dependencies"]));
    content.push(...dependencyNotes(ctx, receipt.dependency_preflight,
      ["Dependencias de la fusión propuesta", "Proposed merge dependencies"]));
    content.push(...dependencyNotes(ctx, receipt.undo_dependencies,
      ["Dependencias tras deshacer la fusión", "Dependencies after undoing the merge"]));
    if (receipt.live) {
      const passed = receipt.live.counts.passed &&
        (receipt.state === "integrated" || receipt.state === "undone");
      const focused = receipt.plan.live_mode !== "contract";
      content.push(el("p", "", focused
        ? (passed ? t(ctx, "Caso de origen comprobado tras la fusión", "Original case passed after integration")
                  : t(ctx, "Caso de origen ejecutado tras la fusión", "Original case ran after integration"))
        : (passed ? t(ctx, "Pruebas focales repetidas tras la fusión", "Focused tests passed again after integration")
                  : t(ctx, "Pruebas focales ejecutadas tras la fusión", "Focused tests ran after integration"))));
    }
    const reloads = receipt.undo_app_reload ?? receipt.app_reload ?? [];
    for (const app of reloads) {
      if (app.reason_code !== "desktop_in_use") continue;
      content.push(el("p", "", t(ctx,
        `Se ha aplazado la recarga de ${app.app} mientras está en uso en el escritorio.`,
        `Reloading ${app.app} was deferred while it is in use on the desktop.`)));
    }
    const actions = {
      restart_hub_when_idle: ["Reinicia el Hub cuando no haya trabajadores ejecutándose.",
                              "Restart the Hub when no workers are running."],
      reopen_hub_window: ["Cierra y vuelve a abrir la ventana del Hub.", "Close and reopen the Hub window."],
      reopen_native_console: ["Cierra y vuelve a abrir la consola de escritorio.",
                              "Close and reopen the desktop console."],
      verify_spark_deployment: ["Comprueba el despliegue de Studio en la Spark.",
                                "Verify the Studio deployment on the Spark."],
    };
    for (const item of receipt.reload_pending ?? []) {
      if (item.action === "reload_app") {
        if (!reloads.some(app => app.app === item.target && app.reason_code === "desktop_in_use")) {
          content.push(el("p", "", t(ctx, `Recarga pendiente de ${item.target}.`, `Reload pending for ${item.target}.`)));
        }
      } else if (actions[item.action]) {
        content.push(el("p", "", t(ctx, ...actions[item.action])));
      }
    }
    if (receipt.merge_commit) {
      const commit = el("p", "", receipt.plan?.published_commit
        ? t(ctx, "Commit publicado comprobado ", "Checked published commit ")
        : t(ctx, "Commit de fusión ", "Merge commit "));
      commit.append(el("code", "", receipt.merge_commit.slice(0, 12)));
      content.push(commit);
    }
    if (receipt.events.some(event => event.state === "restored_checkout" && event.parked?.length)) {
      content.push(el("p", "", t(ctx, "Cambios locales restaurados", "Local changes restored")));
    }
    if (receipt.cleanup) {
      if (receipt.cleanup.retained) content.push(el("p", "",
        t(ctx, "Árbol de entrega conservado porque contiene cambios o archivos ignorados",
          "Delivery worktree kept because it contains changes or ignored files")));
      if (receipt.cleanup.removed_branch && receipt.state !== "undone") {
        content.push(el("p", "", receipt.cleanup.removed_worktree
          ? t(ctx, "Rama y árbol de entrega retirados", "Delivery branch and worktree removed")
          : t(ctx, "Rama de entrega retirada", "Delivery branch removed")));
      }
    }
    if (receipt.error) {
      const timeout = receipt.test_timeout;
      const message = dependencyFailure(receipt) && receipt.state === "rolled_back" ? t(ctx,
        "La actualización de dependencias quedó incompleta. Se ha restaurado la copia anterior.",
        "The dependency update did not complete. The previous checkout was restored.") : timeout ? t(ctx,
        `Las pruebas de ${timeout.group} en ${timeout.commit} superaron el límite de ${timeout.seconds} s. Ajusta el plazo del repositorio o acota las pruebas.`,
        `Tests in ${timeout.group} at ${timeout.commit} exceeded the ${timeout.seconds} s limit. Adjust the repository budget or narrow the test selection.`) : receipt.error;
      const error = el("p", "muted", message);
      error.style.overflowWrap = "anywhere";
      content.push(error);
    }
    target.replaceChildren(...content);
  }

  function renderBaseline(ctx, rows, open) {
    const {el} = ctx;
    const section = el("section", "integration-suite-baseline");
    section.append(el("h3", "", t(ctx, "Fallos de la línea base", "Baseline failures")));
    if (!rows) {
      section.append(el("p", "muted", t(ctx,
        "No se pudo consultar la línea base.", "Could not load the baseline.")));
      return section;
    }
    const repos = rows.filter(row => row.failures.length || !["passed", "failed"].includes(row.status));
    if (!repos.length) {
      section.append(el("p", "muted", rows.length
        ? t(ctx, "No hay fallos registrados.", "No recorded failures.")
        : t(ctx, "Todavía no hay mediciones de referencia.", "No baseline measurements yet.")));
      return section;
    }
    const dateFormat = new Intl.DateTimeFormat(t(ctx, "es-ES", "en-GB"), {
      dateStyle: "medium", timeStyle: "short",
    });
    const date = value => {
      if (!value) return el("span", "", t(ctx, "Sin fecha", "Undated"));
      const time = el("time", "", dateFormat.format(new Date(value)));
      time.dateTime = value;
      return time;
    };
    const list = el("div", "hub-project-list");
    for (const row of repos) {
      const details = el("details", "hub-project-row");
      details.dataset.id = `baseline:${row.repo}`;
      details.open = open.has(details.dataset.id);
      const count = row.failures.length;
      const label = count === 1 ? t(ctx, "1 fallo", "1 failure")
        : t(ctx, `${count} fallos`, `${count} failures`);
      const partial = !["passed", "failed"].includes(row.status);
      const status = partial ? t(ctx, "Medición incompleta", "Incomplete measurement") : label;
      const summary = el("summary", "", `${row.repo} · ${partial && count ? `${label} · ` : ""}${status}`);
      const body = el("div");
      if (count) {
        const tests = el("ul", "integration-baseline-tests");
        for (const failure of row.failures) {
          const item = el("li");
          const meta = el("dl");
          for (const [name, value] of [
            ["Commit", el("code", "", failure.commit)],
            [t(ctx, "Primer fallo observado", "First observed failure"), date(failure.first_seen)],
            [t(ctx, "Último fallo observado", "Last observed failure"), date(failure.last_seen)],
          ]) {
            const field = el("div");
            const answer = el("dd");
            answer.append(value);
            field.append(el("dt", "muted", name), answer);
            meta.append(field);
          }
          item.append(el("code", "", failure.test), meta);
          tests.append(item);
        }
        body.append(tests);
      } else if (row.commit) {
        body.append(el("code", "", row.commit));
      }
      details.append(summary, body);
      list.append(details);
    }
    section.append(list);
    return section;
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
      const [result, baseline] = await Promise.all([
        ctx.api(api),
        ctx.api(`${api}?suite_baseline=1`).catch(() => null),
      ]);
      if (id !== generation) return;
      if (!result.ok) throw new Error();
      const repositories = baseline?.ok ? baseline.repositories : null;
      const rows = JSON.stringify([result.receipts, result.runtime, repositories]);
      if (rows === lastRows) return;
      lastRows = rows;
      const open = new Set([...root.querySelectorAll("details[open]")].map(row => row.dataset.id));
      const messages = [renderBaseline(ctx, repositories, open)];
      if (result.runtime?.stale) {
        const loaded = result.runtime.loaded_commit.slice(0, 12);
        const main = result.runtime.main_commit.slice(0, 12);
        const warning = el("p", "", t(ctx,
          `El puesto de fusión ejecuta ${loaded} y la rama principal ya está en ${main}.`,
          `The integration station is running ${loaded} and the main branch has advanced to ${main}.`));
        warning.setAttribute("role", "status");
        messages.push(warning);
      }
      if (!result.receipts.length) {
        root.replaceChildren(...messages, el("p", "muted", t(ctx, "Todavía no hay integraciones.", "No integrations yet.")));
        return;
      }
      const list = el("div", "hub-project-list");
      for (const row of result.receipts) {
        const details = el("details", "hub-project-row");
        details.dataset.id = row.id;
        details.open = open.has(row.id);
        const kept = row.cleanup?.retained || row.repositories?.some(repo => repo.cleanup?.retained);
        const retained = kept && row.state !== "delivery_retained"
          ? ` · ${state(ctx, "delivery_retained")}` : "";
        const summary = el("summary", "", `#${row.task_id} · ${row.repo} · ${state(ctx, row.state)}${retained}`);
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
      root.replaceChildren(...messages, list);
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

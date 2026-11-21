/* Source controls for dossiers and pieces on the Orders page. */
(() => {
  "use strict";
  const { el, api, lang } = window.HubFacets.ctx;
  const tr = (es, en) => lang() === "es" ? es : en;
  const contracts = {
    drucker_engagement_get: "drucker",
    wh_production_dossier: "writer",
    coase_case: "coase",
    coase_backend_packet: "coase",
  };
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const modelUndos = new Map();

  function figureModel(caption, order, piece, refresh) {
    const step = piece.missing_figure_model;
    if (!step) return;
    const notice = el("small", "ord-muted",
      tr("No hay un modelo articulado de origen registrado.", "No articulated source model is registered."));
    const request = el("button", "ghost small", tr("Pedir modelo articulado", "Request articulated model"));
    const undo = el("button", "ghost small", tr("Deshacer encargo", "Undo request"));
    const status = el("small", "ord-muted");
    request.type = undo.type = "button";
    status.setAttribute("role", "status");
    let task = step.task_id;
    let available = false;
    const show = () => {
      request.hidden = !!task || available;
      undo.hidden = !task || modelUndos.get(piece.id)?.task !== task;
      notice.hidden = available;
      status.textContent = task
        ? tr(`Modelo encargado a Vulcan · tarea #${task}`, `Model requested from Vulcan · task #${task}`)
        : available ? tr("El modelo de origen ya está disponible.", "The source model is now available.") : "";
      status.hidden = !status.textContent;
    };
    const write = async (op, fields) => {
      request.disabled = undo.disabled = true;
      const result = await api(`/api/agora/${op}`, { space_id: order.space_id, ...fields }).catch(() => null);
      request.disabled = undo.disabled = false;
      if (!result?.ok) {
        status.hidden = false;
        status.textContent = op === "order_undo"
          ? tr("No se ha podido deshacer el encargo.", "Could not undo the request.")
          : tr("No se ha podido encargar el modelo.", "Could not request the model.");
      }
      return result;
    };
    request.onclick = async () => {
      const result = await write("draft_model_request", { order_id: order.id, piece_id: piece.id });
      if (!result?.ok) return;
      task = result.task_id;
      available = result.status === "model_available";
      if (result.undo) modelUndos.set(piece.id, { token: result.undo, task });
      show();
      await refresh();
    };
    undo.onclick = async () => {
      const entry = modelUndos.get(piece.id);
      if (!entry) return;
      const result = await write("order_undo", { token: entry.token });
      if (!result?.ok) return;
      modelUndos.delete(piece.id);
      task = null;
      show();
      await refresh();
    };
    show();
    caption.append(notice, request, undo, status);
  }

  function native(tool, payload, id) {
    if (!object(payload)) throw new Error(tr("El origen no devolvió un expediente.", "The source did not return a dossier."));
    const identity = id || payload.subject?.id || payload.job_id || payload.case?.id || payload.id;
    const rev = payload.packet_hash || payload.source_rev || payload.updatedAt || payload.updated || payload.case?.updated;
    return {
      app: contracts[tool], tool, payload,
      ...(identity ? { object: String(identity) } : {}),
      ...(rev ? { source_rev: String(rev) } : {}),
    };
  }

  async function call(app, tool, args = {}) {
    const res = await api(`/api/apps/${app}/call`, { tool, arguments: args, timeout_s: 15 });
    if (!res?.ok || res.result?.ok === false) {
      throw new Error(res?.error || res?.result?.error || tr("Origen no disponible.", "Source unavailable."));
    }
    return res.result;
  }

  async function readFile(file) {
    if (!file) throw new Error(tr("Elige el fichero del expediente.", "Choose the dossier file."));
    let saved;
    try {
      saved = JSON.parse(await file.text());
    } catch (_) {
      throw new Error(tr("El fichero debe contener un expediente JSON.", "The file must contain a JSON dossier."));
    }
    const payload = saved?.payload || saved?.result || saved;
    const tool = saved?.tool || (payload?.schema === "hoard.production-dossier/1" ? "wh_production_dossier"
      : payload?.packet_hash && payload?.job_id ? "coase_backend_packet"
      : payload?.case && payload?.sources ? "coase_case"
      : payload?.id && payload?.items && payload?.deliverables ? "drucker_engagement_get" : "");
    if (!Object.hasOwn(contracts, tool) || (saved?.app && saved.app !== contracts[tool])) {
      throw new Error(tr("El fichero no corresponde a un expediente de Drucker, Writer o Coase.",
        "The file does not contain a Drucker, Writer or Coase dossier."));
    }
    const source = native(tool, payload, saved?.payload && saved.object);
    if (saved?.payload && saved.source_rev) source.source_rev = String(saved.source_rev);
    return source;
  }

  function picker(field) {
    const node = el("fieldset", "ord-native-source");
    const origin = el("select");
    const options = (select, prompt, rows = []) => {
      select.replaceChildren(el("option", "", prompt));
      select.options[0].value = "";
      for (const row of rows) {
        if (row.id == null) continue;
        const option = el("option", "", row.title || row.name || String(row.id));
        option.value = row.id;
        select.append(option);
      }
    };
    options(origin, tr("Sin expediente de origen", "No source dossier"), [
      { id: "drucker", name: "Drucker" }, { id: "writer", name: "Writer" }, { id: "coase", name: "Coase" },
      { id: "file", name: tr("Fichero de expediente (.json)", "Dossier file (.json)") },
    ]);
    const choices = el("div", "ord-compose-grid");
    const status = el("p", "ord-muted");
    status.setAttribute("role", "status");
    status.hidden = true;
    node.append(el("legend", "", tr("Expediente de origen", "Source dossier")),
      field(tr("Origen del expediente", "Dossier source"), origin), choices, status);
    let selection = null;

    async function selectRecord(label, load) {
      const select = el("select");
      const wrapper = field(label, select);
      select.disabled = true;
      options(select, tr("Cargando…", "Loading…"));
      choices.append(wrapper);
      try {
        const rows = await load();
        options(select, rows.length ? tr("Elige una opción", "Choose an option") : tr("No hay resultados", "No results"), rows);
        select.disabled = !rows.length;
      } catch (error) {
        options(select, tr("No se pudo cargar", "Could not load"));
        if (wrapper.isConnected) {
          status.hidden = false;
          status.textContent = error.message;
        }
      }
      return { select, wrapper };
    }

    origin.onchange = async () => {
      const app = origin.value;
      choices.replaceChildren();
      status.hidden = true;
      selection = null;
      if (!app) return;
      if (app === "file") {
        const file = el("input");
        file.type = "file";
        file.accept = ".json,application/json";
        choices.append(field(tr("Fichero del expediente", "Dossier file"), file));
        selection = () => readFile(file.files[0]);
        return;
      }
      if (app === "writer") {
        const { select: project, wrapper } = await selectRecord(tr("Proyecto de Writer", "Writer project"),
          async () => (await call(app, "wh_list_projects")).projects);
        if (!wrapper.isConnected) return;
        project.onchange = async () => {
          while (wrapper.nextSibling) wrapper.nextSibling.remove();
          status.hidden = true;
          selection = null;
          const projectId = project.value;
          if (!projectId) return;
          const { select: writing, wrapper: chapter } = await selectRecord(tr("Capítulo", "Chapter"),
            async () => (await call(app, "wh_list_writings", { projectId })).writings);
          if (chapter.isConnected) selection = async () => native("wh_production_dossier",
            await call(app, "wh_production_dossier", { projectId, writingId: chosen(writing) }));
        };
        return;
      }
      const drucker = app === "drucker";
      const { select, wrapper } = await selectRecord(drucker ? tr("Encargo", "Engagement") : tr("Expediente de empresa", "Company dossier"),
        async () => drucker ? await call(app, "drucker_engagements") : (await call(app, "coase_cases")).cases);
      if (!wrapper.isConnected) return;
      const tool = drucker ? "drucker_engagement_get" : "coase_case";
      selection = async () => {
        const id = chosen(select);
        return native(tool, await call(app, tool, { [drucker ? "engagement_id" : "case_id"]: id }), id);
      };
    };
    function chosen(select) {
      if (!select.value) throw new Error(tr("Elige el expediente de origen.", "Choose the source dossier."));
      return select.value;
    }
    return {
      node,
      async read() {
        if (!origin.value) return undefined;
        if (!selection) throw new Error(tr("Elige el expediente de origen.", "Choose the source dossier."));
        return selection();
      },
      reset() {
        origin.value = "";
        origin.onchange();
      },
    };
  }
  window.HubOrderSources = { picker, figureModel };
})();

/* Skills: which procedures of the library each app carries. Uses the Hub's own type, colours and controls. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const { el, api, L, fmtWhen, toast } = H.ctx;
  const text = (es, en) => L({ es, en });
  let list, note, button, frame, picked = "";
  let viewingFlows = false;
  async function flows() {
    viewingFlows = true;
    note.textContent = text("Consultando flujos…", "Loading flows…");
    try {
      const r = await api("/api/skills/flows");
      if (!r.ok) throw new Error(r.error);
      list.replaceChildren();
      const search = el("input"); search.type = "search";
      search.placeholder = text("Buscar app o flujo", "Find app or flow");
      search.setAttribute("aria-label", search.placeholder); list.append(search);
      const groups = [];
      for (const app of r.apps || []) {
        const section = el("section", "sk-flows");
        section.append(el("h2", "", app.name));
        const shelf = el("button", "small", text("Elegir skills", "Choose skills"));
        shelf.onclick = () => open(app); section.append(shelf);
        if (!app.flows.length) section.append(el("p", "hint", text("Esta app no tiene flujos escritos en su estantería.", "This app has no written flows on its shelf.")));
        const entries = [];
        for (const flow of app.flows) {
          const item = el("details"); item.append(el("summary", "", flow.name));
          item.append(el("p", "hint", `${flow.version || ""} · ${flow.status} · ${flow.why || ""}`));
          for (const [key, es, en] of [["trigger", "Disparador", "Trigger"], ["steps", "Pasos", "Steps"], ["outputs", "Salidas esperadas y comprobación", "Expected outputs and checks"]]) {
            item.append(el("h3", "", text(es, en)));
            item.append(el("p", "sk-flow-text", flow[key] || text("No declarado en la skill.", "Not declared in the skill.")));
          }
          const read = el("button", "small", text("Leer skill completa", "Read full skill"));
          const source = el("pre", "sk-flow-text"); source.hidden = true;
          read.onclick = async () => {
            try { const data = await api(flow.skill_url); if (!data.text) throw new Error(data.error || "Error"); source.textContent = data.text; source.hidden = !source.hidden; }
            catch (error) { source.hidden = false; source.textContent = String(error); }
          };
          item.append(read, source); section.append(item);
          entries.push({item, hay: `${app.name} ${flow.name} ${flow.trigger} ${flow.steps}`.toLocaleLowerCase()});
        }
        list.append(section); groups.push({section, entries, name: app.name.toLocaleLowerCase()});
      }
      search.oninput = () => { const q = search.value.toLocaleLowerCase().trim(); for (const group of groups) { for (const entry of group.entries) entry.item.hidden = !entry.hay.includes(q); group.section.hidden = !group.name.includes(q) && !group.entries.some(e => !e.item.hidden); } };
      note.textContent = text("Procedimientos escritos de las skills actuales. Las salidas se toman de Outputs o, si falta, de Verification. Consultarlos no ejecuta el flujo.", "Written procedures from current skills. Outputs come from Outputs or, when absent, Verification. Reading them does not run a flow.");
    } catch { note.textContent = text("No se pudieron cargar los flujos. Vuelve a pulsar Ver flujos.", "Flows could not be loaded. Select Show flows to retry."); }
  }
  function open(app) {
    picked = app.id;
    frame.hidden = false;
    frame.title = text("Skills de ", "Skills of ") + app.name;
    frame.src = "/api/skills/page?app=" + encodeURIComponent(app.id);
    frame.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  async function load(quiet) {
    if (!list) return;
    if (viewingFlows) return;
    try {
      const r = await api("/api/skills/overview");
      if (!r.ok) { note.textContent = r.error || text("No se pudo consultar el Hub.", "Could not reach the Hub."); return; }
      list.replaceChildren();
      for (const app of r.apps || []) {
        const row = el("div", "sk-row");
        row.setAttribute("role", "listitem");
        row.appendChild(el("strong", "", app.name));
        const what = app.count ? app.skills.join(", ") : text("Ninguna", "None");
        row.appendChild(el("span", app.count ? "" : "hint", what));
        const state = el("span", app.serves ? "ok" : "warn", app.serves ? text(`${app.count} para sus agentes`, `${app.count} for its agents`)
          : text("Aún no las sirve", "Does not serve them yet"));
        if (!app.serves) state.title = text("Su puente no reparte skills todavía: copia anterior del código común, puente propio o app Node.", "Its bridge does not hand out skills yet: an older copy of the shared code, its own bridge, or a Node app.");
        row.appendChild(state);
        const pick = el("button", "small", text("Elegir", "Choose"));
        pick.setAttribute("aria-label", text("Elegir skills de ", "Choose skills of ") + app.name);
        pick.onclick = () => open(app);
        row.appendChild(pick);
        list.appendChild(row);
      }
      note.textContent = r.library ? text(`Biblioteca: ${r.library_skills} skills en ${r.library}.`, `Library: ${r.library_skills} skills in ${r.library}.`)
        : text("No encuentro la biblioteca de skills en este equipo; las apps siguen con sus copias.", "The skill library was not found on this computer; apps keep their copies.");
    } catch { if (!quiet) note.textContent = text("No se pudo consultar el Hub.", "Could not reach the Hub."); }
  }
  H.register("skills", {
    placement: "tab", label: { es: "Skills", en: "Skills" }, order: 83,
    mount(container) {
      const style = el("style");
      style.textContent = ".sk-list{max-width:1100px}.sk-row{display:grid;grid-template-columns:minmax(150px,1fr) minmax(200px,3fr) minmax(130px,auto) auto;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);align-items:baseline}.sk-frame{width:100%;height:78vh;border:1px solid var(--line);border-radius:12px;margin-top:18px;background:transparent}.sk-note{margin-top:14px;max-width:90ch}@media(max-width:700px){.sk-row{grid-template-columns:1fr auto}.sk-row>span:first-of-type{grid-column:1/-1}}";
      container.appendChild(style);
      const bar = el("div", "tab-tools");
      const showFlows = el("button", "small", text("Ver flujos", "Show flows"));
      showFlows.onclick = () => flows();
      const showShelf = el("button", "small", text("Ver estanterías", "Show shelves"));
      showShelf.onclick = () => { viewingFlows = false; void load(false); };
      bar.append(showFlows, showShelf);
      const flowStyle = el("style");
      flowStyle.textContent = ".sk-flows{margin:28px 0;max-width:75ch}.sk-flows details{padding:12px 0;border-bottom:1px solid var(--line)}.sk-flows summary{cursor:pointer;overflow-wrap:anywhere}.sk-flow-text{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.6}.sk-flows h3{margin-top:20px}";
      container.append(flowStyle);
      button = el("button", "small", text("Actualizar todas desde la biblioteca", "Refresh all from the library"));
      button.onclick = async () => {
        button.disabled = true;
        try {
          const r = await api("/api/skills/refresh", {});
          toast(r.ok ? text(`Copias al día. Apps con cambios: ${(r.changed || []).length}.`, `Copies up to date. Apps changed: ${(r.changed || []).length}.`) : (r.error || "Error"), r.ok ? "info" : "err");
          await load(false);
          if (viewingFlows) await flows();
          if (picked && frame.contentWindow) frame.contentWindow.location.reload();
        } finally { button.disabled = false; }
      };
      bar.appendChild(button); container.appendChild(bar);
      list = el("div", "sk-list"); list.setAttribute("role", "list"); container.appendChild(list);
      note = el("p", "hint sk-note", text("Consultando…", "Checking…")); note.setAttribute("aria-live", "polite"); container.appendChild(note);
      frame = el("iframe", "sk-frame"); frame.hidden = true; container.appendChild(frame);
      window.addEventListener("focus", () => load(true));
    }, load: () => load(false), tick: () => load(true),
  });
})();

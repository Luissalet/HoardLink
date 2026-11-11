/* Skills: which procedures of the library each app carries. Uses the Hub's own type, colours and controls. */
(() => {
  "use strict";
  const H = window.HubFacets;
  if (!H) return;
  const { el, api, L, fmtWhen, toast } = H.ctx;
  const text = (es, en) => L({ es, en });
  let list, note, button, frame, picked = "";
  function open(app) {
    picked = app.id;
    frame.hidden = false;
    frame.title = text("Skills de ", "Skills of ") + app.name;
    frame.src = "/api/skills/page?app=" + encodeURIComponent(app.id);
    frame.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  async function load(quiet) {
    if (!list) return;
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
        if (!app.serves) state.title = text("Su copia del código común es anterior a las skills, o es una app Node.", "Its copy of the shared code predates skills, or it is a Node app.");
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
      button = el("button", "small", text("Actualizar todas desde la biblioteca", "Refresh all from the library"));
      button.onclick = async () => {
        button.disabled = true;
        try {
          const r = await api("/api/skills/refresh", {});
          toast(r.ok ? text(`Copias al día. Apps con cambios: ${(r.changed || []).length}.`, `Copies up to date. Apps changed: ${(r.changed || []).length}.`) : (r.error || "Error"), r.ok ? "info" : "err");
          await load(false);
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

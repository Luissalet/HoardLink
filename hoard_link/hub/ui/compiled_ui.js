(() => {
  'use strict';
  const {el, api, lang, L} = window.HubFacets.ctx;
  const tr = (es, en) => L({es, en});
  const priority = {behind: 0, missing: 1, unverified: 2, current: 3, not_declared: 4, no_compiled_ui: 5};
  let list, refresh, notice, request, data;
  const expanded = new Set();
  const date = time => new Intl.DateTimeFormat(lang() === 'es' ? 'es-ES' : 'en-GB', {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(time * 1000);

  function addField(fields, label, value, technical = false) {
    if (value === null || value === undefined) return;
    const desc = el('dd');
    if (technical) desc.append(el('code', '', value));
    else desc.textContent = value;
    fields.append(el('dt', '', label), desc);
  }

  function appRow(app) {
    const row = el('li', 'compiled-ui-row');
    const header = el('div', 'compiled-ui-row-head');
    const title = el('h2', '', app.name);
    const state = el('span', 'compiled-ui-state', L(app.label));
    state.dataset.state = app.state;
    header.append(title, state);
    row.append(header);
    if (app.behind_by) {
      const amount = app.behind_by === 1 ? tr('1 commit de retraso', '1 commit behind')
        : tr(app.behind_by + ' commits de retraso', app.behind_by + ' commits behind');
      row.append(el('p', '', amount + ' · ' + tr('desde ', 'since ') + date(app.behind_since)));
    } else if (app.detail) {
      row.append(el('p', '', L(app.detail)));
    }
    if (app.built_at || app.source_at) {
      const dates = el('div', 'compiled-ui-dates');
      if (app.built_at) dates.append(el('span', '', tr('Compilación del ', 'Built ') + date(app.built_at)));
      if (app.source_at) dates.append(el('span', '', tr('Último cambio del ', 'Source changed ') + date(app.source_at)));
      row.append(dates);
    }
    if (app.output || app.fingerprint || app.source_commit) {
      const details = el('details', 'compiled-ui-details');
      details.open = expanded.has(app.app);
      details.ontoggle = () => details.open ? expanded.add(app.app) : expanded.delete(app.app);
      details.append(el('summary', '', tr('Datos de la compilación', 'Build details')));
      const fields = el('dl');
      addField(fields, tr('Carpeta compilada', 'Build folder'), app.output, true);
      addField(fields, tr('Huella SHA-256', 'SHA-256 fingerprint'), app.fingerprint, true);
      addField(fields, tr('Último commit de interfaz', 'Latest interface commit'), app.source_commit, true);
      addField(fields, tr('Commit de la compilación', 'Build commit'), app.build_commit, true);
      if (app.basis) addField(fields, tr('Comparación', 'Comparison'),
        app.basis === 'build_receipt' ? tr('Historial de Git', 'Git history') : tr('Fecha del archivo compilado', 'Compiled file date'));
      if (app.fingerprint) addField(fields, tr('Respuesta instalada', 'Installed response'), {
        matched: tr('Coincide con la compilación', 'Matches the build'),
        mismatch: tr('Difiere de la compilación', 'Differs from the build'),
        unreachable: tr('Sin respuesta', 'No response'),
        unverified: tr('Sin verificar', 'Unverified'),
      }[app.served]);
      details.append(fields);
      row.append(details);
    }
    return row;
  }

  function render() {
    refresh.textContent = tr('Actualizar', 'Refresh');
    refresh.disabled = Boolean(request);
    list.setAttribute('aria-busy', String(Boolean(request)));
    if (request) notice.textContent = tr('Comprobando las interfaces…', 'Checking interfaces…');
    else if (!data?.ok) notice.textContent = tr('No se ha podido comprobar. Vuelve a intentarlo.', 'Could not check interfaces. Try again.');
    else notice.textContent = '';
    if (data?.ok) {
      const apps = [...data.apps].sort((a, b) => priority[a.state] - priority[b.state] || a.name.localeCompare(b.name, lang()));
      list.replaceChildren(...apps.map(appRow));
    }
  }

  async function load() {
    if (request) return;
    request = api('/api/compiled-ui');
    render();
    try { data = await request; }
    catch (_) { data = null; }
    finally { request = null; render(); }
  }

  window.HubFacets.register('compiled_ui', {
    placement: 'tab',
    label: {es: 'Interfaces compiladas', en: 'Compiled interfaces'},
    mount(container) {
      const tools = el('div', 'tab-tools');
      refresh = el('button', 'ghost small');
      refresh.type = 'button';
      refresh.onclick = load;
      notice = el('span', 'hint');
      notice.setAttribute('role', 'status');
      tools.append(refresh, notice);
      list = el('ul', 'compiled-ui-list');
      container.append(tools, list);
      refresh.textContent = tr('Actualizar', 'Refresh');
    },
    load,
  });
})();

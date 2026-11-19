(() => {
  const lang = localStorage.getItem('hub.lang') || navigator.language;
  const es = lang.startsWith('es');
  const tr = (spanish, english) => es ? spanish : english;
  const $ = (selector) => document.querySelector(selector);
  document.documentElement.lang = tr('es', 'en');
  if (localStorage.getItem('hub.theme') === 'light') document.documentElement.dataset.hubTheme = 'light';
  if (es) {
    document.title = 'Programas locales · Hoard Hub';
    for (const element of document.querySelectorAll('[data-es]')) element.textContent = element.dataset.es;
  }

  const search = $('#search');
  const refresh = $('#refresh');
  const status = $('#status');
  const form = $('#register');
  const results = $('#results');
  const inventory = $('#inventory');
  let programs = [];
  let ready = false;
  let busy = false;

  function element(tag, content, className = '') {
    const node = document.createElement(tag);
    node.textContent = content;
    if (className) node.className = className;
    return node;
  }

  function programRow(row) {
    const item = element('li', '', 'program-row');
    const about = element('div', '');
    const version = row.version || tr('Versión desconocida', 'Unknown version');
    const source = row.source === 'manual' ? tr('Manual', 'Manual') : tr('Detección automática', 'Auto-detected');
    about.append(element('h2', row.name), element('p', row.path ? `${version} · ${source}` : tr('Sin detectar', 'Not detected')));

    if (row.source === 'manual') {
      const undo = element('button', tr('Deshacer alta', 'Undo registration'));
      undo.type = 'button';
      undo.setAttribute('aria-label', tr(`Deshacer alta de ${row.name}`, `Undo registration of ${row.name}`));
      undo.onclick = () => change('/api/programs/undo', {id: row.id},
        tr('Alta deshecha.', 'Registration undone.'), () => search.focus());
      about.append(undo);
    }

    const details = element('div', '', 'program-details');
    if (row.path) {
      details.append(element('code', row.path));
      if (row.interpreter) details.append(element('code', row.interpreter));
    }
    const usage = es && row.headless_es ? row.headless_es : row.headless;
    details.append(element('code', usage));
    item.append(about, details);
    return item;
  }

  function render(message = '') {
    const query = search.value.trim().toLocaleLowerCase();
    const matches = programs.filter((row) => [row.name, row.path, row.version].some(
      (value) => value?.toLocaleLowerCase().includes(query)
    ));
    results.replaceChildren(...matches.map(programRow));
    inventory.hidden = matches.length === 0;
    status.textContent = message || (matches.length ? '' : tr('No hay programas que coincidan.', 'No matching programs.'));
  }

  async function request(path, body) {
    const options = body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {};
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error);
    return data;
  }

  async function update(work, failure, loading = '') {
    if (busy) return;
    busy = true;
    const buttons = [refresh, ...form.querySelectorAll('button'), ...results.querySelectorAll('button')];
    buttons.forEach((button) => { button.disabled = true; });
    status.textContent = loading;
    try {
      await work();
    } catch {
      status.textContent = failure;
    } finally {
      busy = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  async function load(rescan = false, message = '') {
    const data = await request('/api/programs' + (rescan ? '?refresh=1' : ''));
    programs = data.programs;
    ready = true;
    render(message);
  }

  function change(path, args, message, after) {
    return update(async () => {
      await request(path, args);
      after?.();
      try {
        await load(false, message);
      } catch {
        status.textContent = message + ' ' + tr('Pulsa «Volver a detectar» para actualizar la lista.', 'Select “Detect again” to update the list.');
      }
    }, tr('No se pudo guardar el cambio. Comprueba los datos e inténtalo de nuevo.', 'Could not save the change. Check the details and try again.'));
  }

  function detect(rescan) {
    return update(() => load(rescan), tr('No se pudo consultar el registro. Vuelve a intentarlo.', 'Could not read the registry. Try again.'),
      tr('Detectando programas…', 'Detecting programs…'));
  }

  search.oninput = () => { if (ready && !busy) render(); };
  refresh.onclick = () => detect(true);
  form.onsubmit = (event) => {
    event.preventDefault();
    change('/api/programs/register', Object.fromEntries(new FormData(form)), tr('Programa guardado.', 'Program saved.'), () => form.reset());
  };
  detect(false);
})();

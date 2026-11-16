/* Hoard Hub — command palette (Ctrl/⌘ + K).
   Two modes over one list: «Ir a» (pages, tools, apps, projects, a content search) and «Acciones» (open, start, stop,
   restart and keep alive an app; start or stop everything; Faustus; theme and language). Tab or a leading «>» switches
   mode. Every command runs through the paths the page already uses (hubAppAction, hubOpenApplication, hubBatch…). */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const node = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const es = () => (localStorage.getItem('hub.lang') || navigator.language || 'en').startsWith('es');
  const tr = (spanish, english) => es() ? spanish : english;
  const fold = value => String(value || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLocaleLowerCase();
  const RECENT_KEY = 'hub.palette.recent';
  // Theme: dark is the family's default; light is a choice kept in this browser (index.html applies it before paint).
  window.hubSetTheme = theme => {
    const next = theme === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.hubTheme = next;
    try { localStorage.setItem('hub.theme', next); } catch (_) { /* private mode */ }
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', next === 'light' ? '#f6f3ec' : '#1c1a14');
    window.dispatchEvent(new CustomEvent('hub:theme', {detail: {theme: next}}));
  };
  let dialog, input, list, modeButtons, hint, items = [], active = 0, mode = 'go';

  function recent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); } catch (_) { return []; } }
  function remember(id) { try { localStorage.setItem(RECENT_KEY, JSON.stringify([id, ...recent().filter(x => x !== id)].slice(0, 8))); } catch (_) { /* private mode */ } }

  // -- commands ---------------------------------------------------------------------------------------------------
  function pages() {
    const out = [];
    const add = (id, title, section, keywords = '') => out.push({id: 'go:' + id, mode: 'go', group: tr('Ir a', 'Go to'), title, detail: section,
      keywords, icon: 'arrow', run: () => { location.hash = 'hub/' + id; }});
    add('home', tr('Inicio', 'Home'), '', 'resumen pendientes');
    add('projects', tr('Proyectos', 'Projects'), '', 'agora espacios');
    add('apps', tr('Aplicaciones', 'Applications'), '', 'hoards catalogo');
    add('communication', tr('Bandeja', 'Inbox'), '', 'avisos correo');
    add('automation', tr('Automatización', 'Automation'), '', 'reglas programaciones');
    add('system', tr('Sistema', 'System'), '', 'estado recursos');
    add('today', tr('Agenda y resumen', 'Agenda and digest'), tr('Inicio', 'Home'), 'calendario hoy');
    add('help', tr('Ayuda y ajustes', 'Help and settings'), '', 'configuracion');
    for (const tab of document.querySelectorAll('#family-tabs .tab[data-tab]')) {
      const id = tab.dataset.tab;
      if (id === 'agora') continue;
      const title = window.hubFacetLabel?.(id) || tab.querySelector('b')?.textContent || id;
      add(id, title, tr('Pestaña', 'Tab'), id);
    }
    add('resources', tr('Estado y conexiones', 'Status and connections'), tr('Sistema', 'System'), 'gpu reservas carriles');
    return out;
  }

  function appCommands() {
    const apps = window.hubApplications?.() || [];
    const out = [];
    for (const app of apps) {
      const running = app.state === 'running', starting = app.state === 'starting';
      const kw = [app.id, app.purpose, window.HubDiscovery?.description(app, es())].join(' ');
      const state = running ? tr('En marcha', 'Running') : starting ? tr('Arrancando', 'Starting') : tr('Parada', 'Stopped');
      const opener = {id: 'open:' + app.id, mode: 'both', group: tr('Aplicaciones', 'Applications'), title: (running ? tr('Abrir ', 'Open ') : tr('Arrancar y abrir ', 'Start and open ')) + app.name,
        detail: state, keywords: kw, app, run: () => window.hubOpenApplication?.(app.id)};
      if (running || app.launchable) out.push(opener);
      const act = (verb, title, extra = {}) => out.push({id: verb + ':' + app.id, mode: 'actions', group: tr('Aplicaciones', 'Applications'), title: title + ' ' + app.name,
        detail: state, keywords: kw, app, run: () => window.hubAppAction?.(app.id, verb), ...extra});
      if (!running && !starting && app.launchable) act('start', tr('Arrancar', 'Start'));
      if (running || starting) act('stop', tr('Parar', 'Stop'), {danger: true});
      if (running && app.launchable) act('restart', tr('Reiniciar', 'Restart'));
      const manualOnly = app.family?.auto_start === false || app.id === 'baird';
      if (app.keep_alive || (app.launchable && !manualOnly)) {
        out.push({id: 'keep:' + app.id, mode: 'actions', group: tr('Aplicaciones', 'Applications'),
          title: (app.keep_alive ? tr('Dejar de mantener viva ', 'Stop keeping alive ') : tr('Mantener viva ', 'Keep alive ')) + app.name,
          detail: app.keep_alive ? tr('El Hub la arranca y la relanza si cae', 'The Hub starts it and restarts it if it falls') : state,
          keywords: kw + ' siempre relanzar keep alive', app, run: () => window.hubKeepAlive?.(app.id, !app.keep_alive)});
      }
      out.push({id: 'log:' + app.id, mode: 'actions', group: tr('Aplicaciones', 'Applications'), title: tr('Ver registro de ', 'View log of ') + app.name,
        detail: state, keywords: kw + ' log', app, run: () => window.hubOpenLog?.(app.id)});
    }
    return out;
  }

  function projectCommands() {
    return (window.hubProjects?.() || []).map(space => ({id: 'project:' + space.id, mode: 'go', group: tr('Proyectos', 'Projects'),
      title: space.title, detail: space.counts?.waiting ? tr(`${space.counts.waiting} te esperan`, `${space.counts.waiting} waiting for you`) : tr('Proyecto', 'Project'),
      keywords: [space.id, space.description].join(' '), icon: 'projects', run: () => { location.href = '?space_id=' + encodeURIComponent(space.id) + '#hub/project'; }}));
  }

  function globalActions() {
    const out = [];
    const add = (id, title, detail, run, extra = {}) => out.push({id, mode: 'actions', group: tr('Hub', 'Hub'), title, detail, run, keywords: '', ...extra});
    add('all:start', tr('Arrancar todas las aplicaciones', 'Start all applications'), tr('Las que se pueden arrancar solas', 'The ones that can start on their own'), () => window.hubBatch?.('start'));
    add('all:stop', tr('Parar todas las aplicaciones', 'Stop all applications'), tr('También Faustus y los servicios', 'Faustus and services too'), () => window.hubBatch?.('stop'), {danger: true});
    add('all:rescan', tr('Volver a leer el catálogo', 'Rescan the catalogue'), tr('Busca apps nuevas en las carpetas', 'Look for new apps in the folders'), () => $('#btn-rescan')?.click());
    const snap = window.hubSnapshot?.(), faustus = snap?.faustus;
    if (faustus && faustus.available !== false && snap?.placement?.location !== 'sparks') {
      const on = !!(faustus.reachable || faustus.listening);
      add('faustus:toggle', on ? tr('Parar Faustus', 'Stop Faustus') : tr('Arrancar Faustus', 'Start Faustus'), 'Faustus', () => window.hubFaustus?.(on ? 'stop' : 'start'), on ? {danger: true} : {});
    }
    const dark = document.documentElement.dataset.hubTheme !== 'light';
    add('theme', dark ? tr('Cambiar a tema claro', 'Switch to light theme') : tr('Cambiar a tema oscuro', 'Switch to dark theme'), tr('Apariencia', 'Appearance'), () => window.hubSetTheme?.(dark ? 'light' : 'dark'), {keywords: 'tema claro oscuro theme'});
    add('lang', es() ? 'Switch to English' : 'Cambiar a español', tr('Idioma', 'Language'), () => $('#btn-lang')?.click(), {keywords: 'idioma language'});
    return out;
  }

  function contentSearch(query) {
    return {id: 'search:content', mode: 'both', group: tr('Buscar', 'Search'), title: query ? tr(`Buscar «${query}» en todo el contenido`, `Search “${query}” in all content`) : tr('Buscar en todo el contenido', 'Search all content'),
      detail: tr('Correo, chats y aplicaciones', 'Mail, chats and applications'), keywords: '', always: true, icon: 'search', run: () => searchContent(query)};
  }

  function searchContent(query) {
    location.hash = 'hub/search';
    const fill = (tries = 0) => {
      const field = document.querySelector('.sr-q');
      if (!field || field.offsetParent === null) { if (tries < 40) setTimeout(() => fill(tries + 1), 50); return; }
      field.value = query; field.focus();
      if (query) field.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    };
    fill();
  }

  // -- matching ----------------------------------------------------------------------------------------------------
  function score(cmd, words) {
    if (!words.length) return 1;
    const title = fold(cmd.title), hay = title + ' ' + fold(cmd.detail) + ' ' + fold(cmd.keywords) + ' ' + fold(cmd.app?.name);
    let total = 0;
    for (const word of words) {
      if (!hay.includes(word)) return 0;
      total += title.startsWith(word) ? 6 : title.split(/\s+/).some(part => part.startsWith(word)) ? 4 : title.includes(word) ? 2 : 1;
    }
    return total;
  }

  function compute() {
    let raw = input.value;
    if (raw.startsWith('>')) { if (mode !== 'actions') setMode('actions', false); raw = raw.slice(1); }
    const query = raw.trim(), words = fold(query).split(/\s+/).filter(Boolean);
    const all = [...pages(), ...projectCommands(), ...appCommands(), ...globalActions()];
    const pool = all.filter(c => c.mode === 'both' || c.mode === mode);
    const recents = recent();
    let ranked = pool.map(c => [score(c, words), c]).filter(([s]) => s > 0);
    if (!words.length) {
      // Empty: what you ran last, then the most useful entries of this mode.
      ranked = ranked.map(([s, c]) => [recents.includes(c.id) ? 100 - recents.indexOf(c.id) : c.id.startsWith('go:') ? 4 : c.id.startsWith('project:') ? 3 : c.app?.state === 'running' ? 2 : s, c]);
    }
    ranked.sort((a, b) => b[0] - a[0] || a[1].title.localeCompare(b[1].title));
    // One block per group, in the order each group's best entry ranks, so a heading never repeats.
    const blocks = new Map();
    for (const [, c] of ranked.slice(0, 40)) {
      const cmd = {...c, recent: !words.length && recents.includes(c.id)};
      const key = cmd.recent ? ' recent' : cmd.group;
      if (!blocks.has(key)) blocks.set(key, []);
      blocks.get(key).push(cmd);
    }
    const out = [...blocks.values()].flat();
    if (mode === 'go') out.push(contentSearch(query));
    return out;
  }

  // -- rendering -------------------------------------------------------------------------------------------------------
  function render() {
    items = compute();
    active = Math.min(active, Math.max(0, items.length - 1));
    list.replaceChildren();
    let lastGroup = '';
    items.forEach((cmd, index) => {
      const group = cmd.recent ? tr('Recientes', 'Recent') : cmd.group;
      if (group !== lastGroup) { const head = node('li', 'hub-palette-group', group); head.setAttribute('role', 'presentation'); list.append(head); lastGroup = group; }
      const row = node('li', 'hub-palette-item' + (cmd.danger ? ' is-danger' : ''));
      row.id = 'hub-palette-opt-' + index; row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(index === active));
      if (cmd.app) {
        const img = node('img'); img.alt = ''; img.width = img.height = 24; img.loading = 'lazy';
        img.src = window.hubAppIcon ? window.hubAppIcon(cmd.app.id) : '/ui/fallback-icon.svg';
        img.onerror = () => { if (!img.src.endsWith('/ui/fallback-icon.svg')) img.src = '/ui/fallback-icon.svg'; };
        row.append(img);
      } else row.append(node('span', 'hub-palette-glyph', cmd.icon === 'search' ? '⌕' : cmd.mode === 'actions' ? '›' : '→'));
      const text = node('span', 'hub-palette-text'); text.append(node('strong', '', cmd.title));
      if (cmd.detail) text.append(node('small', '', cmd.detail));
      row.append(text);
      if (cmd.app) row.append(node('span', 'hub-palette-state is-' + cmd.app.state, ''));
      row.onmousemove = () => { if (active !== index) { active = index; mark(); } };
      row.onclick = () => run(index);
      list.append(row);
    });
    if (!items.length) list.append(node('li', 'hub-palette-empty', tr('Nada coincide. Prueba otra palabra o cambia de modo con Tab.', 'Nothing matches. Try another word or switch mode with Tab.')));
    mark();
  }

  function mark() {
    for (const row of list.querySelectorAll('.hub-palette-item')) row.setAttribute('aria-selected', String(row.id === 'hub-palette-opt-' + active));
    const current = list.querySelector('#hub-palette-opt-' + active);
    input.setAttribute('aria-activedescendant', current ? current.id : '');
    current?.scrollIntoView({block: 'nearest'});
  }

  function run(index) {
    const cmd = items[index];
    if (!cmd) return;
    if (!cmd.always) remember(cmd.id);
    close();
    try { cmd.run(); } catch (error) { window.hubToast?.(String(error), 'err'); }
  }

  function setMode(next, rerender = true) {
    mode = next;
    for (const b of modeButtons) b.setAttribute('aria-pressed', String(b.dataset.mode === mode));
    input.placeholder = mode === 'actions' ? tr('Arrancar, parar, mantener viva… (escribe una app)', 'Start, stop, keep alive… (type an app)') : tr('Ir a una página, una app o un proyecto…', 'Go to a page, an app or a project…');
    if (rerender) { active = 0; render(); }
  }

  function build() {
    dialog = node('dialog', 'hub-palette'); dialog.id = 'hub-palette'; dialog.setAttribute('aria-label', tr('Paleta de comandos', 'Command palette'));
    const head = node('div', 'hub-palette-head');
    const modes = node('div', 'hub-palette-modes'); modes.setAttribute('role', 'group'); modes.setAttribute('aria-label', tr('Modo', 'Mode'));
    modeButtons = [['go', tr('Ir a', 'Go to')], ['actions', tr('Acciones', 'Actions')]].map(([id, label]) => {
      const b = node('button', '', label); b.type = 'button'; b.dataset.mode = id; b.onclick = () => { setMode(id); input.focus(); }; modes.append(b); return b;
    });
    input = node('input', 'hub-palette-input'); input.type = 'text'; input.autocomplete = 'off'; input.spellcheck = false;
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-controls', 'hub-palette-list'); input.setAttribute('aria-autocomplete', 'list');
    head.append(input, modes);
    list = node('ul', 'hub-palette-list'); list.id = 'hub-palette-list'; list.setAttribute('role', 'listbox');
    hint = node('p', 'hub-palette-hint');
    dialog.append(head, list, hint);
    document.body.append(dialog);
    input.addEventListener('input', () => { active = 0; render(); });
    input.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') { event.preventDefault(); active = Math.min(items.length - 1, active + 1); mark(); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); active = Math.max(0, active - 1); mark(); }
      else if (event.key === 'Home' && event.ctrlKey) { active = 0; mark(); }
      else if (event.key === 'Enter') { event.preventDefault(); run(active); }
      else if (event.key === 'Tab') { event.preventDefault(); setMode(mode === 'go' ? 'actions' : 'go'); }
      else if (event.key === 'Backspace' && !input.value && mode === 'actions') { setMode('go'); }
    });
    dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
    dialog.addEventListener('close', () => document.documentElement.classList.remove('hub-palette-open'));
  }

  function open(nextMode = 'go') {
    if (!dialog) build();
    hint.textContent = tr('↑↓ elegir · Intro ejecutar · Tab cambia de modo · «>» acciones · Esc cerrar', '↑↓ choose · Enter run · Tab switches mode · “>” actions · Esc close');
    for (const b of modeButtons) b.textContent = b.dataset.mode === 'go' ? tr('Ir a', 'Go to') : tr('Acciones', 'Actions');
    input.value = ''; active = 0; setMode(nextMode, false); render();
    if (!dialog.open) dialog.showModal();
    document.documentElement.classList.add('hub-palette-open');
    input.focus();
  }

  function close() { if (dialog?.open) dialog.close(); }

  // Ctrl/⌘+K anywhere opens it; this listener runs before the content search's own shortcut.
  window.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault(); event.stopImmediatePropagation();
      if (dialog?.open) close(); else open();
    }
  }, true);

  window.HubPalette = {open, close, commands: () => [...pages(), ...projectCommands(), ...appCommands(), ...globalActions()].map(({run, app, ...rest}) => ({...rest, app: app?.id}))};
})();

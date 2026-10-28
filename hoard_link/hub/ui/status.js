/* Hoard Hub status: Faustus, the Hoards and the GPUs on every page, the Hoards status table and the
   compact launcher. Everything is drawn from the snapshot app.js already polls (/api/apps every 5 s)
   and the GPU lease poll, so this file adds no requests of its own. */
(() => {
  'use strict';
  const es = () => (window.hubLang ? window.hubLang() : (localStorage.getItem('hub.lang') || navigator.language || 'en')).startsWith('es');
  const tr = (spanish, english) => es() ? spanish : english;
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = text; return n; };
  const gb = mb => (mb / 1024).toFixed(1);
  let snap = null, apps = [], busy = new Set(), fPending = new Set(), batchBusy = false, gpu = null;
  const mounts = new Set();
  const STATES = {running: ['En marcha', 'Running'], starting: ['Arrancando', 'Starting'], foreign: ['Puerto ocupado', 'Port busy'], down: ['Parada', 'Stopped'], unavailable: ['No disponible', 'Unavailable']};
  const stateOf = app => app.state === 'down' && !app.launchable ? 'unavailable' : app.state;
  const stateText = state => tr(...(STATES[state] || [state, state]));
  const rank = {running: 0, starting: 1, foreign: 2, down: 3, unavailable: 4};
  const sorted = list => [...list].sort((a, b) => (rank[stateOf(a)] - rank[stateOf(b)]) || a.name.localeCompare(b.name));
  const icon = app => window.hubAppIcon ? window.hubAppIcon(app.id) : '/api/apps/' + encodeURIComponent(app.id) + '/icon';
  function img(app, size) {
    const i = node('img'); i.src = icon(app); i.alt = ''; i.width = i.height = size; i.loading = 'lazy';
    i.onerror = () => { if (!i.src.endsWith('/ui/fallback-icon.svg')) i.src = '/ui/fallback-icon.svg'; };
    return i;
  }
  function tally() {
    const c = {total: apps.length, running: 0, starting: 0, attention: 0, stopped: 0, unavailable: 0};
    for (const a of apps) {
      const s = stateOf(a);
      if (s === 'running') c.running++;
      else if (s === 'starting') { c.starting++; c.attention++; }
      else if (s === 'foreign') c.attention++;
      else if (s === 'unavailable') c.unavailable++;
      else c.stopped++;
    }
    return c;
  }
  function faustus() {
    const f = snap?.faustus || {};
    const state = fPending.has('stop') ? 'stopping' : fPending.has('start') ? 'starting' : f.reachable ? 'running' : f.listening ? 'busy' : 'down';
    const label = {running: tr('En marcha', 'Running'), busy: tr('Ocupado, no responde', 'Busy, not answering'), starting: tr('Arrancando…', 'Starting…'), stopping: tr('Parando…', 'Stopping…'), down: f.available === false ? tr('No instalado', 'Not installed') : tr('Apagado', 'Off')}[state];
    let port = '';
    try { port = f.url ? ':' + new URL(f.url).port : ''; } catch (_) { /* keep empty */ }
    return {f, state, label, port};
  }
  function button(label, cls, onclick, {disabled = false, title = ''} = {}) {
    const b = node('button', cls, label); b.type = 'button'; b.onclick = onclick; b.disabled = disabled; if (title) b.title = title; return b;
  }
  function faustusButtons(compact) {
    const {f, state} = faustus();
    const out = [];
    if ((f.reachable || f.listening) && f.url) {
      const open = node('a', 'hub-btn hub-btn-primary', tr('Abrir', 'Open')); open.href = f.url; open.target = '_blank'; open.rel = 'noopener';
      open.title = tr('Abrir Faustus en ', 'Open Faustus at ') + f.url; out.push(open);
    }
    const busyNow = fPending.size > 0 || batchBusy;
    if (state === 'down' || state === 'starting') out.push(button(tr('Arrancar', 'Start'), 'hub-btn' + (f.reachable ? '' : ' hub-btn-primary'), () => window.hubFaustus?.('start'), {disabled: busyNow || !f.startable, title: tr('Arrancar Faustus', 'Start Faustus')}));
    if (state === 'running' || state === 'stopping' || state === 'busy') out.push(button(compact ? tr('Parar', 'Stop') : tr('Parar Faustus', 'Stop Faustus'), 'hub-btn hub-btn-danger', () => window.hubFaustus?.('stop'), {disabled: busyNow || !f.stoppable}));
    return out;
  }

  // ---- header status bar ------------------------------------------------------------------------------------
  function statusbar() {
    const bar = document.getElementById('hub-statusbar'); if (!bar) return;
    bar.replaceChildren();
    if (!snap) { bar.append(node('span', 'hub-sb-item hub-sb-loading', tr('Consultando estado…', 'Checking status…'))); return; }
    const {state, label, port} = faustus();
    const fz = node('div', 'hub-sb-item hub-sb-faustus is-' + state);
    fz.append(node('span', 'hub-dot'), node('strong', '', 'Faustus'), node('span', 'hub-sb-text', label + ((state === 'running' || state === 'busy') && port ? ' · ' + port : '')));
    fz.append(...faustusButtons(true));
    const c = tally();
    const hoards = node('a', 'hub-sb-item hub-sb-link' + (c.attention ? ' has-attention' : ''));
    hoards.href = '#hub/system'; hoards.title = tr('Estado de los Hoards', 'Hoards status');
    hoards.append(node('span', 'hub-dot ' + (c.running ? 'is-running' : 'is-down')), node('strong', '', `${c.running}/${c.total}`), node('span', 'hub-sb-text', tr('Hoards en marcha', 'Hoards running')));
    if (c.attention) hoards.append(node('span', 'hub-sb-warn', `${c.attention} ⚠`));
    bar.append(fz, hoards);
    if (gpu?.gpus?.length) {
      const g = node('a', 'hub-sb-item hub-sb-link hub-sb-gpu'); g.href = '#hub/system'; g.title = gpu.gpus.map(x => `GPU ${x.index}: ${gb(x.used_mb)} / ${gb(x.total_mb)} GB`).join('\n');
      g.append(node('strong', '', 'GPU'));
      for (const x of gpu.gpus) {
        const meter = node('span', 'hub-mini-meter'); const fill = node('span'); fill.style.height = Math.min(100, (x.used_mb + (x.reserved_mb || 0)) / x.total_mb * 100) + '%'; meter.append(fill); g.append(meter);
      }
      const used = gpu.gpus.reduce((s, x) => s + x.used_mb, 0), total = gpu.gpus.reduce((s, x) => s + x.total_mb, 0);
      g.append(node('span', 'hub-sb-text', `${gb(used)}/${gb(total)} GB`));
      bar.append(g);
    }
    const dot = document.querySelector('.hub-connection-dot'); if (dot) dot.classList.add('is-on');
  }

  // ---- home overview ------------------------------------------------------------------------------------------
  function overview(box) {
    box.replaceChildren();
    const {f, state, label} = faustus();
    const fz = node('section', 'hub-tile hub-tile-faustus is-' + state);
    const head = node('div', 'hub-tile-head'); head.append(node('span', 'hub-dot'), node('h2', '', 'Faustus'), node('span', 'hub-tile-state', label));
    const sub = f.reachable ? (f.url || '').replace(/^https?:\/\//, '')
      : f.listening ? tr(`Su puerto sigue abierto (PID ${f.pid}) pero no contesta en 3 s: suele ser un turno largo bloqueándolo. Espera o páralo.`, `Its port is still open (PID ${f.pid}) but it does not answer within 3 s, usually a long turn blocking it. Wait or stop it.`)
      : tr('Tu espacio de trabajo con modelos. Arráncalo desde aquí.', 'Your model workspace. Start it from here.');
    fz.append(head, node('p', 'hub-tile-sub', sub));
    const fa = node('div', 'hub-tile-actions'); fa.append(...faustusButtons(false)); fz.append(fa);

    const c = tally();
    const hz = node('section', 'hub-tile hub-tile-hoards');
    const hh = node('div', 'hub-tile-head'); hh.append(node('span', 'hub-dot ' + (c.running ? 'is-running' : 'is-down')), node('h2', '', 'Hoards'), node('span', 'hub-tile-state', `${c.running} ${tr('de', 'of')} ${c.total} ${tr('en marcha', 'running')}`));
    hz.append(hh);
    const chips = node('div', 'hub-tile-chips');
    const chip = (n, text, cls, filter) => { if (!n) return; const a = node('a', 'hub-count ' + cls); a.href = '#hub/system'; a.onclick = () => { try { localStorage.setItem('hub.status.filter', filter); } catch (_) {} }; a.append(node('b', '', String(n)), document.createTextNode(' ' + text)); chips.append(a); };
    chip(c.running, tr('en marcha', 'running'), 'is-running', 'running');
    chip(c.attention, tr('con incidencias', 'need attention'), 'is-attention', 'attention');
    chip(c.stopped, tr('paradas', 'stopped'), 'is-down', 'stopped');
    chip(c.unavailable, tr('no disponibles', 'unavailable'), 'is-unavailable', 'unavailable');
    hz.append(chips);
    const ha = node('div', 'hub-tile-actions');
    const see = node('a', 'hub-btn', tr('Ver estado', 'View status')); see.href = '#hub/system'; ha.append(see);
    ha.append(button(tr('Arrancar todo', 'Start all'), 'hub-btn', () => window.hubBatch?.('start'), {disabled: batchBusy}));
    hz.append(ha);

    const gz = node('section', 'hub-tile hub-tile-gpu');
    const gh = node('div', 'hub-tile-head'); gh.append(node('h2', '', 'GPU'));
    const leases = (gpu?.leases || []).length, queued = (gpu?.queue || []).length;
    gh.append(node('span', 'hub-tile-state', gpu ? `${leases} ${tr(leases === 1 ? 'reserva' : 'reservas', leases === 1 ? 'lease' : 'leases')}${queued ? ` · ${queued} ${tr('en cola', 'queued')}` : ''}` : tr('Consultando…', 'Checking…')));
    gz.append(gh);
    const bars = node('div', 'hub-gpu-bars');
    for (const x of gpu?.gpus || []) {
      const row = node('a', 'hub-gpu-bar'); row.href = '#hub/system';
      const pct = Math.min(100, x.used_mb / x.total_mb * 100), res = Math.max(0, Math.min(100 - pct, (x.reserved_mb || 0) / x.total_mb * 100));
      const track = node('span', 'hub-bar'); const u = node('span', 'u'); u.style.width = pct + '%'; const r = node('span', 'r'); r.style.width = res + '%'; track.append(u, r);
      row.append(node('b', '', String(x.index)), track, node('span', 'hub-gpu-num', `${gb(x.used_mb)}/${gb(x.total_mb)}`));
      bars.append(row);
    }
    if (gpu && !(gpu.gpus || []).length) bars.append(node('p', 'hub-tile-sub', tr('Sin GPU NVIDIA detectada.', 'No NVIDIA GPU detected.')));
    gz.append(bars);
    box.append(fz, hz, gz);
  }

  // ---- compact launcher -------------------------------------------------------------------------------------
  function launcher(box) {
    const list = sorted(apps);
    const known = new Map([...box.children].map(n => [n.dataset.id, n]));
    const order = [];
    for (const app of list) {
      let b = known.get(app.id);
      if (!b) {
        b = node('button', 'hub-launch'); b.type = 'button'; b.dataset.id = app.id;
        b.append(img(app, 28), node('span', 'hub-launch-name'), node('span', 'hub-dot'));
        b.onclick = () => {
          const a = apps.find(x => x.id === b.dataset.id); if (!a) return;
          if (stateOf(a) === 'unavailable') { location.hash = 'hub/apps'; window.hubFocusApplication?.(a.id); }
          else window.hubOpenApplication?.(a.id);
        };
      }
      const s = stateOf(app);
      b.className = 'hub-launch is-' + s + (busy.has(app.id) ? ' is-busy' : '');
      b.querySelector('.hub-launch-name').textContent = app.name;
      b.querySelector('.hub-dot').className = 'hub-dot is-' + s;
      const desc = window.HubDiscovery ? window.HubDiscovery.description(app, es()) : (app.purpose || '');
      b.title = `${app.name} · ${stateText(s)}${app.port ? ' · :' + app.port : ''}\n${desc}\n${s === 'running' ? tr('Clic: abrir', 'Click: open') : s === 'unavailable' ? tr('Clic: ver por qué', 'Click: see why') : tr('Clic: arrancar y abrir', 'Click: start and open')}`;
      b.setAttribute('aria-label', `${app.name}, ${stateText(s)}`);
      order.push(b); known.delete(app.id);
    }
    known.forEach(n => n.remove());
    order.forEach(n => box.append(n));
    if (!list.length && !box.querySelector('.hub-loading')) box.append(node('p', 'hub-loading', tr('Cargando Hoards…', 'Loading Hoards…')));
    if (list.length) box.querySelector('.hub-loading')?.remove();
  }

  // ---- status table ------------------------------------------------------------------------------------------
  const FILTERS = [['all', ['Todos', 'All']], ['running', ['En marcha', 'Running']], ['attention', ['Incidencias', 'Attention']], ['stopped', ['Parados', 'Stopped']], ['unavailable', ['No disponibles', 'Unavailable']]];
  const matches = (app, filter) => {
    const s = stateOf(app);
    return filter === 'all' || (filter === 'running' && s === 'running') || (filter === 'attention' && (s === 'foreign' || s === 'starting')) || (filter === 'stopped' && s === 'down') || (filter === 'unavailable' && s === 'unavailable');
  };
  function savedFilter() { try { return localStorage.getItem('hub.status.filter') || 'all'; } catch (_) { return 'all'; } }
  function table(box) {
    if (!box._built) {
      box._built = true; box.classList.add('hub-status-table');
      const tools = node('div', 'hub-status-tools');
      const seg = node('div', 'hub-seg'); seg.setAttribute('role', 'group');
      for (const [id] of FILTERS) { const b = node('button', ''); b.type = 'button'; b.dataset.filter = id; b.onclick = () => { try { localStorage.setItem('hub.status.filter', id); } catch (_) {} renderAll(); }; seg.append(b); }
      const q = node('input', 'hub-status-q'); q.type = 'search'; q.oninput = () => table(box);
      const bulk = node('div', 'hub-status-bulk');
      bulk.append(button('', 'hub-btn hub-bulk-start', () => window.hubBatch?.('start')), button('', 'hub-btn hub-btn-danger hub-bulk-stop', () => window.hubBatch?.('stop')));
      tools.append(seg, q, bulk);
      const wrap = node('div', 'hub-table-wrap');
      const t = node('table', 'hub-table'); t.append(node('thead'), node('tbody')); wrap.append(t);
      const empty = node('p', 'hub-empty hub-status-empty'); empty.hidden = true;
      box.append(tools, wrap, empty);
    }
    const filter = savedFilter(); const c = tally();
    const n = {all: c.total, running: c.running, attention: c.attention, stopped: c.stopped, unavailable: c.unavailable};
    for (const b of box.querySelectorAll('.hub-seg button')) {
      const [id, label] = FILTERS.find(x => x[0] === b.dataset.filter);
      b.textContent = `${tr(...label)} ${n[id]}`; b.setAttribute('aria-pressed', String(id === filter)); b.hidden = id !== 'all' && !n[id] && id !== filter;
    }
    const q = box.querySelector('.hub-status-q'); q.placeholder = tr('Filtrar Hoards…', 'Filter Hoards…'); q.setAttribute('aria-label', q.placeholder);
    box.querySelector('.hub-bulk-start').textContent = tr('Arrancar todo', 'Start all');
    box.querySelector('.hub-bulk-stop').textContent = tr('Parar todo', 'Stop all');
    box.querySelectorAll('.hub-status-bulk button').forEach(b => { b.disabled = batchBusy; });
    const head = box.querySelector('thead');
    const labels = [tr('Estado', 'State'), 'Hoard', tr('Puerto', 'Port'), 'PID', tr('Activo', 'Uptime'), 'RAM', tr('Ventanas', 'Windows'), ''];
    if (head.dataset.lang !== String(es())) { head.dataset.lang = String(es()); const tr0 = node('tr'); labels.forEach((l, i) => { const th = node('th', i >= 2 && i <= 6 ? 'num' : '', l); th.scope = 'col'; tr0.append(th); }); head.replaceChildren(tr0); }
    const query = q.value.trim().toLocaleLowerCase();
    const list = sorted(apps).filter(a => matches(a, filter) && (!query || [a.name, a.id, a.purpose, String(a.port || '')].join(' ').toLocaleLowerCase().includes(query)));
    const body = box.querySelector('tbody');
    const known = new Map([...body.children].map(r => [r.dataset.id, r]));
    const order = [];
    for (const a of list) {
      let row = known.get(a.id);
      if (!row) {
        row = node('tr'); row.dataset.id = a.id;
        const st = node('td', 'st'); st.append(node('span', 'hub-dot'), node('span', 'st-label'));
        const nm = node('td', 'nm'); const name = node('span', 'nm-wrap'); name.append(img(a, 22), node('strong'), node('small')); nm.append(name);
        row.append(st, nm, node('td', 'num port'), node('td', 'num pid'), node('td', 'num up'), node('td', 'num mem'), node('td', 'num win'), node('td', 'acts'));
      }
      const s = stateOf(a), p = a.process || null;
      row.className = 'is-' + s + (busy.has(a.id) ? ' is-busy' : '');
      row.querySelector('.st .hub-dot').className = 'hub-dot is-' + s;
      row.querySelector('.st-label').textContent = busy.has(a.id) ? tr('Trabajando…', 'Working…') : stateText(s);
      row.querySelector('.st').title = a.health?.detail || (a.launchable ? '' : (window.HubDiscovery?.launchMessage(a, es()) || ''));
      row.querySelector('.nm strong').textContent = a.name;
      const desc = window.HubDiscovery ? window.HubDiscovery.description(a, es()) : (a.purpose || '');
      row.querySelector('.nm small').textContent = desc; row.querySelector('.nm').title = `${a.id}\n${a.purpose || ''}`;
      row.querySelector('.port').textContent = a.port ? String(a.port) : '—';
      row.querySelector('.pid').textContent = p ? String(p.pid) : '—';
      row.querySelector('.up').textContent = p && p.uptime_s != null ? (window.hubFmtUptime ? window.hubFmtUptime(p.uptime_s) : p.uptime_s + 's') : '—';
      row.querySelector('.mem').textContent = p && p.rss_mb != null ? `${Math.round(p.rss_mb)} MB` : '—';
      const wins = (a.windows || []).length; row.querySelector('.win').textContent = wins ? String(wins) : '—';
      const acts = row.querySelector('.acts'); const sig = [s, busy.has(a.id), a.launchable, a.stoppable, wins, es()].join('|');
      if (acts.dataset.sig !== sig) {
        acts.dataset.sig = sig; acts.replaceChildren();
        const dis = busy.has(a.id) || batchBusy;
        if (s === 'running' || (a.launchable && s !== 'unavailable')) acts.append(button(s === 'running' ? tr('Abrir', 'Open') : tr('Arrancar y abrir', 'Start & open'), 'hub-btn hub-btn-sm' + (s === 'running' ? ' hub-btn-primary' : ''), () => window.hubOpenApplication?.(a.id), {disabled: dis}));
        if (s === 'down' && a.launchable) acts.append(button(tr('Arrancar', 'Start'), 'hub-btn hub-btn-sm', () => window.hubAppAction?.(a.id, 'start'), {disabled: dis}));
        if (s === 'running') acts.append(button(tr('Reiniciar', 'Restart'), 'hub-btn hub-btn-sm', () => window.hubAppAction?.(a.id, 'restart'), {disabled: dis || !a.launchable}));
        if (s === 'running' || s === 'starting') acts.append(button(tr('Parar', 'Stop'), 'hub-btn hub-btn-sm hub-btn-danger', () => window.hubAppAction?.(a.id, 'stop'), {disabled: dis || (!a.stoppable && s !== 'starting')}));
        if (s === 'foreign' && a.port_adaptable) acts.append(button(tr('Arrancar en otro puerto', 'Start on another port'), 'hub-btn hub-btn-sm', () => window.hubAppAction?.(a.id, 'start'), {disabled: dis}));
        acts.append(button(tr('Log', 'Log'), 'hub-btn hub-btn-sm hub-btn-ghost', () => window.hubOpenLog?.(a.id)));
        if (s === 'unavailable') acts.append(button(tr('Por qué', 'Why'), 'hub-btn hub-btn-sm hub-btn-ghost', () => { location.hash = 'hub/apps'; window.hubFocusApplication?.(a.id); }));
      }
      order.push(row); known.delete(a.id);
    }
    known.forEach(r => r.remove());
    order.forEach(r => body.append(r));
    const empty = box.querySelector('.hub-status-empty');
    empty.hidden = !!list.length || !snap; empty.textContent = tr('No hay Hoards con este filtro.', 'No Hoards match this filter.');
  }

  // ---- mounting ---------------------------------------------------------------------------------------------
  const kinds = {overview, launcher, table};
  function mount(box, kind) { box.dataset.hubStatus = kind; mounts.add(box); kinds[kind](box); return box; }
  function renderAll() {
    statusbar();
    for (const box of [...mounts]) { if (!box.isConnected && box.dataset.keep !== '1') { mounts.delete(box); continue; } kinds[box.dataset.hubStatus](box); }
  }
  window.addEventListener('hub:snapshot', event => {
    const d = event.detail; snap = d.snapshot; apps = d.apps || []; busy = new Set(d.busy || []); fPending = new Set(d.faustusPending || []); batchBusy = !!d.batchBusy;
    renderAll();
  });
  window.addEventListener('hub:leases', event => { gpu = event.detail; renderAll(); });
  document.addEventListener('click', event => { if (event.target.closest?.('#btn-lang')) setTimeout(renderAll, 0); });
  // An app's Manage menu floats over the grid: close it when the click lands elsewhere or on Escape.
  document.addEventListener('click', event => { for (const d of document.querySelectorAll('#hub-apps details.app-management[open]')) if (!d.contains(event.target)) d.open = false; });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') for (const d of document.querySelectorAll('#hub-apps details.app-management[open]')) { d.open = false; d.querySelector('summary')?.focus(); } });
  window.HubStatus = {
    overview: box => mount(box, 'overview'),
    launcher: box => mount(box, 'launcher'),
    table: (box, {keep = false} = {}) => { if (keep) box.dataset.keep = '1'; return mount(box, 'table'); },
    counts: tally,
  };
  statusbar();
})();

/* Project-first Hub navigation. Existing facets keep their forms, API and state. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const node = (tag, cls, text) => {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const es = () => (localStorage.getItem('hub.lang') || navigator.language || 'en').startsWith('es');
  const tr = (spanish, english) => es() ? spanish : english;
  const icons = {
    home: '<path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
    projects: '<path d="M3 6h7l2 3h9v11H3Z"/><path d="M3 9V4h7l2 2h7v3"/>',
    apps: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    communication: '<path d="M21 4H3v13h5v4l5-4h8Z"/><path d="M7 8h10M7 12h6"/>',
    automation: '<path d="m13 2-9 12h7l-1 8 10-12h-7Z"/>',
    system: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9ZM9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/>',
    search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  };
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = icons[name] || icons.arrow;
    return svg;
  }
  const groups = [
    {id: 'home', label: ['Inicio', 'Home'], description: ['Abre una aplicación, continúa un proyecto o consulta el trabajo compartido.', 'Open an app, continue a project or check shared work.']},
    {id: 'projects', label: ['Proyectos', 'Projects'], description: ['Cada proyecto tiene su propia ágora, sus tareas y sus conversaciones.', 'Each project has its own Agora, tasks and conversations.']},
    {id: 'apps', label: ['Aplicaciones', 'Applications'], description: ['Abre una aplicación o gestiona sus grupos de arranque.', 'Open an app or manage startup groups.']},
    {id: 'communication', label: ['Bandeja', 'Inbox'], defaultView: 'notify', facets: ['notify', 'mailgate', 'chats', 'refs', 'search'], description: ['Avisos, correo, chats y enlaces de la familia.', 'Family notifications, mail, chats and links.']},
    {id: 'automation', label: ['Automatización', 'Automation'], defaultView: 'work', facets: ['work', 'rules', 'jobs', 'purchases'], description: ['Trabajos, reglas, tareas programadas y compras.', 'Work, rules, scheduled jobs and purchases.']},
    {id: 'system', label: ['Sistema', 'System'], defaultView: 'resources', facets: ['resources', 'services', 'capabilities', 'agent_sessions', 'repos', 'events', 'backups', 'audit', 'web'], description: ['Recursos, servidores y mantenimiento del entorno compartido.', 'Resources, servers and shared environment maintenance.']},
  ];
  const descriptions = {
    notify: ['Avisos de tus aplicaciones, con filtros de estado y esfera.', 'Notifications from your apps, filtered by state and sphere.'],
    mailgate: ['Consulta el correo guardado y abre cada mensaje para leerlo.', 'Browse stored mail and open each message to read it.'],
    chats: ['Conversaciones de tus fuentes conectadas, con búsqueda y filtros.', 'Conversations from your connected sources, with search and filters.'],
    refs: ['Encuentra las relaciones entre documentos, compras y otros registros.', 'Find relationships between documents, purchases and other records.'],
    search: ['Busca destinos del Hub y contenido de las aplicaciones y el correo.', 'Find Hub destinations and content from your apps and mail.'],
    work: ['Sigue los trabajos en marcha y consulta los que han terminado.', 'Follow running work and review completed jobs.'],
    purchases: ['Compras, pedidos y su estado a lo largo del proceso.', 'Purchases, orders and their progress.'],
    services: ['Comprueba qué aplicación ofrece cada servicio compartido.', 'Check which application supplies each shared service.'],
    capabilities: ['Encuentra herramientas disponibles y sus requisitos.', 'Find available tools and their requirements.'],
    agent_sessions: ['Consulta las escrituras de los agentes y sus motivos.', 'Review agent writes and their reasons.'],
    web: ['Leer páginas y buscar en la web con los servicios configurados.', 'Read pages and search the web using configured services.'],
    resources: ['GPU, reservas y carriles de navegador.', 'GPUs, leases and browser lanes.'],
    events: ['Actividad que publican las aplicaciones.', 'Activity published by applications.'],
    rules: ['Acciones que responden a eventos.', 'Actions triggered by events.'],
    jobs: ['Acciones con una programación horaria.', 'Actions on a schedule.'],
    backups: ['Crear, verificar y restaurar copias.', 'Create, verify and restore backups.'],
    repos: ['Repositorios, cambios y publicación.', 'Repositories, changes and publishing.'],
    audit: ['Revisar el estado de las aplicaciones.', 'Inspect application health.'],
    agora: ['Tareas y debates de este proyecto.', 'Tasks and discussions for this project.'],
  };
  let spaces = [], spacesError = '', workspaceUrl = '', apps = [], ready = false;
  let route = 'home', selecting = false, appsSignature = '';
  const activeView = () => groups.find(g => g.id === route)?.defaultView || route;
  const labels = value => es() ? value[0] : value[1];
  const facetButtons = () => [...document.querySelectorAll('#family-tabs .tab')];
  function facetLabel(id) {
    return id === 'resources' ? tr('Recursos', 'Resources') : $('#family-tabs .tab[data-tab="' + id + '"] b')?.textContent || id;
  }
  function groupFor(id) { return groups.find(group => group.facets?.includes(id)) || groups[5]; }
  function spaceId() { return new URLSearchParams(location.search).get('space_id') || 'ecosystem'; }
  function activeSpace() { return spaces.find(space => space.id === spaceId()); }
  function projectUrl(space) {
    if (!workspaceUrl) return '';
    const url = new URL(workspaceUrl, location.origin);
    url.searchParams.set('space_id', space.id);
    return url.href;
  }
  function link(label, href, cls = '') {
    const result = node('a', cls, label); result.href = href; return result;
  }
  function agoraLink(space) {
    const url = projectUrl(space);
    if (!url) return null;
    const result = link(tr('Abrir ágora', 'Open Agora'), url, 'hub-primary-link');
    result.target = '_blank'; result.rel = 'noopener'; result.append(icon('arrow')); return result;
  }
  function go(next, {focus = false} = {}) {
    if (location.hash !== '#hub/' + next) location.hash = 'hub/' + next;
    else show(next);
    if (focus) { $('#hub-content').focus({preventScroll: true}); window.scrollTo({top: 0}); }
  }
  function selectProject(space) {
    const url = new URL(location.href);
    url.searchParams.set('space_id', space.id); url.hash = 'hub/project';
    history.pushState(null, '', url);
    window.HubFacets.ctx.bus.dispatchEvent(new CustomEvent('select-space', {detail: {space_id: space.id}}));
    show('project'); $('#hub-content').focus({preventScroll: true}); window.scrollTo({top: 0});
  }
  function navigation() {
    const nav = $('#hub-navigation'); nav.replaceChildren();
    const activeGroup = route === 'project' || route === 'agora' ? 'projects' : groups.some(g => g.id === route) ? route : groupFor(route).id;
    for (const group of groups) {
      const item = link(labels(group.label), '#hub/' + group.id, 'hub-nav-link');
      item.prepend(icon(group.id));
      if (activeGroup === group.id) item.setAttribute('aria-current', 'page');
      nav.append(item);
    }
    const search = link(tr('Buscar', 'Search'), '#hub/search', 'hub-nav-link hub-nav-search'); search.prepend(icon('search')); nav.append(search);
    $('#hub-sidebar-note').textContent = tr('Tu entorno local', 'Your local workspace');
    $('#hub-global-search span').textContent = tr('Buscar apps, mensajes y herramientas', 'Search apps, messages and tools');
    for (const n of document.querySelectorAll('[data-hub-copy]')) n.textContent = ({state:tr('Estado','State'),catalog_actions:tr('Gestionar catálogo','Manage catalog'),startup_groups:tr('Grupos de arranque','Startup groups')})[n.dataset.hubCopy] || n.textContent;
    $('#hub-connections-heading').textContent = tr('Conexiones y motores','Connections and engines');
    $('#hub-connections-description').textContent = tr('Servicios compartidos por todas las aplicaciones.','Services shared across every application.');
    const labelsByState = {all:tr('Todas las aplicaciones','All applications'),running:tr('En marcha','Running'),stopped:tr('Paradas','Stopped'),attention:tr('Con incidencias','Needs attention')};
    for (const option of $('#app-state-filter').options) option.textContent = labelsByState[option.value];
  }
  function contextTools() {
    const box = $('#hub-context-tools'); box.replaceChildren();
    const group = groups.find(g => g.facets?.includes(activeView())) || (facetButtons().some(b => b.dataset.tab === activeView()) ? groups[5] : null); box.hidden = !group;
    if (!group) return;
    const label = node('label', 'hub-view-label', tr('Vista de ' + labels(group.label).toLocaleLowerCase(), labels(group.label) + ' view'));
    const select = node('select', 'hub-view-select'); select.id = 'hub-view-select';
    const ids = [...group.facets, ...facetButtons().filter(b => !groups.some(g => g.facets?.includes(b.dataset.tab)) && b.dataset.tab !== 'agora').map(b => b.dataset.tab).filter(() => group.id === 'system')];
    for (const id of ids) {
      if (id !== 'resources' && !$('#family-tabs .tab[data-tab="' + id + '"]')) continue;
      const option = node('option', '', facetLabel(id)); option.value = id; select.append(option);
    }
    select.value = activeView(); select.onchange = () => go(select.value, {focus:true}); label.append(select); box.append(label);
    const description = node('p', 'hub-view-description', descriptions[activeView()] ? labels(descriptions[activeView()]) : tr('Herramientas compartidas entre tus aplicaciones.', 'Tools shared across your applications.'));
    box.append(description);
  }
  function projectRows(container, limit) {
    const list = node('div', 'hub-project-list');
    if (spacesError) {
      const error = node('div', 'hub-load-error'); error.setAttribute('role', 'alert');
      error.append(node('p', '', spacesError));
      const retry = node('button', '', tr('Reintentar', 'Retry')); retry.onclick = loadProjects;
      error.append(retry); list.append(error);
    } else if (!ready) {
      const loading = node('p', 'hub-loading', tr('Cargando proyectos…', 'Loading projects…')); loading.setAttribute('role', 'status'); list.append(loading);
    } else if (!spaces.length) {
      list.append(node('p', 'hub-empty', tr('No hay espacios de proyecto. Crea uno para organizar sus tareas y conversaciones.', 'No project spaces yet. Create one to organize its tasks and conversations.')));
      const create = node('button', '', tr('Crear espacio de proyecto', 'Create project space'));
      create.onclick = () => go('project'); list.append(create);
    }
    for (const space of (limit ? spaces.slice(0, limit) : spaces)) {
      const row = node('article', 'hub-project-row'); row.dataset.spaceId = space.id;
      const open = node('button', 'hub-project-open'); open.append(icon('projects'));
      const name = node('span', 'hub-project-name'); name.append(node('strong', '', space.title));
      name.append(node('small', '', space.workspace || tr('Espacio de proyecto', 'Project space'))); open.append(name);
      open.onclick = () => selectProject(space); row.append(open);
      const actions = node('div', 'hub-row-actions'); const agora = agoraLink(space); if (agora) actions.append(agora);
      const board = node('button', 'ghost', tr('Tareas y debates', 'Tasks and discussions')); board.onclick = () => selectProject(space); actions.append(board);
      row.append(actions); list.append(row);
    }
    container.append(list);
  }
  function appLaunch(app) {
    const button = node('button', 'hub-app-launch'); button.type = 'button'; button.disabled = app.state !== 'running' && !app.launchable; if (button.disabled) button.title = app.launch_reason || tr('No disponible','Unavailable');
    const image = node('img'); image.src = window.hubAppIcon ? window.hubAppIcon(app.id) : '/api/apps/' + encodeURIComponent(app.id) + '/icon'; image.alt = ''; image.width = image.height = 52;
    const text = node('span'); text.append(node('strong', '', app.name), node('small', '', app.purpose || app.id));
    const state = node('span', 'hub-launch-state', app.state === 'running' ? tr('Abrir', 'Open') : app.launchable ? tr('Iniciar y abrir', 'Start and open') : tr('No disponible','Unavailable'));
    button.append(image, text, state, icon('arrow')); button.onclick = () => window.hubOpenApplication?.(app.id); return button;
  }
  function home(container) {
    const layout = node('div', 'hub-workbench');
    const appArea = node('section', 'hub-home-apps');
    const head = node('div', 'hub-section-heading'); head.append(node('h2', '', tr('Aplicaciones', 'Applications')), link(tr('Ver todas', 'View all'), '#hub/apps')); appArea.append(head);
    const live = apps.filter(app => app.state === 'running');
    appArea.append(node('p', 'hub-section-description', live.length ? tr('En marcha · listas para continuar', 'Running · ready to continue') : tr('Elige una aplicación para empezar.', 'Choose an application to get started.')));
    const launches = node('div', 'hub-home-launches');
    for (const app of (live.length ? live : apps).slice(0, 5)) launches.append(appLaunch(app));
    if (!apps.length) launches.append(node('p', 'hub-empty', tr('El catálogo está cargando. Puedes abrir Aplicaciones para consultar el estado.', 'The catalog is loading. Open Applications to check its status.')));
    appArea.append(launches);
    if (live.length) {
      const others = apps.filter(a => a.state !== 'running').slice(0, 4);
      if (others.length) {
        const more = node('div', 'hub-home-more'); more.append(node('h3', '', tr('También disponibles', 'Also available')));
        const row = node('div', 'hub-app-dock');
        for (const app of others) { const b = appLaunch(app); row.append(b); } more.append(row); appArea.append(more);
      }
    }
    layout.append(appArea);
    const projects = node('section', 'hub-home-projects');
    const projectHead = node('div', 'hub-section-heading'); projectHead.append(node('h2', '', tr('Proyectos', 'Projects')), link(tr('Ver todos', 'View all'), '#hub/projects')); projects.append(projectHead);
    projects.append(node('p', 'hub-section-description', tr('Cada proyecto conserva su propia ágora.', 'Each project keeps its own Agora.'))); projectRows(projects, 3); layout.append(projects); container.append(layout);
    const work = node('section', 'hub-home-work'); const title = node('div', 'hub-section-heading'); title.append(node('h2', '', tr('Trabajo compartido', 'Shared work'))); work.append(title);
    const entries = [
      ['communication','Tu bandeja','Your inbox','Correo, avisos y conversaciones.','Mail, notifications and conversations.'],
      ['automation','Automatización','Automation','Trabajos en marcha, reglas y programaciones.','Running work, rules and schedules.'],
      ['system','Tu entorno','Your environment','Motores, GPU, servicios y mantenimiento.','Engines, GPUs, services and maintenance.'],
    ];
    const shortcuts = node('div', 'hub-home-shortcuts');
    for (const [id, esTitle, enTitle, esDesc, enDesc] of entries) { const item = link('', '#hub/' + id, 'hub-work-link'); item.append(icon(id)); const copy = node('span'); copy.append(node('strong','',tr(esTitle,enTitle)),node('small','',tr(esDesc,enDesc))); item.append(copy,icon('arrow')); shortcuts.append(item); }
    work.append(shortcuts); container.append(work);
  }
  function show(next) {
    if (!groups.some(group => group.id === next) && !['project', 'resources', 'agora'].includes(next) && !facetButtons().some(btn => btn.dataset.tab === next)) next = 'home';
    route = next;
    $('#hub-landing').hidden = true; $('#hub-resources').hidden = true; $('#hub-apps').hidden = true;
    $('#family-panel').hidden = true;
    const today = $('#facet-today'); if (today) today.hidden = route !== 'home';
    const group = groups.find(item => item.id === route);
    const project = route === 'project' || route === 'agora';
    const title = route === 'home' ? tr('Tu espacio de trabajo', 'Your workspace') : group ? labels(group.label) : project ? (activeSpace()?.title || spaceId()) : facetLabel(route);
    $('#hub-page-title').textContent = title;
    $('#hub-page-description').textContent = group ? labels(group.description) : project ? tr('Ágora de proyecto · tareas, debates y decisiones de este espacio.', 'Project Agora · tasks, discussions and decisions in this space.') : descriptions[route] ? labels(descriptions[route]) : '';
    const breadcrumb = $('#hub-breadcrumb'); breadcrumb.replaceChildren();
    const parent = project ? groups[1] : group ? null : groupFor(route);
    breadcrumb.hidden = !parent;
    if (parent) { breadcrumb.append(link(labels(parent.label), '#hub/' + parent.id), document.createTextNode(' / ' + title)); }
    const actions = $('#hub-page-actions'); actions.replaceChildren();
    const landing = $('#hub-landing'); landing.replaceChildren();
    if (route === 'home') { landing.hidden = false; home(landing); }
    else if (route === 'projects') {
      landing.hidden = false;
      const filter = node('input', 'hub-project-filter'); filter.type = 'search'; filter.placeholder = tr('Buscar proyecto…', 'Find a project…'); filter.setAttribute('aria-label', filter.placeholder);
      landing.append(filter); const results = node('div'); projectRows(results); landing.append(results);
      filter.oninput = () => {
        const query = filter.value.trim().toLocaleLowerCase(); let found = false;
        for (const row of results.querySelectorAll('.hub-project-row')) { row.hidden = !row.textContent.toLocaleLowerCase().includes(query); found ||= !row.hidden; }
        let empty = results.querySelector('.hub-no-match');
        if (!empty) { empty = node('p', 'hub-no-match', tr('No hay proyectos que coincidan con la búsqueda.', 'No projects match your search.')); results.append(empty); }
        empty.hidden = found || !query;
      };
      const create = node('button', 'ghost', tr('Gestionar espacios', 'Manage spaces')); create.onclick = () => go('project'); actions.append(create);
    } else if (route === 'apps') $('#hub-apps').hidden = false;
    else if (activeView() === 'resources') $('#hub-resources').hidden = false;
    else {
      $('#family-panel').hidden = false; $('#family-body').hidden = false;
      const id = project ? 'agora' : activeView();
      const button = $('#family-tabs .tab[data-tab="' + id + '"]');
      if (button) { selecting = true; button.click(); selecting = false; }
      if (project) {
        const space = activeSpace(); const open = space && agoraLink(space); if (open) actions.append(open);
        window.HubFacets.ctx.bus.dispatchEvent(new CustomEvent('select-space', {detail: {space_id: spaceId()}}));
      }
    }
    navigation(); contextTools();
    document.body.dataset.hubView = activeView();
    $('#hub-menu-toggle').textContent = tr('Menú', 'Menu');
    document.title = title + ' · Hoard Hub';
    $('#hub-navigation').classList.remove('is-open'); $('#hub-menu-toggle').setAttribute('aria-expanded', 'false');
  }
  function fromLocation() {
    const match = /^#hub\/([a-z_]+)$/.exec(location.hash);
    if (/^#agora(?:-\d+)?$/.test(location.hash)) show('project');
    else show(match?.[1] || 'home');
    (route === 'search' ? document.querySelector('.sr-q') : $('#hub-content'))?.focus({preventScroll: true}); window.scrollTo({top: 0});
  }
  async function loadProjects() {
    spacesError = '';
    try {
      const result = await window.HubFacets.ctx.api('/api/agora/spaces');
      if (!result?.ok) throw new Error('spaces');
      spaces = [...(result.spaces || [])].sort((a, b) => a.title.localeCompare(b.title)); ready = true;
    } catch (_) { spacesError = tr('No se han podido cargar los proyectos. Revisa la conexión con el Hub y reintenta.', 'Projects could not be loaded. Check the Hub connection and retry.'); }
    if (['home', 'projects', 'project', 'agora'].includes(route)) show(route);
  }
  function boot() {
    window.hubSearchDestinations = query => {
      const q = query.trim().toLocaleLowerCase(); if (!q) return [];
      return [...groups.map(g => ({title:labels(g.label),href:'#hub/' + g.id})),
        ...facetButtons().filter(b => b.dataset.tab !== 'agora').map(b => ({title:facetLabel(b.dataset.tab),href:'#hub/' + b.dataset.tab})),
        ...apps.map(a => ({title:a.name,description:a.purpose,app_id:a.id,href:'#hub/apps'}))]
        .filter(item => [item.title,item.description].join(' ').toLocaleLowerCase().includes(q)).slice(0,12);
    };
    window.hubNavigationView = () => ({section: activeView(), ...(route === 'project' || route === 'agora' ? {space_id: spaceId()} : {})});
    $('.hub-skip').onclick = event => { event.preventDefault(); $('#hub-content').focus(); $('#hub-content').scrollIntoView(); };
    const resourcePage = $('#hub-resources');
    for (const selector of ['#gpu-panel', '#strip', '#lane-panel']) resourcePage.append($(selector));
    const toolbar = $('#hub-app-toolbar');
    toolbar.prepend($('#search'));
    for (const selector of ['#btn-rescan', '#btn-start-all', '#btn-stop-all']) $('#hub-catalog-actions').append($(selector));
    $('#hub-global-search').onclick = () => { go('search'); if (route === 'search') document.querySelector('.sr-q')?.focus(); };
    const addResourceHeading = (root, title, description) => { const h = node('div', 'hub-section-heading'); const heading = node('h2', '', title); heading.id = 'hub-connections-heading'; h.append(heading); const p = node('p', 'hub-section-description', description); p.id = 'hub-connections-description'; root.prepend(h, p); };
    addResourceHeading($('#strip'), tr('Conexiones y motores', 'Connections and engines'), tr('Servicios compartidos por todas las aplicaciones.', 'Services shared across every application.'));
    $('#family-tabs').setAttribute('aria-label', tr('Herramientas', 'Tools'));
    $('#hub-menu-toggle').onclick = () => {
      const open = $('#hub-navigation').classList.toggle('is-open'); $('#hub-menu-toggle').setAttribute('aria-expanded', String(open));
    };
    window.addEventListener('hub:tab', event => {
      if (selecting || !event.detail.openPanel) return;
      const id = event.detail.name;
      if (id === 'agora' && /^#agora-\d+$/.test(location.hash)) show('project');
      else go(id === 'agora' ? 'project' : id);
    });
    window.addEventListener('hub:space', event => {
      // A space can also be selected from the preserved project tools.
      const url = new URL(location.href); url.searchParams.set('space_id', event.detail.space_id);
      history.replaceState(null, '', url);
      if (route === 'project' || route === 'agora') { loadProjects(); }
    });
    window.addEventListener('hub:spaces-changed', loadProjects);
    window.addEventListener('hub:apps', event => {
      apps = event.detail.apps || [];
      const signature = JSON.stringify(apps.map(app => [app.id, app.name, app.state]));
      if (signature !== appsSignature) { appsSignature = signature; if (route === 'home') show('home'); }
    });
    window.addEventListener('hashchange', fromLocation); window.addEventListener('popstate', fromLocation);
    $('#btn-lang').addEventListener('click', () => setTimeout(() => show(route), 0));
    window.HubFacets.ctx.api('/api/coordination/config').then(config => {
      workspaceUrl = config.workspace_url || '';
      if (workspaceUrl) {
        const faustus = link('Faustus', new URL('/', workspaceUrl).href, 'hub-faustus-link'); faustus.target = '_blank'; faustus.rel = 'noopener';
        $('.top-actions').prepend(faustus);
      }
      if (['home', 'projects', 'project'].includes(route)) show(route);
    }).catch(() => {});
    fromLocation(); loadProjects();
  }
  window.addEventListener('hub:facets-ready', boot, {once: true});
})();

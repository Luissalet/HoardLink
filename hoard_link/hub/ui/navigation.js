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
    {id: 'home', label: ['Inicio', 'Home'], description: ['Proyectos y aplicaciones en un solo punto de acceso.', 'One starting point for projects and apps.']},
    {id: 'projects', label: ['Proyectos', 'Projects'], description: ['Cada proyecto tiene su propia ágora, sus tareas y sus conversaciones.', 'Each project has its own Agora, tasks and conversations.']},
    {id: 'apps', label: ['Aplicaciones', 'Applications'], description: ['Abre una aplicación o gestiona sus grupos de arranque.', 'Open an app or manage startup groups.']},
    {id: 'communication', label: ['Comunicación', 'Communication'], facets: ['notify', 'mailgate', 'chats', 'refs', 'search'], description: ['Avisos, correo, chats y enlaces de la familia.', 'Family notifications, mail, chats and links.']},
    {id: 'automation', label: ['Automatización', 'Automation'], facets: ['work', 'rules', 'jobs', 'purchases'], description: ['Trabajos, reglas, tareas programadas y compras.', 'Work, rules, scheduled jobs and purchases.']},
    {id: 'system', label: ['Sistema', 'System'], facets: ['resources', 'services', 'capabilities', 'agent_sessions', 'repos', 'events', 'backups', 'audit', 'web'], description: ['Recursos, servidores y mantenimiento del entorno compartido.', 'Resources, servers and shared environment maintenance.']},
  ];
  const descriptions = {
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
      if (activeGroup === group.id && group.facets) {
        const subnav = node('div', 'hub-subnav');
        const ids = [...group.facets, ...facetButtons().filter(btn => !groups.some(g => g.facets?.includes(btn.dataset.tab)) && btn.dataset.tab !== 'agora').map(btn => btn.dataset.tab).filter(() => group.id === 'system')];
        for (const id of ids) {
          const source = $('#family-tabs .tab[data-tab="' + id + '"]');
          if (!source && id !== 'resources') continue;
          const sub = link(facetLabel(id), '#hub/' + id);
          if (route === id) sub.setAttribute('aria-current', 'page');
          subnav.append(sub);
        }
        nav.append(subnav);
      }
    }
    const search = link(tr('Buscar en el Hub', 'Search the Hub'), '#hub/search', 'hub-nav-link hub-nav-search');
    search.prepend(icon('search')); nav.append(search);
    $('#hub-sidebar-note').textContent = tr('Las ágoras pertenecen a sus proyectos. Los recursos se comparten.', 'Agoras belong to their projects. Resources are shared.');
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
      const row = node('article', 'hub-project-row');
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
  function home(container) {
    const layout = node('div', 'hub-home-layout');
    const projects = node('section');
    const head = node('div', 'hub-section-heading'); head.append(node('h3', '', tr('Tus proyectos', 'Your projects')));
    head.append(link(tr('Ver todos', 'View all'), '#hub/projects')); projects.append(head); projectRows(projects, 4);
    layout.append(projects);
    const running = node('section', 'hub-running'); running.append(node('h3', '', tr('Aplicaciones en marcha', 'Running applications')));
    const live = apps.filter(app => app.state === 'running' && app.catalog_visible !== false);
    if (!apps.length) running.append(node('p', 'hint', tr('Consulta el catálogo para ver el estado de las aplicaciones.', 'Open the catalog to see application status.')));
    else if (!live.length) running.append(node('p', 'hint', tr('No hay aplicaciones en marcha. Puedes abrirlas desde el catálogo.', 'No apps are running. Open one from the catalog.')));
    for (const app of live) {
      const row = node('div', 'hub-running-row'); row.append(node('strong', '', app.name));
      row.append(node('span', 'hub-live-label', tr('En marcha', 'Running'))); running.append(row);
    }
    running.append(link(tr('Ver aplicaciones', 'View applications'), '#hub/apps', 'hub-text-link')); layout.append(running); container.append(layout);
    const shortcuts = node('section', 'hub-shortcuts'); shortcuts.append(node('h3', '', tr('Herramientas compartidas', 'Shared tools')));
    for (const group of groups.slice(3)) {
      const item = link('', '#hub/' + group.id, 'hub-shortcut'); item.append(icon(group.id));
      const content = node('span'); content.append(node('strong', '', labels(group.label)), node('small', '', labels(group.description))); item.append(content, icon('arrow')); shortcuts.append(item);
    }
    container.append(shortcuts);
  }
  function directory(container, group) {
    const ids = facetButtons().map(btn => btn.dataset.tab).filter(id => id !== 'agora' && groupFor(id).id === group.id);
    if (group.id === 'system') ids.unshift('resources');
    const list = node('div', 'hub-tool-list');
    for (const id of ids) {
      const item = link('', '#hub/' + id, 'hub-tool-row'); const text = node('span');
      text.append(node('strong', '', facetLabel(id)));
      if (descriptions[id]) text.append(node('small', '', labels(descriptions[id])));
      item.append(text, icon('arrow')); list.append(item);
    }
    container.append(list);
  }
  function show(next) {
    if (!groups.some(group => group.id === next) && !['project', 'resources', 'agora'].includes(next) && !facetButtons().some(btn => btn.dataset.tab === next)) next = 'home';
    route = next;
    $('#hub-landing').hidden = true; $('#hub-resources').hidden = true; $('#hub-apps').hidden = true;
    $('#family-panel').hidden = true;
    const today = $('#facet-today'); if (today) today.hidden = route !== 'home';
    const group = groups.find(item => item.id === route);
    const project = route === 'project' || route === 'agora';
    const title = group ? labels(group.label) : project ? (activeSpace()?.title || spaceId()) : facetLabel(route);
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
    else if (route === 'resources') $('#hub-resources').hidden = false;
    else if (group) { landing.hidden = false; directory(landing, group); }
    else {
      $('#family-panel').hidden = false; $('#family-body').hidden = false;
      const id = project ? 'agora' : route;
      const button = $('#family-tabs .tab[data-tab="' + id + '"]');
      if (button) { selecting = true; button.click(); selecting = false; }
      if (project) {
        const space = activeSpace(); const open = space && agoraLink(space); if (open) actions.append(open);
        window.HubFacets.ctx.bus.dispatchEvent(new CustomEvent('select-space', {detail: {space_id: spaceId()}}));
      }
    }
    navigation();
    $('#hub-menu-toggle').textContent = tr('Menú', 'Menu');
    document.title = title + ' · Hoard Hub';
    $('#hub-navigation').classList.remove('is-open'); $('#hub-menu-toggle').setAttribute('aria-expanded', 'false');
  }
  function fromLocation() {
    const match = /^#hub\/([a-z_]+)$/.exec(location.hash);
    if (/^#agora(?:-\d+)?$/.test(location.hash)) show('project');
    else show(match?.[1] || 'home');
    $('#hub-content').focus({preventScroll: true}); window.scrollTo({top: 0});
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
    window.hubNavigationView = () => ({section: route, ...(route === 'project' || route === 'agora' ? {space_id: spaceId()} : {})});
    $('.hub-skip').onclick = event => { event.preventDefault(); $('#hub-content').focus(); $('#hub-content').scrollIntoView(); };
    const resourcePage = $('#hub-resources');
    for (const selector of ['#strip', '#gpu-panel', '#lane-panel']) resourcePage.append($(selector));
    const toolbar = $('#hub-app-toolbar');
    for (const selector of ['#search', '#btn-rescan', '#btn-start-all', '#btn-stop-all']) toolbar.append($(selector));
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

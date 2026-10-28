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
    {id: 'home', label: ['Inicio', 'Home'], description: ['Tus pendientes y el trabajo que quieres continuar.', 'Open an app, continue a project or check shared work.']},
    {id: 'projects', label: ['Proyectos', 'Projects'], description: ['Cada proyecto tiene su propia ágora, sus tareas y sus conversaciones.', 'Each project has its own Agora, tasks and conversations.']},
    {id: 'apps', label: ['Aplicaciones', 'Applications'], description: ['Abre una aplicación o gestiona sus grupos de arranque.', 'Open an app or manage startup groups.']},
    {id: 'communication', label: ['Bandeja', 'Inbox'], defaultView: 'notify', facets: ['notify', 'mailgate', 'chats', 'refs', 'search'], description: ['Avisos, correo, chats y enlaces de la familia.', 'Family notifications, mail, chats and links.']},
    {id: 'automation', label: ['Automatización', 'Automation'], facets: ['jobs', 'rules', 'work', 'purchases'], description: ['Trabajos, reglas, tareas programadas y compras.', 'Work, rules, scheduled jobs and purchases.']},
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
    const plain = {jobs:['Programaciones','Schedules'],rules:['Cuando ocurra algo','When something happens'],work:['Historial de ejecuciones','Run history'],resources:['Estado y conexiones','Status and connections'],agent_sessions:['Actividad de agentes','Agent activity'],refs:['Relaciones y enlaces','Relationships and links'],repos:['Código y repositorios','Code and repositories'],audit:['Diagnóstico','Diagnostics']};
    if (plain[id]) return labels(plain[id]);
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
    rememberProject(space.id); show('project'); $('#hub-content').focus({preventScroll: true}); window.scrollTo({top: 0});
  }
  function navigation() {
    const nav = $('#hub-navigation'); nav.replaceChildren();
    const activeGroup = route === 'search' ? 'search' : route === 'project' || route === 'agora' ? 'projects' : groups.some(g => g.id === route) ? route : groupFor(route).id;
    for (const group of groups) {
      const item = link(labels(group.label), '#hub/' + group.id, 'hub-nav-link');
      item.prepend(icon(group.id));
      if (activeGroup === group.id) item.setAttribute('aria-current', 'page');
      nav.append(item);
    }
    const search = link(tr('Buscar', 'Search'), '#hub/search', 'hub-nav-link hub-nav-search'); search.prepend(icon('search')); if (route === 'search') search.setAttribute('aria-current','page'); nav.append(search);
    $('#hub-sidebar-note').replaceChildren(link(tr('Ayuda y ajustes','Help and settings'),'#hub/help'));
    $('#hub-global-search span').textContent = tr('Buscar apps, mensajes y herramientas', 'Search apps, messages and tools');
    for (const n of document.querySelectorAll('[data-hub-copy]')) n.textContent = ({state:tr('Estado','State'),catalog_actions:tr('Gestionar catálogo','Manage catalog'),startup_groups:tr('Grupos de arranque','Startup groups')})[n.dataset.hubCopy] || n.textContent;
    $('#hub-connections-heading').textContent = tr('Conexiones y motores','Connections and engines');
    $('#hub-connections-description').textContent = tr('Servicios compartidos por todas las aplicaciones.','Services shared across every application.');
    const labelsByState = {all:tr('Todas las aplicaciones','All applications'),running:tr('En marcha','Running'),stopped:tr('Paradas','Stopped'),attention:tr('Con incidencias','Needs attention')};
    for (const option of $('#app-state-filter').options) option.textContent = labelsByState[option.value];
    const categories=window.HubDiscovery.categories(es());
    document.querySelectorAll('.hub-app-categories button').forEach((b,i)=>{b.textContent=categories[i][1];});
  }
  function contextTools() {
    const box = $('#hub-context-tools'); box.replaceChildren();
    if (route === 'project' || route === 'agora') {
      box.hidden=false; const nav=node('nav','hub-local-nav'); nav.setAttribute('aria-label',tr('Dentro del proyecto','Within the project'));
      for (const [id,title] of [['project',tr('Resumen','Overview')],['agora',tr('Ágora · tareas y conversaciones','Agora · tasks and conversations')]]) {
        const a=link(title,'#hub/'+id); if(route===id) a.setAttribute('aria-current','page'); nav.append(a);
      } box.append(nav); return;
    }
    const group = groups.find(g => g.id === route || g.facets?.includes(activeView())) || (facetButtons().some(b=>b.dataset.tab===activeView())?groups.find(g=>g.id==='system'):null);
    box.hidden=!group?.facets; if(box.hidden) return;
    const nav=node('nav','hub-local-nav'); nav.setAttribute('aria-label',tr('Herramientas de ','Tools in ')+labels(group.label));
    const ids=[...group.facets,...facetButtons().filter(b => !groups.some(g => g.facets?.includes(b.dataset.tab)) && b.dataset.tab!=='agora').map(b=>b.dataset.tab).filter(()=>group.id==='system')];
    for (const id of ids) {
      if(id!=='resources' && !$('#family-tabs .tab[data-tab="'+id+'"]')) continue;
      const a=link(facetLabel(id),'#hub/'+id); if(activeView()===id) a.setAttribute('aria-current','page'); nav.append(a);
    } box.append(nav);
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
      name.append(node('small', '', space.description || (recentIds().includes(space.id)?tr('Visitado recientemente','Recently visited'):tr('Ágora del proyecto','Project Agora')))); open.append(name);
      const c = space.counts || null;
      if (c) {
        const meta = node('span', 'hub-project-meta');
        const add = (n, text, cls) => { if (n) { const b = node('span', 'hub-count ' + cls); b.append(node('b', '', String(n)), document.createTextNode(' ' + text)); meta.append(b); } };
        add(c.waiting, tr('te esperan', 'waiting for you'), 'is-attention');
        add(c.open, tr('pendientes', 'open'), '');
        add(c.active, tr('en curso', 'in progress'), 'is-running');
        add(c.review, tr('en revisión', 'in review'), '');
        add(c.blocked, tr('bloqueadas', 'blocked'), 'is-attention');
        if (!c.open && !c.waiting) meta.append(node('span', 'hub-project-quiet', tr('Sin tareas pendientes', 'No open tasks')));
        if (space.last_activity) meta.append(node('span', 'hub-project-when', ago(space.last_activity)));
        name.append(meta);
      }
      open.onclick = () => selectProject(space); row.append(open);
      const actions = node('div', 'hub-row-actions');
      const board = node('button', 'hub-primary-link', tr('Continuar proyecto', 'Continue project')); board.onclick = () => selectProject(space); actions.append(board);
      row.append(actions); list.append(row);
    }
    container.append(list);
  }
  function ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 3600) return tr(`hace ${Math.max(1, Math.round(s / 60))} min`, `${Math.max(1, Math.round(s / 60))} min ago`);
    if (s < 86400) return tr(`hace ${Math.round(s / 3600)} h`, `${Math.round(s / 3600)} h ago`);
    return tr(`hace ${Math.round(s / 86400)} d`, `${Math.round(s / 86400)} d ago`);
  }
  // Projects where someone waits for you come first, then the most recently active.
  const byUrgency = (a, b) => ((b.counts?.waiting || 0) > 0) - ((a.counts?.waiting || 0) > 0) || (b.last_activity || 0) - (a.last_activity || 0) || a.title.localeCompare(b.title);
  function appLaunch(app) {
    const button = node('button', 'hub-app-launch'); button.type = 'button'; const unavailable=app.state !== 'running' && !app.launchable;
    const image = node('img'); image.src = window.hubAppIcon ? window.hubAppIcon(app.id) : '/api/apps/' + encodeURIComponent(app.id) + '/icon'; image.alt = ''; image.width = image.height = 52;
    image.onerror=()=>{if(!image.src.endsWith('/ui/fallback-icon.svg'))image.src='/ui/fallback-icon.svg';};
    const text = node('span'); const purpose = node('small', '', window.HubDiscovery.description(app, es())); purpose.title = app.purpose || app.id; text.append(node('strong', '', app.name), purpose);
    const state = node('span', 'hub-launch-state', app.state === 'running' ? tr('Abrir', 'Open') : app.launchable ? tr('Iniciar y abrir', 'Start and open') : tr('Ver disponibilidad','Check availability'));
    button.append(image, text, state, icon('arrow')); button.onclick = () => {if(unavailable){go('apps');window.hubFocusApplication?.(app.id);}else window.hubOpenApplication?.(app.id);}; return button;
  }
  let homeData=null, homeLoading=false, homeError=false, homeEpoch=0;
  const boards=new Map();
  function recentIds() { try { return JSON.parse(localStorage.getItem('hub.recent-projects') || '[]'); } catch (_) {return [];} }
  function rememberProject(id) {try {localStorage.setItem('hub.recent-projects',JSON.stringify([id,...recentIds().filter(x=>x!==id)].slice(0,8)));} catch (_) {}}
  function heading(parent,title,more,href) {const h=node('div','hub-section-heading');h.append(node('h2','',title));if(more)h.append(link(more,href));parent.append(h);}
  function rowLink(parent,title,description,href,cls='') {
    const a=link('',href,'hub-attention-row '+cls); const text=node('span');text.append(node('strong','',title));if(description)text.append(node('small','',description));a.append(text,icon('arrow'));parent.append(a);return a;
  }
  async function loadHome(force=false) {
    if(homeLoading || homeData && !force) return;
    homeLoading=true; homeError=false; const epoch=++homeEpoch;
    const sphere=document.documentElement.dataset.sphere || '';
    try {const [data,notices]=await Promise.all([window.HubFacets.ctx.api('/api/today'+(sphere?'?sphere='+encodeURIComponent(sphere):'')),window.HubFacets.ctx.api('/api/notify?unseen=1&limit=5'+(sphere?'&sphere='+encodeURIComponent(sphere):'')).catch(()=>null)]);
      if(epoch!==homeEpoch)return;if(!data?.ok)throw Error('today');homeData={...data,pendingNotices:notices?.notifications || [],noticesUnavailable:!notices?.ok};
    }catch(_){if(epoch===homeEpoch)homeError=true;}finally{if(epoch===homeEpoch){homeLoading=false;if(route==='home')show('home');}}
  }
  function home(container) {
    const overview=node('div','hub-home-status');container.append(overview);window.HubStatus?.overview(overview);
    const layout=node('div','hub-home-layout'); const attention=node('section','hub-attention');
    heading(attention,tr('Necesita tu atención','Needs your attention'),tr('Abrir bandeja','Open inbox'),'#hub/communication');
    if(!homeData) {
      attention.append(node('p','hub-empty',homeError?tr('No se ha podido consultar tu resumen. Puedes seguir entrando en tus proyectos y aplicaciones.','Your summary could not be loaded. You can still open projects and apps.'):tr('Consultando pendientes…','Checking pending items…')));
      if(homeError){const retry=node('button','ghost',tr('Reintentar','Retry'));retry.onclick=()=>loadHome(true);attention.append(retry);}
    } else {
      const items=[];const agenda=homeData.agenda || {};
      for(const x of homeData.pendingNotices || [])items.push([x.title,tr('Aviso sin revisar','Unread notification'),'#hub/notify']);
      for(const key of ['overdue','today'])for(const x of agenda[key] || [])items.push([x.title,tr(key==='overdue'?'Vencido · ver agenda':'Hoy · ver agenda',key==='overdue'?'Overdue · view agenda':'Today · view agenda'),'#hub/today']);
      for(const x of homeData.attention?.mail || [])items.push([x.subject || tr('Correo pendiente','Pending mail'),x.from_name || tr('Requiere revisión','Needs review'),'#hub/mailgate']);
      for(const x of homeData.attention?.chats || [])items.push([x.snippet || x.text || tr('Mensaje pendiente','Pending message'),tr('Conversación pendiente','Pending conversation'),'#hub/chats']);
      for(const x of homeData.system?.incidents || [])items.push([tr('Revisar ','Review ')+(apps.find(a=>a.id===x.app)?.name || x.app),tr('Hay una incidencia registrada. Consulta su estado.','An incident is recorded. Check its status.'),'#hub/audit']);
      if(!items.length)attention.append(node('p','hub-empty',tr('No hay pendientes en las fuentes consultadas.','No pending items in the checked sources.')));
      if(homeData.noticesUnavailable)attention.append(node('p','hint',tr('Los avisos no se han podido consultar. Abre la bandeja para reintentar.','Notifications could not be checked. Open your inbox to retry.')));
      for(const item of items.slice(0,5))rowLink(attention,...item);
      if(items.length>5)attention.append(link(tr('Ver el resto de pendientes','See remaining items'),'#hub/today','hub-text-link'));
      const foot=node('div','hub-section-foot');foot.append(link(tr('Agenda y resumen completo','Full agenda and digest'),'#hub/today'));const refresh=node('button','ghost',tr('Actualizar','Refresh'));refresh.onclick=()=>loadHome(true);foot.append(refresh);attention.append(foot);
    }
    layout.append(attention);const projects=node('section','hub-resume');
    heading(projects,tr('Continúa un proyecto','Continue a project'),tr('Ver todos','View all'),'#hub/projects');
    const ordered=[...spaces].sort((a,b)=>{const ids=recentIds();return (ids.includes(a.id)?ids.indexOf(a.id):999)-(ids.includes(b.id)?ids.indexOf(b.id):999);});
    const saved=spaces;spaces=ordered;projectRows(projects,3);spaces=saved;layout.append(projects);container.append(layout);
    const appArea=node('section','hub-home-apps');heading(appArea,'Hoards',tr('Gestionar aplicaciones','Manage applications'),'#hub/apps');
    const launches=node('div','hub-launch-grid');
    if(window.HubStatus)window.HubStatus.launcher(launches);else for(const app of apps)launches.append(appLaunch(app));
    appArea.append(launches);container.append(appArea);
    const footer=node('div','hub-home-footer');footer.append(link(tr('Programar una acción','Schedule an action'),'#hub/jobs'),link(tr('Estado del entorno','Environment status'),'#hub/system'),link(tr('Ayuda y ajustes','Help and settings'),'#hub/help'));container.append(footer);
    if(!homeData && !homeLoading && !homeError)loadHome();
  }
  function projectOverview(container) {
    const sid=spaceId(), space=activeSpace();
    const section=node('section','hub-project-overview');container.append(section);
    heading(section,tr('Dónde continuar','Where to continue'));
    rowLink(section,tr('Entrar en el ágora del proyecto','Enter this project’s Agora'),tr('Tareas, conversaciones, revisiones y decisiones de este proyecto.','This project’s tasks, conversations, reviews and decisions.'),'#hub/agora','hub-project-agora');
    const b=boards.get(sid);const recent=node('div','hub-project-recent');section.append(recent);
    if(b?.error){recent.append(node('p','hub-empty',tr('No se ha podido consultar la actividad del proyecto.','Project activity could not be loaded.')));const retry=node('button','ghost',tr('Reintentar','Retry'));retry.onclick=()=>{boards.delete(sid);show('project');};recent.append(retry);}
    else if(!b || b.loading) {recent.append(node('p','hub-loading',tr('Consultando el trabajo del proyecto…','Checking project work…')));loadProjectBoard(sid);}
    else {
      const pending=(b.tasks || []).filter(x=>!['done','dropped','cancelled'].includes(x.status));
      heading(recent,tr('Trabajo pendiente','Pending work'));
      if(!pending.length)recent.append(node('p','hub-empty',tr('Este proyecto no tiene tareas pendientes en su ágora.','This project has no pending Agora tasks.')));
      for(const task of pending.slice(0,5)){const a=rowLink(recent,task.title,task.owner?tr('Responsable: ','Owner: ')+task.owner:tr('Sin asignar','Unassigned'),'#hub/agora');a.onclick=event=>{event.preventDefault();go('agora');window.HubFacets.ctx.bus.dispatchEvent(new CustomEvent('open-project-task',{detail:{id:task.id}}));};}
      if(pending.length>5)recent.append(link(tr('Ver todas las tareas en el ágora','See all tasks in Agora'),'#hub/agora'));
      const threads=(b.threads || []).filter(x=>x.kind!=='task').slice(0,3);
      if(threads.length){const conversations=node('div','hub-project-conversations');heading(conversations,tr('Conversaciones del proyecto','Project conversations'));for(const thread of threads){const a=rowLink(conversations,thread.title,thread.last_author?tr('Último mensaje: ','Last message: ')+thread.last_author:'','#hub/agora');a.onclick=event=>{event.preventDefault();go('agora');window.HubFacets.ctx.bus.dispatchEvent(new CustomEvent('open-project-thread',{detail:{id:thread.id}}));};}recent.append(conversations);}
    }
    const details=node('details','hub-project-details');details.append(node('summary','',tr('Ubicación y ajustes del proyecto','Project location and settings')),node('p','',space?.workspace || tr('Sin carpeta asociada','No associated folder')));const manage=node('button','ghost',tr('Gestionar este espacio','Manage this space'));manage.onclick=()=>go('agora');details.append(manage);section.append(details);
  }
  async function loadProjectBoard(sid) {
    if(boards.has(sid))return;boards.set(sid,{loading:true});
    try {const b=await window.HubFacets.ctx.api('/api/agora/board?space_id='+encodeURIComponent(sid));if(!b?.ok)throw Error('board');boards.set(sid,b);}catch(_){boards.set(sid,{error:true});}
    if(route==='project' && spaceId()===sid)show('project');
  }
  function automationHome(container) {
    const section=node('section','hub-automation-home');container.append(section);
    heading(section,tr('¿Qué quieres automatizar?','What would you like to automate?'));
    rowLink(section,tr('Programar una acción','Schedule an action'),tr('Elige qué hacer y a qué hora o con qué frecuencia.','Choose an action and when or how often to run it.'),'#hub/jobs');
    rowLink(section,tr('Cuando ocurra algo','When something happens'),tr('Haz que una novedad de una aplicación active una acción.','Let an application event trigger an action.'),'#hub/rules');
    rowLink(section,tr('Consultar ejecuciones','Review runs'),tr('Comprueba lo que está en marcha y los resultados anteriores.','Check current work and previous results.'),'#hub/work');
    rowLink(section,tr('Compras y pedidos','Purchases and orders'),tr('Consulta el estado y seguimiento de tus compras.','Track the status of your purchases.'),'#hub/purchases');
  }
  function help(container) {
    const section=node('section','hub-help');container.append(section);
    for(const [title,desc,href] of [[tr('Retomar un proyecto','Resume a project'),tr('Entra en Proyectos. Cada uno tiene su resumen y su propia ágora.','Open Projects. Each has an overview and its own Agora.'),'#hub/projects'],[tr('Encontrar una herramienta','Find a tool'),tr('Busca lo que quieres hacer, por ejemplo recetas, escribir una novela o analizar datos.','Search what you want to do, such as recipes, write a novel or analyze data.'),'#hub/apps'],[tr('Conexiones y modelos','Connections and models'),tr('Consulta qué servicios responden. Las aplicaciones paradas se inician cuando tú lo eliges.','Check which services respond. Stopped applications start when you choose.'),'#hub/system'],[tr('Avisos y notificaciones','Alerts and notifications'),tr('En Bandeja puedes consultar los avisos y configurar cómo recibirlos.','Inbox lets you read alerts and configure delivery.'),'#hub/notify'],[tr('Agenda, calendario y resúmenes','Agenda, calendar and digests'),tr('Consulta fechas, suscripción al calendario y opciones de resumen.','View dates, calendar subscription and digest options.'),'#hub/today']])rowLink(section,title,desc,href);
    section.append(node('p','hub-empty',tr('Ctrl + K abre la búsqueda. El selector Personal de la cabecera cambia la esfera; ES / EN cambia el idioma.','Ctrl + K opens search. The Personal selector changes sphere; ES / EN changes language.')));
  }
  function show(next) {
    if(next==='home' && route!=='home'){homeData=null;homeError=false;if(homeLoading){homeEpoch++;homeLoading=false;}}
    if(next==='project' && route==='agora')boards.delete(spaceId());
    if (!groups.some(group => group.id === next) && !['project', 'resources', 'agora', 'help', 'today'].includes(next) && !facetButtons().some(btn => btn.dataset.tab === next)) next = 'home';
    route = next;
    $('#hub-landing').hidden = true; $('#hub-resources').hidden = true; $('#hub-apps').hidden = true;
    $('#family-panel').hidden = true;
    const today = $('#facet-today'); if (today) today.hidden = route !== 'today';
    const group = groups.find(item => item.id === route);
    const project = route === 'project' || route === 'agora';
    const title = route === 'home' ? tr('Inicio', 'Home') : route === 'help' ? tr('Ayuda y ajustes', 'Help and settings') : route === 'today' ? tr('Agenda y resumen', 'Agenda and digest') : group ? labels(group.label) : project ? (activeSpace()?.title || spaceId()) : facetLabel(route);
    $('#hub-page-title').textContent = title;
    $('#hub-page-description').textContent = group ? labels(group.description) : project ? tr('Tu trabajo, conversaciones y decisiones en un mismo proyecto.', 'Project Agora · tasks, discussions and decisions in this space.') : descriptions[route] ? labels(descriptions[route]) : '';
    const breadcrumb = $('#hub-breadcrumb'); breadcrumb.replaceChildren();
    const parent = project ? groups[1] : group || ['search','help','today'].includes(route) ? null : groupFor(route);
    breadcrumb.hidden = !parent;
    if (parent) { breadcrumb.append(link(labels(parent.label), '#hub/' + parent.id), document.createTextNode(' / ' + title)); }
    const actions = $('#hub-page-actions'); actions.replaceChildren();
    const landing = $('#hub-landing'); landing.replaceChildren();
    if (route === 'home') { landing.hidden = false; home(landing); }
    else if(route==='help'){landing.hidden=false;help(landing);}
    else if(route==='today'){}
    else if(route==='automation'){landing.hidden=false;automationHome(landing);}
    else if(route==='project'){landing.hidden=false;projectOverview(landing);}
    else if (route === 'projects') {
      landing.hidden = false;
      const filter = node('input', 'hub-project-filter'); filter.type = 'search'; filter.placeholder = tr('Buscar proyecto…', 'Find a project…'); filter.setAttribute('aria-label', filter.placeholder);
      landing.append(filter); const results = node('div', 'hub-project-directory'); const saved = spaces; spaces = [...spaces].sort(byUrgency); projectRows(results); spaces = saved; landing.append(results);
      filter.oninput = () => {
        const query = filter.value.trim().toLocaleLowerCase(); let found = false;
        for (const row of results.querySelectorAll('.hub-project-row')) { row.hidden = !row.textContent.toLocaleLowerCase().includes(query); found ||= !row.hidden; }
        let empty = results.querySelector('.hub-no-match');
        if (!empty) { empty = node('p', 'hub-no-match', tr('No hay proyectos que coincidan con la búsqueda.', 'No projects match your search.')); results.append(empty); }
        empty.hidden = found || !query;
      };
      const create = node('button', 'hub-primary-link', tr('Nuevo proyecto', 'New project')); create.onclick = () => {go('agora');window.HubFacets.ctx.bus.dispatchEvent(new Event('create-project-space'));}; actions.append(create);
    } else if (route === 'apps') $('#hub-apps').hidden = false;
    else if (activeView() === 'resources') $('#hub-resources').hidden = false;
    else {
      $('#family-panel').hidden = false; $('#family-body').hidden = false;
      const id = project ? 'agora' : activeView();
      const button = $('#family-tabs .tab[data-tab="' + id + '"]');
      if (button) { selecting = true; button.click(); selecting = false; }
      if (project) {
        const space = activeSpace(); const open = space && agoraLink(space); if (open) {open.textContent=tr('Abrir conversación completa','Open full conversation');actions.append(open);}
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
    if (/^#agora(?:-\d+)?$/.test(location.hash)) show('agora');
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
    apps=window.hubApplications?.() || [];
    window.hubSearchDestinations = query => {
      const q=query.trim().toLocaleLowerCase();if(!q)return [];
      return [...apps.filter(a=>window.HubDiscovery.matches(a,q)).map(a=>({title:a.name,description:window.HubDiscovery.description(a,es()),app_id:a.id,href:'#hub/apps'})),
        ...spaces.filter(s=>[s.title,s.description].join(' ').toLocaleLowerCase().includes(q)).map(s=>({title:s.title,description:tr('Proyecto','Project'),href:'?space_id='+encodeURIComponent(s.id)+'#hub/project'})),
        ...[...groups.map(g=>({title:labels(g.label),href:'#hub/'+g.id})),...facetButtons().filter(b=>b.dataset.tab!=='agora').map(b=>({title:facetLabel(b.dataset.tab),href:'#hub/'+b.dataset.tab}))].filter(x=>x.title.toLocaleLowerCase().includes(q))].slice(0,12);
    };
    window.hubNavigationView = () => ({section: activeView(), ...(route === 'project' || route === 'agora' ? {space_id: spaceId()} : {})});
    $('.hub-skip').onclick = event => { event.preventDefault(); $('#hub-content').focus(); $('#hub-content').scrollIntoView(); };
    const resourcePage = $('#hub-resources');
    for (const selector of ['#strip', '#gpu-panel', '#lane-panel']) resourcePage.append($(selector));
    const systemIntro=node('div','hub-system-intro');systemIntro.append(node('h2','',tr('Faustus y Hoards','Faustus and Hoards')),node('p','',tr('Una aplicación parada está disponible bajo demanda. Un servicio sin estado confirmado necesita comprobarse; no significa que haya fallado.','A stopped app is available on demand. An unconfirmed service status needs checking; it does not mean failure.')));
    const statusOverview=node('div','hub-home-status hub-system-overview');const statusTable=node('section','hub-hoards-status');statusTable.id='hub-hoards-status';
    resourcePage.prepend(systemIntro,statusOverview,statusTable);window.HubStatus?.overview(statusOverview);window.HubStatus?.table(statusTable);
    const categories=node('nav','hub-app-categories');categories.setAttribute('aria-label',tr('Aplicaciones por uso','Applications by purpose'));
    for(const [id,name] of window.HubDiscovery.categories(es())){const b=node('button','ghost',name);b.setAttribute('aria-pressed',String(id==='todas'));b.onclick=()=>{for(const x of categories.children)x.setAttribute('aria-pressed',String(x===b));window.hubFilterCategory(id);};categories.append(b);}$('#hub-apps').prepend(categories);
    const toolbar = $('#hub-app-toolbar');
    toolbar.prepend($('#search'));
    const appTable=node('section','hub-apps-table');appTable.hidden=true;$('#grid').after(appTable);
    const viewSeg=node('div','hub-seg hub-view-toggle');viewSeg.setAttribute('role','group');
    const savedView=()=>{try{return localStorage.getItem('hub.apps.view')||'tiles';}catch(_){return 'tiles';}};
    const applyView=()=>{const v=savedView();$('#grid').hidden=v==='table';appTable.hidden=v!=='table';for(const b of viewSeg.children){b.setAttribute('aria-pressed',String(b.dataset.view===v));b.textContent=b.dataset.view==='table'?tr('Tabla','Table'):tr('Mosaico','Tiles');}if(v==='table'&&!appTable.dataset.hubStatus)window.HubStatus?.table(appTable);};
    for(const v of ['tiles','table']){const b=node('button','');b.type='button';b.dataset.view=v;b.onclick=()=>{try{localStorage.setItem('hub.apps.view',v);}catch(_){}applyView();};viewSeg.append(b);}
    toolbar.append(viewSeg);applyView();$('#btn-lang').addEventListener('click',()=>setTimeout(applyView,0));
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
      if (id === 'agora' && /^#agora-\d+$/.test(location.hash)) show('agora');
      else go(id);
    });
    window.addEventListener('hub:space', event => {
      // A space can also be selected from the preserved project tools.
      const url = new URL(location.href); url.searchParams.set('space_id', event.detail.space_id);
      history.replaceState(null, '', url);
      if (route === 'project' || route === 'agora') { loadProjects(); }
    });
    window.addEventListener('hub:spaces-changed', loadProjects);
    window.HubFacets.ctx.bus.addEventListener('sphere',()=>{homeEpoch++;homeData=null;homeLoading=false;homeError=false;if(route==='home')show('home');});
    window.addEventListener('hub:apps', event => {
      apps = event.detail.apps || [];
      const signature = JSON.stringify(apps.map(app => [app.id, app.name]));
      if (signature !== appsSignature) { appsSignature = signature; if (route === 'home') show('home'); }
    });
    window.addEventListener('hashchange', fromLocation); window.addEventListener('popstate', fromLocation);
    $('#btn-lang').addEventListener('click', () => setTimeout(() => show(route), 0));
    window.HubFacets.ctx.api('/api/coordination/config').then(config => {
      workspaceUrl = config.workspace_url || '';
      if (['home', 'projects', 'project'].includes(route)) show(route);
    }).catch(() => {});
    fromLocation(); loadProjects();
  }
  window.addEventListener('hub:facets-ready', boot, {once: true});
})();

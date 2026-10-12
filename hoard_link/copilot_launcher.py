"""The Faustus copilot launcher every Hoard page can load: ``/api/copilot/launcher.js`` (see ``copilot.install_copilot``).

Plain browser JavaScript, no framework and no build: a floating «Faustus» button and a side panel with the shared
``<faustus-chat>`` element that Faustus serves (``/api/copilot/panel.js``). Every request of the panel goes through the
app's own relay (``/api/copilot/proxy``), so tokens never reach the page.

What the panel learns about the page (``contextProvider``): the app (``origin``), the view (path, title, first heading,
language), the text selected, and the objects open in the view, guessed from the route: pairs ``/<kind>/<id>`` where the
id looks like an id (digits, a ULID, a UUID); ``new`` is never an object. An app adds or overrides any of it by defining
``window.hoardCopilot = { context: async () => ({ project, active_objects, ... }), onTurnEnd() {...} }``; returning
``active_objects: []`` drops the guesses from the URL.

Where the button sits: bottom right by default; an app whose own controls live there moves it with the CSS variables
``--hoard-copilot-bottom`` / ``--hoard-copilot-right`` (or ``--hoard-copilot-left`` with
``<html data-hoard-copilot-position="left">``).

Keys typed in the panel stay in the panel (the app's shortcuts never see them). The conversation is kept per data space
(localStorage, best effort; the route of each turn travels in the context) and, when the app's context names a project
(``project.external_project_id`` / ``id``), per project: switching project in the app shows that project's conversation
(or a fresh one) instead of mixing two projects in one thread. The launcher checks on every navigation, when the panel
opens, when the page dispatches ``hoard:copilot-context-changed`` and, last, right before each turn. When a turn ends
the page gets ``hoard:copilot-turn-end`` and ``onTurnEnd``.
"""

import json

FAUSTUS_BRAND = "#e06c75"
FAUSTUS_MARK_SVG = ('<svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true">'
    '<path fill="currentColor" d="M16 0.738L4.674 25.559L13.004 28.992L13.004 24.798L9.229 24.798L9.229 19.644L16 12.094L22.772 19.644L22.772 24.798L17.678 24.798L13.4 31.264L27.326 25.559Z"/>'
    '<g fill="currentColor" opacity="0.62">'
    '<path d="M5.993 5.022L0.839 13.292L6.233 18.985L9.049 13.172L4.555 11.434Z"/>'
    '<path d="M26.008 5.022L31.161 13.292L25.768 18.985L22.952 13.172L27.446 11.434Z"/>'
    '</g></svg>')

LAUNCHER_JS = r"""
(() => {
  if (window.__hoardCopilotLauncher) return;
  window.__hoardCopilotLauncher = true;
  const ID_RE = /^(?:\d+|[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Za-z]+_[0-9A-Za-z]{8,}|[0-9a-f]{12,})$/;
  const SKIP = new Set(['new', 'edit', 'create', 'add', 'api', 'static', 'assets']);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch { /* private mode */ } },
  };
  const lang = () => (document.documentElement.lang || navigator.language || 'en').slice(0, 2).toLowerCase();
  const routeOf = () => (location.hash && location.hash.length > 1 ? location.hash.slice(1) : location.pathname) || '/';

  function guessObjects(route) {
    const parts = route.split('?')[0].split('/').filter(Boolean).map(decodeURIComponent);
    const found = [];
    for (let i = 0; i < parts.length; i += 1) {
      const id = parts[i + 1];
      if (!id || SKIP.has(id.toLowerCase()) || !ID_RE.test(id)) continue;
      const kind = parts[i].replace(/s$/, '');
      if (!/^[A-Za-z][\w-]*$/.test(kind) || SKIP.has(kind)) continue;
      found.push({ kind, object_id: id });
      i += 1;
    }
    return found;
  }

  const TURN_WAIT_S = 600;

  function relayFetch(input, init = {}) {
    const request = new Request(input, init);
    return (async () => {
      const url = new URL(request.url);
      const envelope = { path: url.pathname + url.search, method: request.method, headers: {} };
      for (const name of ['X-Faustus-Client-Version', 'X-Odysseus-Run-Id']) {
        const value = request.headers.get(name);
        if (value) envelope.headers[name] = value;
      }
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        const bytes = new Uint8Array(await request.arrayBuffer());
        const type = request.headers.get('Content-Type') || '';
        if (type.startsWith('application/json')) {
          envelope.body = JSON.parse(new TextDecoder().decode(bytes) || 'null');
          // Someone typing in an app waits for the answer: a turn queued behind another agent on the shared model
          // lane keeps its place (the panel shows the queue) instead of failing at the endpoint's short default.
          if (url.pathname === '/api/chat_stream' && envelope.body && typeof envelope.body === 'object'
              && envelope.body.initial_response_timeout_s == null) envelope.body.initial_response_timeout_s = TURN_WAIT_S;
        }
        else {
          let binary = '';
          for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
          envelope.content_type = type;
          envelope.raw_body = btoa(binary);
        }
      }
      return fetch('/api/copilot/proxy', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(envelope), signal: request.signal });
    })();
  }

  const css = `
    .hoard-copilot-toggle{position:fixed;right:var(--hoard-copilot-right,18px);bottom:var(--hoard-copilot-bottom,18px);z-index:2147483000;font:600 13px/1 system-ui,sans-serif;
      display:grid;place-items:center;width:48px;height:48px;padding:0;border-radius:50%;border:1px solid rgba(127,127,127,.45);background:#1f1b2e;color:__FAUSTUS_BRAND__;
      box-shadow:0 6px 18px rgba(0,0,0,.35);cursor:pointer}
    .hoard-copilot-toggle:focus-visible{outline:2px solid __FAUSTUS_BRAND__;outline-offset:2px}
    html[data-hoard-copilot-position=left] .hoard-copilot-toggle{right:auto;left:var(--hoard-copilot-left,18px)}
    .hoard-copilot{position:fixed;top:14px;right:14px;bottom:calc(var(--hoard-copilot-bottom,18px) + 48px);width:min(430px,calc(100vw - 28px));z-index:2147483001;
      display:flex;flex-direction:column;background:#17141f;color:#f4f1ff;border:1px solid rgba(127,127,127,.4);
      border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.45);font:14px/1.4 system-ui,sans-serif}
    .hoard-copilot[hidden]{display:none}
    .hoard-copilot header{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;
      border-bottom:1px solid rgba(127,127,127,.25)}
    .hoard-copilot header button{background:transparent;border:0;color:inherit;font-size:20px;line-height:1;cursor:pointer;padding:2px 6px}
    .hoard-copilot .hoard-copilot-body{flex:1;min-height:0;display:flex}
    .hoard-copilot faustus-chat{flex:1;min-height:0;border:0;border-radius:0 0 12px 12px}
    .hoard-copilot [role=alert]{margin:10px 12px;color:#ffb4a8}
    @media (max-width:600px){.hoard-copilot{top:6px;right:6px;bottom:60px;width:calc(100vw - 12px)}}`;

  let info = null;
  let aside = null;
  let panel = null;
  let currentKey = null;          // localStorage key of the conversation on screen: data space (+ the app's project)
  let busy = false;               // a turn is running: a project switch waits for its end
  let lastAsk = 0;                // when the panel last asked for the context (a send is starting)
  let refreshAfterTurn = false;   // the conversation changed under a send: show its history when the turn ends

  function stopKeys(node) {
    for (const type of ['keydown', 'keyup', 'keypress']) node.addEventListener(type, (event) => event.stopPropagation());
  }

  async function appContext() {
    const route = routeOf();
    const selected = String(window.getSelection?.() || '').trim();
    const base = {
      origin: { app_id: info.app_id, app_name: info.app_name, data_space_id: info.data_space_id,
                surface: 'page', tool_server_id: info.tool_server_id },
      view: { route, title: document.title, heading: (document.querySelector('h1')?.textContent || '').trim().slice(0, 200),
              locale: lang() },
      active_objects: guessObjects(route),
      selection: selected ? { kind: 'text', text: selected.slice(0, 4000) } : null,
    };
    const hook = window.hoardCopilot && window.hoardCopilot.context;
    if (typeof hook !== 'function') return base;
    try {
      const extra = (await hook(base)) || {};
      const merged = { ...base, ...extra, origin: base.origin, view: { ...base.view, ...(extra.view || {}) } };
      if (merged.project == null) delete merged.project;   // a view without a project omits it
      return merged;
    } catch (error) {
      return { ...base, context_error: String(error && error.message || error) };
    }
  }

  function projectKey(ctx) {
    const project = ctx && ctx.project;
    if (!project || typeof project !== 'object') return '';
    const id = project.external_project_id ?? project.id ?? project.project_link_id ?? project.project_id ?? project.title;
    return id == null || id === '' ? '' : String(id).slice(0, 200);
  }

  function keyFor(ctx) {
    const project = projectKey(ctx);
    return 'hoard-copilot:' + info.data_space_id + (project ? ':project:' + project : '');
  }

  // A conversation saved against another Faustus (or deleted there) would leave the panel stuck on a 404: check it and
  // start fresh when it is gone. Network failures keep the saved id so a restart does not lose the thread.
  async function savedSession(key) {
    const saved = store.get(key);
    if (!saved) return null;
    let gone = false;
    try { gone = (await relayFetch(window.location.origin + '/api/hoard-copilot/conversations/' + encodeURIComponent(saved))).status === 404; }
    catch { gone = false; }
    if (gone) { store.set(key, null); return null; }
    return saved;
  }

  // What the panel sends with each turn. It asks right before picking the conversation, so a project change nothing
  // announced still sends this turn to that project's conversation (its history is shown when the turn ends).
  async function context() {
    lastAsk = Date.now();
    const ctx = await appContext();
    const key = keyFor(ctx);
    if (panel && key !== currentKey) {
      currentKey = key;
      panel.session = await savedSession(key);
      refreshAfterTurn = true;
    }
    return ctx;
  }

  function buildPanel(sessionId) {
    const element = document.createElement('faustus-chat');
    element.server = window.location.origin;
    element.locale = lang();
    element.theme = 'dark';
    element.fetch = relayFetch;
    element.onOpenSettings = () => window.open(info.settings_url, '_blank', 'noopener');
    element.contextProvider = context;
    if (sessionId) element.session = sessionId;
    element.addEventListener('faustus:session-change', (event) => {
      if (currentKey) store.set(currentKey, event.detail && event.detail.sessionId);
    });
    element.addEventListener('faustus:turn-start', () => { busy = true; });
    element.addEventListener('faustus:turn-end', (event) => {
      busy = false;
      window.dispatchEvent(new CustomEvent('hoard:copilot-turn-end', { detail: event.detail || {} }));
      try { window.hoardCopilot && typeof window.hoardCopilot.onTurnEnd === 'function' && window.hoardCopilot.onTurnEnd(event.detail || {}); }
      catch { /* the app's refresh must never break the panel */ }
      if (refreshAfterTurn) {
        refreshAfterTurn = false;
        if (typeof element.loadHistory === 'function') element.loadHistory().catch(() => {});
      }
      schedule();   // a navigation during the turn waited for its end
    });
    return element;
  }

  async function mount(body, title) {
    const response = await fetch('/api/copilot/info');
    if (!response.ok) throw new Error((await response.text()) || ('HTTP ' + response.status));
    info = await response.json();
    title.textContent = 'Faustus · ' + (info.app_name || info.app_id);
    await import(info.panel_url);
    // One conversation per app data space (and per project when the app names one): the route of every turn travels
    // in the context, so moving around the app keeps the thread instead of scattering it over one chat per page.
    currentKey = keyFor(await appContext());
    panel = buildPanel(await savedSession(currentKey));
    body.replaceChildren(panel);
    if (typeof panel.loadHistory === 'function') await panel.loadHistory();
  }

  // A different project in the app is a different conversation: put that one (or a fresh one) on screen. Never in the
  // middle of a turn or of a send that is asking for its context.
  async function checkProject() {
    if (!panel || !info || !aside || aside.hidden || busy || Date.now() - lastAsk < 3000) return;
    const key = keyFor(await appContext());
    if (key === currentKey || busy || Date.now() - lastAsk < 3000) return;
    currentKey = key;
    const next = buildPanel(await savedSession(key));
    panel.replaceWith(next);
    panel = next;
    if (typeof next.loadHistory === 'function') await next.loadHistory();
  }

  let checkTimer = 0;
  function schedule() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(() => { checkProject().catch(() => {}); }, 250);
  }

  function open(toggle) {
    if (aside) { aside.hidden = false; toggle.setAttribute('aria-expanded', 'true'); schedule(); return; }
    aside = document.createElement('aside');
    aside.className = 'hoard-copilot';
    aside.setAttribute('aria-label', 'Faustus');
    const header = document.createElement('header');
    const title = document.createElement('strong');
    title.textContent = 'Faustus';
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.setAttribute('aria-label', lang() === 'es' ? 'Cerrar Faustus' : 'Close Faustus');
    close.addEventListener('click', () => { aside.hidden = true; toggle.setAttribute('aria-expanded', 'false'); toggle.focus(); });
    header.append(title, close);
    const body = document.createElement('div');
    body.className = 'hoard-copilot-body';
    aside.append(header, body);
    stopKeys(aside);
    document.body.appendChild(aside);
    toggle.setAttribute('aria-expanded', 'true');
    mount(body, title).catch((error) => {
      const alert = document.createElement('p');
      alert.setAttribute('role', 'alert');
      alert.textContent = String(error && error.message || error);
      body.replaceChildren(alert);
    });
  }

  function start() {
    if (document.querySelector('.hoard-copilot-toggle, faustus-chat')) return;
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'hoard-copilot-toggle';
    toggle.innerHTML = __FAUSTUS_MARK_SVG__;
    toggle.setAttribute('aria-label', 'Faustus');
    toggle.title = 'Faustus';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => (aside && !aside.hidden ? (aside.hidden = true, toggle.setAttribute('aria-expanded', 'false')) : open(toggle)));
    document.body.appendChild(toggle);
    for (const name of ['pushState', 'replaceState']) {
      const original = history[name];
      if (typeof original !== 'function') continue;
      history[name] = function (...args) { const out = original.apply(this, args); schedule(); return out; };
    }
    window.addEventListener('popstate', schedule);
    window.addEventListener('hashchange', schedule);
    window.addEventListener('hoard:copilot-context-changed', schedule);
  }

  window.hoardCopilotGuessObjects = guessObjects;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
""".replace('__FAUSTUS_MARK_SVG__', json.dumps(FAUSTUS_MARK_SVG)).replace('__FAUSTUS_BRAND__', FAUSTUS_BRAND)

"""The Faustus copilot launcher every Hoard page can load: ``/api/copilot/launcher.js`` (see ``copilot.install_copilot``).

Plain browser JavaScript, no framework and no build: a floating «Faustus» button and a side panel with the shared
``<faustus-chat>`` element that Faustus serves (``/api/copilot/panel.js``). Every request of the panel goes through the
app's own relay (``/api/copilot/proxy``), so tokens never reach the page.

What the panel learns about the page (``contextProvider``): the app (``origin``), the view (path, title, first heading,
language), the text selected, and the objects open in the view, guessed from the route: pairs ``/<kind>/<id>`` where the
id looks like an id (digits, a ULID, a UUID); ``new`` is never an object. An app adds or overrides any of it by defining
``window.hoardCopilot = { context: async () => ({ project, active_objects, ... }), onTurnEnd() {...} }``.

Keys typed in the panel stay in the panel (the app's shortcuts never see them). The conversation is kept per data space
(localStorage, best effort; the route of each turn travels in the context). When a turn ends the page gets ``hoard:copilot-turn-end`` and ``onTurnEnd``.
"""

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
    .hoard-copilot-toggle{position:fixed;right:18px;bottom:18px;z-index:2147483000;font:600 13px/1 system-ui,sans-serif;
      padding:10px 14px;border-radius:999px;border:1px solid rgba(127,127,127,.45);background:#1f1b2e;color:#f4f1ff;
      box-shadow:0 6px 18px rgba(0,0,0,.35);cursor:pointer}
    .hoard-copilot-toggle:focus-visible{outline:2px solid #9b87f5;outline-offset:2px}
    .hoard-copilot{position:fixed;top:14px;right:14px;bottom:66px;width:min(430px,calc(100vw - 28px));z-index:2147483001;
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
  let sessionKey = null;

  function stopKeys(node) {
    for (const type of ['keydown', 'keyup', 'keypress']) node.addEventListener(type, (event) => event.stopPropagation());
  }

  async function context() {
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
      return { ...base, ...extra, origin: base.origin, view: { ...base.view, ...(extra.view || {}) } };
    } catch (error) {
      return { ...base, context_error: String(error && error.message || error) };
    }
  }

  async function mount(body, title) {
    const response = await fetch('/api/copilot/info');
    if (!response.ok) throw new Error((await response.text()) || ('HTTP ' + response.status));
    info = await response.json();
    title.textContent = 'Faustus · ' + (info.app_name || info.app_id);
    await import(info.panel_url);
    panel = document.createElement('faustus-chat');
    panel.server = window.location.origin;
    panel.locale = lang();
    panel.theme = 'dark';
    panel.fetch = relayFetch;
    panel.onOpenSettings = () => window.open(info.settings_url, '_blank', 'noopener');
    panel.contextProvider = context;
    // One conversation per app data space: the route of every turn travels in the context, so moving around the app
    // keeps the thread instead of scattering it over one chat per page.
    sessionKey = 'hoard-copilot:' + info.data_space_id;
    const saved = store.get(sessionKey);
    // A conversation saved against another Faustus (or deleted there) would leave the panel stuck on a 404: check it
    // first and start fresh when it is gone. Network failures keep the saved id so a restart does not lose the thread.
    if (saved) {
      let gone = false;
      try { gone = (await relayFetch(window.location.origin + '/api/hoard-copilot/conversations/' + encodeURIComponent(saved))).status === 404; }
      catch { gone = false; }
      if (gone) store.set(sessionKey, null);
      else panel.session = saved;
    }
    panel.addEventListener('faustus:session-change', (event) => store.set(sessionKey, event.detail && event.detail.sessionId));
    panel.addEventListener('faustus:turn-end', (event) => {
      window.dispatchEvent(new CustomEvent('hoard:copilot-turn-end', { detail: event.detail || {} }));
      try { window.hoardCopilot && typeof window.hoardCopilot.onTurnEnd === 'function' && window.hoardCopilot.onTurnEnd(event.detail || {}); }
      catch { /* the app's refresh must never break the panel */ }
    });
    body.replaceChildren(panel);
    if (typeof panel.loadHistory === 'function') await panel.loadHistory();
  }

  function open(toggle) {
    if (aside) { aside.hidden = false; toggle.setAttribute('aria-expanded', 'true'); return; }
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
    toggle.textContent = 'Faustus';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => (aside && !aside.hidden ? (aside.hidden = true, toggle.setAttribute('aria-expanded', 'false')) : open(toggle)));
    document.body.appendChild(toggle);
  }

  window.hoardCopilotGuessObjects = guessObjects;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
"""

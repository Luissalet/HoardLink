// App Dock page bridge. Served after the copilot launcher, without a build step.
(() => {
  'use strict';
  if (window.HoardApp?.bridgeVersion) return;
  const previous = window.HoardApp || {};
  const tools = new Map();
  const actors = new Map();
  const refs = new Map();
  let elementRefs = new WeakMap();
  let sequence = 0;
  let origin = null;
  let contextSource = {};
  let contextTimer;
  let layer;
  let active = false;
  const changedHandlers = new Set();
  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
  const compact = value => String(value || '').replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const controls = 'button,a[href],input:not([type=hidden]),textarea,select,[contenteditable=true],[role=button],[role=tab],[role=checkbox],[role=radio],[role=combobox],[role=switch]';
  const readable = `${controls},h1,h2,h3,h4,h5,h6,p,li,td,th,dt,dd,label,[data-hoard-ref]`;
  const targets = `${readable},div,span,section,article,main,header,footer`;

  function fail(code, message, candidates) {
    const error = new Error(message);
    error.code = code;
    if (candidates) error.candidates = candidates;
    throw error;
  }

  function visible(element, screen = false) {
    if (element.closest('[data-hoard-dock-layer],.hoard-copilot,.hoard-copilot-toggle,[hidden],[inert]')) return false;
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && (!screen || rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth);
  }

  function labels(element) {
    const labelled = (element.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ');
    return [element.getAttribute('aria-label'), labelled, ...(element.labels || []), element.getAttribute('placeholder'),
      element.getAttribute('title'), element.innerText || element.textContent, ['button', 'submit'].includes(element.type) ? element.value : '']
      .map(value => compact(value?.textContent ?? value)).filter(Boolean);
  }

  function label(element) {
    return (labels(element)[0] || element.getAttribute('name') || element.tagName.toLowerCase()).slice(0, 180);
  }

  function ref(element) {
    if (!elementRefs.has(element)) {
      const key = `e${++sequence}`;
      elementRefs.set(element, key);
      refs.set(key, element);
    }
    return elementRefs.get(element);
  }

  function candidate(element) {
    return {ref: ref(element), label: label(element), kind: element.getAttribute('role') || element.tagName.toLowerCase()};
  }

  function target(args, selector = targets) {
    if (args.ref) {
      const element = refs.get(args.ref);
      if (!element?.isConnected || !visible(element)) fail('not_found', `Target ${args.ref} is no longer visible`);
      if (!element.matches(selector)) fail('bad_args', 'Target does not support this action');
      return element;
    }
    const query = normalize(args.text ?? args.label);
    if (!query) fail('bad_args', 'Provide a ref, text or label');
    const elements = [...document.querySelectorAll(selector)].filter(element => visible(element));
    const exact = elements.filter(element => labels(element).some(value => normalize(value) === query));
    let matches = exact.length ? exact : elements.filter(element => labels(element).some(value => normalize(value).includes(query)));
    // Prefer the innermost semantic match when a label wraps a control.
    matches = matches.filter(element => !matches.some(other => element !== other && element.contains(other)));
    if (matches.length === 1) return matches[0];
    if (matches.length > 1) fail('ambiguous', 'Several targets match. Use a ref.', matches.slice(0, 12).map(candidate));
    const words = query.split(' ');
    const nearest = elements.map(element => ({element, score: Math.max(...labels(element).map(value => words.filter(word => normalize(value).includes(word)).length), 0)}))
      .sort((a, b) => b.score - a.score).slice(0, 8).map(item => candidate(item.element));
    fail('not_found', 'No visible target matches. Read the page or use one of these labels.', nearest);
  }

  function enabled(element) {
    if (element.matches(':disabled,[aria-disabled=true]') || element.closest('[aria-disabled=true]')) fail('disabled', 'Target is disabled');
  }

  function context() {
    let custom = {};
    try { custom = typeof contextSource === 'function' ? contextSource() : contextSource; } catch (_) { /* A page callback must not break the bridge. */ }
    const value = {title: document.title, url: location.pathname + location.search + location.hash};
    for (const key of ['view', 'focus']) if (custom?.[key] !== undefined) value[key] = custom[key];
    if (custom?.summary !== undefined) value.summary = String(custom.summary).slice(0, 400);
    const selection = window.getSelection()?.toString().slice(0, 500);
    if (selection) value.selection = selection;
    return value;
  }

  function send(type, fields = {}) {
    if (origin === null) return;
    window.parent.postMessage({hoardDock: 1, type, ...fields}, origin);
  }

  function scheduleContext() {
    if (origin === null) return;
    clearTimeout(contextTimer);
    contextTimer = setTimeout(() => send('dock/context', {context: context()}), 400);
  }

  function ready() {
    const appId = document.documentElement.dataset.hoardApp || window.HOARD_APP_ID || location.hostname;
    send('dock/ready', {app: window.HoardApp?.app || {id: appId, name: document.title || appId}, tools: [...tools.values()].map(({name, description, inputSchema}) => ({name, description, inputSchema})), context: context()});
  }

  function ensureLayer() {
    if (layer) return;
    const host = document.createElement('div');
    host.dataset.hoardDockLayer = '';
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;contain:layout style;';
    layer = host.attachShadow({mode: 'open'});
    const style = document.createElement('style');
    style.textContent = `
      :host{pointer-events:none!important}
      .cursor{position:absolute;left:0;top:0;opacity:1;transition:opacity .3s;will-change:transform;pointer-events:none}
      .arrow{display:block;width:25px;height:30px;filter:drop-shadow(0 2px 2px #0008)}
      .name,.note{position:absolute;left:17px;top:25px;max-width:min(240px,60vw);padding:4px 8px;border-radius:5px;
        color:#fff;background:#25251f;font:600 12px/1.4 'Source Sans 3',system-ui,sans-serif;white-space:nowrap;
        box-shadow:0 2px 6px #0005;overflow:hidden;text-overflow:ellipsis;border:1px solid #ffffff70}
      .name{border-top:3px solid var(--actor-color)}
      .note{top:56px;font-weight:400;white-space:normal;width:max-content}
      .note:empty{display:none}
      .ring{position:absolute;left:-13px;top:-13px;width:26px;height:26px;border:2px solid var(--actor-color);border-radius:50%;opacity:0;box-sizing:border-box}
      .highlight .ring{opacity:.75;transform:scale(1.2)}
      .thinking .arrow{animation:thinking 1.5s ease-in-out infinite}
      @keyframes thinking{50%{opacity:.45}}
      @media(prefers-reduced-motion:reduce){.cursor{transition:none}.thinking .arrow{animation:none}}
    `;
    try {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(style.textContent);
      layer.adoptedStyleSheets = [sheet];
    } catch (_) { layer.append(style); }
    document.documentElement.append(host);
  }

  function cursorFor(actor = {}) {
    ensureLayer();
    const id = String(actor.id || 'agent');
    let cursor = actors.get(id);
    if (!cursor) {
      const node = document.createElement('div');
      node.className = 'cursor';
      node.dataset.actor = id;
      node.innerHTML = '<div class="ring"></div><svg class="arrow" viewBox="0 0 25 30" xmlns="http://www.w3.org/2000/svg"><path d="M2 2L2 23L8 18L12 27L17 25L13 16L22 16Z" fill="var(--actor-color)" stroke="white" stroke-width="1.5" stroke-linejoin="round"/></svg><span class="name"></span><span class="note"></span>';
      layer.append(node);
      cursor = {node, x: 28 + actors.size * 34, y: 32, active: 0, timer: null, animation: null};
      actors.set(id, cursor);
    }
    const color = typeof actor.color === 'string' && CSS.supports('color', actor.color) ? actor.color : '#b58937';
    cursor.node.style.setProperty('--actor-color', color);
    cursor.node.querySelector('.name').textContent = String(actor.name || id).slice(0, 80);
    cursor.node.style.transform = `translate(${cursor.x}px,${cursor.y}px)`;
    cursor.node.style.opacity = '1';
    positionAnnotations(cursor);
    clearTimeout(cursor.timer);
    cursor.timer = setTimeout(() => { if (!cursor.active) cursor.node.style.opacity = '0'; }, 20000);
    return cursor;
  }

  function positionAnnotations(cursor) {
    const name = cursor.node.querySelector('.name');
    const note = cursor.node.querySelector('.note');
    const nameBox = name.getBoundingClientRect();
    const noteBox = note.getBoundingClientRect();
    const height = nameBox.height + (note.textContent ? noteBox.height + 4 : 0);
    const top = Math.max(6, Math.min(cursor.y + 25, innerHeight - height - 6));
    for (const [element, box, offset] of [[name, nameBox, 0], [note, noteBox, nameBox.height + 4]]) {
      element.style.left = `${Math.max(6, Math.min(cursor.x + 17, innerWidth - box.width - 6)) - cursor.x}px`;
      element.style.top = `${top + offset - cursor.y}px`;
    }
  }

  function cursorApi(actor, signal) {
    const cursor = cursorFor(actor);
    return {
      async to(element) {
        signal?.throwIfAborted();
        element.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'instant'});
        const rect = element.getBoundingClientRect();
        const x = Math.max(4, Math.min(innerWidth - 28, rect.left + Math.min(rect.width / 2, 60)));
        const y = Math.max(4, Math.min(innerHeight - 36, rect.top + rect.height / 2));
        cursor.node.classList.remove('thinking');
        cursor.node.classList.remove('highlight');
        cursor.node.querySelector('.note').textContent = '';
        cursor.target = element;
        const distance = Math.hypot(x - cursor.x, y - cursor.y);
        cursor.animation?.cancel();
        if (!reduced()) {
          cursor.animation = cursor.node.animate([
            {transform: `translate(${cursor.x}px,${cursor.y}px)`},
            {transform: `translate(${(cursor.x + x) / 2}px,${(cursor.y + y) / 2 - Math.min(45, distance / 8)}px)`, offset: .5},
            {transform: `translate(${x}px,${y}px)`}
          ], {duration: Math.min(600, 250 + distance / 3), easing: 'cubic-bezier(.2,.7,.2,1)'});
          await cursor.animation.finished.catch(() => {});
        }
        signal?.throwIfAborted();
        cursor.x = x;
        cursor.y = y;
        cursor.node.style.transform = `translate(${x}px,${y}px)`;
        positionAnnotations(cursor);
        cursor.animation = null;
      },
      press() {
        if (reduced()) return;
        cursor.node.querySelector('.ring').animate([{opacity: .9, transform: 'scale(.5)'}, {opacity: 0, transform: 'scale(2)'}], {duration: 450});
      },
      note(text) {
        cursor.node.classList.add('highlight');
        cursor.node.querySelector('.note').textContent = String(text || '').slice(0, 300);
        positionAnnotations(cursor);
      },
    };
  }

  function registerTool(name, definition, handler) {
    if (!/^[a-z][a-z0-9_]{1,40}$/.test(name) || !definition || typeof definition.description !== 'string' ||
        !definition.inputSchema || definition.inputSchema.type !== 'object' || typeof handler !== 'function') {
      fail('bad_args', 'Tools need a valid name, description, object inputSchema and handler');
    }
    if (tools.has(name) && tools.get(name).generic) fail('bad_args', 'Generic tools cannot be replaced');
    if (!tools.has(name) && tools.size >= 64) fail('bad_args', 'A page supports up to 64 tools');
    tools.set(name, {name, description: definition.description, inputSchema: definition.inputSchema, handler});
    ready();
  }

  function validate(value, schema, path = 'args') {
    if (!schema || typeof schema !== 'object') return;
    const type = schema.type;
    const valid = {object: () => value !== null && typeof value === 'object' && !Array.isArray(value),
      array: () => Array.isArray(value), string: () => typeof value === 'string', boolean: () => typeof value === 'boolean',
      number: () => typeof value === 'number' && Number.isFinite(value), integer: () => Number.isInteger(value), null: () => value === null};
    if (type && !(Array.isArray(type) ? type : [type]).some(item => valid[item]?.())) fail('bad_args', `${path} must be ${type}`);
    if (schema.enum && !schema.enum.includes(value)) fail('bad_args', `${path} has an unsupported value`);
    if (typeof value === 'number' && (value < (schema.minimum ?? -Infinity) || value > (schema.maximum ?? Infinity))) fail('bad_args', `${path} is outside its supported range`);
    if (typeof value === 'string' && (value.length < (schema.minLength ?? 0) || value.length > (schema.maxLength ?? Infinity))) fail('bad_args', `${path} has an invalid length`);
    if (type === 'object') {
      for (const key of schema.required || []) if (value[key] === undefined) fail('bad_args', `${path}.${key} is required`);
      for (const [key, item] of Object.entries(value)) {
        if (schema.additionalProperties === false && !Object.hasOwn(schema.properties || {}, key)) fail('bad_args', `${path}.${key} is unsupported`);
        validate(item, schema.properties?.[key], `${path}.${key}`);
      }
    }
    if (Array.isArray(value)) for (const item of value) validate(item, schema.items, path);
  }

  const targetProperties = {ref: {type: 'string'}, text: {type: 'string'}, label: {type: 'string'}};
  function generic(name, description, properties, required, handler) {
    registerTool(name, {description, inputSchema: {type: 'object', properties, required, additionalProperties: false}}, handler);
    tools.get(name).generic = true;
  }

  generic('app_read', 'Read visible text and controls with stable refs', {scope: {type: 'string', enum: ['screen', 'page']}, max_chars: {type: 'integer', minimum: 1}}, [], async args => {
    const lines = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
      if (element.closest('script,style,noscript,template')) continue;
      if (!visible(element, args.scope !== 'page')) continue;
      if (node.nodeType === Node.TEXT_NODE) {
        if (!element.closest(readable) && compact(node.textContent)) lines.push(compact(node.textContent));
        continue;
      }
      if (!element.matches(readable)) continue;
      if (element.parentElement?.closest(controls)) continue;
      if (!element.matches(controls) && element.querySelector(readable)) continue;
      const state = [element.matches(':disabled,[aria-disabled=true]') ? 'disabled' : '',
        element.checked ? 'checked' : '', element.getAttribute('aria-selected') === 'true' ? 'selected' : '',
        element.matches('input:not([type=password]),textarea,select') ? `value=${JSON.stringify(element.value)}` : ''].filter(Boolean).join(', ');
      lines.push(`[${ref(element)}] ${element.getAttribute('role') || element.tagName.toLowerCase()} ${JSON.stringify(label(element))}${state ? ` (${state})` : ''}`);
    }
    return {text: lines.join('\n').slice(0, args.max_chars ?? 6000), context: context()};
  });

  generic('app_click', 'Click a visible control', targetProperties, [], async (args, {cursor}) => {
    const element = target(args, controls);
    enabled(element);
    await cursor.to(element);
    enabled(element);
    cursor.press();
    element.click();
    return {clicked: label(element), url_after: location.pathname + location.search + location.hash};
  });

  function setValue(element, value) {
    const proto = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (element.isContentEditable) element.textContent = value;
    else setter.call(element, value);
    element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
  }

  generic('app_fill', 'Fill a text field, optionally submit it', {...targetProperties, value: {type: 'string'}, submit: {type: 'boolean'}}, ['value'], async (args, {cursor, signal}) => {
    const element = target(args, 'input:not([type=hidden]),textarea,[contenteditable=true]');
    enabled(element);
    if (element.readOnly || element.matches('input[type=file],input[type=checkbox],input[type=radio],input[type=button],input[type=submit]')) fail('bad_args', 'Target is not an editable text field');
    await cursor.to(element);
    element.focus();
    cursor.press();
    const characters = Array.from(args.value);
    const step = Math.max(1, Math.ceil(characters.length / 60));
    if (!reduced()) for (let index = step; index < characters.length; index += step) {
      signal.throwIfAborted();
      setValue(element, characters.slice(0, index).join(''));
      await sleep(22);
    }
    signal.throwIfAborted();
    setValue(element, args.value);
    element.dispatchEvent(new Event('change', {bubbles: true}));
    if (args.submit) {
      const proceed = element.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}));
      element.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
      if (proceed && element.form) element.form.requestSubmit();
    }
    return {filled: label(element), length: args.value.length, submitted: Boolean(args.submit)};
  });

  generic('app_select', 'Select an option, radio, checkbox or tab', {...targetProperties, option: {type: ['string', 'boolean']}}, ['option'], async (args, {cursor}) => {
    const element = target(args, 'select,input[type=radio],input[type=checkbox],[role=tab],[role=radio],[role=checkbox],[role=switch]');
    enabled(element);
    await cursor.to(element);
    cursor.press();
    if (element instanceof HTMLSelectElement) {
      const query = normalize(args.option);
      const options = [...element.options];
      const matches = options.filter(option => normalize(option.value) === query || normalize(option.label) === query);
      const chosen = matches.length ? matches : options.filter(option => normalize(option.label).includes(query));
      if (!chosen.length) fail('not_found', 'Option was not found', options.map(option => ({label: option.label, value: option.value})));
      if (chosen.length > 1) fail('ambiguous', 'Several options match', chosen.map(option => ({label: option.label, value: option.value})));
      enabled(chosen[0]);
      element.value = chosen[0].value;
      element.dispatchEvent(new Event('input', {bubbles: true}));
      element.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
      const desired = typeof args.option === 'boolean' ? args.option : !['false', 'off', 'unchecked'].includes(normalize(args.option));
      const current = element.matches('input') ? element.checked : element.getAttribute('aria-checked') === 'true' || element.getAttribute('aria-selected') === 'true';
      if (current !== desired) element.click();
    }
    return {selected: label(element), option: args.option};
  });

  generic('app_navigate', 'Navigate to a hash or same-origin path', {to: {type: 'string', minLength: 1}}, ['to'], async args => {
    let url;
    try { url = new URL(args.to, location.href); } catch (_) { fail('bad_args', 'Invalid navigation target'); }
    if (url.origin !== location.origin || !/^(#|\/(?!\/))/.test(args.to)) fail('bad_args', 'Use a hash or same-origin path');
    const result = {url_after: url.pathname + url.search + url.hash};
    if (url.pathname === location.pathname && url.search === location.search) location.hash = url.hash;
    else setTimeout(() => location.assign(url.href), 50);
    return result;
  });

  generic('app_scroll', 'Scroll to a target or move the page', {...targetProperties, by: {type: 'string', enum: ['down', 'up', 'top', 'bottom']}}, [], async (args, {cursor}) => {
    if (args.ref || args.text || args.label) await cursor.to(target(args));
    else {
      if (!args.by) fail('bad_args', 'Provide a target or scroll direction');
      const top = args.by === 'top' ? 0 : args.by === 'bottom' ? document.documentElement.scrollHeight : scrollY + innerHeight * (args.by === 'up' ? -.8 : .8);
      window.scrollTo({top, behavior: reduced() ? 'instant' : 'smooth'});
    }
    return {scrolled: true};
  });

  generic('app_highlight', 'Point to a target with a note', {...targetProperties, note: {type: 'string'}}, [], async (args, {cursor}) => {
    const element = target(args);
    await cursor.to(element);
    cursor.press();
    cursor.note(args.note);
    return {highlighted: label(element)};
  });

  generic('app_wait', 'Wait for visible text or a duration', {text: {type: 'string'}, ms: {type: 'integer', minimum: 0, maximum: 5000}}, [], async (args, {signal}) => {
    if (!args.text && args.ms === undefined) fail('bad_args', 'Provide text or ms');
    if (!args.text) { await sleep(args.ms); return {waited: args.ms}; }
    const start = performance.now();
    do {
      signal.throwIfAborted();
      try { target({text: args.text}); return {found: args.text}; } catch (error) { if (error.code === 'ambiguous') return {found: args.text}; if (error.code !== 'not_found') throw error; }
      await sleep(100);
    } while (performance.now() - start < (args.ms ?? 5000));
    fail('timeout', 'Text did not appear');
  });

  const api = Object.assign(previous, {
    bridgeVersion: 1,
    registerTool,
    setContext(value) { contextSource = value || {}; scheduleContext(); },
    onChanged(handler) {
      if (typeof handler !== 'function') fail('bad_args', 'onChanged needs a callback');
      changedHandlers.add(handler);
      return () => changedHandlers.delete(handler);
    },
    message(text, sendNow = false) { send('dock/message', {text: String(text), send: Boolean(sendNow)}); },
  });
  window.HoardApp = api;
  for (const [method, ...args] of previous.q || []) {
    try { if (typeof api[method] === 'function') api[method](...args); } catch (error) { console.warn('HoardApp queue:', error.message); }
  }
  api.q = {push(entry) { const [method, ...args] = entry; if (typeof api[method] === 'function') api[method](...args); }};

  async function call(message) {
    const controller = new AbortController();
    const actor = message.actor || {};
    let actorCursor;
    let timer;
    try {
      const tool = tools.get(message.tool);
      if (!tool) fail('not_found', 'Unknown tool');
      const args = message.args ?? {};
      validate(args, tool.inputSchema);
      actorCursor = cursorFor(actor);
      actorCursor.active++;
      const cursor = cursorApi(actor, controller.signal);
      const timeout = new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); const error = new Error('Tool exceeded 45 seconds'); error.code = 'timeout'; reject(error); }, 45000); });
      const result = await Promise.race([Promise.resolve().then(() => tool.handler(args, {actor, cursor, signal: controller.signal})), timeout]);
      send('dock/result', {id: message.id, ok: true, result: result ?? null});
    } catch (error) {
      const codes = ['not_found', 'ambiguous', 'disabled', 'timeout', 'bad_args'];
      send('dock/result', {id: message.id, ok: false, error: {code: codes.includes(error.code) ? error.code : 'bad_args', message: error.message || String(error), ...(error.candidates ? {candidates: error.candidates} : {})}});
    } finally {
      clearTimeout(timer);
      if (actorCursor) { actorCursor.active--; cursorFor(actor); }
      scheduleContext();
    }
  }

  function navigation() {
    refs.clear();
    elementRefs = new WeakMap();
    scheduleContext();
  }

  if (window.parent === window) return;
  window.addEventListener('message', event => {
    const message = event.data;
    if (event.source !== window.parent || !message || message.hoardDock !== 1 || typeof message.type !== 'string') return;
    if (origin !== null && event.origin !== origin) return;
    if (message.type === 'dock/hello') {
      if (message.host !== 'faustus') return;
      origin = event.origin;
      activate();
      document.documentElement.dataset.hoardDock = '1';
      const style = document.getElementById('hoard-dock-launcher-style') || document.createElement('style');
      style.id = 'hoard-dock-launcher-style';
      style.textContent = 'html[data-hoard-dock="1"] .hoard-copilot-toggle{display:none!important}';
      if (!style.isConnected) {
        try {
          const sheet = new CSSStyleSheet();
          sheet.replaceSync(style.textContent);
          document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
        } catch (_) { document.documentElement.append(style); }
      }
      if (['dark', 'light'].includes(message.theme)) document.documentElement.dataset.theme = message.theme;
      ready();
      return;
    }
    if (origin === null) return;
    if (message.type === 'dock/call' && typeof message.id === 'string' && typeof message.tool === 'string') void call(message);
    if (message.type === 'dock/theme' && ['light', 'dark'].includes(message.theme)) document.documentElement.dataset.theme = message.theme;
    if (message.type === 'dock/changed') {
      const detail = {server: message.server, tool: message.tool};
      for (const handler of changedHandlers) Promise.resolve().then(() => handler(detail)).catch(error => console.warn('HoardApp onChanged:', error.message));
      document.dispatchEvent(new CustomEvent('hoard:changed', {detail}));
      if (!changedHandlers.size && api.reloadOnChange === true) location.reload();
      scheduleContext();
    }
    if (message.type === 'dock/cursor') {
      const id = String(message.actor?.id || 'agent');
      if (message.state === 'gone') {
        const cursor = actors.get(id);
        if (cursor) { clearTimeout(cursor.timer); cursor.animation?.cancel(); cursor.node.remove(); actors.delete(id); }
      } else if (['idle', 'thinking'].includes(message.state)) cursorFor(message.actor).node.classList.toggle('thinking', message.state === 'thinking');
    }
  });
  function activate() {
    if (active) return;
    active = true;
    for (const event of ['selectionchange', 'input', 'change', 'focusin']) document.addEventListener(event, scheduleContext);
    for (const event of ['hashchange', 'popstate']) window.addEventListener(event, navigation);
    const positionCursors = () => {
      for (const cursor of actors.values()) {
        if (!cursor.target?.isConnected || cursor.animation) continue;
        const rect = cursor.target.getBoundingClientRect();
        cursor.x = Math.max(4, Math.min(innerWidth - 28, rect.left + Math.min(rect.width / 2, 60)));
        cursor.y = Math.max(4, Math.min(innerHeight - 36, rect.top + rect.height / 2));
        cursor.node.style.transform = `translate(${cursor.x}px,${cursor.y}px)`;
        positionAnnotations(cursor);
      }
    };
    window.addEventListener('resize', positionCursors);
    document.addEventListener('scroll', positionCursors, true);
    for (const method of ['pushState', 'replaceState']) {
      const original = history[method];
      history[method] = function (...args) { const result = original.apply(this, args); navigation(); return result; };
    }
    new MutationObserver(records => {
      if (records.some(record => !record.target.closest?.('[data-hoard-dock-layer]') && record.target.id !== 'hoard-dock-launcher-style')) scheduleContext();
    }).observe(document.documentElement, {subtree: true, childList: true, characterData: true});
  }
})();

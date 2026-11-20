import {decisionHeading, decisionUrl, loadHistory, loadPending} from './production_decisions_data.js';

const {api, el, lang, bus} = window.HubFacets.ctx;
const tr = (es, en) => lang() === 'es' ? es : en;
const pending = new Map();
const history = new Map();

function shared(cache, key, read) {
  const existing = cache.get(key);
  if (existing && Date.now() - existing.at < 2000) return existing.result;
  const result = read(api, key);
  cache.set(key, {at: Date.now(), result});
  return result;
}

function heading(title) {
  const wrapper = el('div', 'hub-section-heading');
  wrapper.append(el('h2', '', title));
  return wrapper;
}

function pendingRow(row) {
  const link = el('a', 'hub-decision-row');
  link.href = decisionUrl(row);
  const thumb = (preview) => {
    const image = el('img', 'hub-decision-thumb');
    image.src = preview.path;
    image.alt = preview.title;
    image.loading = 'lazy';
    image.onerror = () => image.remove();
    return image;
  };
  if (row.image) link.append(thumb(row.image));
  const words = el('span', 'hub-decision-words');
  words.append(el('strong', '', row.title), el('small', '', row.order));
  if (row.pieces?.length) words.append(el('small', '', row.pieces.join(' · ')));
  if (row.variants?.length) {
    const variants = el('span', 'hub-decision-variants');
    for (const variant of row.variants) {
      const item = el('span');
      if (variant.image) item.append(thumb(variant.image));
      item.append(el('small', '', variant.title));
      variants.append(item);
    }
    words.append(variants);
  }
  link.append(words, el('span', 'hub-decision-action', tr('Abrir decisión', 'Open decision')));
  return link;
}

export function watchPendingCount(space, count) {
  const refresh = async () => {
    if (document.hidden) return;
    const state = await shared(pending, space, loadPending);
    count(state.rows.length, state.incomplete);
  };
  setInterval(refresh, 8000);
  document.addEventListener('visibilitychange', refresh);
  refresh();
}

export function mount(root, space, {archive = false, count, hideEmpty = false, dedicated = false} = {}) {
  if (!root.isConnected) return;
  let pendingSignature = '', historySignature = '', sequence = 0;
  const waiting = el('section', 'hub-production-decisions');
  const records = el('section', 'hub-production-decisions');
  root.replaceChildren(waiting);
  if (archive) root.append(records);
  async function refresh() {
    if (!root.isConnected || root.closest('[hidden]')) return;
    const revision = ++sequence;
    const current = () => revision === sequence && root.isConnected;
    const refreshPending = async () => {
      const state = await shared(pending, space, loadPending);
      if (!current()) return;
      count?.(state.rows.length, state.incomplete);
      const signature = JSON.stringify([lang(), state]);
      if (pendingSignature === signature) return;
      pendingSignature = signature;
      waiting.hidden = hideEmpty && !state.rows.length && !state.incomplete;
      waiting.replaceChildren();
      if (!dedicated) waiting.append(heading(tr('Tus decisiones pendientes', 'Your pending decisions')));
      for (const row of state.rows) waiting.append(pendingRow(row));
      if (state.incomplete) waiting.append(el('p', 'hint', tr('No se han podido consultar todas las decisiones pendientes.', 'Some pending decisions could not be loaded.')));
      else if (!state.rows.length) waiting.append(el('p', 'hint', tr('No tienes decisiones pendientes.', 'You have no pending decisions.')));
    };
    const refreshHistory = async () => {
      if (!archive) return;
      const rows = await shared(history, space, loadHistory).catch(() => null);
      if (!current()) return;
      const signature = JSON.stringify([lang(), rows]);
      if (historySignature === signature) return;
      historySignature = signature;
      records.replaceChildren(heading(tr('Decisiones de producción registradas', 'Production decision history')));
      if (!rows?.length) records.append(el('p', 'hint', rows ? tr('Aún no hay decisiones de producción registradas.', 'No production decisions have been recorded yet.') : tr('No se ha podido consultar el registro de producción.', 'Production decision history could not be loaded.')));
      for (const row of rows || []) {
        const card = el('article', 'hub-decision-record');
        const verdict = row.decision === 'keep' ? tr('Conservado', 'Kept') : row.draft ? tr('Descartado', 'Discarded') : tr('Cancelado', 'Cancelled');
        card.append(el('strong', '', decisionHeading(row, lang())),
          el('span', '', row.title), el('small', '', [row.order, row.variant, verdict].filter(Boolean).join(' · ')));
        if (row.note) card.append(el('p', '', row.note));
        records.append(card);
      }
    };
    await Promise.all([refreshPending(), refreshHistory()]);
  }
  const interval = setInterval(() => {
    if (!root.isConnected) {
      clearInterval(interval);
      bus.removeEventListener('lang', refresh);
      document.removeEventListener('visibilitychange', onVisibility);
    } else refresh();
  }, 8000);
  const onVisibility = () => { if (!document.hidden) refresh(); };
  bus.addEventListener('lang', refresh);
  document.addEventListener('visibilitychange', onVisibility);
  refresh();
}

export function focusOrderDecision() {
  const target = /^(gate|draft)-(\d+)$/.exec(new URLSearchParams(location.search).get('decision'));
  const orders = document.querySelector('#tab-orders');
  if (!target || !orders) return;
  const attr = target[1] === 'gate' ? 'data-gate' : 'data-draft-pass';
  function focus() {
    if (location.hash !== '#hub/orders') return true;
    const heading = orders.querySelector(`[${attr}="${target[2]}"] h3`);
    if (!heading) return false;
    heading.tabIndex = -1;
    heading.focus({preventScroll: true});
    heading.scrollIntoView({block: 'start'});
    return true;
  }
  if (focus()) return;
  const observer = new MutationObserver(() => { if (focus()) observer.disconnect(); });
  observer.observe(orders, {childList: true, subtree: true});
  setTimeout(() => observer.disconnect(), 15000);
}

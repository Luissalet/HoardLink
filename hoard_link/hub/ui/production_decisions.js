import {decisionHeading, decisionUrl, loadHistory, loadPending} from './production_decisions_data.js';
import {renderDecisionCards} from './production_decisions_card.js';
import {renderPrintSliceCard} from './print_slice_card.js';

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

function slicePieces(row) {
  const artifacts = row.artifacts ?? row.variants?.flatMap(variant => variant.artifacts ?? []) ?? [];
  return artifacts.flatMap(piece => {
    const id = /^\/api\/agora\/piece_file\/(\d+)$/.exec(piece.path || '')?.[1];
    return id && /(?:\.gcode$|^receipt\.json$)/i.test(piece.filename || '') ? [{id, title: piece.title}] : [];
  });
}

async function loadSliceReports(pieces) {
  const ids = new Set(pieces.flat().map(piece => piece.id));
  if (!ids.size) return {};
  const query = new URLSearchParams();
  for (const id of ids) query.append('piece', id);
  const answer = await api('/api/programs/print-slice-reports?' + query);
  return answer?.reports ?? {};
}

function renderSliceReports(cards, pieces, reports) {
  for (const [index, card] of [...cards.children].entries()) {
    const previous = card.querySelector(':scope > .production-card__slicing');
    const items = pieces[index].filter(piece => reports[piece.id]);
    if (!items.length) {
      previous?.remove();
      continue;
    }
    const section = el('section', 'production-card__slicing');
    for (const piece of items) {
      section.append(el('h4', '', piece.title), renderPrintSliceCard(reports[piece.id], tr));
    }
    if (previous) previous.replaceWith(section);
    else card.firstElementChild.after(section);
  }
}

export function watchPendingCount(space, count) {
  const refresh = async () => {
    if (document.hidden) return;
    const state = await shared(pending, space, loadPending);
    count(state.rows.length, state.incomplete);
  };
  setInterval(refresh, 8000);
  document.addEventListener('visibilitychange', refresh);
  bus.addEventListener('production-decision', refresh);
  refresh();
}

export function mount(root, space, {archive = false, count, hideEmpty = false, dedicated = false} = {}) {
  if (!root.isConnected) return;
  let historySignature = '', sequence = 0;
  const waiting = el('section', 'hub-production-decisions');
  const records = el('section', 'hub-production-decisions');
  const pendingTitle = heading('');
  const cards = el('div');
  const message = el('p', 'hint');
  message.setAttribute('role', 'status');
  if (!dedicated) waiting.append(pendingTitle);
  waiting.append(cards, message);
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
      waiting.hidden = hideEmpty && !state.rows.length && !state.incomplete;
      pendingTitle.firstChild.textContent = tr('Tus decisiones pendientes', 'Your pending decisions');
      renderDecisionCards(cards, state.rows, {
        lang: lang(), source: (path, media) => media === 'web' ? null : path, orderHref: decisionUrl, orderInPlace: true,
        post: async (operation, body) => {
          const answer = await api('/api/agora/' + operation, body);
          if (!answer?.ok) throw Object.assign(new Error(answer?.error), {code: answer?.code});
        },
        refresh: () => {
          pending.clear();
          history.clear();
          bus.dispatchEvent(new Event('production-decision'));
        },
      });
      message.textContent = state.incomplete
        ? tr('No se han podido consultar todas las decisiones pendientes.', 'Some pending decisions could not be loaded.')
        : state.rows.length ? '' : tr('No tienes decisiones pendientes.', 'You have no pending decisions.');
      message.hidden = !message.textContent;
      const pieces = state.rows.map(slicePieces);
      const reports = await loadSliceReports(pieces).catch(() => null);
      if (reports && current()) renderSliceReports(cards, pieces, reports);
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
      bus.removeEventListener('production-decision', refresh);
      document.removeEventListener('visibilitychange', onVisibility);
    } else refresh();
  }, 8000);
  const onVisibility = () => { if (!document.hidden) refresh(); };
  bus.addEventListener('lang', refresh);
  bus.addEventListener('production-decision', refresh);
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

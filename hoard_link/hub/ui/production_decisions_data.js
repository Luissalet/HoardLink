async function read(api, operation, query) {
  const answer = await api(`/api/agora/${operation}${query ? `?${query}` : ''}`);
  if (!answer?.ok) throw new Error(answer?.error || operation);
  return answer;
}

function scopeQuery(space) {
  return space === '*' ? 'all_spaces=1' : space ? `space_id=${encodeURIComponent(space)}` : '';
}

export async function loadPending(api, space = '') {
  try {
    const {rows} = await read(api, 'pending_decisions', scopeQuery(space));
    return {rows, incomplete: false};
  } catch {
    return {rows: [], incomplete: true};
  }
}

export async function loadHistory(api, space = '') {
  const query = scopeQuery(space);
  const log = await read(api, 'decisions', [query, 'limit=100'].filter(Boolean).join('&'));
  const decisions = log.production_decisions ?? (await read(api, 'order_decisions', query)).decisions;
  const viewer = 'viewer' in log || !decisions.some(d => d.decided_by?.id)
    ? log.viewer
    : (await read(api, 'guild', [query, 'done_limit=0', 'offline_limit=0'].filter(Boolean).join('&')).catch(() => null))?.viewer;
  const rows = new Map();
  for (const d of decisions) {
    const {gate, draft_pass: pass, variant, order} = d.situation || {};
    const key = JSON.stringify([pass?.id, variant?.id, gate?.id || d.gate_id, Math.floor(d.created), d.decision,
      d.note, d.decided_by?.id]);
    if (!rows.has(key)) rows.set(key, {id: d.id, title: pass?.title || gate?.title || d.piece_title,
      order: order?.title, variant: d.decision === 'keep' ? variant?.title : '', created: d.created,
      decision: d.decision, note: d.note, draft: Boolean(pass), decidedBy: d.decided_by?.name,
      own: Boolean(viewer && viewer === d.decided_by?.id)});
  }
  return [...rows.values()];
}

export function decisionHeading(row, lang) {
  const es = lang === 'es';
  const attribution = row.own ? (es ? 'Tu decisión' : 'Your decision')
    : row.decidedBy ? (es ? `Decisión de ${row.decidedBy}` : `Decision by ${row.decidedBy}`)
    : (es ? 'Decisión registrada' : 'Recorded decision');
  const date = new Date(row.created * 1000).toLocaleDateString(es ? 'es-ES' : 'en-GB');
  return `${attribution} (${date})`;
}

export function decisionUrl(row) {
  return `/?${new URLSearchParams({space_id: row.space, decision: row.key})}#hub/orders`;
}

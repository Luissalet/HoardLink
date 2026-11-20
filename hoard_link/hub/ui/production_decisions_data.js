async function read(api, operation, query) {
  const answer = await api(`/api/agora/${operation}${query ? `?${query}` : ''}`);
  if (!answer?.ok) throw new Error(answer?.error || operation);
  return answer;
}

function scopeQuery(space) {
  return space === '*' ? 'all_spaces=1' : space ? `space_id=${encodeURIComponent(space)}` : '';
}

function thumbnail(pieces) {
  const piece = pieces.find(p => p.previews?.length || p.view === 'image');
  if (!piece) return null;
  const suffix = piece.previews?.length ? '?preview=0' : '';
  return {path: `/api/agora/piece_file/${piece.id}${suffix}`, title: piece.title};
}

export async function loadPending(api, space = '') {
  const query = scopeQuery(space);
  const [orders, drafts] = await Promise.allSettled([
    read(api, 'orders', [query, 'limit=1'].filter(Boolean).join('&')), read(api, 'draft_passes', query),
  ]);
  const anchors = ['keyframe_start', 'keyframe_end', 'keyframe', 'animatic', 'composition', 'blocking_color'];
  const passes = (drafts.value?.draft_passes || []).map(pass => {
    const variants = pass.variants.filter(v => !['kept', 'discarded'].includes(v.status)).map(variant => {
      const preferred = anchors.flatMap(anchor => variant.pieces.filter(piece => piece.anchor === anchor));
      return {id: variant.id, title: variant.title, image: thumbnail(preferred) || thumbnail(variant.pieces)};
    });
    return {key: `draft-${pass.id}`, title: pass.title, order: pass.order?.title, space: pass.space_id,
      image: null, variants};
  });
  const gates = (orders.value?.waiting || []).map(gate => {
    const decided = new Set((gate.decisions || []).map(d => d.piece_id));
    return {key: `gate-${gate.id}`, title: gate.title, order: gate.order?.title, space: gate.space_id,
      image: thumbnail(gate.pieces.filter(p => !decided.has(p.id)))};
  });
  return {rows: [...passes, ...gates], incomplete: orders.status === 'rejected' || drafts.status === 'rejected'};
}

export async function loadHistory(api, space = '') {
  const query = scopeQuery(space);
  const log = await read(api, 'decisions', [query, 'limit=100'].filter(Boolean).join('&'));
  const decisions = log.production_decisions ?? (await read(api, 'order_decisions', query)).decisions;
  const rows = new Map();
  for (const d of decisions) {
    const {gate, draft_pass: pass, variant, order} = d.situation || {};
    const key = JSON.stringify([pass?.id, variant?.id, gate?.id || d.gate_id, Math.floor(d.created), d.decision, d.note]);
    if (!rows.has(key)) rows.set(key, {id: d.id, title: pass?.title || gate?.title || d.piece_title,
      order: order?.title, variant: d.decision === 'keep' ? variant?.title : '', created: d.created,
      decision: d.decision, note: d.note, draft: Boolean(pass)});
  }
  return [...rows.values()];
}

export function decisionUrl(row) {
  return `/?${new URLSearchParams({space_id: row.space, decision: row.key})}#hub/orders`;
}

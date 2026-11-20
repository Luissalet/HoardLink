import {Scale} from 'lucide-react';
import {isTauri} from '@tauri-apps/api/core';
import {useCallback,useEffect,useRef,useState} from 'react';
import {decisionHeading,decisionUrl,loadHistory,loadPending,type DecisionRecord,type PendingDecision} from '../../../hoard_link/hub/ui/production_decisions_data.js';
import {renderDecisionCards} from '../../../hoard_link/hub/ui/production_decisions_card.js';
import {api,openHub,previewSource} from './api';

const es = (localStorage.getItem('hub.lang') || navigator.language).startsWith('es');
const tr = (spanish: string, english: string) => es ? spanish : english;

export function useProductionDecisions(open: boolean) {
  const [state,setState] = useState<{rows: PendingDecision[]; incomplete: boolean}>();
  const [history,setHistory] = useState<DecisionRecord[]>();
  const [historyError,setHistoryError] = useState(false);
  const [revision,setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => {
    let disposed = false;
    let loading = false;
    const refresh = async () => {
      if (loading || document.hidden) return;
      loading = true;
      const reads = [loadPending(api).then(next => { if (!disposed) setState(next); })];
      if (open) reads.push(loadHistory(api).then(next => {
        if (!disposed) { setHistory(next); setHistoryError(false); }
      }).catch(() => { if (!disposed) setHistoryError(true); }));
      await Promise.all(reads);
      loading = false;
    };
    void refresh();
    const timer = window.setInterval(refresh, 8000);
    document.addEventListener('visibilitychange', refresh);
    return () => { disposed = true; window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [open, revision]);
  return {state,history,historyError,refresh};
}

export function DecisionButton({count, open, toggle}: {count?: number; open: boolean; toggle: () => void}) {
  return <button type="button" className="console-decisions-toggle" aria-pressed={open} onClick={toggle}>
    <Scale size={15} aria-hidden="true" /><span>{tr('Tus decisiones pendientes', 'Your pending decisions')}</span>
    {!!count && <strong aria-label={tr(`${count} decisiones pendientes`, `${count} pending decisions`)}>{count}</strong>}
  </button>;
}

export function ProductionDecisions({data, open}: {data: ReturnType<typeof useProductionDecisions>; open: boolean}) {
  const {state,history,historyError,refresh} = data;
  const [openingError,setOpeningError] = useState('');
  const cards = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!cards.current) return;
    renderDecisionCards(cards.current, state?.rows ?? [], {
      lang: es ? 'es' : 'en',
      source: (path, media) => media === 'image' ? previewSource(path) : isTauri() || media === 'web' ? null : path,
      orderHref: decisionUrl,
      openOrder: (event, row) => void openHub(event, row.key).catch(error => setOpeningError(error.message)),
      post: (operation, body) => api('/api/agora/' + operation, body),
      refresh,
    });
  }, [state, refresh, open]);
  if (!open && !state?.rows.length && !state?.incomplete) return null;
  return <section className="console-decisions" data-expanded={open || undefined} aria-labelledby="console-pending-title">
    <h2 id="console-pending-title">{tr('Tus decisiones pendientes', 'Your pending decisions')}</h2>
    <div ref={cards} />
    {openingError && <p role="alert">{openingError}</p>}
    {!state && <p role="status">{tr('Consultando decisiones…', 'Loading decisions…')}</p>}
    {state?.incomplete && <p role="status">{tr('No se han podido consultar todas las decisiones pendientes.', 'Some pending decisions could not be loaded.')}</p>}
    {state && !state.rows.length && !state.incomplete && <p>{tr('No tienes decisiones pendientes.', 'You have no pending decisions.')}</p>}
    {open && <section className="console-decision-history">
      <h2>{tr('Decisiones de producción registradas', 'Production decision history')}</h2>
      {historyError && <p role="status">{tr('No se ha podido consultar el registro de producción.', 'Production decision history could not be loaded.')}</p>}
      {history?.map(row => <article key={row.id}>
        <strong>{decisionHeading(row, es ? 'es' : 'en')}</strong>
        <p>{row.title}</p>
        <small>{[row.order, row.variant, row.decision === 'keep' ? tr('Conservado','Kept') : row.draft ? tr('Descartado','Discarded') : tr('Cancelado','Cancelled')].filter(Boolean).join(' · ')}</small>
        {row.note && <p>{row.note}</p>}
      </article>)}
      {!historyError && history?.length === 0 && <p>{tr('Aún no hay decisiones de producción registradas.', 'No production decisions have been recorded yet.')}</p>}
    </section>}
  </section>;
}

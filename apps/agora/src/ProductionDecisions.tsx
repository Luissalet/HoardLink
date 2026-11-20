import {ArrowUpRight, Scale} from 'lucide-react';
import {useEffect,useState} from 'react';
import {decisionHeading,decisionUrl,loadHistory,loadPending,type DecisionRecord,type PendingDecision} from '../../../hoard_link/hub/ui/production_decisions_data.js';
import {api,openHub,previewSource} from './api';

const es = (localStorage.getItem('hub.lang') || navigator.language).startsWith('es');
const tr = (spanish: string, english: string) => es ? spanish : english;

export function useProductionDecisions(open: boolean) {
  const [state,setState] = useState<{rows: PendingDecision[]; incomplete: boolean}>();
  const [history,setHistory] = useState<DecisionRecord[]>();
  const [historyError,setHistoryError] = useState(false);
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
  }, [open]);
  return {state,history,historyError};
}

export function DecisionButton({count, open, toggle}: {count?: number; open: boolean; toggle: () => void}) {
  return <button type="button" className="console-decisions-toggle" aria-pressed={open} onClick={toggle}>
    <Scale size={15} aria-hidden="true" /><span>{tr('Tus decisiones pendientes', 'Your pending decisions')}</span>
    {!!count && <strong aria-label={tr(`${count} decisiones pendientes`, `${count} pending decisions`)}>{count}</strong>}
  </button>;
}

function Thumbnail({image}: {image: NonNullable<PendingDecision['image']>}) {
  const [source,setSource] = useState('');
  useEffect(() => {
    let current = true;
    void previewSource(image.path).then(value => { if (current) setSource(value); }).catch(() => {});
    return () => { current = false; };
  }, [image.path]);
  return source ? <img src={source} alt={image.title} loading="lazy" onError={() => setSource('')} /> : null;
}

export function ProductionDecisions({data, open}: {data: ReturnType<typeof useProductionDecisions>; open: boolean}) {
  const {state,history,historyError} = data;
  const [openingError,setOpeningError] = useState('');
  if (!open && !state?.rows.length && !state?.incomplete) return null;
  return <section className="console-decisions" data-expanded={open || undefined} aria-labelledby="console-pending-title">
    <h2 id="console-pending-title">{tr('Tus decisiones pendientes', 'Your pending decisions')}</h2>
    {state?.rows.map(row => (
      <a key={row.key} className="console-decision-link" href={decisionUrl(row)} target="_blank" rel="noopener"
        onClick={event => void openHub(event, row.key).catch(error => setOpeningError(error.message))}>
        {row.image && <Thumbnail key={row.image.path} image={row.image} />}
        <span><strong>{row.title}</strong><small>{row.order}</small>
          {!!row.variants?.length && <span className="console-decision-variants">{row.variants.map(variant => <span key={variant.id}>
            {variant.image && <Thumbnail key={variant.image.path} image={variant.image} />}
            <small>{variant.title}</small>
          </span>)}</span>}
        </span>
        <ArrowUpRight size={15} aria-hidden="true" />
      </a>
    ))}
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

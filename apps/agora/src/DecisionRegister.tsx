import {useRef, useState} from 'react';
import {BookOpen, X} from 'lucide-react';
import {api, lang} from './api';
import './decision-register.css';

const copy = {
  es: {
    register: 'Registro del Ágora', threads: 'Hilos', decisions: 'Decisiones', close: 'Cerrar registro',
    threadNumber: 'Número de hilo', openThread: 'Abrir hilo',
    back: 'Volver a la lista', emptyThreads: 'Todavía no hay hilos.', emptyDecisions: 'Todavía no hay decisiones.',
    loading: 'Cargando…', failure: 'No se pudo completar la operación.', retry: 'Volver a cargar',
    retitle: 'Retitular hilo', amend: 'Enmendar decisión', withdraw: 'Retirar decisión',
    title: 'Nuevo título', resolution: 'Nuevo texto', save: 'Guardar', cancel: 'Cancelar',
    confirm: '¿Retirar esta decisión del registro vigente?', withdrawn: 'Decisión retirada',
    current: 'Texto vigente', history: 'Historial', before: 'Anterior', after: 'Después',
    undo: 'Deshacer', updated: 'Cambio guardado.', undone: 'Cambio deshecho.',
    actions: {retitle: 'Título actualizado', amend: 'Decisión enmendada', withdraw: 'Decisión retirada', undo: 'Cambio deshecho', recorded: 'Decisión apuntada'},
  },
  en: {
    register: 'Agora register', threads: 'Threads', decisions: 'Decisions', close: 'Close register',
    threadNumber: 'Thread number', openThread: 'Open thread',
    back: 'Back to list', emptyThreads: 'There are no threads yet.', emptyDecisions: 'There are no decisions yet.',
    loading: 'Loading…', failure: 'The operation could not be completed.', retry: 'Reload',
    retitle: 'Retitle thread', amend: 'Amend decision', withdraw: 'Withdraw decision',
    title: 'New title', resolution: 'New text', save: 'Save', cancel: 'Cancel',
    confirm: 'Withdraw this decision from the current register?', withdrawn: 'Decision withdrawn',
    current: 'Current text', history: 'History', before: 'Before', after: 'After',
    undo: 'Undo', updated: 'Change saved.', undone: 'Change undone.',
    actions: {retitle: 'Title updated', amend: 'Decision amended', withdraw: 'Decision withdrawn', undo: 'Change undone', recorded: 'Decision recorded'},
  },
}[lang];

type Action = 'retitle' | 'amend' | 'withdraw';
type Change = {
  id: number; action: Action | 'undo' | 'recorded'; field: 'title' | 'resolution'; author: string;
  created: number; before_text: string | null; after_text: string | null; message_id: number;
};
type Thread = {id: number; title: string; kind: string; status: string; resolution: string | null; withdrawn?: boolean};
type Decision = Thread & {history?: Change[]};
type Detail = {thread: Thread; history?: Change[]; messages: {id: number; author: string; created: number; body: string}[]};
type Receipt = {thread_id: number; token: string; change: Change};
type Mutation = {undo_token?: string; change?: Change};
type Tab = 'threads' | 'decisions';

function Stamp({author, at}: {author: string; at: number}) {
  const date = new Date(at * 1000);
  return <small>{author} · <time dateTime={date.toISOString()}>{date.toLocaleString(lang === 'es' ? 'es-ES' : 'en-GB')}</time></small>;
}

function History({entries = []}: {entries?: Change[]}) {
  if (!entries.length) return null;
  return <details className="register-history">
    <summary>{copy.history} ({entries.length})</summary>
    <ol>{entries.slice().reverse().map(event => <li key={event.message_id}>
      <strong>{copy.actions[event.action]}</strong>
      <Stamp author={event.author} at={event.created}/>
      {event.before_text !== null && <p><span>{copy.before}</span>{event.before_text}</p>}
      {event.after_text !== null && <p><span>{copy.after}</span>{event.after_text}</p>}
    </li>)}</ol>
  </details>;
}

export function DecisionRegister() {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [tab, setTab] = useState<Tab>('threads');
  const [threads, setThreads] = useState<Thread[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [detail, setDetail] = useState<Detail>();
  const [editing, setEditing] = useState<Action>();
  const [text, setText] = useState('');
  const [threadNumber, setThreadNumber] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [receipts, setReceipts] = useState<Receipt[]>([]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : copy.failure);
    } finally {
      setBusy(false);
    }
  }

  function showList(next: Tab) {
    setTab(next);
    setDetail(undefined);
    setEditing(undefined);
    setNotice('');
    void run(async () => {
      if (next === 'threads') {
        const data = await api<{threads: Thread[]}>('/api/agora/threads?include_tasks=true&limit=200');
        setThreads(data.threads);
      } else {
        const data = await api<{decisions: Decision[]}>('/api/agora/decisions');
        setDecisions(data.decisions);
      }
    });
  }

  function openThread(id: number) {
    setNotice('');
    void run(async () => setDetail(await api<Detail>('/api/agora/threads/' + id)));
  }

  function begin(action: Action) {
    if (!detail) return;
    setText(action === 'retitle' ? detail.thread.title : detail.thread.resolution || '');
    setEditing(action);
    setNotice('');
  }

  function change(action: Action | 'undo', undoToken?: string) {
    if (!detail) return;
    const id = detail.thread.id;
    const routes = {retitle: 'thread_retitle', amend: 'decision_amend', withdraw: 'decision_withdraw', undo: 'decision_history_undo'};
    const fields = action === 'retitle' ? {title: text} : action === 'amend' ? {resolution: text} :
      action === 'undo' ? {undo_token: undoToken} : {};
    void run(async () => {
      const result = await api<Mutation>('/api/agora/' + routes[action], {thread_id: id, ...fields});
      const {change, undo_token} = result;
      if (change && undo_token) {
        setReceipts(previous => [...previous, {thread_id: id, token: undo_token, change}]);
      }
      if (action === 'undo') setReceipts(previous => previous.filter(receipt => receipt.token !== undoToken));
      setEditing(undefined);
      setNotice(action === 'undo' ? copy.undone : copy.updated);
      setDetail(await api<Detail>('/api/agora/threads/' + id));
    });
  }

  const current = detail?.thread;
  const auditIds = new Set(detail?.history?.map(event => event.message_id));
  const undoable = receipts.filter(receipt => receipt.thread_id === current?.id);
  const latest = (['title', 'resolution'] as const).map(field =>
    undoable.slice().reverse().find(receipt => receipt.change.field === field)).filter(receipt => receipt !== undefined);

  return <>
    <button ref={trigger} type="button" className="icon-button register-trigger" aria-label={copy.register}
      title={copy.register} onClick={() => { dialog.current?.showModal(); showList(tab); }}>
      <BookOpen size={17}/>
    </button>
    <dialog ref={dialog} className="register-dialog" aria-labelledby="register-title"
      onClose={() => trigger.current?.focus()}>
      <header>
        <h2 id="register-title">{copy.register}</h2>
        <button type="button" className="icon-button" aria-label={copy.close} onClick={() => dialog.current?.close()}>
          <X size={19}/>
        </button>
      </header>
      <nav aria-label={copy.register}>
        <button type="button" aria-pressed={tab === 'threads'} disabled={busy} onClick={() => showList('threads')}>{copy.threads}</button>
        <button type="button" aria-pressed={tab === 'decisions'} disabled={busy} onClick={() => showList('decisions')}>{copy.decisions}</button>
      </nav>
      <div className="register-content" aria-busy={busy}>
        {error && <div className="register-error" role="alert"><strong>{copy.failure}</strong><span>{error}</span>
          <button type="button" onClick={() => current ? openThread(current.id) : showList(tab)}>{copy.retry}</button>
        </div>}
        {busy && <p role="status">{copy.loading}</p>}
        {notice && <p role="status">{notice}</p>}
        {!current && tab === 'threads' && <form className="register-lookup"
          onSubmit={event => { event.preventDefault(); openThread(Number(threadNumber)); }}>
          <label>{copy.threadNumber}<input type="number" min="1" step="1" required value={threadNumber}
            onChange={event => setThreadNumber(event.target.value)}/></label>
          <button type="submit" disabled={busy || !threadNumber}>{copy.openThread}</button>
        </form>}
        {current && detail ? <section>
          <button type="button" disabled={busy} onClick={() => showList(tab)}>{copy.back}</button>
          <h3>{current.title}</h3>
          {current.withdrawn && <p>{copy.withdrawn}</p>}
          {current.kind !== 'task' && current.status === 'resolved' && current.resolution &&
            <p className="register-text"><strong>{copy.current}</strong>{current.resolution}</p>}
          <div className="register-actions">
            <button type="button" disabled={busy} onClick={() => begin('retitle')}>{copy.retitle}</button>
            {current.kind !== 'task' && current.status === 'resolved' && current.resolution && <>
              <button type="button" disabled={busy} onClick={() => begin('amend')}>{copy.amend}</button>
              <button type="button" disabled={busy} onClick={() => begin('withdraw')}>{copy.withdraw}</button>
            </>}
            {latest.map(receipt => <button key={receipt.token} type="button" disabled={busy}
              onClick={() => change('undo', receipt.token)}>{copy.undo} · {copy.actions[receipt.change.action]}</button>)}
          </div>
          {editing && <form className="register-form" onSubmit={event => { event.preventDefault(); change(editing); }}>
            {editing === 'withdraw' ? <p>{copy.confirm}</p> : <label>
              {editing === 'retitle' ? copy.title : copy.resolution}
              {editing === 'retitle'
                ? <input autoFocus required value={text} maxLength={200} onChange={event => setText(event.target.value)}/>
                : <textarea autoFocus required value={text} maxLength={20000} onChange={event => setText(event.target.value)}/>}
            </label>}
            <div className="register-actions">
              <button type="submit" className="primary" disabled={busy || (editing !== 'withdraw' && !text.trim())}>
                {editing === 'withdraw' ? copy.withdraw : copy.save}
              </button>
              <button type="button" disabled={busy} onClick={() => setEditing(undefined)}>{copy.cancel}</button>
            </div>
          </form>}
          <History entries={detail.history}/>
          <ol className="register-messages">{detail.messages.filter(message => !auditIds.has(message.id)).map(message =>
            <li key={message.id}><Stamp author={message.author} at={message.created}/><p>{message.body}</p></li>)}</ol>
        </section> : tab === 'threads' ? <ul className="register-list">
          {!busy && !threads.length && <li>{copy.emptyThreads}</li>}
          {threads.map(thread => <li key={thread.id}>
            <button type="button" disabled={busy} onClick={() => openThread(thread.id)}>#{thread.id} · {thread.title}</button>
          </li>)}
        </ul> : <ul className="register-list">
          {!busy && !decisions.length && <li>{copy.emptyDecisions}</li>}
          {decisions.map(decision => <li key={decision.id}>
            <button type="button" disabled={busy} onClick={() => openThread(decision.id)}>#{decision.id} · {decision.title}</button>
            {decision.withdrawn ? <p>{copy.withdrawn}</p> :
              <p className="register-text"><strong>{copy.current}</strong>{decision.resolution}</p>}
            <History entries={decision.history}/>
          </li>)}
        </ul>}
      </div>
    </dialog>
  </>;
}

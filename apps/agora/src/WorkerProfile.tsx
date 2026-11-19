import {useEffect, useState} from 'react';
import {ArrowLeft, Settings2} from 'lucide-react';
import {api, spaceId} from './api';
import './worker-profile.css';

const profileRoute = '/api/agora/console/space-profile';
const jobsRoute = '/api/agora/console/auto-review';

type Language = 'es' | 'en';
type Mode = 'build' | 'probe' | 'review' | 'integrate';
type Template = Record<Language, {label: string; hint: string}>;
export type WorkerTemplates = Record<Mode, Template>;
type Profile = {
  revision: number;
  rules: string;
  context: string;
  folders: string[];
  repositories: string[];
  auto_review: boolean;
  review_runner?: Partial<{provider: string; model: string; effort: string}>;
};
type ProfileReply = {profile: Profile; templates: WorkerTemplates};
type Job = {
  id: number;
  kind: 'review' | 'fix';
  task_id: number;
  revision: number;
  state: string;
  worker_state?: string;
  agent_id?: string;
  detail: string;
};

const copy = {
  es: {
    back: 'Volver al espacio', profile: 'Perfil del espacio', rules: 'Reglas permanentes',
    context: 'Contexto del proyecto', folders: 'Carpetas', repositories: 'Repositorios', pathHelp: 'Una ruta por línea.',
    save: 'Guardar perfil', saving: 'Guardando…', saved: 'Perfil guardado.', reload: 'Cargar perfil guardado',
    loadError: 'No se pudo cargar el perfil.', saveError: 'No se pudo guardar el perfil.',
    revisionError: 'El perfil cambió. Vuelve a cargarlo antes de guardar.',
    pathsError: 'Escribe hasta 12 rutas por campo, con un máximo de 260 caracteres por ruta.',
    lengthError: 'Acorta el perfil hasta un máximo de 8000 caracteres.',
    loading: 'Cargando perfil…', review: 'Revisión automática', enable: 'Revisar automáticamente las nuevas entregas',
    automationHelp: 'El Ágora busca un revisor de otro proveedor con cuota medida. Si pide cambios y el autor no está, arranca un relevo.',
    reviewProvider: 'Proveedor de revisión', reviewModel: 'Identificador del modelo de revisión',
    reviewEffort: 'Esfuerzo de revisión', reviewEconomical: 'Automático (bajo coste)',
    reviewRunnerError: 'Indica un modelo y un esfuerzo compatibles con el proveedor de revisión.',
    effortLabels: {minimal: 'Mínimo', low: 'Bajo', medium: 'Medio', high: 'Alto', xhigh: 'Muy alto', max: 'Máximo', default: 'Predeterminado'},
    reviews: 'Revisiones y relevos', empty: 'Aún no hay revisiones automáticas.', jobsLoading: 'Cargando revisiones…',
    reviewJob: 'Revisión', fixJob: 'Relevo', cancel: 'Cancelar turno', cancelling: 'Cancelando…',
    jobsError: 'No se pudieron cargar las revisiones.', cancelError: 'No se pudo cancelar el turno.',
    template: 'Tipo de trabajo', free: 'Encargo libre', detail: 'Encargo', freeHint: 'Una tarea concreta para el ayudante',
    quotaFailed: 'La última consulta de cuota de Codex no se pudo verificar.',
    states: {
      queued: 'En cola', waiting_author: 'El autor sigue presente', waiting_quota: 'Sin proveedor disponible con cuota medida',
      starting: 'Arrancando', handover: 'Esperando el primer latido para el traspaso', launched: 'Sesión iniciada',
      running: 'En curso', stopping: 'Deteniendo', stopped: 'Detenido', completed: 'Resultado comprobado',
      incomplete: 'Resultado incompleto', failed: 'Interrumpido', unavailable: 'No disponible',
      superseded: 'La tarea cambió', cancelled: 'Cancelado', unknown: 'Estado por confirmar',
      skipped: 'Sin proveedor de autor identificado', profile_disabled: 'Automatización desactivada',
      launch_unconfirmed: 'El arranque quedó sin confirmar', launch_failed: 'El proveedor no pudo iniciar la sesión',
      handover_failed: 'Falló el traspaso de la tarea', worker_ended_before_handover: 'La sesión terminó antes del traspaso',
      dispatch_error: 'No se pudo preparar el encargo', no_agent_owner: 'La tarea no tiene un agente asignado',
      author_returned: 'El autor ha vuelto antes del traspaso',
    },
  },
  en: {
    back: 'Back to space', profile: 'Space profile', rules: 'Permanent rules',
    context: 'Project context', folders: 'Folders', repositories: 'Repositories', pathHelp: 'One path per line.',
    save: 'Save profile', saving: 'Saving…', saved: 'Profile saved.', reload: 'Load saved profile',
    loadError: 'Could not load the profile.', saveError: 'Could not save the profile.',
    revisionError: 'The profile changed. Reload it before saving.',
    pathsError: 'Enter up to 12 paths per field, with up to 260 characters per path.',
    lengthError: 'Shorten the profile to 8000 characters or fewer.',
    loading: 'Loading profile…', review: 'Automatic review', enable: 'Automatically review new submissions',
    automationHelp: 'Agora looks for a reviewer from another provider with measured quota. If the reviewer requests changes and the author is away, Agora starts a replacement.',
    reviewProvider: 'Review provider', reviewModel: 'Review model ID',
    reviewEffort: 'Review effort', reviewEconomical: 'Automatic (low cost)',
    reviewRunnerError: 'Enter a model and effort supported by the review provider.',
    effortLabels: {minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Very high', max: 'Maximum', default: 'Default'},
    reviews: 'Reviews and handovers', empty: 'No automatic reviews yet.', jobsLoading: 'Loading reviews…',
    reviewJob: 'Review', fixJob: 'Handover', cancel: 'Cancel turn', cancelling: 'Cancelling…',
    jobsError: 'Could not load the reviews.', cancelError: 'Could not cancel the turn.',
    template: 'Work type', free: 'Custom assignment', detail: 'Assignment', freeHint: 'A concrete task for the helper',
    quotaFailed: 'The latest Codex quota check could not be verified.',
    states: {
      queued: 'Queued', waiting_author: 'Author still present', waiting_quota: 'No available provider with measured quota',
      starting: 'Starting', handover: 'Waiting for the first heartbeat before transfer', launched: 'Session started',
      running: 'Running', stopping: 'Stopping', stopped: 'Stopped', completed: 'Result verified',
      incomplete: 'Incomplete result', failed: 'Interrupted', unavailable: 'Unavailable',
      superseded: 'Task changed', cancelled: 'Cancelled', unknown: 'State unconfirmed',
      skipped: 'Could not identify the author’s provider', profile_disabled: 'Automation disabled',
      launch_unconfirmed: 'Worker start could not be confirmed', launch_failed: 'The provider could not start the session',
      handover_failed: 'Task handover failed', worker_ended_before_handover: 'The session ended before handover',
      dispatch_error: 'Could not prepare the assignment', no_agent_owner: 'The task has no agent owner',
      author_returned: 'The author returned before the handover',
    },
  },
};

const lang: Language = (new URLSearchParams(location.search).get('lang') || localStorage.getItem('hub.lang')) === 'en' ? 'en' : 'es';
const words = copy[lang];
const stateLabels: Record<string, string> = words.states;

function saveError(cause: unknown) {
  const message = (cause as Error).message || '';
  if (message.includes('profile changed')) return words.revisionError;
  if (message.includes('single-line paths')) return words.pathsError;
  if (message.includes('8000 characters')) return words.lengthError;
  if (message.includes('Review runner')) return words.reviewRunnerError;
  return words.saveError;
}

export const loadWorkerProfile = () => api<ProfileReply>(profileRoute);

export function WorkerProfileButton({onClick}: {onClick: () => void}) {
  return <button className="icon-button worker-profile-trigger" aria-label={words.profile} title={words.profile} onClick={onClick}>
    <Settings2 size={17}/>
  </button>;
}

export function WorkerAssignment({templates, mode, setMode, task, setTask}: {
  templates?: WorkerTemplates; mode: string; setMode: (value: string) => void;
  task: string; setTask: (value: string) => void;
}) {
  const hint = mode && templates?.[mode as Mode]?.[lang].hint;
  return <>
    {templates && <label>{words.template}<select value={mode} onChange={event => setMode(event.target.value)}>
      <option value="">{words.free}</option>
      {Object.entries(templates).map(([id, label]) => <option key={id} value={id}>{label[lang].label}</option>)}
    </select></label>}
    <label>{words.detail}<textarea maxLength={12000} value={task} onChange={event => setTask(event.target.value)}
      placeholder={hint || words.freeHint}/></label>
  </>;
}

export function CodexQuotaNotice() {
  return <p className="worker-quota-notice">{words.quotaFailed}</p>;
}

function canCancel(job: Job) {
  return ['waiting_quota', 'waiting_author', 'queued', 'starting', 'handover'].includes(job.state) ||
    Boolean(job.worker_state && ['running', 'unknown', 'starting'].includes(job.worker_state));
}

export function WorkerProfile({onClose}: {onClose: () => void}) {
  const [profile, setProfile] = useState<Profile>();
  const [jobs, setJobs] = useState<Job[]>();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const [jobError, setJobError] = useState('');
  const [cancelling, setCancelling] = useState<number>();

  async function load() {
    setLoading(true);
    setError('');
    setSaved(false);
    try { setProfile((await loadWorkerProfile()).profile); }
    catch { setError(words.loadError); }
    finally { setLoading(false); }
  }

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    let current = true;
    let fetching = false;
    async function refresh() {
      if (fetching) return;
      fetching = true;
      try {
        const response = await api<{jobs: Job[]}>(jobsRoute);
        if (current) { setJobs(response.jobs); setJobError(''); }
      } catch {
        if (current) setJobError(words.jobsError);
      } finally { fetching = false; }
    }
    void refresh();
    const poll = window.setInterval(refresh, 3000);
    return () => { current = false; window.clearInterval(poll); };
  }, []);

  function update(change: Partial<Profile>) {
    setProfile(previous => previous && {...previous, ...change});
    setSaved(false);
  }

  async function submit() {
    if (!profile || saving) return;
    setSaving(true);
    setError('');
    const {revision, ...values} = profile;
    try {
      const response = await api<ProfileReply>(profileRoute, {expected_revision: revision, profile: values});
      setProfile(response.profile);
      setSaved(true);
    } catch (cause) { setError(saveError(cause)); }
    finally { setSaving(false); }
  }

  async function cancel(job: Job) {
    if (cancelling !== undefined) return;
    setCancelling(job.id);
    setJobError('');
    try { setJobs((await api<{jobs: Job[]}>(jobsRoute, {job_id: job.id})).jobs); }
    catch { setJobError(words.cancelError); }
    finally { setCancelling(undefined); }
  }

  return <div className="worker-profile-page" lang={lang}>
    <header className="worker-profile-header">
      <button className="icon-button" onClick={onClose} autoFocus><ArrowLeft size={17}/>{words.back}</button>
      <span>{spaceId}</span>
    </header>
    <main className="worker-profile-content">
      <section aria-labelledby="worker-profile-title">
        <h1 id="worker-profile-title">{words.profile}</h1>
        {loading && <p role="status">{words.loading}</p>}
        {error && <p role="alert">{error}</p>}
        {profile && <form onSubmit={event => {event.preventDefault(); void submit();}}>
          <fieldset disabled={loading || saving}>
            <label>{words.rules}<textarea rows={7} value={profile.rules} maxLength={3500}
              onChange={event => update({rules: event.target.value})}/></label>
            <label>{words.context}<textarea rows={4} value={profile.context} maxLength={2500}
              onChange={event => update({context: event.target.value})}/></label>
            <div className="worker-profile-paths">
              <label>{words.folders}<textarea rows={3} value={profile.folders.join('\n')} aria-describedby="worker-profile-path-help"
                onChange={event => update({folders: event.target.value.split('\n')})}/></label>
              <label>{words.repositories}<textarea rows={3} value={profile.repositories.join('\n')} aria-describedby="worker-profile-path-help"
                onChange={event => update({repositories: event.target.value.split('\n')})}/></label>
            </div>
            <p id="worker-profile-path-help">{words.pathHelp}</p>
            <div className="worker-profile-auto">
              <h2>{words.review}</h2>
              <label><input type="checkbox" checked={profile.auto_review} aria-describedby="worker-profile-auto-help"
                onChange={event => update({auto_review: event.target.checked})}/>{words.enable}</label>
              <p id="worker-profile-auto-help">{words.automationHelp}</p>
              <label>{words.reviewProvider}
                <select value={profile.review_runner?.provider || ''} onChange={event => update({
                  review_runner: event.target.value ? {provider: event.target.value, model: '', effort: 'low'} : {},
                })}>
                  <option value="">{words.reviewEconomical}</option>
                  <option value="claude">Claude</option>
                  <option value="codex">Codex</option>
                  <option value="cursor">Cursor</option>
                  <option value="faustus">Faustus</option>
                </select>
              </label>
              {profile.review_runner?.provider && <div className="worker-profile-paths">
                <label>{words.reviewModel}<input required maxLength={120} value={profile.review_runner.model || ''}
                  onChange={event => update({review_runner: {...profile.review_runner, model: event.target.value}})}/></label>
                <label>{words.reviewEffort}
                  <select value={profile.review_runner.effort || 'low'}
                    onChange={event => update({review_runner: {...profile.review_runner, effort: event.target.value}})}>
                    {Object.entries(words.effortLabels).map(([effort, label]) =>
                      <option key={effort} value={effort}>{label}</option>)}
                  </select>
                </label>
              </div>}
            </div>
          </fieldset>
          <div className="worker-profile-actions">
            <button className="primary" type="submit" disabled={saving || loading || saved}>{saving ? words.saving : words.save}</button>
            {saved && <span role="status">{words.saved}</span>}
          </div>
        </form>}
        {error && <button className="icon-button" type="button" onClick={() => void load()}>{words.reload}</button>}
      </section>
      <section className="worker-profile-jobs" aria-labelledby="worker-profile-jobs-title">
        <h2 id="worker-profile-jobs-title">{words.reviews}</h2>
        {jobError && <p role="alert">{jobError}</p>}
        {!jobs && !jobError && <p role="status">{words.jobsLoading}</p>}
        {jobs?.length === 0 && <p>{words.empty}</p>}
        <ul>{jobs?.map(job => <li key={job.id}>
          <div className="worker-profile-job-heading">
            <strong>{job.kind === 'review' ? words.reviewJob : words.fixJob} · #{job.task_id} r{job.revision}</strong>
            {canCancel(job) && <button className="icon-button" type="button" disabled={cancelling !== undefined}
              onClick={() => void cancel(job)}>{cancelling === job.id ? words.cancelling : words.cancel}</button>}
          </div>
          <p>{stateLabels[job.state === 'launched' ? job.worker_state || 'launched' : job.state] || job.state}
            {job.agent_id && <> · {job.agent_id}</>}</p>
          {stateLabels[job.detail] && <p>{stateLabels[job.detail]}</p>}
        </li>)}</ul>
      </section>
    </main>
  </div>;
}

import {useState,type FormEvent} from 'react';
import {api,providerNames} from './api';
import {lang,locale,tr} from './locale';

type Window = {
  window_id: string;
  label?: string;
  source?: string;
  remaining_percent?: number | null;
  reset_at?: string | number;
  reported_at?: string;
  measurement?: string;
  stale?: boolean;
  window_duration_mins?: number;
};

export type QuotaProvider = {
  provider_id: string;
  pools: {
    pool_id: string;
    pool_label?: string;
    source?: string;
    agent_ids?: string[];
    shared_agent_count?: number;
    reported_at?: string;
    stale?: boolean;
    windows: Window[];
  }[];
};

export type QuotaFloorState = {
  revision: number;
  floors: Record<string,number | undefined>;
  providers: Record<string,{weekly?: {remaining_percent: number | null; reported_at: string | null}} | undefined>;
};
type FloorWrite = {expected_revision: number; floors: QuotaFloorState['floors']};
const floorRoute = '/api/agora/console/quota_floors';
const weeklyProviders = ['claude','codex'];
const unmeasured = tr('Sin medir','Unmeasured');
const number = new Intl.NumberFormat(locale,{maximumFractionDigits:2});
const time = new Intl.DateTimeFormat(locale,{hour:'2-digit',minute:'2-digit'});

export function confirmQuotaFloor(policy:QuotaFloorState|null, provider:string) {
  const floor = policy?.floors[provider];
  const remaining = policy?.providers[provider]?.weekly?.remaining_percent;
  if (floor == null || remaining == null || remaining >= floor) return undefined;
  const name = providerNames[provider] || provider;
  const confirmed = window.confirm(tr(
    `${name} tiene ${number.format(remaining)}% de cuota semanal libre. El suelo es ${number.format(floor)}%. ¿Crear el ayudante de todas formas?`,
    `${name} has ${number.format(remaining)}% of weekly quota left. The floor is ${number.format(floor)}%. Create the worker anyway?`));
  return confirmed ? {provider_id:provider, floor_percent:floor} : null;
}

export function WeeklyQuotaFloor({policy,provider}:{policy:QuotaFloorState|null;provider:string}) {
  const floor=policy?.floors[provider];
  if (floor == null) return null;
  const remaining=policy?.providers[provider]?.weekly?.remaining_percent;
  const below=remaining != null && remaining < floor;
  return <p className={below?'weekly-quota-floor is-below':'weekly-quota-floor'}>
    {tr('Suelo semanal','Weekly floor')} {number.format(floor)}%
    {remaining==null?' · '+unmeasured:below?' · '+tr('Por debajo del suelo','Below floor'):''}
  </p>;
}

export function QuotaFloorSettings({policy,refresh}:{policy:QuotaFloorState;refresh:()=>Promise<void>}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const [undo,setUndo]=useState<FloorWrite>();
  async function write(values:FloorWrite, undoing=false) {
    setBusy(true);setError('');
    try {
      const result=await api<{undo?:{arguments:FloorWrite}}>(floorRoute,values);
      if (undoing) setUndo(undefined);
      else if (result.undo) setUndo(result.undo.arguments);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
      await refresh();
    } finally {setBusy(false);}
  }
  function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data=new FormData(event.currentTarget);
    const floors:FloorWrite['floors']={};
    for (const id of weeklyProviders) {
      const value=data.get(id);
      if (typeof value==='string' && value.trim()) floors[id]=Number(value);
    }
    void write({expected_revision:policy.revision,floors});
  }
  return <details id="quota-floors" className="quota-settings" open={location.hash==='#quota-floors'||undefined}>
    <summary>{tr('Suelos de cuota del espacio','Space quota floors')}</summary>
    <form key={policy.revision} onSubmit={submit}>
      <small>{tr('Porcentaje semanal libre. Deja vacío para quitar el suelo.','Weekly percentage remaining. Leave blank to remove a floor.')}</small>
      {weeklyProviders.map(id=><label key={id}>{providerNames[id]} <input type="number" min="0" max="100" step="any"
        inputMode="decimal" name={id} defaultValue={policy.floors[id]}/></label>)}
      <div className="quota-settings-actions">
        <button className="primary" disabled={busy}>{tr('Guardar','Save')}</button>
        {undo?.expected_revision===policy.revision&&
          <button className="icon-button" type="button" disabled={busy} onClick={()=>void write(undo,true)}>{tr('Deshacer','Undo')}</button>}
      </div>
      {error&&<small role="alert">{error}</small>}
    </form>
  </details>;
}

// Existing snapshots already store these collector labels. Keep custom labels verbatim.
const quotaLabels = new Map(Object.entries({
  'Cuenta de Claude': 'Claude account',
  'Cuenta de Cursor': 'Cursor account',
  'Sesión (5 h)': 'Session (5 h)',
  'Semana · todos los modelos': 'Week · all models',
  'Plan sin límite': 'Unlimited plan',
  'Modelos de Cursor (Auto, Composer, Grok)': 'Cursor models (Auto, Composer, Grok)',
  'Otros modelos': 'Other models',
  'Incluido en el plan': 'Included in the plan',
  'Bajo demanda': 'On demand',
  'No se pudo identificar la cuenta': 'Could not identify the account',
  'Claude Code no ha iniciado sesión en este PC': 'Claude Code is signed out on this computer',
  'No hay una sesión guardada de Claude Code': 'No Claude Code sign-in is stored',
  'No se pudieron leer las credenciales de Claude Code': 'Could not read Claude Code credentials',
  'La sesión de Claude Code no permite leer la cuota (falta user:profile)': 'The Claude Code session cannot read quota (missing user:profile)',
  'Hay que comprobar si Claude Code puede renovar el acceso': 'Claude Code access renewal needs checking',
  'El acceso guardado de Claude Code ha caducado': 'The stored Claude Code access token has expired',
  'Claude ha rechazado el acceso a la cuota': 'Claude denied access to quota',
  'Respuesta de cuota de Claude no válida': 'Invalid Claude quota response',
  'Claude no devolvió ventanas de cuota': 'Claude returned no quota windows',
  'Claude Code indica que no hay una sesión iniciada': 'Claude Code reports that it is signed out',
  'Claude Code intentó renovar el acceso y recibió un rechazo': 'Claude Code access renewal was rejected',
  'No se pudo verificar la renovación del acceso a la cuota de Claude': 'Could not verify renewed access to Claude quota',
  'La app de Cursor no ha iniciado sesión en este PC': 'The Cursor app is signed out on this computer',
  'No se pudo leer la sesión de la app de Cursor': 'Could not read the Cursor app session',
  'Sesión de la app de Cursor caducada: ábrela para renovarla': 'The Cursor app session expired. Open the app to renew it',
  'No se pudo identificar la cuenta de Cursor': 'Could not identify the Cursor account',
  'Respuesta de uso de Cursor no válida': 'Invalid Cursor usage response',
  'Cursor no devolvió cuotas del plan': 'Cursor returned no plan quotas',
  'No se pudo leer el uso de Cursor ahora': 'Could not read Cursor usage just now',
}));

function windowLabel(window: Window, poolLabel?: string) {
  if (['codex.primary','codex.secondary'].includes(window.window_id) && window.window_duration_mins === 7*24*60) {
    return tr('Semana · todos los modelos','Week · all models');
  }
  const label = window.label || poolLabel;
  if (!label) return tr('Cuenta compartida', 'Shared account');
  if (lang === 'es') {
    if (label === 'Codex account') return 'Cuenta de Codex';
    const codex = /^(.*)\.(primary|secondary)$/.exec(window.window_id);
    if (codex && label === `${codex[1]} ${codex[2]}`) return `${codex[1]} · ${codex[2] === 'primary' ? 'principal' : 'secundaria'}`;
    return label;
  }
  return quotaLabels.get(label) ?? (window.window_id.startsWith('claude.week-') ? label.replace(/^Semana · /, 'Week · ') : label);
}

export function Quota({provider}:{provider?:QuotaProvider}) {
  if (!provider?.pools?.length) return <div className="quota unknown">
    <div className="quota-label"><span>{tr('Cuota sin datos', 'No quota data')}</span><strong>{unmeasured}</strong></div>
    <div className="meter"/>
  </div>;
  return provider.pools.map(pool => <div className="quota" key={pool.pool_id}>
    {(pool.windows || []).map(window => {
      const label = windowLabel(window, pool.pool_label);
      const remaining = pool.stale || window.stale ? null : window.remaining_percent;
      const used = remaining == null ? null : Math.round(100 - remaining);
      const count = pool.shared_agent_count ?? pool.agent_ids?.length;
      const shared = count === 1 ? tr('1 agente comparte esta cuota', '1 agent shares this quota') :
        tr(`${count || 'Varios'} agentes comparten esta cuota`, `${count || 'Several'} agents share this quota`);
      const observed = window.reported_at || pool.reported_at;
      const notes = [
        observed && (remaining != null && window.measurement === 'measured' ? tr('Medido ', 'Measured ') :
          tr('Última consulta ', 'Last checked ')) + time.format(new Date(observed)),
        pool.stale || window.stale ? tr('Dato caducado', 'Stale data') :
          window.measurement === 'declared' ? tr('Declarado', 'Declared') : '',
        remaining == null ? '' : tr(`${number.format(remaining)}% libre`,`${number.format(remaining)}% remaining`),
        window.reset_at && tr('reinicia ', 'resets ') + new Date(typeof window.reset_at === 'number' ? window.reset_at * 1000 : window.reset_at).toLocaleDateString(locale,{day:'2-digit',month:'2-digit'}),
      ].filter(Boolean);
      const name = providerNames[provider.provider_id] || provider.provider_id;
      return <div key={window.window_id} title={[window.source || pool.source, shared].filter(Boolean).join(' · ')}>
        <div className="quota-label"><span>{label}</span><strong>{used == null ? unmeasured : tr(`${used}% usado`,`${used}% used`)}</strong></div>
        <div className="meter" role={used == null ? undefined : 'meter'} aria-label={tr(`${name}: ${label} usada`, `${name}: ${label} used`)}
          aria-valuenow={used ?? undefined} aria-valuemin={0} aria-valuemax={100}>
          <span className={remaining != null && remaining <= 10 ? 'full' : ''} style={{width:`${remaining == null ? 0 : 100 - remaining}%`}}/>
        </div>
        <small>{notes.join(' · ')}</small>
      </div>;
    })}
  </div>);
}

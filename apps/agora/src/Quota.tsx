import {providerNames} from './api';
import {lang,locale,tr} from './locale';

type Window = {
  window_id: string;
  label?: string;
  source?: string;
  remaining_percent?: number | null;
  reset_at?: string | number;
  measurement?: string;
  stale?: boolean;
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
  'No se pudieron leer las credenciales de Claude Code': 'Could not read Claude Code credentials',
  'La sesión de Claude Code no permite leer la cuota (falta user:profile)': 'The Claude Code session cannot read quota (missing user:profile)',
  'Hay que comprobar si Claude Code puede renovar el acceso': 'Claude Code access renewal needs checking',
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
    <span>{tr('Cuota sin datos', 'No quota data')}</span><div className="meter"/>
  </div>;
  return provider.pools.map(pool => <div className="quota" key={pool.pool_id}>
    {(pool.windows || []).map(window => {
      const label = windowLabel(window, pool.pool_label);
      const remaining = window.remaining_percent;
      const used = remaining == null ? null : Math.round(100 - remaining);
      const count = pool.shared_agent_count ?? pool.agent_ids?.length;
      const shared = count === 1 ? tr('1 agente comparte esta cuota', '1 agent shares this quota') :
        tr(`${count || 'Varios'} agentes comparten esta cuota`, `${count || 'Several'} agents share this quota`);
      const notes = [
        pool.reported_at && tr('Actualizado ', 'Updated ') + new Date(pool.reported_at).toLocaleTimeString(locale, {hour:'2-digit',minute:'2-digit'}),
        pool.stale || window.stale ? tr('Dato caducado', 'Stale data') : window.measurement === 'declared' ?
          tr('Declarado', 'Declared') : remaining == null ? '' : tr(`${Math.round(remaining)}% libre`, `${Math.round(remaining)}% remaining`),
        window.reset_at && tr('reinicia ', 'resets ') + new Date(typeof window.reset_at === 'number' ? window.reset_at * 1000 : window.reset_at).toLocaleDateString(locale,{day:'2-digit',month:'2-digit'}),
      ].filter(Boolean);
      const name = providerNames[provider.provider_id] || provider.provider_id;
      return <div key={window.window_id} title={[window.source || pool.source, shared].filter(Boolean).join(' · ')}>
        <div className="quota-label"><span>{label}</span><strong>{used == null ? tr('Sin datos','Unknown') : tr(`${used}% usado`,`${used}% used`)}</strong></div>
        <div className="meter" role={used == null ? undefined : 'meter'} aria-label={tr(`${name}: ${label} usada`, `${name}: ${label} used`)}
          aria-valuenow={used ?? undefined} aria-valuemin={0} aria-valuemax={100}>
          <span className={remaining != null && remaining <= 10 ? 'full' : ''} style={{width:`${remaining == null ? 0 : 100 - remaining}%`}}/>
        </div>
        <small>{notes.join(' · ')}</small>
      </div>;
    })}
  </div>);
}

import {locale, tr} from './locale';

export type ForemanStatus = {
  agent_id: string;
  last_heartbeat_at: number | null;
  seconds_since_heartbeat: number | null;
  overdue: boolean;
  outage_thread_id: number | null;
};

const relativeTime = new Intl.RelativeTimeFormat(locale);
const absoluteTime = new Intl.DateTimeFormat(locale, {dateStyle: 'short', timeStyle: 'short'});
const timeUnits = [[86400, 'day'], [3600, 'hour'], [60, 'minute'], [1, 'second']] as const;

function formatAge(seconds: number) {
  if (seconds === 0) return tr('ahora mismo', 'just now');
  for (const [size, unit] of timeUnits) {
    if (seconds >= size) return relativeTime.format(-Math.floor(seconds / size), unit);
  }
}

export function Foreman({status}: {status: ForemanStatus}) {
  const last = status.last_heartbeat_at === null ? null : new Date(status.last_heartbeat_at * 1000);
  const outage = status.outage_thread_id !== null;
  return (
    <section className="foreman-status" aria-label={tr('Latido del capataz', 'Foreman heartbeat')}>
      <div>
        <strong>{tr('Capataz', 'Foreman')}</strong>{' '}
        <span>{status.agent_id}</span>
      </div>
      <div>
        {last ? <>
          {tr('Último latido', 'Last heartbeat')}{' '}
          <time dateTime={last.toISOString()}>{absoluteTime.format(last)}</time>
          {status.seconds_since_heartbeat !== null && <> · {formatAge(status.seconds_since_heartbeat)}</>}
        </> : tr('Aún no se ha recibido un latido', 'No heartbeat received yet')}
      </div>
      <div role="status" className="foreman-warning">
        {outage ? tr('Aviso de interrupción abierto', 'Outage alert open')
          : status.overdue ? tr('Latido atrasado', 'Heartbeat overdue') : ''}
      </div>
    </section>
  );
}

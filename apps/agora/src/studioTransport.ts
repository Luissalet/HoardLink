import {lang, spaceId} from './api';
import './studio-transport.css';

const root = document.documentElement;
export const inStudio = root.hasAttribute('data-studio-write');
export const studioOrderBase = root.dataset.studioOrderBase;

export function studioPiece(path: string, media: string): string | null {
  const piece = /^\/api\/agora\/piece_file\/(\d+)(\?preview=\d+)?$/.exec(path);
  if (!piece || media === 'web') return null;
  const route = media === 'image' ? 'piece-preview' : 'piece-content';
  return `/api/agora/${route}/${piece[1]}${piece[2] ?? ''}`;
}

if (inStudio) {
  const spanish = lang === 'es';
  root.lang = lang;
  const session = spanish
    ? 'Inicia sesión en Studio para escribir y guardar decisiones.'
    : 'Sign in to Studio to write messages and save decisions.';
  const readOnly = spanish
    ? 'Aquí solo puedes consultar porque esta instalación de Faustus no tiene inicio de sesión. En el Ágora de Faustus puedes escribir en el canal y decidir sobre las piezas.'
    : 'This panel is read-only because sign-in is disabled on this Faustus installation. Use the Faustus Agora to write in the channel and make decisions about pieces.';
  const offline = spanish
    ? 'No se puede conectar con el Hub. Puedes conservar el borrador y volver a intentarlo.'
    : 'The Hub is unavailable. You can keep your draft and try again.';
  const signInDisabled = root.dataset.studioWrite === 'disabled';
  const writeNotice = signInDisabled ? readOnly : session;
  const pending = document.createElement('a');
  pending.href = '/agora?' + new URLSearchParams({space_id: spaceId, c: 'v:pending-decisions'});
  pending.target = '_top';
  pending.textContent = spanish
    ? 'Tus decisiones pendientes en el Ágora de Faustus'
    : 'Your pending decisions in the Faustus Agora';
  const status = document.createElement('p');
  status.className = 'studio-console-status';
  status.setAttribute('role', 'status');
  let visibleMessage: string | undefined;
  const show = (message: string) => {
    if (message === visibleMessage) return;
    visibleMessage = message;
    status.textContent = message;
    if (message === readOnly) status.append(' ', pending);
    status.hidden = !message;
  };
  document.body.prepend(status);
  const showWriteNotice = () => show(root.dataset.studioWrite === 'yes' ? '' : writeNotice);
  showWriteNotice();

  // The console API and its components use absolute Hub paths. In the
  // same-origin Studio mount those go through Studio's authenticated relay.
  const send = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const prefix = '/api/agora/';
    if (typeof input !== 'string' || !input.startsWith(prefix)) return send(input, init);
    const write = init?.method === 'POST';
    const path = input.slice(prefix.length);
    const response = await send(prefix + (write ? 'development/' : 'x/') + path, init);
    const data = await response.clone().json().catch(() => null);
    const error = data?.code === 'hub_unreachable' ? offline
      : response.status === 401 || data?.code === 'studio_session_required' ? writeNotice : '';
    if (error) {
      if (error === writeNotice && !signInDisabled) root.dataset.studioWrite = 'no';
      show(error);
      return Response.json({...data, ok: false, error}, {status: response.status});
    }
    if (data?.ok && write) root.dataset.studioWrite = 'yes';
    if (data?.ok && (write || path.split('?', 1)[0] === 'board')) showWriteNotice();
    return response;
  };
}

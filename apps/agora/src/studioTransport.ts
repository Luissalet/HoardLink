import './studio-transport.css';

const root = document.documentElement;
export const inStudio = root.hasAttribute('data-studio-write');

export function studioPiece(path: string, media: string): string | null {
  const piece = /^\/api\/agora\/piece_file\/(\d+)(\?preview=\d+)?$/.exec(path);
  if (!piece || media === 'web') return null;
  const route = media === 'image' ? 'piece-preview' : 'piece-content';
  return `/api/agora/${route}/${piece[1]}${piece[2] ?? ''}`;
}

if (inStudio) {
  const spanish = new URLSearchParams(location.search).get('lang') === 'es';
  root.lang = spanish ? 'es' : 'en';
  const session = spanish
    ? 'Inicia sesión en Studio para escribir y guardar decisiones.'
    : 'Sign in to Studio to write messages and save decisions.';
  const offline = spanish
    ? 'No se puede conectar con el Hub. Puedes conservar el borrador y volver a intentarlo.'
    : 'The Hub is unavailable. You can keep your draft and try again.';
  const status = document.createElement('p');
  status.className = 'studio-console-status';
  status.setAttribute('role', 'status');
  const show = (message: string) => {
    status.textContent = message;
    status.hidden = !message;
  };
  document.body.prepend(status);
  let needsSession = root.dataset.studioWrite !== 'yes';
  show(needsSession ? session : '');

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
      : response.status === 401 || data?.code === 'studio_session_required' ? session : '';
    if (error) {
      if (error === session) needsSession = true;
      show(error);
      return Response.json({...data, ok: false, error}, {status: response.status});
    }
    if (data?.ok && write) needsSession = false;
    if (data?.ok && (write || path.split('?', 1)[0] === 'board')) show(needsSession ? session : '');
    return response;
  };
}

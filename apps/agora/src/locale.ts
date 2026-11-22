export const lang = (new URLSearchParams(location.search).get('lang') ||
  localStorage.getItem('hub.lang') || navigator.language).toLowerCase().startsWith('es') ? 'es' : 'en';
export const tr = (spanish: string, english: string) => lang === 'es' ? spanish : english;
export const locale = tr('es-ES', 'en-GB');

export function spaceTitle(id: string, title?: string) {
  if (id === 'estudio' && title === 'Estudio de producción') return tr(title, 'Production studio');
  if (id === 'ecosystem' && (!title || title === 'Ecosistema' || title === 'Ecosystem')) {
    return tr('Ecosistema', 'Ecosystem');
  }
  return title || id;
}

export function messageBody(message: {agent?: string; author?: string; kind?: string; body: string}) {
  if ((message.author || message.agent) === 'codex-console' && message.kind === 'proposal' &&
    message.body === 'Conversacion de este espacio.') {
    return tr('Conversación de este espacio.', 'Conversation for this space.');
  }
  return message.body;
}

const stylesheet = new URL('./production_decisions_card.css?no-inline', import.meta.url).href;
const mounted = new WeakMap();

function node(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  if (text) element.textContent = text;
  return element;
}

function button(text, className = '') {
  const element = node('button', className, text);
  element.type = 'button';
  return element;
}

function link(text, href) {
  const element = node('a', '', text);
  element.href = href;
  element.target = '_blank';
  element.rel = 'noopener noreferrer';
  return element;
}

function webUrl(value) {
  try {
    const url = new URL(value);
    const loopback = host => ['127.0.0.1', 'localhost', '[::1]'].includes(host);
    if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
        && (!loopback(url.hostname) || loopback(location.hostname))) return url.href;
  } catch { /* An unregistered or invalid URL has no browser link. */ }
  return null;
}

function imagePreview(piece, options, tr) {
  const open = button('', 'production-card__zoom');
  open.setAttribute('aria-label', tr('Ampliar', 'Enlarge') + ' · ' + piece.title);
  const img = node('img', '');
  img.alt = piece.title;
  img.loading = 'lazy';
  open.disabled = true;
  img.addEventListener('load', () => { open.disabled = false; });
  open.append(img);
  const failed = () => { open.hidden = true; };
  img.addEventListener('error', failed);
  Promise.resolve(options.source(piece.path, 'image')).then(src => {
    if (src) img.src = src;
    else failed();
  }).catch(failed);
  open.addEventListener('click', () => {
    const dialog = node('dialog', 'production-card__dialog');
    dialog.setAttribute('aria-label', piece.title);
    const close = button(tr('Cerrar', 'Close'));
    const large = node('img', '');
    large.src = img.src;
    large.alt = piece.title;
    close.onclick = () => dialog.close();
    dialog.append(close, large);
    dialog.addEventListener('close', () => { dialog.remove(); open.focus(); }, {once: true});
    open.after(dialog);
    dialog.showModal();
  });
  return open;
}

function artifactList(pieces, options, tr, heading) {
  const list = node('ul', 'production-card__pieces');
  for (const piece of pieces) {
    const item = node('li', '');
    if (piece.image) item.append(imagePreview(piece.image, options, tr));
    if (piece.title !== heading || !piece.image) item.append(node('span', '', piece.title));
    if (piece.path && piece.media !== 'image') {
      Promise.resolve(options.source(piece.path, piece.media)).then(src => {
        if (!src) return;
        if (src.startsWith('/') && ['video', 'audio'].includes(piece.media)) {
          const player = node(piece.media, '');
          player.src = src;
          player.preload = 'none';
          player.controls = true;
          player.setAttribute('aria-label', piece.title);
          item.append(player);
        }
        item.append(link(tr('Abrir pieza', 'Open piece'), src));
      }).catch(() => {});
    }
    const url = piece.url && webUrl(piece.url);
    if (url) item.append(link(tr('Abrir sitio', 'Open website'), url));
    list.append(item);
  }
  return list;
}

function card(row, options, previousNote) {
  const tr = (es, en) => options.lang === 'es' ? es : en;
  const root = node('article', 'production-card hub-decision-row');
  root.dataset.decision = row.key;
  const title = node('h3', '', row.title);
  title.tabIndex = -1;
  const header = node('header', 'production-card__heading');
  header.append(title, node('p', '', row.order));
  const href = options.orderHref?.(row);
  if (href) {
    const order = link(tr('Abrir la orden', 'Open order'), href);
    if (options.orderInPlace) order.target = '_self';
    if (options.openOrder) order.addEventListener('click', event => options.openOrder(event, row));
    header.append(order);
  }
  root.append(header);

  const label = node('label', 'production-card__note', tr('Nota (opcional)', 'Note (optional)'));
  const note = node('textarea', '');
  note.rows = 2;
  note.maxLength = 20000;
  note.value = previousNote;
  label.append(note);
  const status = node('p', 'production-card__status');
  status.role = 'status';
  const buttons = [];
  const action = (word, decision, variant) => {
    const control = button(word, decision === 'keep' ? 'production-card__keep' : '');
    buttons.push(control);
    if (variant) control.setAttribute('aria-label', word + ' · ' + variant.title);
    control.onclick = async () => {
      buttons.forEach(button => { button.disabled = true; });
      note.readOnly = true;
      status.textContent = tr('Guardando decisión…', 'Saving decision…');
      try {
        await options.post('production_decide', {space_id: row.space, key: row.key, decision, note: note.value.trim(),
          ...(variant ? {variant_id: variant.id, signature: variant.signature} : {})});
        note.value = '';
        status.textContent = tr('Decisión guardada.', 'Decision saved.');
        options.refresh();
      } catch (error) {
        const message = tr('No se ha guardado la decisión. Actualiza la lista e inténtalo de nuevo.', 'The decision was not saved. Refresh the list and try again.');
        status.textContent = message;
        status.title = error instanceof Error ? error.message : '';
        options.refresh();
      } finally {
        note.readOnly = false;
        buttons.forEach(button => { button.disabled = false; });
        if (!root.contains(document.activeElement) && document.activeElement === document.body && root.isConnected) {
          control.focus();
        }
      }
    };
    return control;
  };

  if (row.variants) {
    const variants = node('div', 'production-card__variants');
    for (const variant of row.variants) {
      const group = node('section', 'production-card__variant');
      const name = node('h4', '', variant.title);
      const actions = node('div', 'production-card__actions');
      const pieces = variant.artifacts ?? legacyPieces(variant);
      const primary = pieces.find(piece => piece.image) ?? pieces[0];
      group.append(name);
      if (primary) group.append(artifactList([primary], options, tr, variant.title));
      if (pieces.length > 1) {
        const details = node('details', 'production-card__detail');
        details.append(node('summary', '', tr('Más piezas', 'More pieces')),
          artifactList(pieces.filter(piece => piece !== primary), options, tr));
        group.append(details);
      }
      actions.append(action(tr('Conservar esta', 'Keep this one'), 'keep', variant),
        action(tr('Descartar', 'Discard'), 'cancel', variant));
      group.append(actions);
      variants.append(group);
    }
    root.append(label, variants);
  } else {
    const actions = node('div', 'production-card__actions');
    actions.append(action(tr('Conservar', 'Keep'), 'keep'), action(tr('Cancelar', 'Cancel'), 'cancel'));
    root.append(artifactList(row.artifacts ?? legacyPieces(row), options, tr), label, actions);
  }
  root.append(status);
  return {root, note, title, options};
}

function legacyPieces(row) {
  return row.image ? [{title: row.image.title, image: row.image, media: 'image'}] : [];
}

export function renderDecisionCards(container, rows, options) {
  if (![...document.styleSheets].some(sheet => sheet.href === stylesheet)
      && !document.head.querySelector('link[data-production-cards]')) {
    const style = node('link', '');
    style.rel = 'stylesheet';
    style.href = stylesheet;
    style.dataset.productionCards = '';
    document.head.append(style);
  }
  const previous = mounted.get(container) ?? new Map();
  const next = new Map();
  const restoreFocus = [...previous.values()].find(entry => entry.root.contains(document.activeElement));
  rows.forEach((row, index) => {
    const signature = JSON.stringify([options.lang, options.orderHref?.(row), row]);
    const old = previous.get(row.key);
    const entry = old?.signature === signature ? old : {
      ...card(row, {...options}, old?.note.value ?? ''), signature,
    };
    Object.assign(entry.options, options);
    next.set(row.key, entry);
    if (old && old !== entry) old.root.replaceWith(entry.root);
    if (container.children[index] !== entry.root) container.insertBefore(entry.root, container.children[index] ?? null);
  });
  for (const [key, entry] of previous) {
    if (!next.has(key)) entry.root.remove();
  }
  mounted.set(container, next);
  if (restoreFocus && !restoreFocus.root.isConnected) {
    const target = next.get(restoreFocus.root.dataset.decision)?.title ?? next.values().next().value?.title ?? container;
    target.tabIndex = -1;
    target.focus();
  }
}

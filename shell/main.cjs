'use strict';
// Hoard Window — one desktop window per Hoard app.
//
// The window has no native frame: a 36 px bar of our own sits on top
// (a WebContentsView with bar.html) and the app's UI fills the rest
// (a second WebContentsView). Windows draws the real minimise/maximise/
// close buttons over the bar's right edge (titleBarOverlay), so Snap
// layouts, double-click to maximise and drag-to-move all stay native.
// The bar takes the app's colours from the page itself: --hoard-deep /
// --hoard-text from the shared Hoard theme, else <meta name=theme-color>,
// else the body background.
//
// argv: --hoard-url=<url> --hoard-id=<id> [--hoard-name=<name>]
//       [--hoard-icon=<png>] [--user-data-dir=<dir>] [--hoard-size=WxH]

const { app, BaseWindow, WebContentsView, Menu, nativeImage, ipcMain, shell, nativeTheme, screen } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const BAR_H = 36;

function arg(name, fallback = '') {
  const pre = `--${name}=`;
  const hit = process.argv.find((a) => a.startsWith(pre));
  return hit ? hit.slice(pre.length).replace(/^"|"$/g, '') : fallback;
}

const appUrl = arg('hoard-url');
const appId = arg('hoard-id', 'app').replace(/[^a-z0-9_.-]/gi, '') || 'app';
const appName = arg('hoard-name', appId);
const iconPath = arg('hoard-icon');
const dataDir = arg('user-data-dir') || path.join(app.getPath('appData'), 'hoard-window', appId);
const [defW, defH] = (arg('hoard-size', '1280x860').split('x').map((n) => parseInt(n, 10)));

if (!appUrl) {
  console.error('hoard-window: --hoard-url is required');
  app.exit(2);
}

app.setPath('userData', dataDir);
// A window opened by a background process (the hub) is not given the
// foreground; Chromium's native occlusion tracking then treats it as hidden
// and never paints it. A desktop app window is never "occluded" for us.
app.commandLine.appendSwitch('disable-features', 'CalculateNativeWinOcclusion');
app.commandLine.appendSwitch('disable-backgrounding-occluded-windows');
app.setName(appName);
if (process.platform === 'win32') app.setAppUserModelId(`hoard.${appId}`);
nativeTheme.themeSource = 'dark';

// One window per app: a second launch focuses the first.
if (!app.requestSingleInstanceLock({ appId })) {
  app.exit(0);
}

let win = null;
let bar = null;
let view = null;
let colours = { bg: '#17191a', fg: '#eeeae2', muted: '#babcb6', accent: '#d5b575' };
const origin = (() => { try { return new URL(appUrl).origin; } catch { return ''; } })();
const stateFile = path.join(dataDir, 'hoard-window.json');
const logFile = path.join(dataDir, 'hoard-window.log');

function log(...parts) {
  const line = `${new Date().toISOString()} ${parts.map((p) => (typeof p === 'string' ? p : JSON.stringify(p))).join(' ')}\n`;
  try { fs.mkdirSync(dataDir, { recursive: true }); fs.appendFileSync(logFile, line); } catch { /* ignore */ }
}
app.on('child-process-gone', (_e, d) => log('child-process-gone', d));
app.on('render-process-gone', (_e, _wc, d) => log('render-process-gone', d));
app.on('gpu-info-update', () => {});

function loadState() {
  try { return JSON.parse(fs.readFileSync(stateFile, 'utf8')); } catch { return {}; }
}
function saveState() {
  if (!win || win.isDestroyed()) return;
  const s = { maximized: win.isMaximized(), zoom: view ? view.webContents.getZoomFactor() : 1 };
  if (!win.isMaximized() && !win.isMinimized()) s.bounds = win.getBounds();
  else s.bounds = loadState().bounds;
  try { fs.mkdirSync(dataDir, { recursive: true }); fs.writeFileSync(stateFile, JSON.stringify(s)); } catch { /* ignore */ }
}

function visibleOnSomeDisplay(b) {
  if (!b) return false;
  return screen.getAllDisplays().some((d) => {
    const a = d.workArea;
    return b.x < a.x + a.width - 80 && b.x + b.width > a.x + 80 && b.y >= a.y - 10 && b.y < a.y + a.height - 60;
  });
}

function layout() {
  if (!win || win.isDestroyed()) return;
  const { width, height } = win.getContentBounds();
  bar.setBounds({ x: 0, y: 0, width, height: BAR_H });
  view.setBounds({ x: 0, y: BAR_H, width, height: Math.max(0, height - BAR_H) });
}

function applyColours(c) {
  colours = { ...colours, ...c };
  if (!win || win.isDestroyed()) return;
  if (process.platform !== 'darwin') {
    try { win.setTitleBarOverlay({ color: colours.bg, symbolColor: colours.fg, height: BAR_H }); } catch { /* ignore */ }
  }
  win.setBackgroundColor(colours.bg);
  bar.webContents.send('hoard:colours', colours);
}

async function readColours() {
  if (!view) return;
  try {
    const c = await view.webContents.executeJavaScript(`(() => {
      const cs = getComputedStyle(document.documentElement);
      const v = (n) => cs.getPropertyValue(n).trim();
      const meta = document.querySelector('meta[name="theme-color"]');
      const bodyBg = getComputedStyle(document.body || document.documentElement).backgroundColor;
      return {
        bg: v('--hoard-deep') || (meta && meta.content) || bodyBg || '',
        fg: v('--hoard-text') || '',
        muted: v('--hoard-text-muted') || '',
        accent: v('--hoard-accent') || '',
      };
    })()`, true);
    const out = {};
    for (const k of Object.keys(c)) {
      const val = toHex(c[k]);
      if (val) out[k] = val;
    }
    applyColours(out);
  } catch { /* page not ready */ }
}

function toHex(css) {
  if (!css) return '';
  css = String(css).trim();
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(css)) {
    if (css.length === 4) return '#' + css.slice(1).split('').map((x) => x + x).join('');
    return css.toLowerCase();
  }
  const m = css.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?/i);
  if (!m) return '';
  if (m[4] !== undefined && parseFloat(m[4]) === 0) return '';
  return '#' + [m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, '0')).join('');
}

function menuTemplate() {
  const wc = view.webContents;
  return [
    { label: 'Recargar', accelerator: 'F5', click: () => wc.reload() },
    { label: 'Recargar sin caché', accelerator: 'Ctrl+F5', click: () => wc.reloadIgnoringCache() },
    { type: 'separator' },
    { label: 'Atrás', accelerator: 'Alt+Left', enabled: wc.navigationHistory.canGoBack(), click: () => wc.navigationHistory.goBack() },
    { label: 'Adelante', accelerator: 'Alt+Right', enabled: wc.navigationHistory.canGoForward(), click: () => wc.navigationHistory.goForward() },
    { label: 'Ir al inicio', click: () => wc.loadURL(appUrl) },
    { type: 'separator' },
    { label: 'Ampliar', accelerator: 'Ctrl+=', click: () => zoom(+0.1) },
    { label: 'Reducir', accelerator: 'Ctrl+-', click: () => zoom(-0.1) },
    { label: 'Tamaño real', accelerator: 'Ctrl+0', click: () => zoom(0) },
    { type: 'separator' },
    { label: 'Abrir en el navegador', click: () => shell.openExternal(wc.getURL() || appUrl) },
    { label: 'Copiar dirección', click: () => require('electron').clipboard.writeText(wc.getURL() || appUrl) },
    { label: 'Siempre encima', type: 'checkbox', checked: win.isAlwaysOnTop(), click: (i) => win.setAlwaysOnTop(i.checked) },
    { label: 'Herramientas de desarrollo', accelerator: 'Ctrl+Shift+I', click: () => wc.toggleDevTools() },
    { type: 'separator' },
    { label: 'Cerrar ventana', accelerator: 'Ctrl+W', click: () => win.close() },
  ];
}

function zoom(delta) {
  const wc = view.webContents;
  const z = delta === 0 ? 1 : Math.min(3, Math.max(0.5, Math.round((wc.getZoomFactor() + delta) * 10) / 10));
  wc.setZoomFactor(z);
  saveState();
}

function create() {
  const st = loadState();
  const bounds = visibleOnSomeDisplay(st.bounds) ? st.bounds : { width: defW || 1280, height: defH || 860 };
  let icon;
  if (iconPath && fs.existsSync(iconPath)) icon = nativeImage.createFromPath(iconPath);
  win = new BaseWindow({
    ...bounds,
    minWidth: 720,
    minHeight: 480,
    title: appName,
    icon,
    show: false,
    backgroundColor: colours.bg,
    ...(process.platform !== 'darwin'
      ? { titleBarStyle: 'hidden', titleBarOverlay: { color: colours.bg, symbolColor: colours.fg, height: BAR_H } }
      : { titleBarStyle: 'hiddenInset' }),
  });

  bar = new WebContentsView({
    webPreferences: { preload: path.join(__dirname, 'preload-bar.cjs'), contextIsolation: true, sandbox: true, backgroundThrottling: false },
  });
  view = new WebContentsView({
    webPreferences: { preload: path.join(__dirname, 'preload-app.cjs'), contextIsolation: true, sandbox: true, spellcheck: true, backgroundThrottling: false },
  });
  bar.setBackgroundColor(colours.bg);
  win.contentView.addChildView(view);
  win.contentView.addChildView(bar);
  layout();

  const iconUrl = icon ? icon.resize({ width: 32, height: 32 }).toDataURL() : '';
  bar.webContents.loadFile(path.join(__dirname, 'bar.html'), {
    query: { name: appName, icon: iconUrl ? '1' : '0', platform: process.platform },
  });
  bar.webContents.once('did-finish-load', () => {
    bar.webContents.send('hoard:init', { name: appName, icon: iconUrl, colours });
  });

  const wc = view.webContents;
  if (st.zoom && st.zoom !== 1) wc.once('did-finish-load', () => wc.setZoomFactor(st.zoom));
  wc.on('did-finish-load', () => { log('loaded', wc.getURL()); readColours(); setTimeout(readColours, 800); });
  wc.on('did-navigate-in-page', () => readColours());
  wc.on('page-title-updated', (_e, t) => {
    const title = t && t !== appName && !t.startsWith('http') ? `${t}` : appName;
    win.setTitle(title.includes(appName) ? title : `${title} — ${appName}`);
    bar.webContents.send('hoard:title', title);
  });
  wc.on('did-fail-load', (_e, code, desc, url, isMain) => {
    log('did-fail-load', { code, desc, url, isMain });
    if (!isMain || code === -3) return; // -3 = aborted (a new navigation)
    bar.webContents.send('hoard:status', { kind: 'error', text: `No responde (${desc})` });
    setTimeout(() => wc.loadURL(url || appUrl), 2500);
  });
  wc.on('did-start-loading', () => bar.webContents.send('hoard:status', { kind: 'loading' }));
  wc.on('did-stop-loading', () => bar.webContents.send('hoard:status', { kind: 'idle' }));

  // Same-origin links stay here; anything else goes to the real browser.
  wc.setWindowOpenHandler(({ url }) => {
    try {
      if (new URL(url).origin === origin) return { action: 'allow', overrideBrowserWindowOptions: { autoHideMenuBar: true, icon } };
    } catch { /* fallthrough */ }
    if (/^(https?|mailto):/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  wc.on('will-navigate', (e, url) => {
    try { if (new URL(url).origin === origin) return; } catch { /* ignore */ }
    e.preventDefault();
    if (/^(https?|mailto):/i.test(url)) shell.openExternal(url);
  });

  // Local apps on loopback: grant what a desktop app would have.
  wc.session.setPermissionRequestHandler((_wc, perm, cb, details) => {
    const ok = (details.requestingUrl || '').startsWith(origin);
    cb(ok && ['clipboard-read', 'clipboard-sanitized-write', 'notifications', 'media', 'fullscreen', 'openExternal', 'fileSystem'].includes(perm));
  });

  // Shortcuts that the bar's menu advertises, handled even when the
  // page has focus.
  wc.on('before-input-event', (e, input) => {
    if (input.type !== 'keyDown') return;
    const ctrl = input.control || input.meta;
    const k = input.key;
    if (k === 'F5' && !ctrl) { wc.reload(); e.preventDefault(); }
    else if (k === 'F5' && ctrl) { wc.reloadIgnoringCache(); e.preventDefault(); }
    else if (ctrl && input.shift && (k === 'I' || k === 'i')) { wc.toggleDevTools(); e.preventDefault(); }
    else if (ctrl && (k === '=' || k === '+')) { zoom(+0.1); e.preventDefault(); }
    else if (ctrl && k === '-') { zoom(-0.1); e.preventDefault(); }
    else if (ctrl && k === '0') { zoom(0); e.preventDefault(); }
    else if (ctrl && (k === 'w' || k === 'W')) { win.close(); e.preventDefault(); }
    else if (input.alt && k === 'ArrowLeft' && wc.navigationHistory.canGoBack()) { wc.navigationHistory.goBack(); e.preventDefault(); }
    else if (input.alt && k === 'ArrowRight' && wc.navigationHistory.canGoForward()) { wc.navigationHistory.goForward(); e.preventDefault(); }
  });

  wc.loadURL(appUrl);

  win.on('resize', () => { layout(); });
  win.on('maximize', layout);
  win.on('unmaximize', layout);
  win.on('close', saveState);
  win.on('closed', () => { win = null; app.quit(); });
  if (st.maximized) win.maximize();
  win.show();
  win.focus();
  view.webContents.focus();
}

ipcMain.on('hoard:menu', (_e, pos) => {
  if (!win) return;
  Menu.buildFromTemplate(menuTemplate()).popup({ window: win, x: Math.round(pos?.x ?? 8), y: Math.round(pos?.y ?? BAR_H) });
});
ipcMain.on('hoard:home', () => { if (view) view.webContents.loadURL(appUrl); });
ipcMain.on('hoard:reload', () => { if (view) view.webContents.reload(); });

app.on('second-instance', () => {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
});

app.whenReady().then(() => {
  log('start', { url: appUrl, id: appId, electron: process.versions.electron, gpu: app.getGPUFeatureStatus() });
  Menu.setApplicationMenu(null);
  create();
});
app.on('window-all-closed', () => app.quit());

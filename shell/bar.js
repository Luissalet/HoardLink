'use strict';
const root = document.documentElement.style;
const status = document.getElementById('status');
const load = document.getElementById('load');
const params = new URLSearchParams(location.search);
if (params.get('platform') === 'darwin') document.body.classList.add('mac');
document.title = params.get('name') || 'Hoard';

function setColours(c) {
  if (!c) return;
  if (c.bg) root.setProperty('--bg', c.bg);
  if (c.fg) root.setProperty('--fg', c.fg);
  if (c.muted) root.setProperty('--muted', c.muted);
  if (c.accent) root.setProperty('--accent', c.accent);
}

document.getElementById('menu').addEventListener('click', (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  window.hoardBar.menu(r.left, r.bottom + 2);
});
window.hoardBar.on('hoard:init', (d) => { setColours(d.colours); });
window.hoardBar.on('hoard:colours', setColours);
window.hoardBar.on('hoard:status', (s) => {
  load.classList.toggle('on', s.kind === 'loading');
  if (s.kind === 'error') { status.textContent = s.text; status.className = 'error'; }
  else if (s.kind === 'idle') { status.textContent = ''; status.className = ''; }
});

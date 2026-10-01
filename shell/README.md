# Hoard Window

The desktop window the Hoard Hub opens for every app: the app's own UI under
a 36 px title bar that takes the app's colours, with the native Windows
caption buttons (Snap layouts included) drawn over its right edge. It is one
small Electron program shared by every app; nothing is packaged per app.

```
npm install            # once, inside this folder (Electron ~110 MB)
npx electron . --hoard-url=http://127.0.0.1:5181/ --hoard-id=links --hoard-name="Links Hoard" --hoard-icon=..\app-icon.png
```

The hub uses it automatically when `shell/node_modules/electron` exists
(`window_engine: auto`); otherwise it falls back to a Chromium `--app`
window. Set `window_engine` to `shell` or `chromium` in `data/hub.json` to
force one.

What the window does:

- **Colours** — read from the page after every load: `--hoard-deep`,
  `--hoard-text`, `--hoard-accent` from the shared Hoard theme
  (`hoard_link/ui/hoard-theme.css`), else `<meta name="theme-color">`, else
  the body background.
- **Menu** (the ☰ button) — reload, back/forward, home, zoom, open in the
  browser, copy the address, always on top, developer tools, close.
- **One window per app** — each app gets its own profile folder
  (`--user-data-dir`, the hub passes `data/profiles/<id>`), its own taskbar
  group and icon; launching it again focuses the open window. Size,
  position, maximised state and zoom are remembered per app.
- **Links** — same-origin links stay in the window; anything else opens in
  the default browser.
- Pages can tell they run inside it: `window.hoardWindow` exists and
  `<html data-hoard-window="1">` is set.

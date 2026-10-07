"""Real-browser proof that the Hub refreshes icon files on its normal poll."""
from __future__ import annotations

import binascii
import json
import os
import struct
import threading
import zlib
from pathlib import Path

import pytest

from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server


def _png(color: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes((*color, 255)) * 8
    raw = row * 8
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _app_icon_state(page, *, revision: str | None = None, color: tuple[int, int, int] | None = None,
                    available: bool | None = None) -> bool:
    return page.evaluate("""({revision, color, available}) => {
      const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
      if (!img || !img.complete || !img.naturalWidth) return false;
      const url = new URL(img.currentSrc);
      if (revision !== null && url.searchParams.get('revision') !== revision) return false;
      if (available !== null && url.searchParams.get('available') !== (available ? '1' : '0')) return false;
      if (color !== null) {
        const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
        const ctx = canvas.getContext('2d'); ctx.drawImage(img, 0, 0, 1, 1);
        if (Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3).join(',') !== color.join(',')) return false;
      }
      return true;
    }""", {"revision": revision, "color": list(color) if color else None, "available": available})


def test_hub_browser_refreshes_missing_replaced_and_priority_icons_on_normal_poll(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    root = tmp_path / "apps"
    app_dir = root / "Icon Fixture"
    app_dir.mkdir(parents=True)
    icon_dir = tmp_path / "Icons"
    icon_dir.mkdir()
    app_url = "http://127.0.0.1:1"
    (app_dir / "faustus-plugin.json").write_text(json.dumps({
        "schema": 1, "id": "iconfixture", "name": "Icon Fixture", "purpose": "Temporary browser fixture",
        "defaults": {"APP_URL": app_url},
        "app": {"url_default": app_url, "health": {"path": "/api/health", "expect": {"service": "iconfixture-hoard"}}},
    }), encoding="utf-8")

    config = HubConfig(port=0, data_dir=str(tmp_path / "hub-data"), roots=[str(root)], icon_dirs=[str(icon_dir)],
                       faustus_urls=[], jobs_enabled=False)
    hub = Hub(config, gpu_fn=lambda: [])
    server = make_server(hub, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        app = hub.get("iconfixture")
        assert app is not None and app.icon_path is None
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page_errors = []
            icon_requests: list[str] = []
            navigations: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("request", lambda request: icon_requests.append(request.url)
                    if "/api/apps/iconfixture/icon" in request.url else None)
            page.on("framenavigated", lambda frame: navigations.append(frame.url)
                    if frame == page.main_frame else None)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              return img && img.complete && img.naturalWidth > 0 && new URL(img.currentSrc).searchParams.get('revision') === 'missing';
            }""", timeout=10_000)

            # The app starts without an icon. Add a matching shared icon, then a lower-priority
            # app-local icon, then app-icon.png. The Hub must discover every choice on its 5 s poll.
            shared = icon_dir / "iconfixture.png"
            shared.write_bytes(_png((220, 25, 35)))
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              return img && img.complete && img.naturalWidth && new URL(img.currentSrc).searchParams.get('revision') !== 'missing';
            }""", timeout=9_000)
            assert _app_icon_state(page, color=(220, 25, 35), available=True)
            shared_revision = app.to_dict()["icon_revision"]

            local_lower = app_dir / "icon.png"
            local_lower.write_bytes(_png((25, 55, 220)))
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              if (!img || !img.complete || !img.naturalWidth) return false;
              const c = document.createElement('canvas'); c.width = c.height = 1;
              const x = c.getContext('2d'); x.drawImage(img, 0, 0, 1, 1);
              return Array.from(x.getImageData(0, 0, 1, 1).data).slice(0,3).join(',') === '25,55,220';
            }""", timeout=9_000)
            lower_revision = app.to_dict()["icon_revision"]
            assert lower_revision != shared_revision

            local_higher = app_dir / "app-icon.png"
            local_higher.write_bytes(_png((20, 180, 55)))
            fixed_mtime = local_lower.stat().st_mtime_ns
            os.utime(local_higher, ns=(fixed_mtime, fixed_mtime))
            lower_stat = local_lower.stat()
            assert (local_higher.stat().st_size, local_higher.stat().st_mtime_ns) == (lower_stat.st_size, lower_stat.st_mtime_ns)
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              if (!img || !img.complete || !img.naturalWidth) return false;
              const c = document.createElement('canvas'); c.width = c.height = 1;
              const x = c.getContext('2d'); x.drawImage(img, 0, 0, 1, 1);
              return Array.from(x.getImageData(0, 0, 1, 1).data).slice(0,3).join(',') === '20,180,55';
            }""", timeout=9_000)
            higher_revision = app.to_dict()["icon_revision"]
            assert higher_revision != lower_revision  # path identity invalidates identical mtime/size.

            # Replace the selected file with another valid PNG. Wait for the ordinary UI poll,
            # without page reload, rescan, or a user-triggered refresh.
            replacement = _png((225, 135, 15))
            old_mtime = local_higher.stat().st_mtime_ns
            local_higher.write_bytes(replacement)
            os.utime(local_higher, ns=(old_mtime, old_mtime + 3_000_000_000))
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              if (!img || !img.complete || !img.naturalWidth) return false;
              const c = document.createElement('canvas'); c.width = c.height = 1;
              const x = c.getContext('2d'); x.drawImage(img, 0, 0, 1, 1);
              return Array.from(x.getImageData(0, 0, 1, 1).data).slice(0,3).join(',') === '225,135,15';
            }""", timeout=9_000)

            # Shared DOM-contract probe for image nodes used by refs/family/search/work.
            # The central dashboard poll upgrades them without per-image timers.
            page.evaluate("""() => {
              for (const surface of ['refs', 'family', 'search', 'work']) {
                const img = document.createElement('img'); img.id = `icon-probe-${surface}`;
                img.dataset.surface = surface; img.src = '/api/apps/iconfixture/icon'; document.body.appendChild(img);
              }
            }""")
            page.wait_for_function("""() => [...document.querySelectorAll('[data-surface]')].every(img => {
              if (!img.complete || !img.naturalWidth) return false;
              const url = new URL(img.currentSrc);
              return url.searchParams.get('revision') && url.searchParams.get('revision') !== 'missing';
            })""", timeout=9_000)
            assert page.evaluate("""() => [...document.querySelectorAll('[data-surface]')].every(img => {
              const c = document.createElement('canvas'); c.width = c.height = 1;
              const x = c.getContext('2d'); x.drawImage(img, 0, 0, 1, 1);
              return Array.from(x.getImageData(0, 0, 1, 1).data).slice(0,3).join(',') === '225,135,15';
            })""")
            assert page.locator("[data-surface]").count() == 4
            assert all("revision=" in page.locator(f"#icon-probe-{surface}").get_attribute("src")
                       for surface in ("refs", "family", "search", "work"))

            stable_count = len(icon_requests)
            page.wait_for_timeout(10_500)  # pump browser callbacks through two ordinary polls.
            assert len(icon_requests) == stable_count

            # Removing the selected icon falls back to icon.png; removing it falls back to the
            # shared icon. Removing the last candidate shows the UI fallback; reappearance is found.
            local_higher.unlink()
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              if (!img || !img.complete || !img.naturalWidth) return false;
              const c = document.createElement('canvas'); c.width = c.height = 1;
              const x = c.getContext('2d'); x.drawImage(img, 0, 0, 1, 1);
              return Array.from(x.getImageData(0, 0, 1, 1).data).slice(0,3).join(',') === '25,55,220';
            }""", timeout=9_000)
            local_lower.unlink()
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              if (!img || !img.complete || !img.naturalWidth) return false;
              const c = document.createElement('canvas'); c.width = c.height = 1;
              const x = c.getContext('2d'); x.drawImage(img, 0, 0, 1, 1);
              return Array.from(x.getImageData(0, 0, 1, 1).data).slice(0,3).join(',') === '220,25,35';
            }""", timeout=9_000)
            shared.unlink()
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              return img && img.complete && img.naturalWidth && new URL(img.currentSrc).searchParams.get('available') === '0';
            }""", timeout=9_000)
            reappeared = app_dir / "app-icon.png"
            reappeared.write_bytes(_png((120, 25, 180)))
            page.wait_for_function("""() => {
              const img = document.querySelector('#grid .card[data-id="iconfixture"] .icon');
              if (!img || !img.complete || !img.naturalWidth) return false;
              const c = document.createElement('canvas'); c.width = c.height = 1;
              const x = c.getContext('2d'); x.drawImage(img, 0, 0, 1, 1);
              return Array.from(x.getImageData(0, 0, 1, 1).data).slice(0,3).join(',') === '120,25,180';
            }""", timeout=9_000)
            assert navigations == [f"http://127.0.0.1:{server.server_port}/"]
            assert not page_errors, page_errors
            assert len(icon_requests) <= 16, icon_requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        hub.close()

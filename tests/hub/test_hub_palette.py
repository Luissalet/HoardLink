"""The Hub page's design layer in a real browser: grouped sidebar, type scale, command palette, keep-alive, theme."""
import sys
from pathlib import Path

import pytest

from ._hub_fakes import make_hub, serve
from .conftest import free_port, write_manifest


@pytest.mark.parametrize('width', [1280, 390])
def test_palette_keep_alive_theme_and_scale(tmp_path, width):
    browser_api = pytest.importorskip('playwright.sync_api')
    hub = make_hub(tmp_path, [], ['cookhoard'])
    write_manifest(tmp_path / 'apps' / "Writer's Hoard", 'writer', free_port(), service='writer-hoard',
                   launch={'kind': 'process', 'executable': sys.executable, 'argv': ['-c', 'pass']})
    hub.rescan()
    hub.config.agent_watch = {'enabled': False}
    server = serve(hub)
    keepalive = hub.facet('keepalive')
    try:
        with browser_api.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip('Headless Chromium is not installed')
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': width, 'height': 844}, locale='es-ES')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = hub.config.url
                page.goto(base + '/#hub/apps')
                page.locator('#grid .card').first.wait_for()
                # Type scale: the title and body read the scale tokens.
                assert page.locator('#hub-page-title').evaluate('e => getComputedStyle(e).fontSize') == '25px'
                assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--hub-fs-3').trim()") == '16px'
                # Page actions sit next to the title.
                for button in ('#btn-start-all', '#btn-stop-all', '#btn-rescan'):
                    assert page.locator('#hub-page-actions ' + button).count() == 1
                # Quick actions: keep an app alive from the palette.
                page.keyboard.press('Control+k')
                page.locator('#hub-palette[open]').wait_for()
                page.keyboard.type('>writer')
                assert page.locator('#hub-palette .hub-palette-modes [aria-pressed="true"]').get_attribute('data-mode') == 'actions'
                page.locator('#hub-palette .hub-palette-item', has_text='Mantener viva').first.click()
                page.wait_for_function('() => document.querySelector("#hub-palette") && !document.querySelector("#hub-palette").open')
                page.wait_for_function("() => [...document.querySelectorAll('#grid .card')].some(c => c.querySelector('h2').textContent.includes('Writer') && !c.querySelector('.keep-badge').hidden)")
                assert 'writer' in keepalive.apps()
                # Manual-only and unlaunchable apps are not offered.
                page.keyboard.press('Control+k')
                page.keyboard.type('>cookhoard')
                assert page.locator('#hub-palette .hub-palette-item', has_text='Mantener viva').count() == 0
                page.keyboard.press('Escape')
                # Go-to mode reaches a tab.
                page.keyboard.press('Control+k')
                page.keyboard.type('copias')
                page.keyboard.press('Enter')
                page.wait_for_url('**/#hub/backups')
                # Theme: light from the palette, kept after a reload.
                page.keyboard.press('Control+k')
                page.keyboard.type('>tema claro')
                page.keyboard.press('Enter')
                assert page.evaluate('document.documentElement.dataset.hubTheme') == 'light'
                page.reload()
                assert page.evaluate('document.documentElement.dataset.hubTheme') == 'light'
                assert page.evaluate("getComputedStyle(document.body).backgroundColor") == 'rgb(246, 243, 236)'
                if width > 900:
                    labels = page.locator('.hub-nav-label').all_inner_texts()
                    assert [x.strip().lower() for x in labels] == ['tu espacio', 'herramientas', 'entorno']
                else:
                    assert not page.locator('#hub-navigation').is_visible()
                    page.locator('#hub-menu-toggle').click()
                    assert page.locator('.hub-nav-group').first.is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()

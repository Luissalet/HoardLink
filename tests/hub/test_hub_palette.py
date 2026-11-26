"""The Hub page's design layer in a real browser: grouped sidebar, type scale, command palette, keep-alive, theme."""
import sys

import pytest

from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub

from ._hub_fakes import serve, ui_url
from .conftest import free_port, write_manifest


@pytest.mark.parametrize('width', [1280, 390])
def test_palette_keep_alive_theme_and_scale(tmp_path, width, monkeypatch):
    from hoard_link.hub import facets

    browser_api = pytest.importorskip('playwright.sync_api')
    monkeypatch.setattr(facets, 'start_all', lambda _: None)
    apps = tmp_path / 'apps'
    launch = {'kind': 'process', 'executable': sys.executable, 'argv': ['-c', 'pass']}
    # Use a generic app so manifest-based isolation does not depend on a Writer source checkout.
    isolated = {'isolated_instance': {'data_env': 'PALETTE_DATA_DIR', 'port_env': 'PALETTE_PORT'}}
    write_manifest(apps / 'Palette', 'palette-app', free_port(), launch=launch, extra={'x-family': isolated})
    write_manifest(apps / 'Manual', 'manual-app', free_port(), launch=launch,
                   extra={'x-family': {**isolated, 'auto_start': False}})
    write_manifest(apps / 'Legacy', 'legacy-app', free_port())
    config = HubConfig(port=free_port(), roots=[str(apps)], data_dir=str(tmp_path / 'hubdata'),
                       icon_dirs=[], faustus_urls=[], jobs_enabled=False, isolated_catalog=True)
    config.agent_watch = {'enabled': False}
    hub = Hub(config)
    server = serve(hub)
    keepalive = hub.facet('keepalive')
    try:
        with browser_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel='chrome', headless=True)
            except browser_api.Error as exc:
                if 'distribution' in str(exc) and 'not found' in str(exc):
                    pytest.skip('Chrome is not installed')
                raise
            try:
                page = browser.new_page(viewport={'width': width, 'height': 844}, locale='es-ES')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(ui_url(hub, '/#hub/apps'))
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
                page.keyboard.type('>palette-app')
                assert page.locator('#hub-palette .hub-palette-modes [aria-pressed="true"]').get_attribute('data-mode') == 'actions'
                keep_action = page.locator('#hub-palette .hub-palette-item', has_text='Mantener viva')
                keep_action.click()
                page.wait_for_function('() => document.querySelector("#hub-palette") && !document.querySelector("#hub-palette").open')
                card = page.locator('#grid .card', has=page.get_by_role('heading', name="Palette-App's Hoard"))
                card.locator('.keep-badge').wait_for(state='visible')
                assert 'palette-app' in keepalive.apps()
                # Manual-only and unlaunchable apps are not offered.
                assert hub.get('manual-app').launchable
                assert not hub.get('legacy-app').launchable
                for app_id in ('manual-app', 'legacy-app'):
                    page.keyboard.press('Control+k')
                    page.keyboard.type('>' + app_id)
                    assert page.locator('#hub-palette .hub-palette-item', has_text='Ver registro de').count() == 1
                    assert keep_action.count() == 0
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

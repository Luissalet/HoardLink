"""Project links and single-page navigation, over the real isolated Hub server."""
from pathlib import Path

import pytest

from ._hub_fakes import make_hub, serve


@pytest.mark.parametrize('width', [1280, 390])
def test_project_navigation_preserves_scope_and_all_tools(tmp_path, width):
    browser_api = pytest.importorskip('playwright.sync_api')
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {'enabled': False}
    server = serve(hub)
    ag = hub.facet('agora').agora
    for ident, title in [('alpha', 'Alpha'), ('beta', 'Beta')]:
        ag.space_create({'agent': 'builder', 'id': ident, 'title': title})
        ag.task_add({'agent': 'builder', 'space_id': ident, 'title': title + ' task'})
    try:
        with browser_api.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip('Headless Chromium is not installed')
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': width, 'height': 844})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = hub.config.url
                page.goto(base)
                page.locator('.hub-project-open').filter(has_text='Alpha').click()
                page.locator('.ag-card', has_text='Alpha task').wait_for()
                assert page.locator('.ag-card', has_text='Beta task').count() == 0
                assert page.locator('#hub-page-title').inner_text() == 'Alpha'
                assert page.locator('#hub-page-actions a').get_attribute('href').endswith('space_id=alpha')
                for href in page.locator('#tab-agora [data-ag-space-link]').evaluate_all('(links) => links.map(link => link.href)'):
                    assert 'space_id=alpha' in href
                page.locator('.ag-space-actions summary').click()
                page.locator('.ag-space-actions button').nth(1).click()
                page.locator('.ag-form input').fill('Alpha renamed')
                page.locator('.ag-form button').first.click()
                page.get_by_role('heading', name='Alpha renamed', exact=True).wait_for()
                page.reload()
                page.locator('.ag-card', has_text='Alpha task').wait_for()
                page.goto(base + '/?space_id=beta#hub/project')
                page.locator('.ag-card', has_text='Beta task').wait_for()
                assert page.locator('.ag-card', has_text='Alpha task').count() == 0
                for route in ['communication', 'automation', 'system']:
                    page.goto(base + '/#hub/' + route)
                    page.locator('.hub-tool-row').first.wait_for()
                    assert page.locator('.hub-tool-row').count() > 0
                page.goto(base + '/#hub/projects')
                field = page.locator('.hub-project-filter')
                field.fill('beta')
                assert page.locator('.hub-project-row:visible').count() == 1
                field.fill('no match')
                assert page.locator('.hub-no-match').is_visible()
                page.goto(base + '/#hub/events')
                page.locator('#tab-events').wait_for()
                assert page.locator('#family-body').evaluate('e => getComputedStyle(e).maxHeight') == 'none'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()

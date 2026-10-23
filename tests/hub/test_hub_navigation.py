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
        ag.task_add({'agent': 'builder', 'space_id': ident, 'title': title + ' task ' + 'long-title-' * 15})
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
                page.locator('.hub-project-agora').click()
                page.locator('.ag-card', has_text='Alpha task').wait_for()
                assert page.locator('.ag-card', has_text='Beta task').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.locator('.ag-card', has_text='Alpha task').evaluate('(e) => e.getBoundingClientRect().right <= innerWidth')
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
                page.goto(base + '/?space_id=beta#hub/agora')
                page.locator('.ag-card', has_text='Beta task').wait_for()
                assert page.locator('.ag-card', has_text='Alpha task').count() == 0
                for route, view in [('communication', 'notify'), ('system', 'resources')]:
                    page.goto(base + '/#hub/' + route)
                    page.locator('.hub-local-nav').wait_for()
                    assert page.locator('#hub-navigation > a').count() == 7
                    if view == 'resources':
                        assert page.locator('#hub-resources').is_visible()
                    else:
                        assert page.locator('#tab-' + view).is_visible()
                    options = page.locator('.hub-local-nav a').evaluate_all('(items) => items.map(item => item.hash.slice(5))')
                    for option in options:
                        page.locator('.hub-local-nav a[href="#hub/' + option + '"]').click()
                        page.wait_for_url('**/#hub/' + option)
                        assert page.locator('#hub-resources' if option == 'resources' else '#tab-' + option).is_visible()
                page.locator('#hub-global-search').click()
                page.locator('.sr-q').wait_for()
                assert page.locator('.sr-q').evaluate('e => document.activeElement === e')
                page.goto(base + '/#hub/jobs')
                page.locator('#job-new').click()
                page.locator('#edit-advanced summary').click()
                page.locator('#edit-fields').wait_for(state='hidden')
                document = {'name': 'Advanced draft', 'at': '04:30', 'days': ['mon'], 'note': 'Keep advanced text', 'enabled': False,
                            'then': [{'kind': 'hub', 'tool': 'hub_backup_run', 'args': {}}]}
                import json
                page.locator('#edit-body').fill(json.dumps(document))
                page.locator('#edit-advanced summary').click()
                page.locator('#edit-fields input[name="name"]').fill('Visual rename')
                page.locator('#edit-advanced summary').click()
                saved = json.loads(page.locator('#edit-body').input_value())
                assert saved['name'] == 'Visual rename' and saved['note'] == document['note'] and saved['days'] == ['mon']
                page.locator('#edit-body').fill('{invalid')
                page.locator('#edit-advanced summary').click()
                page.wait_for_function('document.querySelector("#edit-advanced").open && document.querySelector("#edit-error").textContent.length > 0')
                page.locator('#edit-fields').wait_for(state='hidden')
                page.locator('#edit-body').fill(json.dumps(saved))
                page.locator('#edit-advanced summary').click()
                page.locator('#edit-save').click()
                page.locator('#edit-dialog').wait_for(state='hidden')
                assert any(j['name'] == 'Visual rename' and j['note'] == document['note'] and j['days'] == ['mon'] and not j['enabled'] for j in hub.jobs.list())
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


@pytest.mark.parametrize('width', [1280, 390])
def test_task_discovery_project_resume_and_plain_automation(tmp_path, width):
    browser_api = pytest.importorskip('playwright.sync_api')
    hub = make_hub(tmp_path, [], ['writer', 'cookhoard'])
    hub.config.agent_watch = {'enabled': False}
    ag = hub.facet('agora').agora
    ag.space_create({'agent': 'qa', 'id': 'novel', 'title': 'Mi novela'})
    ag.task_add({'agent': 'qa', 'space_id': 'novel', 'title': 'Revisar el capítulo'})
    hub.facet('notify').send('Informe preparado', '## Resumen\n' + 'Detalle de la investigación. ' * 60, channels_override=[])
    server = serve(hub)
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
                page.goto(base + '/#hub/home')
                page.locator('.hub-attention-row', has_text='Informe preparado').wait_for()
                page.goto(base + '/#hub/apps')
                page.locator('#grid .card').first.wait_for()
                page.locator('#hub-app-toolbar #search').fill('escribir una novela')
                assert page.locator('#grid .card:visible').count() == 1
                assert 'Writer' in page.locator('#grid .card:visible h2').inner_text()
                page.locator('#search').fill('recetas')
                assert 'Cookhoard' in page.locator('#grid .card:visible h2').inner_text()
                page.goto(base + '/#hub/projects')
                page.locator('.hub-project-open', has_text='Mi novela').click()
                page.locator('.hub-project-recent', has_text='Revisar el capítulo').wait_for()
                assert 'space_id=novel' in page.url
                page.locator('.hub-project-recent .hub-attention-row').click()
                page.locator('.ag-detail', has_text='Revisar el capítulo').wait_for()
                assert 'space_id=novel' in page.url
                page.goto(base + '/#hub/rules')
                page.locator('#rule-new').click()
                page.get_by_label('Cuando…', exact=True).select_option('hub.app.started')
                page.get_by_label('Aplicación afectada', exact=True).select_option('writer')
                page.get_by_label('Acción', exact=True).select_option('start_app')
                page.get_by_label('Aplicación', exact=True).select_option('writer')
                page.get_by_label('Nombre', exact=True).fill('Abrir Writer al iniciar una app')
                page.get_by_label('Activada', exact=True).uncheck()
                assert not page.locator('#edit-fields textarea:visible').count()
                page.locator('#edit-save').click()
                page.locator('#edit-dialog').wait_for(state='hidden')
                rule = next(r for r in hub.rules.list() if r['name'] == 'Abrir Writer al iniciar una app')
                assert rule['when']['type'] == 'hub.app.started'
                from hoard_link.hub.rules import rule_matches
                assert rule['when']['source'] == 'hub'
                assert rule_matches(rule, {'type': 'hub.app.started', 'source': 'hub', 'data': {'app': 'writer'}})
                assert not rule_matches(rule, {'type': 'hub.app.started', 'source': 'hub', 'data': {'app': 'cookhoard'}})
                assert rule['then'] == [{'kind': 'start_app', 'app': 'writer'}]
                assert rule['enabled'] is False
                page.goto(base + '/#hub/notify')
                page.locator('.nt-message summary').wait_for()
                assert not page.locator('.nt-message .nt-body').is_visible()
                assert len(page.locator('.nt-message summary').inner_text()) <= 181
                page.locator('.nt-message summary').click()
                assert len(page.locator('.nt-message .nt-body').inner_text()) > 1000
                page.goto(base + '/#hub/search')
                page.locator('.sr-q').fill('zz-unknown-qa')
                page.locator('.sr-q').press('Enter')
                page.locator('.sr-out a[href="#hub/apps"]').wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        hub.close()

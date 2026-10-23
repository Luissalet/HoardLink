"""Family identity and answering the person without loading a whole conversation."""
from pathlib import Path
import threading

import pytest

from ._hub_fakes import make_hub, serve


def seed(hub):
    ag = hub.facet('agora').agora
    for sid in ('alpha', 'beta'):
        ag.space_create({'agent':'builder', 'id':sid, 'title':sid.title()})
    th = ag.thread_open({'agent':'builder', 'space_id':'alpha', 'title':'Choose the chapter',
                         'kind':'handoff', 'body':'Long conversation history. ' * 200})['thread']
    ag.escalate({'agent':'builder', 'space_id':'alpha', 'thread_id':th['id'], 'question':'Do we keep chapter two?'})
    task = ag.task_add({'agent':'builder', 'space_id':'alpha', 'title':'Review the manuscript'})['task']
    ag.escalate({'agent':'builder', 'space_id':'alpha', 'thread_id':task['thread_id'], 'question':'Which notes should we use?'})
    return ag, th, task


def test_board_does_not_hydrate_finished_history(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, [])
    try:
        ag = hub.facet('agora').agora
        for n in range(22):
            task = ag.task_add({'agent':'builder', 'title':f'Old task {n}'})['task']
            ag.db.execute("UPDATE tasks SET status='done',closed_at=? WHERE id=?", (n+1, task['id']))
        active = ag.task_add({'agent':'builder', 'title':'Current task'})['task']
        hydrated = []
        original = ag._task_dict
        def record(row, **kwargs):
            hydrated.append(dict(row))
            return original(row, **kwargs)
        monkeypatch.setattr(ag, '_task_dict', record)
        board = ag.board({})
        assert board['counts']['done'] == 22
        assert len(board['tasks']) == 16
        assert active['id'] in [t['id'] for t in board['tasks']]
        assert len([t for t in hydrated if t['status'] == 'done']) == 15
    finally:
        hub.close()


def contrast(fg, bg):
    def luminance(rgb):
        v = [int(n.strip())/255 for n in rgb.removeprefix('rgb(').removesuffix(')').split(',')]
        v = [n/12.92 if n <= .04045 else ((n+.055)/1.055)**2.4 for n in v]
        return sum(n*w for n,w in zip(v, (.2126,.7152,.0722)))
    a,b=sorted((luminance(fg),luminance(bg)))
    return (b+.05)/(a+.05)


@pytest.mark.parametrize('width', [1280,390])
def test_dark_identity_and_quick_reply_before_slow_board(tmp_path, monkeypatch, width):
    browser_api = pytest.importorskip('playwright.sync_api')
    hub = make_hub(tmp_path, [])
    hub.config.agent_watch = {'enabled':False}
    ag, th, task = seed(hub)
    facet = hub.facet('agora')
    original = facet._board
    release = threading.Event()
    def slow_board(args=None):
        release.wait(10)
        return original(args)
    monkeypatch.setattr(facet, '_board', slow_board)
    server=serve(hub)
    try:
        with browser_api.sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':width,'height':900})
            errors=[]; page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(hub.config.url+'/?space_id=alpha#hub/agora',wait_until='domcontentloaded')
            item=page.locator(f'.ag-quick-item[data-thread-id="{th["id"]}"]')
            item.get_by_role('button',name='Reply here',exact=True).wait_for(timeout=6000)
            assert not release.is_set(), 'Waiting must render while the board is still blocked'
            assert page.locator('.ag-threads').first.inner_text() == 'Nothing.'
            assert page.locator('.ag-quick-question').first.inner_text() == 'Do we keep chapter two?'
            colors=page.evaluate("""() => {
              const body=getComputedStyle(document.body),root=getComputedStyle(document.documentElement);
              const sidebar=getComputedStyle(document.querySelector('.hub-sidebar'));
              return {scheme:root.colorScheme,bg:body.backgroundColor,fg:body.color,
                sidebar:sidebar.backgroundColor,accent:root.getPropertyValue('--hoard-accent').trim()};
            }""")
            assert colors['scheme']=='dark'
            assert colors['bg']=='rgb(28, 26, 20)'
            assert colors['accent']=='#e2b24c'
            assert contrast(colors['fg'],colors['bg']) >= 4.5
            assert page.locator('link[rel="icon"]').get_attribute('href')=='/ui/hub-icon.png'
            canonical=(Path(__file__).parents[2]/'app-icon.png').read_bytes()
            assert page.request.get(hub.config.url+'/ui/hub-icon.png').body()==canonical
            assert page.locator('.hub-home-link img').evaluate('(e)=>e.complete && e.naturalWidth>0')
            release.set()
            item.get_by_role('button',name='Reply here',exact=True).click()
            item.locator('textarea').fill('Keep chapter two and revise its ending.')
            page.locator('.ag-spacebar select').select_option('beta')
            page.get_by_role('heading',name='Beta',exact=True).wait_for()
            if width < 900:
                page.locator('#hub-menu-toggle').click()
            page.locator('#hub-navigation a[href$="#hub/projects"]').click()
            page.locator('.hub-project-open').filter(has_text='Alpha').click()
            page.locator('.hub-project-agora').click()
            item.locator('textarea').wait_for(state='visible')
            assert item.locator('textarea').input_value()=='Keep chapter two and revise its ending.'
            page.route('**/api/agora/post', lambda route:route.fulfill(status=503,content_type='application/json',body='{"ok":false}'))
            item.get_by_role('button',name='Send',exact=True).click()
            item.locator('.ag-quick-error').get_by_text('Could not send.',exact=False).wait_for()
            assert item.locator('textarea').input_value()=='Keep chapter two and revise its ending.'
            page.unroute('**/api/agora/post')
            item.get_by_role('button',name='Send',exact=True).click()
            item.wait_for(state='detached')
            saved=ag.thread({'space_id':'alpha','thread_id':th['id']})
            assert saved['thread']['status']=='open'
            assert saved['messages'][-1]['author']=='luis'
            assert saved['messages'][-1]['body']=='Keep chapter two and revise its ending.'
            assert not page.locator('.ag-detail').is_visible()
            task_item=page.locator(f'.ag-quick-item[data-thread-id="{task["thread_id"]}"]')
            task_item.get_by_role('button',name='Reply here',exact=True).click()
            assert task_item.get_by_role('button',name='Reply and resolve',exact=True).count()==0
            task_item.locator('textarea').fill('Use the original source notes.')
            page.locator('#btn-lang').click()
            task_item.get_by_role('button',name='Responder aquí',exact=True).wait_for()
            assert task_item.locator('textarea').input_value()=='Use the original source notes.'
            page.locator('#btn-lang').click()
            task_item.get_by_role('button',name='Reply here',exact=True).wait_for()
            task_item.locator('textarea').press('Control+Enter')
            task_item.wait_for(state='detached')
            assert ag.task({'space_id':'alpha','task_id':task['id']})['task']['status']=='open'
            ag.escalate({'agent':'builder','space_id':'alpha','thread_id':th['id'],'question':'Final approval?'})
            page.locator('.hub-local-nav a[href$="#hub/project"]').click()
            page.locator('.hub-project-agora').click()
            item.get_by_text('Final approval?',exact=True).wait_for()
            item.get_by_role('button',name='Reply here',exact=True).click()
            item.locator('textarea').fill('Approved for this synthetic example.')
            item.get_by_role('button',name='Reply and resolve',exact=True).click()
            item.wait_for(state='detached')
            resolved=ag.thread({'space_id':'alpha','thread_id':th['id']})['thread']
            assert resolved['status']=='resolved'
            assert resolved['resolution']=='Approved for this synthetic example.'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
            browser.close()
    finally:
        release.set();server.shutdown();server.server_close();hub.close()

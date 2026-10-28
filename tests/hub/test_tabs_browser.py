"""Real browser regression: every Hub tool opens by its address and shows only its own panel."""
import threading
import pytest
from hoard_link.hub.config import HubConfig
from hoard_link.hub.core import Hub
from hoard_link.hub.server import make_server
from .conftest import free_port


def test_tabs_reopen_collapsed_panel_without_expanding_on_reload(tmp_path):
    playwright=pytest.importorskip('playwright.sync_api')
    cfg=HubConfig(port=free_port(),data_dir=str(tmp_path/'data'),roots=[],icon_dirs=[],
                  faustus_urls=[],faustus_dir=str(tmp_path/'none'),jobs_enabled=False,
                  repos={'roots':[],'extra':[],'ci':False})
    hub=Hub(cfg);server=make_server(hub)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser=p.chromium.launch()
            page=browser.new_page()
            # Tools are pages of the Hub navigation now: every tab opens through its own address,
            # shows exactly its panel and survives a reload; a saved tab that no longer exists is ignored.
            page.add_init_script("if(localStorage.getItem('hub.ftab')===null){localStorage.setItem('hub.ftab','purchases');}")
            page.goto(cfg.url)
            page.locator('#hub-navigation a').first.wait_for()
            page.locator('#family-tabs [data-tab=purchases]').wait_for(state='attached')
            names=page.locator('#family-tabs [data-tab]').evaluate_all('(xs)=>xs.map(x=>x.dataset.tab)')
            assert 'purchases' in names and 'events' in names
            for name in names:
                if name=='agora':
                    continue
                page.goto(cfg.url+'/#hub/'+name)
                page.locator(f'#tab-{name}').wait_for()
                assert page.locator('#family-panel').is_visible(),name
                assert page.locator('#family-body .tab-body:visible').count()==1,name
                assert page.locator('#family-tabs .tab.on').get_attribute('data-tab')==name
            page.reload()
            page.locator(f'#tab-{name}').wait_for()
            page.set_viewport_size({'width': 390, 'height': 844})
            page.goto(cfg.url+'/#hub/purchases')
            page.locator('#tab-purchases').wait_for()
            page.evaluate("localStorage.setItem('hub.ftab','removed-facet')")
            page.goto(cfg.url+'/#hub/home')
            page.reload()
            page.locator('#hub-landing .hub-home-status').wait_for()
            assert page.locator('#family-panel').is_hidden()
            assert page.locator('#hub-landing .hub-home-status').is_visible()
            browser.close()
    finally:
        server.shutdown();server.server_close();hub.close();thread.join(timeout=2)

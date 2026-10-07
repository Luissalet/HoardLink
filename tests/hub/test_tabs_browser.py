"""Real browser regression: selected tabs must be usable after collapse."""
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
            page.add_init_script("if(localStorage.getItem('hub.fopen')===null){localStorage.setItem('hub.fopen','0');localStorage.setItem('hub.ftab','purchases');}")
            page.goto(cfg.url)
            page.locator('#family-tabs [data-tab=purchases]').wait_for()
            assert page.locator('#family-body').is_hidden()
            for name in page.locator('#family-tabs [data-tab]').evaluate_all('(xs)=>xs.map(x=>x.dataset.tab)'):
                if page.locator('#family-body').is_visible():page.locator('#family-toggle').click()
                page.locator(f'#family-tabs [data-tab="{name}"]').click()
                assert page.locator('#family-body').is_visible(),name
                assert page.locator(f'#tab-{name}').is_visible(),name
                assert page.locator('#family-body .tab-body:visible').count()==1
                assert page.locator('#family-toggle').get_attribute('aria-expanded')=='true'
            page.locator('#family-toggle').click()
            page.reload()
            page.locator('#family-tabs [data-tab=purchases]').wait_for()
            assert page.locator('#family-body').is_hidden()
            assert page.locator('#family-toggle').get_attribute('aria-expanded')=='false'
            assert page.locator('#family-tabs .tab.on').get_attribute('data-tab') == name
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#family-tabs [data-tab=purchases]').click()
            assert page.locator('#tab-purchases').is_visible()
            page.locator('#family-toggle').click()
            page.evaluate("localStorage.setItem('hub.ftab','removed-facet')")
            page.reload()
            page.locator('#family-tabs [data-tab=purchases]').wait_for()
            assert page.locator('#family-tabs .tab.on').get_attribute('data-tab') == 'events'
            assert page.locator('#family-body').is_hidden()
            browser.close()
    finally:
        server.shutdown();server.server_close();hub.close();thread.join(timeout=2)

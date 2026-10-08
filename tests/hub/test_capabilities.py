from types import SimpleNamespace
from hoard_link.hub.capabilities import CapabilitiesFacet
from hoard_link.hub.registry import App

def app(id,capabilities):
    return App(id,id,'Purpose','/local','http://127.0.0.1:1234','/health',id,capabilities=capabilities,manifest_path='/local/faustus-plugin.json')

def test_all_installed_hoards_are_discoverable_without_starting_or_probing():
    apps=[app('app'+str(i),['projects']) for i in range(37)]
    facet=CapabilitiesFacet(SimpleNamespace(apps=apps))
    result=facet.find({'limit':50})
    assert len(result['apps'])==37 and all(r['evidence']=='manifest' and 'reachable' not in r for r in result['apps'])

def test_live_catalogue_retains_exact_schema_and_failure_does_not_claim_readiness(monkeypatch):
    import hoard_link.hub.capabilities as module
    def probe(app,timeout):
        return {'ok':True,'contract':'shared','tools':[{'name':'voice_tts','description':'voice','inputSchema':{'type':'object','required':['text']}}]} if app.id=='voice' else {'ok':False,'error':'not reachable'}
    monkeypatch.setattr(module,'app_tools',probe)
    facet=CapabilitiesFacet(SimpleNamespace(apps=[app('voice',['voice']),app('other',['voice'])]))
    result=facet.find({'query':'voz','check':True})
    rows={r['app']:r for r in result['apps']}
    assert rows['voice']['tools'][0]['inputSchema']['required']==['text']
    assert rows['voice']['evidence']=='live-catalogue' and rows['other']['reachable'] is False

def test_recipe_exposes_missing_stage_without_inventing_provider():
    facet=CapabilitiesFacet(SimpleNamespace(apps=[app('designer',['design-tokens'])]))
    plan=facet.recipes({'id':'design-motion'})['recipes'][0]
    assert plan['stages'][0]['providers'][0]['matches']==['design-tokens']
    assert plan['stages'][2]['missing']==['motion-compositions']

def test_stdio_only_hoards_are_in_capabilities_without_fake_http_health(tmp_path):
    import json
    folder=tmp_path/'Game';folder.mkdir()
    (folder/'faustus-plugin.json').write_text(json.dumps({'schema':1,'id':'game','capabilities':['games'],'mcp':{'transport':'stdio','command':'node','args':['server.mjs']}}))
    config=SimpleNamespace(roots=[str(tmp_path)],exclude_ids=[])
    facet=CapabilitiesFacet(SimpleNamespace(apps=[],config=config))
    rows=facet.find({'query':'games','check':True})['apps']
    assert rows[0]['kind']=='mcp-only' and not rows[0]['agent_contract_declared'] and 'reachable' not in rows[0]


def test_capability_search_avoids_partial_word_false_matches():
    facet=CapabilitiesFacet(SimpleNamespace(apps=[app('cad',['parametric-cad']),app('home',['appliance-card'])]))
    result=facet.find({'query':'parametric-cad'})
    assert [row['app'] for row in result['apps']]==['cad']

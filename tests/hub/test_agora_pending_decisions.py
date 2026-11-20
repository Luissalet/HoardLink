from pathlib import Path

from hoard_link.hub.agora import TOOLS
from tests.test_agora_drafts import draft_pass, ready

from ._hub_fakes import http, make_hub, serve


def test_pending_decisions_counts_each_gate_once_in_its_space(tmp_path):
    hub = make_hub(tmp_path)
    root = hub.facet('agora').agora
    root.space_create({'agent': 'builder', 'id': 'test-studio', 'title': 'Test Studio'})
    studio = root.for_space('test-studio')
    _, opened, pieces = draft_pass(studio, tmp_path)
    ready(studio, opened, pieces)
    pass_id = opened['draft_pass']['id']
    studio.draft_gate_open({'agent': 'builder', 'pass_id': pass_id})

    order = root.order_create({'agent': 'builder', 'title': 'Report', 'folder': str(tmp_path)})['order']
    (tmp_path / 'report.txt').write_text('Report', encoding='utf-8')
    piece = root.piece_add({'agent': 'builder', 'order_id': order['id'], 'title': 'Report file',
                            'kind': 'text', 'path': 'report.txt'})['piece']
    gate = root.gate_open({'agent': 'builder', 'order_id': order['id'], 'title': 'Read report',
                           'piece_ids': [piece['id']]})['gate']
    server = serve(hub)
    try:
        url = hub.config.url + '/api/agora/pending_decisions'
        headers = {'Authorization': 'Bearer ' + Path(hub.config.token_file).read_text().strip()}
        status, all_spaces = http(url + '?all_spaces=1', headers=headers)
        assert status == 200
        assert all_spaces['counts'] == {'test-studio': 1, root.space_id: 1}
        draft, report = all_spaces['rows']
        assert draft['key'] == f'draft-{pass_id}'
        assert len(draft['variants']) == 3
        assert draft['variants'][0]['image']['piece_id'] == pieces['a']['keyframe_start']
        assert report['key'] == f'gate-{gate["id"]}'
        assert report['pieces'] == ['Report file']
        assert report['image'] is None
        status, scoped = http(url + '?space_id=test-studio', headers=headers)
        assert status == 200
        assert scoped['rows'] == [draft]
        assert scoped['counts'] == {'test-studio': 1}
        assert any(tool['name'] == 'hub_agora_pending_decisions' for tool in TOOLS)
    finally:
        server.shutdown()
        server.server_close()
        hub.close()

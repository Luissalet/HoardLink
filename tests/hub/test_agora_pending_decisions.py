from pathlib import Path

from hoard_link.hub.agora import TOOLS
from hoard_link.hub import agora_person
from tests.test_agora_drafts import draft_pass, ready

from ._hub_fakes import http, make_hub, serve, ui_headers


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
        assert draft['variants'][0]['signature']
        assert draft['variants'][0]['artifacts'][0]['image']['piece_id'] == pieces['a']['keyframe_start']
        assert report['key'] == f'gate-{gate["id"]}'
        assert report['pieces'] == ['Report file']
        assert report['image'] is None
        assert report['artifacts'] == [{'title': 'Report file', 'media': 'text',
                                        'path': f"/api/agora/piece_file/{piece['id']}", 'url': None, 'image': None}]
        status, scoped = http(url + '?space_id=test-studio', headers=headers)
        assert status == 200
        assert scoped['rows'] == [draft]
        assert scoped['counts'] == {'test-studio': 1}
        assert any(tool['name'] == 'hub_agora_pending_decisions' for tool in TOOLS)

        endpoint = hub.config.url + '/api/agora/'
        gate_choice = {'key': report['key'], 'decision': 'cancel', 'note': 'Check the numbers'}
        variant = draft['variants'][1]
        draft_choice = {'key': draft['key'], 'space_id': 'test-studio', 'decision': 'keep',
                        'variant_id': variant['id'], 'signature': variant['signature']}
        studio_header = {**headers, 'X-Hoard-Person': 'faustus-studio'}
        page_without_session = {'Sec-Fetch-Site': 'same-origin', 'Origin': hub.config.url}
        for operation, choice in [('production_decide', gate_choice), ('production_decide', draft_choice),
                                  ('gate_decide', {**gate_choice, 'gate_id': gate['id']}),
                                  ('draft_decide', draft_choice)]:
            status, _ = http(endpoint + operation, {**choice, '_person': True}, studio_header)
            assert status == 403
            status, denied = http(endpoint + operation, choice, page_without_session)
            assert status == 403 and denied['code'] == 'hub_ui_session_required'
        assert http(url + '?all_spaces=1', headers=headers)[1] == all_spaces
        assert not {'hub_agora_production_decide', 'hub_agora_gate_decide', 'hub_agora_draft_decide'} & {
            tool['name'] for tool in TOOLS}
        proof = {**studio_header, agora_person.PROOF_HEADER: agora_person.token(hub)}
        status, _ = http(endpoint + 'production_decide', {**draft_choice, 'key': f'draft-{pass_id + 1}'}, proof)
        assert status == 404
        status, _ = http(endpoint + 'production_decide', {**draft_choice, 'signature': 'stale'}, proof)
        assert status == 409
        status, result = http(endpoint + 'draft_decide', draft_choice, ui_headers(hub))
        assert status == 200 and result['kept'] == variant['id']
        assert http(url + '?space_id=test-studio', headers=headers)[1]['counts'] == {}
        status, result = http(endpoint + 'production_decide', gate_choice, proof)
        assert status == 200 and result['gate']['status'] == 'decided'
        result = http(endpoint + 'decisions', headers=headers)[1]['production_decisions']
        assert result[0]['note'] == 'Check the numbers'
        assert result[0]['decided_by']['id'] == root.person_id
        assert http(url + '?all_spaces=1', headers=headers)[1]['counts'] == {}
    finally:
        server.shutdown()
        server.server_close()
        hub.close()

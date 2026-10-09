"""Heartbeat metadata and renewal are independent; selection is exact, not a grant."""
import pytest
from hoard_link.hub.agora import AgoraError
from .test_agora import ag


def expiries(ag):
    return {row['resource']:row['expires'] for row in ag.locks({})['locks']}


def test_selected_heartbeat_records_state_and_renews_only_exact_owned_live_resources(ag):
    ag.lock({'agent':'faustus','resources':['path:Faustus/src/a.py','path:Faustus/src/b.py','model:reserved-qa'],'ttl_s':600})
    ag.lock({'agent':'other','resources':['port:61880'],'ttl_s':600})
    before=expiries(ag);ag.fake_clock.t+=20
    result=ag.heartbeat({'agent':'faustus','doing':'native project work','state':'waiting',
                         'renew_resources':['path:Faustus/src/a.py','port:61880','model:not-held-qa']})
    after=expiries(ag)
    assert result['ok']
    assert after['path:faustus/src/a.py']==before['path:faustus/src/a.py']+20
    assert all(after[key]==value for key,value in before.items() if key!='path:faustus/src/a.py')
    assert 'model:not-held-qa' not in after
    assert result['lock_renewal']['renewed_resources']==['path:faustus/src/a.py']
    actor=next(row for row in ag.board()['agents'] if row['id']=='faustus')
    assert actor['doing']=='native project work' and actor['state']=='waiting'


def test_empty_selection_and_parent_path_never_renew_descendant_locks(ag):
    ag.lock({'agent':'faustus','resources':['path:Faustus/src/a.py'],'ttl_s':600})
    before=expiries(ag);ag.fake_clock.t+=20
    for selection in ([],['path:Faustus/src']):
        assert ag.heartbeat({'agent':'faustus','doing':'metadata only','renew_resources':selection})['ok']
        assert expiries(ag)==before


def test_legacy_default_still_renews_all_owned_locks(ag):
    ag.lock({'agent':'faustus','resources':['model:reserved-qa','path:Faustus/src/a.py'],'ttl_s':600})
    before=expiries(ag);ag.fake_clock.t+=20
    assert ag.heartbeat({'agent':'faustus'})['ok']
    assert expiries(ag)=={key:value+20 for key,value in before.items()}


def test_expired_selection_does_not_mint_or_revive_a_lock(ag):
    ag.lock({'agent':'faustus','resources':['model:expired-qa'],'ttl_s':600})
    ag.fake_clock.t+=601
    result=ag.heartbeat({'agent':'faustus','renew_resources':['model:expired-qa']})
    assert result['ok'] and result['lock_renewal']['renewed_resources']==[]
    assert ag.locks({'check':'model:expired-qa'})['free']


def test_sync_passes_explicit_empty_selector_to_heartbeat(ag):
    ag.lock({'agent':'faustus','resources':['model:reserved-qa'],'ttl_s':600})
    before=expiries(ag);ag.fake_clock.t+=20
    result=ag.sync({'agent':'faustus','renew_resources':[]})
    assert result['heartbeat']['lock_renewal']['mode']=='selected' and expiries(ag)==before


def test_live_locks_advertise_the_backend_contract(ag):
    assert ag.locks({})['capabilities']['heartbeat_selective_renewal'] is True


@pytest.mark.parametrize('selection',[None,False,'model:reserved-qa',[None],[''],['no-colon']])
def test_invalid_selector_is_a_code_error_without_partial_metadata_write(ag,selection):
    ag.heartbeat({'agent':'faustus','doing':'before','renew_resources':[]})
    with pytest.raises(AgoraError):
        ag.heartbeat({'agent':'faustus','doing':'after','renew_resources':selection})
    assert next(row for row in ag.board()['agents'] if row['id']=='faustus')['doing']=='before'

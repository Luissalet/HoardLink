from hoard_link.hub.fleet_windows import cadence,metadata


def test_native_repeat_daily_weekday_and_unsupported_calendar_cadences():
    assert cadence([{'kind':'MSFT_TaskTimeTrigger','interval':'PT15M'}])=={'every':'900s'}
    assert cadence([{'kind':'MSFT_TaskDailyTrigger','days':1,'start':'2026-10-10T08:00:00'}])=={'at':'08:00'}
    assert cadence([{'kind':'MSFT_TaskWeeklyTrigger','weeks':1,'weekday_mask':62,'start':'2026-10-10T08:45:00'}])=={'at':'08:45','days':['mon','tue','wed','thu','fri']}
    assert cadence([{'kind':'MSFT_TaskLogonTrigger'}])=={}
    assert cadence([{'kind':'MSFT_TaskLogonTrigger','interval':'PT15M'}])=={}
    assert cadence([{'kind':'MSFT_TaskDailyTrigger','interval':'PT15M','duration':'PT1H'}])=={}
    assert cadence([{'kind':'MSFT_TaskTimeTrigger','interval':'PT15M','end':'2026-10-10T09:00:00'}])=={}
    assert cadence([{'interval':'PT15M'},{'interval':'PT1H'}])=={}


def test_running_is_not_a_completed_success_and_disabled_is_explicit_pause():
    row={'path':'\\','name':'QA task','state':'Running','result':267009,'last_start':'2026-10-10T08:00:00+00:00','triggers':[{'kind':'MSFT_TaskTimeTrigger','interval':'PT15M'}]}
    running=metadata(row,1791660000)
    assert running['last_success_ts'] is None and running['last_error_ts'] is None and running['active']
    done=metadata({**row,'state':'Ready','result':0},1791660000)
    assert done['last_status']=='ok' and done['last_success_ts'] is not None
    assert 'lower_bound' in done['timestamp_basis']
    assert metadata({**row,'state':'Disabled'},1791660000)['paused']
    assert running['id']==done['id'] and 'QA task' not in running['id']

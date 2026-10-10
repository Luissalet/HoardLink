"""Read declared Windows task metadata only. Never reads actions, arguments or credentials."""
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime

SCRIPT = r'''
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$names = ConvertFrom-Json $env:HOARD_FLEET_TASK_NAMES
$rows = @(Get-ScheduledTask | Where-Object {$names -contains ($_.TaskPath + $_.TaskName) -or $names -contains $_.TaskName} | ForEach-Object {
 $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath
 [pscustomobject]@{name=$_.TaskName;path=$_.TaskPath;state=[string]$_.State;created=$_.RegistrationInfo.Date;last_start=$info.LastRunTime.ToUniversalTime().ToString('o');result=[long]$info.LastTaskResult;
 triggers=@($_.Triggers | Where-Object {$_} | ForEach-Object {[pscustomobject]@{kind=$_.CimClass.CimClassName;enabled=$_.Enabled;start=$_.StartBoundary;
 interval=$_.Repetition.Interval;duration=$_.Repetition.Duration;end=$_.EndBoundary;delay=$_.RandomDelay;
 days=$_.DaysInterval;weeks=$_.WeeksInterval;weekday_mask=$_.DaysOfWeek}})}
})
ConvertTo-Json -InputObject $rows -Depth 4 -Compress
'''


def read_tasks(names):
    if os.name != 'nt': return {'ok': False, 'error': 'unsupported_platform', 'items': []}
    if not isinstance(names, list) or any(not isinstance(name, str) or not name or len(name)>260 for name in names):
        return {'ok': False, 'error': 'invalid_declared_tasks', 'items': []}
    if not names: return {'ok': True, 'items': []}
    env = {**os.environ, 'HOARD_FLEET_TASK_NAMES': json.dumps(names, ensure_ascii=False)}
    try:
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',SCRIPT],env=env,
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=8,creationflags=0x08000000)
        if result.returncode: return {'ok': False, 'error': 'task_scheduler_unavailable', 'items': []}
        rows = json.loads(result.stdout or '[]')
        return {'ok': True, 'items': rows if isinstance(rows,list) else [rows]}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'ok': False, 'error': 'task_scheduler_unavailable', 'items': []}


def cadence(triggers):
    active = [t for t in triggers if t.get('enabled') is not False]
    if len(active)!=1: return {}
    trigger=active[0]
    if trigger.get('duration') or trigger.get('end') or trigger.get('delay'): return {}
    match=re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?',str(trigger.get('interval') or ''))
    if match:
        if trigger.get('kind') != 'MSFT_TaskTimeTrigger': return {}
        days,hours,minutes,seconds=[int(x or 0) for x in match.groups()]
        period=days*86400+hours*3600+minutes*60+seconds
        return {'every':f'{period}s'} if period>=60 else {}
    try: start=datetime.fromisoformat(trigger.get('start') or '')
    except ValueError: return {}
    if start.tzinfo is not None: return {}
    at=start.strftime('%H:%M')
    if trigger.get('kind')=='MSFT_TaskDailyTrigger' and trigger.get('days')==1: return {'at':at}
    if trigger.get('kind')=='MSFT_TaskWeeklyTrigger' and trigger.get('weeks')==1:
        mask=trigger.get('weekday_mask') or 0
        days=[day for bit,day in ((1,'sun'),(2,'mon'),(4,'tue'),(8,'wed'),(16,'thu'),(32,'fri'),(64,'sat')) if mask & bit]
        return {'at':at,'days':days} if days else {}
    return {}


def metadata(row, now):
    identity=str(row.get('path') or '')+str(row.get('name') or '')
    key='windows:task:'+hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    try:
        when=datetime.fromisoformat(row.get('last_start') or '').timestamp()
        when=when if when>0 else None
    except (ValueError,OverflowError,OSError): when=None
    state=row.get('state')
    code=row.get('result')
    schedule=cadence(row.get('triggers') or [])
    for trigger in row.get('triggers') or []:
        if trigger.get('enabled') is False: continue
        try:
            if datetime.fromisoformat(trigger.get('start') or '').timestamp() > now: schedule={}
        except (ValueError, OverflowError, OSError): pass
    completed=state!='Running' and code==0 and when is not None
    failed=state!='Running' and isinstance(code,int) and code!=0 and code not in (267008,267009,267010,267011,267012,267013)
    try: created=datetime.fromisoformat(row.get('created') or '').timestamp()
    except (ValueError,OverflowError,OSError): created=None
    return {'id':key,'label':'Windows · '+str(row.get('name') or '')[:120],'kind':'scheduled_task','owner':'windows',
        'created_ts':created,'observed_ts':now,'paused':state=='Disabled','active':state=='Running',**schedule,
        'last_success_ts':when if completed else None,'last_error_ts':when if failed else None,
        'last_status':'ok' if completed else 'error' if failed else None,
        'error_code':f'task_scheduler_{code}' if failed else None,
        'cadence_unverified':not bool(schedule),
        'timestamp_basis':'completed_result_start_time_lower_bound; finish_time_not_available'}

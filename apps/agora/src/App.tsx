import React,{useEffect,useRef,useState} from 'react';
import {Activity,ArrowUpRight,ChevronDown,MessageSquare,PanelRightClose,PanelRightOpen,Plus,RefreshCw,Search,Send,Users,X,CheckCircle2,GitPullRequest,Clock3} from 'lucide-react';
import {api,openHub,openWriteSession,spaceId,embedded,hubUrl,lang,Leader,LiveState,Message,Task,Worker,providerNames} from './api';
import {locale,messageBody,spaceTitle,tr} from './locale';
import {confirmQuotaFloor,Quota,QuotaFloorSettings,WeeklyQuotaFloor,type QuotaFloorState,type QuotaProvider} from './Quota';
import {LiveView} from './LiveView';
import {DeliveryDiff} from './DeliveryDiff';
import {DecisionRegister} from './DecisionRegister';
import {Foreman, type ForemanStatus} from './Foreman';
import {DecisionButton,ProductionDecisions,useProductionDecisions} from './ProductionDecisions';
import {CodexQuotaNotice,loadWorkerProfile,WorkerAssignment,WorkerProfile,WorkerProfileButton,WorkerTemplates} from './WorkerProfile';
const initial:Leader[]=Object.entries(providerNames).map(([id,name])=>({id,name,state:'unknown',catalog:[]}));
const labels:Record<string,string>={
 available:tr('Conectado','Connected'), unverified:tr('Sin verificar','Unverified'),
 in_progress:tr('En curso','In progress'), working:tr('Trabajando','Working'), running:tr('Trabajando','Running'),
 idle:tr('Disponible','Available'), ready:tr('Disponible','Available'), queued:tr('En cola','Queued'),
 unavailable:tr('No disponible','Unavailable'), unknown:tr('Sin datos','Unknown'), done:tr('Terminada','Done'),
 claimed:tr('En curso','In progress'), review:tr('En revisión','In review'), approved:tr('Aprobada','Approved'),
 changes:tr('Cambios pedidos','Changes requested'), open:tr('Abierta','Open'), failed:tr('Interrumpido','Interrupted'),
 incomplete:tr('Entrega incompleta','Incomplete delivery'), blocked:tr('Bloqueada','Blocked'),
 starting:tr('Iniciando','Starting'), completed:tr('Entrega comprobada','Delivery verified'),
 not_started:tr('No arrancó','Did not start'),
 stopped:tr('Detenido','Stopped'), stopping:tr('Deteniendo','Stopping'), dropped:tr('Descartada','Dropped'),
};
const status=(value:string)=>labels[value]||value;
const messageLabels:Record<string,string>={
 proposal:tr('Propuesta','Proposal'), decision:tr('Decisión','Decision'), handoff:tr('Traspaso','Handover'),
 approve:tr('Aprobación','Approval'), changes:tr('Cambios pedidos','Changes requested'),
 agree:tr('De acuerdo','Agree'), disagree:tr('En desacuerdo','Disagree'),
 question:tr('Pregunta','Question'), reply:tr('Respuesta','Reply'), system:tr('Sistema','System'),
};
const messageKind=(value:string)=>messageLabels[value]||value;
// Colour per provider (Agora #239): outlines that leader's messages and avatar. Saved in the Hub so
// the browser and the desktop app agree; these are the defaults until the person picks one.
export const defaultColors:Record<string,string>={claude:'#d97757',codex:'#10a37f',cursor:'#7aa2f7',faustus:'#d0545b'};
type WorkerFileProposal={read_files:string[];read_paths:string[];asset_folders:string[]};
const parsePathList=(paths:string)=>paths.split(/\r?\n/).map(path=>path.trim()).filter(Boolean);

const providerOf=(agent?:string)=>{const head=(agent||'').split('-')[0];return head in defaultColors?head:'';};
const time=(value?:number)=>value?new Date(value*1000).toLocaleTimeString(locale,{hour:'2-digit',minute:'2-digit'}):'';
// Live state of the leader's CLI (task #264), read by the Hub from that CLI's own session files.
const liveLabels:Record<string,string>={working:status('working'),waiting:tr('Esperando','Waiting'),
 auth_expired:tr('Sesión caducada','Login expired'),error:'Error',unknown:status('unknown')};
const clock=(value?:string|null)=>{if(!value)return '';const d=new Date(value);return isNaN(d.getTime())?'':d.toLocaleTimeString(locale,{hour:'2-digit',minute:'2-digit'});};
function liveText(state:LiveState) {
 if(state.state!=='idle')return liveLabels[state.state]||state.state;
 const since=clock(state.last_activity_at);
 return since ? tr(`Inactivo desde ${since}`,`Idle since ${since}`) : tr('Inactivo','Idle');
}
function Live({leader}:{leader:Leader}){const s=leader.live_state;if(!s)return null;const n=s.open_tasks?.length||0;return <div className={`live ${s.state}${leader.idle_with_work?' warn':''}`} data-testid={`live-${leader.id}`} title={s.detail_i18n?.[lang] || s.detail}><span className="live-chip"><i/>{liveText(s)}</span>{leader.idle_with_work&&<span className="live-work" role="status">{n===1 ? tr('1 tarea abierta sin atender','1 unattended open task') : tr(`${n} tareas abiertas sin atender`,`${n} unattended open tasks`)}</span>}</div>;}

const workerText=lang==='es'?{
 close:'Cerrar ayudantes',title:(name:string)=>`Ayudantes de ${name}`,worker:'Ayudante',defaultEffort:'predeterminado',
 view:(name:string)=>`Ver ayudantes de ${name}`,summary:(count:number)=>`${count} ${count===1?'ayudante':'ayudantes'} · ver resultados`,
 pending:'Resultado pendiente de revisión',workspace:'Carpeta de trabajo',tools:(ok:number,bad:number)=>`${ok} herramientas correctas · ${bad} fallidas`,
 stop:'Detener turno',continue:'Continuar',message:'Mensaje de seguimiento',send:'Enviar seguimiento',cancel:'Cancelar',
 replace:'Crear relevo',task:'Tarea que se traspasará',selectTask:'Selecciona una tarea',noTask:'Este trabajador no tiene tareas abiertas en este espacio.',
 waiting:(id:number)=>`Esperando el primer latido del relevo para traspasar la tarea #${id}.`,
 transferred:(id:number)=>`Tarea #${id} traspasada al relevo.`,
 unavailable:'La sesión original no está disponible.',missingWorkspace:'No se encuentra la carpeta de trabajo original.',
 missingReceipt:'El recibo no identifica la sesión original.',replaceHint:'Puedes crear un relevo con el informe anterior y traspasarle una tarea.',
}:{
 close:'Close workers',title:(name:string)=>`${name} workers`,worker:'Worker',defaultEffort:'default',
 view:(name:string)=>`View ${name} workers`,summary:(count:number)=>`${count} ${count===1?'worker':'workers'} · view results`,
 pending:'Result awaiting review',workspace:'Workspace',tools:(ok:number,bad:number)=>`${ok} successful tools · ${bad} failed`,
 stop:'Stop turn',continue:'Continue',message:'Follow-up message',send:'Send follow-up',cancel:'Cancel',
 replace:'Replace worker',task:'Task to hand over',selectTask:'Select a task',noTask:'This worker owns no open tasks in this space.',
 waiting:(id:number)=>`Waiting for the replacement’s first heartbeat to hand over task #${id}.`,
 transferred:(id:number)=>`Task #${id} handed over to the replacement.`,
 unavailable:'The original session is unavailable.',missingWorkspace:'The original workspace is missing.',
 missingReceipt:'The receipt does not identify the original session.',replaceHint:'You can start a replacement with the previous report and hand over a task.',
};
function workerStatus(worker:Worker){
 if(worker.startup)return status(worker.startup.state);
 if(worker.state==='completed'&&worker.verification_warnings?.length)return tr('Entrega con avisos','Delivery with warnings');
 return status(worker.state);
}
const orphanLabel=tr('Proceso huérfano activo','Live orphan process');
function rosterWorkerStatus(worker:Worker){
 if(!worker.orphan_process)return workerStatus(worker);
 return worker.startup ? `${orphanLabel} · ${workerStatus(worker)}` : orphanLabel;
}
const startupSummary=(count:number)=>count===1 ? tr('1 no arrancó','1 did not start')
 : tr(`${count} no arrancaron`,`${count} did not start`);
const workerDate=new Intl.DateTimeFormat(locale,{dateStyle:'short',timeStyle:'short'});
function workerAge(seconds:number){
 const [unit,count]:[string,number]=seconds>=86400?['day',seconds/86400]:seconds>=3600?['hour',seconds/3600]:
  seconds>=60?['minute',seconds/60]:['second',seconds];
 return new Intl.NumberFormat(locale,{style:'unit',unit,unitDisplay:'long'}).format(Math.floor(count));
}
function orphanSummary(count:number){
 return count===1?tr('1 proceso huérfano activo','1 live orphan process'):
  tr(`${count} procesos huérfanos activos`,`${count} live orphan processes`);
}
type WorkerAction='stop'|'stop_orphan'|'continue'|'replace';

function WorkerDetail({worker,tasks,busy,control}:{worker:Worker;tasks:Task[];busy:boolean;
 control:(worker:Worker,action:WorkerAction,message?:string,taskId?:number)=>Promise<void>}){
 const [open,setOpen]=useState(false),[message,setMessage]=useState(''),[taskId,setTaskId]=useState(0);
 const [error,setError]=useState<{action:WorkerAction;text:string}>();
 const [confirmStop,setConfirmStop]=useState<Worker['orphan_process']>();
 const form=useRef<HTMLTextAreaElement>(null);
 const orphan=worker.orphan_process;
 const detail=worker.detail_i18n?.[lang] || worker.detail;
 const confirming=orphan&&confirmStop?.pid===orphan.pid&&confirmStop.started_at===orphan.started_at;
 const handover=worker.replacement;
 const replacementActive=handover?.state==='waiting_for_worker'||handover?.state==='transferred';
 const owned=tasks.filter(task=>task.owner===worker.agent_id&&!['done','dropped'].includes(task.state));
 const replace=!orphan&&Boolean(worker.resume_issue)&&!replacementActive;
 const canWrite=!orphan&&(worker.can_continue||(replace&&owned.length>0));
 const issue=worker.resume_issue==='missing_workspace'?workerText.missingWorkspace:worker.resume_issue==='missing_receipt'?workerText.missingReceipt:workerText.unavailable;
 const pickedTask=owned.some(task=>task.id===taskId)?taskId:owned.length===1?owned[0].id:0;
 useEffect(()=>{if(open&&canWrite)form.current?.focus();},[open,canWrite]);

 async function act(action:WorkerAction){
  setError(undefined);
  try{
   await control(worker,action,message,action==='replace'?pickedTask:undefined);
   if(action==='stop_orphan')setConfirmStop(undefined);
   if(action==='continue'||action==='replace'){setOpen(false);setMessage('');}
  }catch(err){setError({action,text:(err as Error).message});}
 }

 return <article className="worker-detail">
  <h3>{worker.agent_id||`${workerText.worker} ${worker.id.slice(0,8)}`}</h3>
  <p>{worker.model} · {worker.effort||workerText.defaultEffort}</p>
  <p>{workerStatus(worker)}{!worker.startup&&worker.outcome==='unverified'&&` · ${workerText.pending}`}</p>
  {worker.startup&&<p>{worker.startup.detail_i18n[lang]}</p>}
  <p>{worker.task}</p>
  {orphan&&<>
   <p role="status"><strong>{orphanLabel}</strong>{` · PID ${orphan.pid} · ${workerAge(orphan.age_seconds)}`}</p>
   <p>{tr('Última actividad registrada','Last recorded activity')} {orphan.last_activity_at==null?
    tr('Sin datos','Unknown'):workerDate.format(orphan.last_activity_at*1000)}</p>
  </>}
  {worker.workspace&&<p>{workerText.workspace} {worker.workspace}</p>}
  {worker.faustus_url&&<p>Faustus {worker.faustus_url}</p>}
  {worker.tool_approval_mode==='auto'&&<p>{tr('Aprobación automática · Ficheros limitados al encargo', 'Automatic approval · Files limited to the assignment')}</p>}
  {worker.tool_success_count!==undefined&&<p>{workerText.tools(worker.tool_success_count,worker.tool_failure_count||0)}</p>}
  {detail&&<p>{detail}</p>}
  {worker.resume_issue&&<p role="status">{issue} {replace&&(owned.length?workerText.replaceHint:workerText.noTask)}</p>}
  {handover?.state==='waiting_for_worker'&&handover.task_id&&<p role="status">{workerText.waiting(handover.task_id)}</p>}
  {handover?.state==='transferred'&&handover.task_id&&<p role="status">{workerText.transferred(handover.task_id)}</p>}
  {handover?.state==='failed'&&handover.detail&&<p role="alert">{handover.detail}</p>}
  {error&&(!worker.resume_issue||error.action!=='continue')&&<p role="alert">{error.text}</p>}
  {!orphan&&(worker.state==='running'||(worker.parent_id==='faustus'&&worker.state==='unknown'))&&
   <button className="model-selector" disabled={busy} onClick={()=>void act('stop')}>{workerText.stop}</button>}
  {orphan&&(confirming?<div>
   <p>{tr('Se terminará este proceso y sus procesos hijos. Esta acción no se puede deshacer.',
    'This process and its child processes will be terminated. This action cannot be undone.')}</p>
   <button className="primary" disabled={busy} onClick={()=>void act('stop_orphan')}>{tr('Confirmar detención','Confirm stop')}</button>{' '}
   <button className="icon-button" disabled={busy} onClick={()=>setConfirmStop(undefined)}>{workerText.cancel}</button>
  </div>:<button className="model-selector" disabled={busy} onClick={()=>setConfirmStop(orphan)}>
   {tr('Detener proceso','Stop process')}
  </button>)}
  {canWrite&&!open&&<button className="model-selector" disabled={busy} onClick={()=>setOpen(true)}>{replace?workerText.replace:workerText.continue}</button>}
  {canWrite&&open&&<form onSubmit={event=>{event.preventDefault();void act(replace?'replace':'continue');}}>
   <label>{workerText.message}<textarea ref={form} value={message} maxLength={4000} onChange={event=>setMessage(event.target.value)}/></label>
   {replace&&<label>{workerText.task}<select value={pickedTask} required onChange={event=>setTaskId(Number(event.target.value))}>
    <option value={0} disabled>{workerText.selectTask}</option>
    {owned.map(task=><option key={task.id} value={task.id}>#{task.id} {task.title}</option>)}
   </select></label>}
   <button className="primary" disabled={busy||(replace&&!pickedTask)}>{replace?workerText.replace:workerText.send}</button>{' '}
   <button type="button" className="icon-button" onClick={()=>setOpen(false)}>{workerText.cancel}</button>
  </form>}
  {worker.result&&<div className="worker-result">{worker.result}</div>}
 </article>;
}

export default function App(){
 const [live,setLive]=useState(false);
 const [quotaFloors,setQuotaFloors]=useState<QuotaFloorState|null>(null);
 const [person,setPerson]=useState<{id:string;name:string}>();
 const [decisionsOpen,setDecisionsOpen]=useState(false);
 const [foreman,setForeman]=useState<ForemanStatus|null>(null);
 const decisions=useProductionDecisions(decisionsOpen);
 const [fileJob,setFileJob]=useState(''),[readPaths,setReadPaths]=useState(''),[writePaths,setWritePaths]=useState('');
 const [fileProposal,setFileProposal]=useState<{task:string;files:WorkerFileProposal}|null>(null);
 const [workerNotice,setWorkerNotice]=useState('');
 const [workerError,setWorkerError]=useState('');
 const [leaders,setLeaders]=useState(initial),[usage,setUsage]=useState<QuotaProvider[]>([]),[workers,setWorkers]=useState<Worker[]>([]),[messages,setMessages]=useState<Message[]>([]),[tasks,setTasks]=useState<Task[]>([]),[counts,setCounts]=useState<any>({}),[error,setError]=useState(''),[sending,setSending]=useState(false),[draft,setDraft]=useState(()=>localStorage.getItem(spaceId==='ecosystem'?'agora-draft':'agora-draft:'+spaceId)||''),[recipient,setRecipient]=useState('all'),[panel,setPanel]=useState(()=>window.innerWidth>900),[tab,setTab]=useState('pending'),[filter,setFilter]=useState(''),[selected,setSelected]=useState<Leader|null>(null),[helper,setHelper]=useState(false),[model,setModel]=useState(''),[effort,setEffort]=useState(''),[assignment,setAssignment]=useState(''),[busy,setBusy]=useState(false),[workerLeader,setWorkerLeader]=useState<Leader|null>(null),[thread,setThread]=useState<any>(null),[reply,setReply]=useState(''),[connection,setConnection]=useState(tr('Conectando', 'Connecting')),[busyWorker,setBusyWorker]=useState('');
 const messageEnd=useRef<HTMLDivElement>(null),lastId=useRef<number>(0),workThread=useRef<number>(spaceId==='ecosystem'?32:0);
 const [spaces,setSpaces]=useState<{id:string;title:string}[]>([]);
 const currentSpaceTitle=spaceTitle(spaceId,spaces.find(space=>space.id===spaceId)?.title);
 useEffect(()=>{void openWriteSession().catch(error=>setError(error.message));},[]);
 useEffect(()=>{api("/api/agora/spaces").then(d=>setSpaces(d.spaces||[])).catch(()=>{});},[]);
 function switchSpace(id:string){localStorage.setItem(spaceId==='ecosystem'?'agora-draft':'agora-draft:'+spaceId,draft);const url=new URL(location.href);url.searchParams.set("space_id",id);location.assign(url);}

 const [profileOpen,setProfileOpen]=useState(false),[templates,setTemplates]=useState<WorkerTemplates>(),[template,setTemplate]=useState(''),[codexQuotaFailed,setCodexQuotaFailed]=useState(false);
 const colorOf=(agent?:string)=>{const id=providerOf(agent);if(!id)return undefined;return leaders.find(l=>l.id===id)?.color||defaultColors[id];};
 async function pickColor(l:Leader,color:string){setLeaders(prev=>prev.map(x=>x.id===l.id?{...x,color}:x));try{await api('/api/agora/console/color',{leader_id:l.id,color});}catch(e){setError((e as Error).message);}}
 const inflight=useRef(new Set<string>()),routeHealth=useRef(new Map<string,boolean>());
 const overviewAvailable=useRef(false);
 async function refresh(){
  const routes=['/api/agora/console/overview','/api/agora/console/usage',`/api/agora/threads/${workThread.current}/recent?limit=60`,'/api/agora/board'];
  await Promise.allSettled(routes.map(async(path,i)=>{
   if((i===2&&!workThread.current)||inflight.current.has(path))return;
   inflight.current.add(path);
   try{
    const d=await api(path);routeHealth.current.set(path,true);
    if(d.person)setPerson(d.person);
    if(i===0)setForeman(d.foreman ?? null);
    if(i===0){overviewAvailable.current=true;if(d.thread_id)workThread.current=d.thread_id;const overviewMessages=Array.isArray(d.messages)?d.messages:d.messages?.messages;if(overviewMessages)setMessages(overviewMessages.map((m:Message)=>({...m,agent:m.agent||m.author})));if(d.leaders)setLeaders(d.leaders);if(d.workers)setWorkers(d.workers);setCodexQuotaFailed(d.capacity_observations?.codex?.ok===false);}
    if(i===1)setUsage(d.providers||[]);
    if(i===2)setMessages((d.messages||d.posts||[]).map((m:Message)=>({...m,agent:m.agent||m.author})));
    if(i===3){setQuotaFloors(d.quota_floors ?? null);setTasks((d.tasks||[]).map((t:Task)=>({...t,state:t.state||t.status||'unknown'})));setCounts(d.counts||d.task_counts||{});
     if(!overviewAvailable.current)setLeaders(initial.map(l=>{const agents=(d.agents||[]).filter((a:any)=>a.id===l.id||a.id.startsWith(l.id+'-'));return {...l,state:agents.some((a:any)=>!a.stale&&a.state==='working')?'working':agents.some((a:any)=>!a.stale)?'idle':'unknown',detail:agents[0]?.doing};}));}
   }catch{routeHealth.current.set(path,false);if(i===0)overviewAvailable.current=false;}
   finally{inflight.current.delete(path);}
  }));
  const online=Array.from(routeHealth.current.values()).some(Boolean);setConnection(online ? tr('Hub conectado', 'Hub connected') : tr('Hub sin conexión', 'Hub disconnected'));
  const notice=tr('No se puede conectar con el Hub. Puedes conservar el borrador y volver a intentarlo.',
    'The Hub is unavailable. You can keep your draft and try again.');
  if(!online)setError(notice);else setError(previous=>previous===notice?'':previous);
 }

 useEffect(()=>{if(live)return;void refresh();const id=setInterval(refresh,5000);return()=>clearInterval(id);},[live]);
 useEffect(()=>{localStorage.setItem(spaceId==='ecosystem'?'agora-draft':'agora-draft:'+spaceId,draft);},[draft]);
 useEffect(()=>{const media=window.matchMedia('(max-width:900px)');const change=()=>{if(media.matches)setPanel(false);};media.addEventListener('change',change);return()=>media.removeEventListener('change',change);},[]);
 useEffect(()=>{if(!selected&&!thread&&!workerLeader)return;const previous=document.activeElement as HTMLElement|null;const dialog=document.querySelector<HTMLElement>('[role="dialog"]');const focusable=()=>Array.from(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),select,textarea,a[href],input')||[]);focusable()[0]?.focus();const key=(e:KeyboardEvent)=>{if(e.key==='Escape'){setSelected(null);setThread(null);setWorkerLeader(null);}if(e.key==='Tab'){const nodes=focusable(),first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}};document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);previous?.focus();};},[Boolean(selected),Boolean(thread),Boolean(workerLeader)]);
 useEffect(()=>{const id=messages.at(-1)?.id||0;if(id!==lastId.current){messageEnd.current?.scrollIntoView({behavior:lastId.current?'smooth':'instant'});lastId.current=id;}},[messages]);
 async function send(){if(!draft.trim()||sending)return;setSending(true);setError('');try{await api('/api/agora/console/message',{body:draft,recipients:recipient==='all'?initial.map(l=>l.id):[recipient]});setDraft('');await refresh();}catch(e){setError((e as Error).message);}finally{setSending(false);}}
 async function controlWorker(worker:Worker,action:WorkerAction,message?:string,taskId?:number){
  if(busyWorker)return;
  const confirm_quota_floor=action==='replace'?confirmQuotaFloor(quotaFloors,worker.parent_id):undefined;
  if(confirm_quota_floor===null)return;
  setBusyWorker(worker.id);
  try{
   const orphan=worker.orphan_process;
   const confirm_process=action==='stop_orphan'&&orphan?{pid:orphan.pid,started_at:orphan.started_at}:undefined;
   const result=await api('/api/agora/console/worker',{action,worker_id:worker.id,message,task_id:taskId,confirm_process,confirm_quota_floor,lang});
   if(result.state)setWorkers(previous=>previous.map(item=>item.id===worker.id?{...item,state:result.state,can_continue:false}:item));
  }finally{await refresh();setBusyWorker('');}
 }
 function configure(l:Leader,child=false){setSelected(l);setHelper(child);setModel(l.model||l.catalog?.[0]?.id||'');setEffort(l.effort||'');setAssignment('');setTemplate('');setFileJob('');setReadPaths('');setWritePaths('');setFileProposal(null);setWorkerNotice('');setWorkerError('');if(child&&!templates)void loadWorkerProfile().then(r=>setTemplates(r.templates)).catch(()=>{});}
 async function previewWorkerFiles(){
  const task=fileJob;
  if(!task)return;
  setBusy(true);setError('');
  try{
   const result=await api<{file_scope:WorkerFileProposal}>('/api/agora/console/worker',{action:'propose_files',task_id:Number(task)});
   setFileProposal({task,files:result.file_scope});
  }catch(e){setError((e as Error).message);}finally{setBusy(false);}
 }
 async function save(){
  if(!selected)return;
  const confirm_quota_floor=helper?confirmQuotaFloor(quotaFloors,selected.id):undefined;
  if(confirm_quota_floor===null)return;
  setBusy(true);setError('');setWorkerError('');
  const files=selected.id==='faustus'?{
   ...(fileJob?{task_id:Number(fileJob)}:{}),
   ...(readPaths.trim()||writePaths.trim()?{file_scope:{read_paths:parsePathList(readPaths),write_paths:parsePathList(writePaths)}}:{})
  }:{};
  try{
   const result=await api<{warning_i18n?:Record<typeof lang,string>}>(helper?'/api/agora/console/worker':'/api/agora/console/preferences',
    helper?{leader_id:selected.id,model,effort,task:assignment,confirm_quota_floor,lang,...(template?{template}:{}),...files}:{leader_id:selected.id,model,effort});
   setWorkerNotice(result.warning_i18n?.[lang] || '');
   setSelected(null);await refresh();
  }catch(e){
   if(helper){setWorkerError((e as Error).message);await refresh();}
   else setError((e as Error).message);
  }finally{setBusy(false);}
 }
 async function openTask(task:Task){setError('');try{const detail=await api(`/api/agora/tasks/${task.id}`);setThread(await api(`/api/agora/threads/${detail.task.thread_id}`));}catch(e){setError((e as Error).message);}}
 const pending=tasks.filter(t=>!['done','dropped'].includes(t.state||t.status||'')).filter(t=>`${t.title} ${t.owner} ${t.repo} ${t.id}`.toLowerCase().includes(filter.toLowerCase()));
 if(live)return <LiveView onClose={()=>setLive(false)}/>;
 if(profileOpen)return <WorkerProfile onClose={()=>{setProfileOpen(false);requestAnimationFrame(()=>document.querySelector<HTMLButtonElement>('.worker-profile-trigger')?.focus());}}/>;
 return <div className={`app ${panel?'':'panel-hidden'} ${embedded?'app--embedded':''}`}><header className="topbar">{embedded?<span className="space-context" title={currentSpaceTitle}>Ágora · {currentSpaceTitle}</span>:(<label className="space-selector">Ágora<select aria-label="Ágora" value={spaceId} onChange={e=>switchSpace(e.target.value)}>{spaces.length?spaces.map(v=><option key={v.id} value={v.id}>{spaceTitle(v.id,v.title)}</option>):<option value={spaceId}>{currentSpaceTitle}</option>}</select></label>)}<a className="brand" href="#"><span className="brand-mark"><Users size={23}/></span><h1>Ágora</h1></a><span className="workspace-name">Faustus & Hoards</span><button className="icon-button live-toggle" aria-label={tr("En directo","Live")} title={tr("En directo","Live")} onClick={()=>setLive(true)}><Activity size={17}/><span>{tr('En directo','Live')}</span></button><WorkerProfileButton onClick={()=>setProfileOpen(true)}/><DecisionRegister/><span className="connection"><i className={connection===tr('Hub conectado','Hub connected')?'online':''}/>{connection}</span><a className="hub-link" href={hubUrl} target="_blank" rel="noreferrer" onClick={e=>{void openHub(e).catch(error=>setError(error.message));}}>Hoard Hub <ArrowUpRight size={14}/></a><button className="icon-button" onClick={()=>{setError('');void refresh();}} aria-label={tr("Actualizar","Refresh")}><RefreshCw size={17}/></button><button className="icon-button" onClick={()=>setPanel(!panel)} aria-label={panel?tr('Ocultar tareas','Hide tasks'):tr('Mostrar tareas','Show tasks')}>{panel?<PanelRightClose size={18}/>:<PanelRightOpen size={18}/>}</button></header>
 <aside className="roster"><div className="section-heading"><h2>{tr('Equipo','Team')}</h2><span>{tr(`${leaders.length} líderes`,`${leaders.length} leaders`)}</span><span className="roster-mobile-hint">{tr('Desliza para ver todos','Swipe to see everyone')}</span></div><DecisionButton count={decisions.state?.rows.length} open={decisionsOpen} toggle={()=>setDecisionsOpen(value=>!value)}/>{quotaFloors&&<QuotaFloorSettings policy={quotaFloors} refresh={refresh}/>}<div className="leader-list">{leaders.map(l=>{
 const children=workers.filter(w=>w.parent_id===l.id);
 const orphans=children.filter(w=>w.orphan_process).length;
 const startupFailures=children.filter(w=>w.startup).length;
 const workerIssues=[orphans&&orphanSummary(orphans),startupFailures&&startupSummary(startupFailures)].filter(Boolean).join(' · ');
 return <section className="leader" key={l.id}><div className="leader-heading"><span className={`avatar ${l.id}`} style={{'--c':l.color||defaultColors[l.id]} as React.CSSProperties}>{l.name.slice(0,1)}</span><div><h3>{l.name}</h3>{(!l.live_state||status(l.state)!==liveText(l.live_state))&&<span title={l.detail} className={`state ${l.state}`}><i/>{status(l.state)}</span>}</div><label className="swatch" title={tr(`Color de ${l.name} en el chat`,`${l.name} chat colour`)} style={{'--c':l.color||defaultColors[l.id]} as React.CSSProperties}><input type="color" aria-label={tr(`Color de ${l.name} en el chat`,`${l.name} chat colour`)} value={l.color||defaultColors[l.id]} onChange={e=>void pickColor(l,e.target.value)}/></label><button className="icon-button add-helper" aria-label={tr(`Crear ayudante de ${l.name}`,`Create ${l.name} worker`)} onClick={()=>configure(l,true)}><Plus size={16}/></button></div><Live leader={l}/><button className="model-selector" onClick={()=>configure(l)} aria-label={tr(`Configurar modelo de ${l.name}`,`Configure ${l.name} model`)}><span>{l.model||tr('Consultar modelos','Browse models')}{l.effort&&<small>{l.effort}</small>}</span><ChevronDown size={14}/></button>{children.length>0&&<button className="worker-summary-control" onClick={()=>setWorkerLeader(l)} aria-label={workerText.view(l.name)+(workerIssues?' · '+workerIssues:'')}>{workerText.summary(children.length)}{workerIssues&&<> · <strong>{workerIssues}</strong></>}</button>}<Quota provider={usage.find(p=>p.provider_id===l.id)}/><WeeklyQuotaFloor policy={quotaFloors} provider={l.id}/>{l.id==='codex'&&codexQuotaFailed&&<CodexQuotaNotice/>}{children.map(w=><div className="worker" key={w.id}><span className="branch"/><div><strong>{w.id}</strong><span>{rosterWorkerStatus(w)} · {w.model}</span><p>{w.task}</p>{w.startup&&<p>{w.startup.detail_i18n[lang]}</p>}{w.result&&<details><summary>{tr('Resultado','Result')}</summary><p>{w.result}</p></details>}</div></div>)}</section>;})}</div><footer className="roster-footer"><span className="local-dot"/>{tr('Trabajo local','Local work')}<span>{tr('Los ayudantes comparten la cuota de su proveedor.','Workers share their provider’s quota.')}</span></footer></aside>
 <main className="conversation"><div className="conversation-head"><div><h2>{tr('Trabajo compartido', 'Shared work')}</h2></div><div className="participants" aria-label={tr("Claude, Codex, Cursor y Faustus","Claude, Codex, Cursor and Faustus")}>{initial.map(l=><span key={l.id} className={`mini-avatar ${l.id}`} title={l.name}>{l.name[0]}</span>)}</div></div>{foreman && <Foreman status={foreman}/>}<ProductionDecisions data={decisions} open={decisionsOpen}/>{error&&<div className="error" role="alert"><span>{error}</span><button className="icon-button" onClick={()=>setError('')} aria-label={tr('Cerrar aviso', 'Dismiss notice')}><X size={15}/></button></div>}{workerNotice&&<div className="error" role="status"><span>{workerNotice}</span><button className="icon-button" onClick={()=>setWorkerNotice('')} aria-label={tr('Cerrar aviso', 'Dismiss notice')}><X size={15}/></button></div>}<div className="messages" role="log" aria-label={tr('Mensajes de equipo', 'Team messages')} aria-live="polite">{messages.length===0?<div className="empty"><MessageSquare size={30}/><h3>{tr('Una conversación para todo el equipo', 'Team conversation')}</h3><p>{tr('El mensaje se guarda en el Ágora y llega a los responsables que elijas.', 'The message is saved in the Agora and sent to the leaders you select.')}</p></div>:messages.map(m=>person&&m.agent===person.id?<article className="message human" aria-label={person.name} key={m.id}><div className="message-content"><div className="message-body bubble">{messageBody(m)}</div><div className="message-meta"><time>{time(m.created||m.ts)}</time>{m.kind&&m.kind!=='comment'&&<span>{messageKind(m.kind)}</span>}</div></div></article>:<article className={`message agent ${providerOf(m.agent)?'provider':''}`} key={m.id} style={colorOf(m.agent)?{'--c':colorOf(m.agent)} as React.CSSProperties:undefined}><span className={`message-avatar ${m.agent?.split('-')[0]||''}`}>{(m.agent||m.author||'?')[0].toUpperCase()}</span><div className="message-content"><div className="message-meta"><strong>{m.agent||m.author}</strong><time>{time(m.created||m.ts)}</time>{m.kind&&m.kind!=='comment'&&<span>{messageKind(m.kind)}</span>}</div><div className="message-body">{messageBody(m)}</div></div></article>)}<div ref={messageEnd}/></div><div className="composer-wrap"><div className="composer"><label className="recipient">{tr('Para', 'To')} <select aria-label={tr('Destinatarios del mensaje', 'Message recipients')} value={recipient} onChange={e=>setRecipient(e.target.value)}><option value="all">{tr('Todo el equipo', 'Entire team')}</option>{initial.map(l=><option key={l.id} value={l.id}>{l.name}</option>)}</select></label><textarea maxLength={20000} aria-label={tr('Mensaje al equipo', 'Message to the team')} placeholder={tr('¿Qué vamos a hacer?', 'What are we working on?')} value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();void send();}}}/><div className="composer-actions"><span>{tr('Ctrl + Intro para enviar', 'Ctrl + Enter to send')}</span><button className="primary" disabled={sending||!draft.trim()} onClick={send}>{sending ? tr('Enviando…', 'Sending…') : tr('Enviar', 'Send')}<Send size={15}/></button></div></div><p className="composer-note">{tr('La entrega y la ejecución se muestran por separado. Un agente sin conexión conserva su encargo.', 'Delivery and execution are shown separately. Agents keep their assignments while offline.')}</p></div></main>
 {panel&&<aside className="operations"><div className="section-heading"><h2>{tr('Trabajo', 'Work')}</h2><a href={hubUrl} target="_blank" rel="noreferrer" onClick={e=>{void openHub(e).catch(error=>setError(error.message));}} aria-label={tr("Abrir todas las operaciones","Open all operations")}><ArrowUpRight size={16}/></a></div><div className="task-tabs"><button className={tab==='pending'?'active':''} onClick={()=>setTab('pending')} >{tr('Pendiente','Pending')} <span>{pending.length}</span></button><button className={tab==='done'?'active':''} onClick={()=>setTab('done')}>{tr('Finalizado','Completed')}</button></div><label className="search"><Search size={15}/><input aria-label={tr("Buscar tareas","Search tasks")} placeholder={tr("Buscar tarea, agente, proyecto…","Search tasks, agents, projects…")} value={filter} onChange={e=>setFilter(e.target.value)}/></label><div className="task-list">{(tab==='pending'?pending:tasks.filter(t=>t.state==='done')).map(t=><button className="task-row" key={t.id} onClick={()=>openTask(t)}><span className="task-number">#{t.id}{['review','approved'].includes(t.state)?<GitPullRequest size={13}/>:t.state==='done'?<CheckCircle2 size={13}/>:<Clock3 size={13}/>}</span><strong>{t.title}</strong><span className="task-meta">{t.repo||tr('Equipo','Team')}<span>{status(t.state||t.status||'')}</span></span><small>{t.owner||tr('Sin asignar','Unassigned')}</small>{t.waiting_on?.length?<small className="task-deps">{tr('Espera a','Waiting for')} {t.waiting_on.map(i=>'#'+i).join(', ')}</small>:null}</button>)}</div><div className="operations-footer"><span><CheckCircle2 size={14}/>{counts.done??'—'} {tr('hechas','done')}</span><a href={hubUrl} target="_blank" rel="noreferrer" onClick={e=>{void openHub(e).catch(error=>setError(error.message));}}>{tr('Hilos, decisiones y bloqueos','Threads, decisions and locks')} <ArrowUpRight size={13}/></a></div></aside>}
 {selected&&<div className="modal-backdrop" onClick={()=>setSelected(null)}><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title" onClick={e=>e.stopPropagation()}><button className="icon-button dialog-close" onClick={()=>setSelected(null)} aria-label={tr('Cerrar', 'Close')}><X size={19}/></button><span className="dialog-meta">{selected.name}</span><h2 id="dialog-title">{helper?tr('Crear ayudante','Create worker'):tr('Modelo del líder','Leader model')}</h2><p>{helper?tr('El ayudante tendrá su propia sesión.','The worker will have its own session.'):tr('Se aplicará al siguiente turno de este líder.','Applies on the leader’s next turn.')}</p><label>{tr('Modelo', 'Model')}<select aria-label={tr('Modelo', 'Model')} value={model} onChange={e=>{setModel(e.target.value);setEffort('');}}>{selected.catalog?.length?selected.catalog.map(m=><option key={m.id} value={m.id}>{m.name||m.id}</option>):<option value="">{tr("No hay catálogo disponible", "Model catalog unavailable")}</option>}</select></label><label>{tr('Razonamiento','Reasoning')}<select aria-label={tr('Razonamiento','Reasoning')} value={effort} onChange={e=>setEffort(e.target.value)}><option value="">{tr("Predeterminado del proveedor","Provider default")}</option>{selected.catalog?.find(m=>m.id===model)?.efforts?.map(v=><option value={v} key={v}>{v}</option>)}</select></label>{helper&&<WorkerAssignment templates={templates} mode={template} setMode={setTemplate} task={assignment} setTask={setAssignment}/>}{helper&&selected.id==='faustus'&&<div className="worker-file-scope">
  <label>{tr('Tarea de producción (opcional)', 'Production task (optional)')}
   <select value={fileJob} onChange={e=>{setFileJob(e.target.value);setFileProposal(null);}}>
    <option value="">{tr('Sin tarea vinculada', 'No linked task')}</option>
    {tasks.map(t=><option key={t.id} value={t.id}>#{t.id} {t.title}</option>)}
   </select>
  </label>
  {fileJob&&<button type="button" className="worker-file-action" disabled={busy} onClick={()=>void previewWorkerFiles()}>
   {tr('Ver piezas y carpetas propuestas', 'View suggested pieces and folders')}
  </button>}
  {fileProposal?.task===fileJob&&<div className="worker-file-proposal">
   <p>{tr('Copias de piezas con permiso de lectura', 'Piece copies with read access')} ({fileProposal.files.read_files.length})</p>
   {fileProposal.files.asset_folders.map(path=><button className="worker-file-action" key={path} type="button"
     disabled={parsePathList(readPaths).includes(path)}
     onClick={()=>setReadPaths(current=>[...parsePathList(current),path].join('\n'))}>
     {tr('Añadir a lectura', 'Add to read access')} · {path}
    </button>)}
  </div>}
  <label>{tr('Carpetas de lectura', 'Read folders')}
   <textarea rows={2} value={readPaths} onChange={e=>setReadPaths(e.target.value)}
    placeholder={tr('Una ruta absoluta por línea', 'One absolute path per line')}/>
  </label>
  <label>{tr('Carpetas de salida', 'Output folders')}
   <textarea rows={2} value={writePaths} onChange={e=>setWritePaths(e.target.value)}
    placeholder={tr('Una ruta absoluta por línea', 'One absolute path per line')}/>
  </label>
 </div>}{selected.detail&&<p className="dialog-note">{selected.detail}</p>}
 {workerError&&<p className="error" role="alert">{workerError}</p>}
 <button className="primary" disabled={busy||!model||(helper&&!assignment.trim())} onClick={save}>{busy?tr('Guardando…','Saving…'):helper?tr('Crear ayudante','Create worker'):tr('Guardar modelo','Save model')}</button></section></div>}
 {workerLeader&&<div className="modal-backdrop" onClick={()=>setWorkerLeader(null)}>
  <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="worker-dialog-title" onClick={event=>event.stopPropagation()}>
   <button className="icon-button dialog-close" onClick={()=>setWorkerLeader(null)} aria-label={workerText.close}><X size={19}/></button>
   <h2 id="worker-dialog-title">{workerText.title(workerLeader.name)}</h2>
   {workers.filter(worker=>worker.parent_id===workerLeader.id).map(worker=>
    <WorkerDetail key={worker.id} worker={worker} tasks={tasks} busy={Boolean(busyWorker)} control={controlWorker}/>)}
  </section>
 </div>}
 {thread&&<div className="modal-backdrop" onClick={()=>setThread(null)}><section className="dialog task-dialog" role="dialog" aria-modal="true" aria-labelledby="task-title" onClick={e=>e.stopPropagation()}><button className="icon-button dialog-close" onClick={()=>setThread(null)} aria-label={tr("Cerrar tarea","Close task")}><X size={19}/></button><span className="dialog-meta">{tr('TAREA','TASK')} #{thread.task?.id}</span><h2 id="task-title">{thread.task?.title}</h2><p>{status(thread.task?.state||'')} · {thread.task?.owner}</p><div className="thread-body">{(thread.messages||thread.posts||[]).map((m:Message)=><article key={m.id}><strong>{m.agent||m.author} · {time(m.created||m.ts)}</strong><p>{messageBody(m)}</p></article>)}{thread.task?.description&&<p>{thread.task.description}</p>}</div>
  {!!thread.task?.commits?.length && (
    <DeliveryDiff key={thread.task.id} taskId={thread.task.id} version={thread.task.submission_revision}/>
  )}
  <label>{tr('Respuesta','Reply')}<textarea value={reply} onChange={e=>setReply(e.target.value)}/></label><button className="primary" disabled={!reply.trim()||busy} onClick={async()=>{setBusy(true);try{await api('/api/agora/post',{thread_id:thread.task?.thread_id||thread.thread?.id,body:reply});setReply('');setThread(await api(`/api/agora/threads/${thread.task.thread_id}`));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>{tr('Responder','Reply')}<Send size={15}/></button></section></div>}</div>;
}

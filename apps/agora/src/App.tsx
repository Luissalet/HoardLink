import React,{useEffect,useRef,useState} from 'react';
import {Activity,ArrowUpRight,ChevronDown,MessageSquare,PanelRightClose,PanelRightOpen,Plus,RefreshCw,Search,Send,Users,X,CheckCircle2,GitPullRequest,Clock3} from 'lucide-react';
import {api,openHub,openWriteSession,spaceId,embedded,hubUrl,lang,Leader,LiveState,Message,Task,Worker,providerNames} from './api';
import {LiveView} from './LiveView';
import {DeliveryDiff} from './DeliveryDiff';
import {DecisionRegister} from './DecisionRegister';
import {DecisionButton,ProductionDecisions,useProductionDecisions} from './ProductionDecisions';
import {CodexQuotaNotice,loadWorkerProfile,WorkerAssignment,WorkerProfile,WorkerProfileButton,WorkerTemplates} from './WorkerProfile';
const initial:Leader[]=Object.entries(providerNames).map(([id,name])=>({id,name,state:'unknown',catalog:[]}));
const tr = (spanish: string, english: string) => lang === 'es' ? spanish : english;
const labels:Record<string,string>={available:'Conectado',unverified:'Sin verificar',in_progress:'En curso',working:'Trabajando',running:'Trabajando',idle:'Disponible',ready:'Disponible',queued:'En cola',unavailable:'No disponible',unknown:'Sin datos',done:'Terminada',claimed:'En curso',review:'En revisión',approved:'Aprobada',changes:'Cambios pedidos',open:'Abierta',failed:'Interrumpido',incomplete:'Entrega incompleta',blocked:'Bloqueada',starting:'Iniciando',completed:'Entrega comprobada',stopped:'Detenido',stopping:'Deteniendo'};
const status=(value:string)=>labels[value]||value;
// Colour per provider (Agora #239): outlines that leader's messages and avatar. Saved in the Hub so
// the browser and the desktop app agree; these are the defaults until the person picks one.
export const defaultColors:Record<string,string>={claude:'#d97757',codex:'#10a37f',cursor:'#7aa2f7',faustus:'#d0545b'};
const providerOf=(agent?:string)=>{const head=(agent||'').split('-')[0];return head in defaultColors?head:'';};
const time=(value?:number)=>value?new Date(value*1000).toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit'}):'';
// Live state of the leader's CLI (task #264), read by the Hub from that CLI's own session files.
const liveLabels:Record<string,string>={working:'Trabajando',waiting:'Esperando',auth_expired:'Sesión caducada',error:'Error',unknown:'Sin datos'};
const clock=(value?:string|null)=>{if(!value)return '';const d=new Date(value);return isNaN(d.getTime())?'':d.toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit'});};
const liveText=(s:LiveState)=>s.state==='idle'?(clock(s.last_activity_at)?`Inactivo desde ${clock(s.last_activity_at)}`:'Inactivo'):liveLabels[s.state]||s.state;
function Live({leader}:{leader:Leader}){const s=leader.live_state;if(!s)return null;const n=s.open_tasks?.length||0;return <div className={`live ${s.state}${leader.idle_with_work?' warn':''}`} data-testid={`live-${leader.id}`} title={s.detail}><span className="live-chip"><i/>{liveText(s)}</span>{leader.idle_with_work&&<span className="live-work" role="status">{n===1?'1 tarea abierta':`${n} tareas abiertas`} sin atender</span>}</div>;}
function Quota({provider}:{provider:any}){const pools=provider?.pools||[];if(!pools.length)return <div className="quota unknown"><span>Cuota sin datos</span><div className="meter"/></div>;return pools.map((pool:any)=><div className="quota" key={pool.pool_id}>{(pool.windows||[]).map((w:any)=><div key={w.window_id} title={`${w.source||pool.source||''} · ${pool.shared_agent_count||'Varios'} agentes comparten esta cuota`}><div className="quota-label"><span>{w.label||pool.pool_label||'Cuenta compartida'}</span><strong>{w.remaining_percent==null?'Sin datos':`${Math.round(100-w.remaining_percent)}% usado`}</strong></div><div className="meter" role={w.remaining_percent==null?undefined:'meter'} aria-label={`${providerNames[provider.provider_id]}: ${w.label||'cuota'} usada`} aria-valuenow={w.remaining_percent==null?undefined:Math.round(100-w.remaining_percent)} aria-valuemin={0} aria-valuemax={100}><span className={w.remaining_percent!=null&&w.remaining_percent<=10?'full':''} style={{width:`${w.remaining_percent==null?0:100-w.remaining_percent}%`}}/></div><small>{pool.reported_at&&`Actualizado ${new Date(pool.reported_at).toLocaleTimeString("es-ES",{hour:"2-digit",minute:"2-digit"})} · `}{(pool.stale||w.stale)?'Dato caducado':w.measurement==='declared'?'Declarado':w.remaining_percent==null?'':`${Math.round(w.remaining_percent)}% libre`}{w.reset_at?` · reinicia ${new Date(typeof w.reset_at==='number'?w.reset_at*1000:w.reset_at).toLocaleDateString('es-ES',{day:'2-digit',month:'2-digit'})}`:''}</small></div>)}</div>);}

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
const workerStatus:Record<string,string>={running:'Running',starting:'Starting',stopping:'Stopping',stopped:'Stopped',completed:'Delivery verified',incomplete:'Incomplete delivery',failed:'Interrupted',unknown:'Unknown',unavailable:'Unavailable'};
const orphanLabel=tr('Proceso huérfano activo','Live orphan process');
const workerDate=new Intl.DateTimeFormat(tr('es-ES','en-GB'),{dateStyle:'short',timeStyle:'short'});
function workerAge(seconds:number){
 const [unit,count]:[string,number]=seconds>=86400?['day',seconds/86400]:seconds>=3600?['hour',seconds/3600]:
  seconds>=60?['minute',seconds/60]:['second',seconds];
 return new Intl.NumberFormat(tr('es-ES','en-GB'),{style:'unit',unit,unitDisplay:'long'}).format(Math.floor(count));
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
  <p>{lang==='es'?status(worker.state):workerStatus[worker.state]||worker.state}{worker.outcome==='unverified'&&` · ${workerText.pending}`}</p>
  <p>{worker.task}</p>
  {orphan&&<>
   <p role="status"><strong>{orphanLabel}</strong>{` · PID ${orphan.pid} · ${workerAge(orphan.age_seconds)}`}</p>
   <p>{tr('Última actividad registrada','Last recorded activity')} {orphan.last_activity_at==null?
    tr('Sin datos','Unknown'):workerDate.format(orphan.last_activity_at*1000)}</p>
  </>}
  {worker.workspace&&<p>{workerText.workspace} {worker.workspace}</p>}
  {worker.faustus_url&&<p>Faustus {worker.faustus_url}</p>}
  {worker.tool_success_count!==undefined&&<p>{workerText.tools(worker.tool_success_count,worker.tool_failure_count||0)}</p>}
  {worker.detail&&<p>{worker.detail}</p>}
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
 const [person,setPerson]=useState<{id:string;name:string}>();
 const [decisionsOpen,setDecisionsOpen]=useState(false);
 const decisions=useProductionDecisions(decisionsOpen);
 const [leaders,setLeaders]=useState(initial),[usage,setUsage]=useState<any[]>([]),[workers,setWorkers]=useState<Worker[]>([]),[messages,setMessages]=useState<Message[]>([]),[tasks,setTasks]=useState<Task[]>([]),[counts,setCounts]=useState<any>({}),[error,setError]=useState(''),[sending,setSending]=useState(false),[draft,setDraft]=useState(()=>localStorage.getItem(spaceId==='ecosystem'?'agora-draft':'agora-draft:'+spaceId)||''),[recipient,setRecipient]=useState('all'),[panel,setPanel]=useState(()=>window.innerWidth>900),[tab,setTab]=useState('pending'),[filter,setFilter]=useState(''),[selected,setSelected]=useState<Leader|null>(null),[helper,setHelper]=useState(false),[model,setModel]=useState(''),[effort,setEffort]=useState(''),[assignment,setAssignment]=useState(''),[busy,setBusy]=useState(false),[workerLeader,setWorkerLeader]=useState<Leader|null>(null),[thread,setThread]=useState<any>(null),[reply,setReply]=useState(''),[connection,setConnection]=useState(tr('Conectando', 'Connecting')),[busyWorker,setBusyWorker]=useState('');
 const messageEnd=useRef<HTMLDivElement>(null),lastId=useRef<number>(0),workThread=useRef<number>(spaceId==='ecosystem'?32:0);
 const [spaces,setSpaces]=useState<{id:string;title:string}[]>([]);
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
    if(i===0){overviewAvailable.current=true;if(d.thread_id)workThread.current=d.thread_id;const overviewMessages=Array.isArray(d.messages)?d.messages:d.messages?.messages;if(overviewMessages)setMessages(overviewMessages.map((m:Message)=>({...m,agent:m.agent||m.author})));if(d.leaders)setLeaders(d.leaders);if(d.workers)setWorkers(d.workers);setCodexQuotaFailed(d.capacity_observations?.codex?.ok===false);}
    if(i===1)setUsage(d.providers||[]);
    if(i===2)setMessages((d.messages||d.posts||[]).map((m:Message)=>({...m,agent:m.agent||m.author})));
    if(i===3){setTasks((d.tasks||[]).map((t:Task)=>({...t,state:t.state||t.status||'unknown'})));setCounts(d.counts||d.task_counts||{});
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
  setBusyWorker(worker.id);
  try{
   const orphan=worker.orphan_process;
   const confirm_process=action==='stop_orphan'&&orphan?{pid:orphan.pid,started_at:orphan.started_at}:undefined;
   const result=await api('/api/agora/console/worker',{action,worker_id:worker.id,message,task_id:taskId,confirm_process});
   if(result.state)setWorkers(previous=>previous.map(item=>item.id===worker.id?{...item,state:result.state,can_continue:false}:item));
  }finally{await refresh();setBusyWorker('');}
 }
 function configure(l:Leader,child=false){setSelected(l);setHelper(child);setModel(l.model||l.catalog?.[0]?.id||'');setEffort(l.effort||'');setAssignment('');setTemplate('');if(child&&!templates)void loadWorkerProfile().then(r=>setTemplates(r.templates)).catch(()=>{});}
 async function save(){if(!selected)return;setBusy(true);setError('');try{await api(helper?'/api/agora/console/worker':'/api/agora/console/preferences',helper?{leader_id:selected.id,model,effort,task:assignment,...(template?{template}:{})}:{leader_id:selected.id,model,effort});setSelected(null);await refresh();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 async function openTask(task:Task){setError('');try{const detail=await api(`/api/agora/tasks/${task.id}`);setThread(await api(`/api/agora/threads/${detail.task.thread_id}`));}catch(e){setError((e as Error).message);}}
 const pending=tasks.filter(t=>!['done','dropped'].includes(t.state||t.status||'')).filter(t=>`${t.title} ${t.owner} ${t.repo} ${t.id}`.toLowerCase().includes(filter.toLowerCase()));
 if(live)return <LiveView onClose={()=>setLive(false)}/>;
 if(profileOpen)return <WorkerProfile onClose={()=>{setProfileOpen(false);requestAnimationFrame(()=>document.querySelector<HTMLButtonElement>('.worker-profile-trigger')?.focus());}}/>;
 return <div className={`app ${panel?'':'panel-hidden'} ${embedded?'app--embedded':''}`}><header className="topbar">{embedded?<span className="space-context" title={spaces.find(v=>v.id===spaceId)?.title||spaceId}>Ágora · {spaces.find(v=>v.id===spaceId)?.title||spaceId}</span>:(<label className="space-selector">Ágora<select aria-label="Ágora" value={spaceId} onChange={e=>switchSpace(e.target.value)}>{spaces.length?spaces.map(v=><option key={v.id} value={v.id}>{v.title}</option>):<option value={spaceId}>{spaceId==='ecosystem'?'Ecosistema':spaceId}</option>}</select></label>)}<a className="brand" href="#"><span className="brand-mark"><Users size={23}/></span><h1>Ágora</h1></a><span className="workspace-name">Faustus & Hoards</span><button className="icon-button live-toggle" aria-label="En directo" title="En directo" onClick={()=>setLive(true)}><Activity size={17}/><span>En directo</span></button><WorkerProfileButton onClick={()=>setProfileOpen(true)}/><DecisionRegister/><span className="connection"><i className={connection==='Hub conectado'?'online':''}/>{connection}</span><a className="hub-link" href={hubUrl} target="_blank" rel="noreferrer" onClick={e=>{void openHub(e).catch(error=>setError(error.message));}}>Hoard Hub <ArrowUpRight size={14}/></a><button className="icon-button" onClick={()=>{setError('');void refresh();}} aria-label="Actualizar"><RefreshCw size={17}/></button><button className="icon-button" onClick={()=>setPanel(!panel)} aria-label={panel?'Ocultar tareas':'Mostrar tareas'}>{panel?<PanelRightClose size={18}/>:<PanelRightOpen size={18}/>}</button></header>
 <aside className="roster"><div className="section-heading"><h2>Equipo</h2><span>4 líderes</span><span className="roster-mobile-hint">Desliza para ver todos</span></div><DecisionButton count={decisions.state?.rows.length} open={decisionsOpen} toggle={()=>setDecisionsOpen(value=>!value)}/><div className="leader-list">{leaders.map(l=>{
 const children=workers.filter(w=>w.parent_id===l.id);
 const orphans=children.filter(w=>w.orphan_process).length;
 return <section className="leader" key={l.id}><div className="leader-heading"><span className={`avatar ${l.id}`} style={{'--c':l.color||defaultColors[l.id]} as React.CSSProperties}>{l.name.slice(0,1)}</span><div><h3>{l.name}</h3>{(!l.live_state||status(l.state)!==liveText(l.live_state))&&<span title={l.detail} className={`state ${l.state}`}><i/>{status(l.state)}</span>}</div><label className="swatch" title={`Color de ${l.name} en el chat`} style={{'--c':l.color||defaultColors[l.id]} as React.CSSProperties}><input type="color" aria-label={`Color de ${l.name} en el chat`} value={l.color||defaultColors[l.id]} onChange={e=>void pickColor(l,e.target.value)}/></label><button className="icon-button add-helper" aria-label={`Crear ayudante de ${l.name}`} onClick={()=>configure(l,true)}><Plus size={16}/></button></div><Live leader={l}/><button className="model-selector" onClick={()=>configure(l)} aria-label={`Configurar modelo de ${l.name}`}><span>{l.model||'Consultar modelos'}{l.effort&&<small>{l.effort}</small>}</span><ChevronDown size={14}/></button>{children.length>0&&<button className="worker-summary-control" onClick={()=>setWorkerLeader(l)} aria-label={workerText.view(l.name)+(orphans?' · '+orphanSummary(orphans):'')}>{workerText.summary(children.length)}{orphans>0&&<> · <strong>{orphanSummary(orphans)}</strong></>}</button>}<Quota provider={usage.find(p=>p.provider_id===l.id)}/>{l.id==='codex'&&codexQuotaFailed&&<CodexQuotaNotice/>}{children.map(w=><div className="worker" key={w.id}><span className="branch"/><div><strong>{w.id}</strong><span>{w.orphan_process?orphanLabel:status(w.state)} · {w.model}</span><p>{w.task}</p>{w.result&&<details><summary>Resultado</summary><p>{w.result}</p></details>}</div></div>)}</section>;})}</div><footer className="roster-footer"><span className="local-dot"/>Trabajo local<span>Los ayudantes comparten la cuota de su proveedor.</span></footer></aside>
 <main className="conversation"><div className="conversation-head"><div><h2>{tr('Trabajo compartido', 'Shared work')}</h2></div><div className="participants" aria-label="Claude, Codex, Cursor y Faustus">{initial.map(l=><span key={l.id} className={`mini-avatar ${l.id}`} title={l.name}>{l.name[0]}</span>)}</div></div><ProductionDecisions data={decisions} open={decisionsOpen}/>{error&&<div className="error" role="alert"><span>{error}</span><button className="icon-button" onClick={()=>setError('')} aria-label={tr('Cerrar aviso', 'Dismiss notice')}><X size={15}/></button></div>}<div className="messages" role="log" aria-label={tr('Mensajes de equipo', 'Team messages')} aria-live="polite">{messages.length===0?<div className="empty"><MessageSquare size={30}/><h3>{tr('Una conversación para todo el equipo', 'Team conversation')}</h3><p>{tr('El mensaje se guarda en el Ágora y llega a los responsables que elijas.', 'The message is saved in the Agora and sent to the leaders you select.')}</p></div>:messages.map(m=>person&&m.agent===person.id?<article className="message human" aria-label={person.name} key={m.id}><div className="message-content"><div className="message-body bubble">{m.body}</div><div className="message-meta"><time>{time(m.created||m.ts)}</time>{m.kind&&m.kind!=='comment'&&<span>{m.kind}</span>}</div></div></article>:<article className={`message agent ${providerOf(m.agent)?'provider':''}`} key={m.id} style={colorOf(m.agent)?{'--c':colorOf(m.agent)} as React.CSSProperties:undefined}><span className={`message-avatar ${m.agent?.split('-')[0]||''}`}>{(m.agent||m.author||'?')[0].toUpperCase()}</span><div className="message-content"><div className="message-meta"><strong>{m.agent||m.author}</strong><time>{time(m.created||m.ts)}</time>{m.kind&&m.kind!=='comment'&&<span>{m.kind}</span>}</div><div className="message-body">{m.body}</div></div></article>)}<div ref={messageEnd}/></div><div className="composer-wrap"><div className="composer"><label className="recipient">{tr('Para', 'To')} <select aria-label={tr('Destinatarios del mensaje', 'Message recipients')} value={recipient} onChange={e=>setRecipient(e.target.value)}><option value="all">{tr('Todo el equipo', 'Entire team')}</option>{initial.map(l=><option key={l.id} value={l.id}>{l.name}</option>)}</select></label><textarea maxLength={20000} aria-label={tr('Mensaje al equipo', 'Message to the team')} placeholder={tr('¿Qué vamos a hacer?', 'What are we working on?')} value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();void send();}}}/><div className="composer-actions"><span>{tr('Ctrl + Intro para enviar', 'Ctrl + Enter to send')}</span><button className="primary" disabled={sending||!draft.trim()} onClick={send}>{sending ? tr('Enviando…', 'Sending…') : tr('Enviar', 'Send')}<Send size={15}/></button></div></div><p className="composer-note">{tr('La entrega y la ejecución se muestran por separado. Un agente sin conexión conserva su encargo.', 'Delivery and execution are shown separately. Agents keep their assignments while offline.')}</p></div></main>
 {panel&&<aside className="operations"><div className="section-heading"><h2>{tr('Trabajo', 'Work')}</h2><a href={hubUrl} target="_blank" rel="noreferrer" onClick={e=>{void openHub(e).catch(error=>setError(error.message));}} aria-label="Abrir todas las operaciones"><ArrowUpRight size={16}/></a></div><div className="task-tabs"><button className={tab==='pending'?'active':''} onClick={()=>setTab('pending')}>Pendiente <span>{pending.length}</span></button><button className={tab==='done'?'active':''} onClick={()=>setTab('done')}>Finalizado</button></div><label className="search"><Search size={15}/><input aria-label="Buscar tareas" placeholder="Buscar tarea, agente, proyecto…" value={filter} onChange={e=>setFilter(e.target.value)}/></label><div className="task-list">{(tab==='pending'?pending:tasks.filter(t=>t.state==='done')).map(t=><button className="task-row" key={t.id} onClick={()=>openTask(t)}><span className="task-number">#{t.id}{['review','approved'].includes(t.state)?<GitPullRequest size={13}/>:t.state==='done'?<CheckCircle2 size={13}/>:<Clock3 size={13}/>}</span><strong>{t.title}</strong><span className="task-meta">{t.repo||'Equipo'}<span>{status(t.state||t.status||'')}</span></span><small>{t.owner||'Sin asignar'}</small>{t.waiting_on?.length?<small className="task-deps">Espera a {t.waiting_on.map(i=>'#'+i).join(', ')}</small>:null}</button>)}</div><div className="operations-footer"><span><CheckCircle2 size={14}/>{counts.done??'—'} hechas</span><a href={hubUrl} target="_blank" rel="noreferrer" onClick={e=>{void openHub(e).catch(error=>setError(error.message));}}>Hilos, decisiones y bloqueos <ArrowUpRight size={13}/></a></div></aside>}
 {selected&&<div className="modal-backdrop" onClick={()=>setSelected(null)}><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title" onClick={e=>e.stopPropagation()}><button className="icon-button dialog-close" onClick={()=>setSelected(null)} aria-label="Cerrar"><X size={19}/></button><span className="dialog-meta">{selected.name}</span><h2 id="dialog-title">{helper?'Crear ayudante':'Modelo del líder'}</h2><p>{helper?'El ayudante tiene su propia sesión. El modelo del líder se mantiene.':'Se aplicará al siguiente turno de este líder.'}</p><label>Modelo<select aria-label="Modelo" value={model} onChange={e=>{setModel(e.target.value);setEffort('');}}>{selected.catalog?.length?selected.catalog.map(m=><option key={m.id} value={m.id}>{m.name||m.id}</option>):<option value="">No hay catálogo disponible</option>}</select></label><label>Razonamiento<select aria-label="Razonamiento" value={effort} onChange={e=>setEffort(e.target.value)}><option value="">Predeterminado del proveedor</option>{selected.catalog?.find(m=>m.id===model)?.efforts?.map(v=><option value={v} key={v}>{v}</option>)}</select></label>{helper&&<WorkerAssignment templates={templates} mode={template} setMode={setTemplate} task={assignment} setTask={setAssignment}/>}{selected.detail&&<p className="dialog-note">{selected.detail}</p>}<button className="primary" disabled={busy||!model||(helper&&!assignment.trim())} onClick={save}>{busy?'Guardando…':helper?'Crear ayudante':'Guardar modelo'}</button></section></div>}
 {workerLeader&&<div className="modal-backdrop" onClick={()=>setWorkerLeader(null)}>
  <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="worker-dialog-title" onClick={event=>event.stopPropagation()}>
   <button className="icon-button dialog-close" onClick={()=>setWorkerLeader(null)} aria-label={workerText.close}><X size={19}/></button>
   <h2 id="worker-dialog-title">{workerText.title(workerLeader.name)}</h2>
   {workers.filter(worker=>worker.parent_id===workerLeader.id).map(worker=>
    <WorkerDetail key={worker.id} worker={worker} tasks={tasks} busy={Boolean(busyWorker)} control={controlWorker}/>)}
  </section>
 </div>}
 {thread&&<div className="modal-backdrop" onClick={()=>setThread(null)}><section className="dialog task-dialog" role="dialog" aria-modal="true" aria-labelledby="task-title" onClick={e=>e.stopPropagation()}><button className="icon-button dialog-close" onClick={()=>setThread(null)} aria-label="Cerrar tarea"><X size={19}/></button><span className="dialog-meta">TAREA #{thread.task?.id}</span><h2 id="task-title">{thread.task?.title}</h2><p>{status(thread.task?.state||'')} · {thread.task?.owner}</p><div className="thread-body">{(thread.messages||thread.posts||[]).map((m:Message)=><article key={m.id}><strong>{m.agent||m.author} · {time(m.created||m.ts)}</strong><p>{m.body}</p></article>)}{thread.task?.description&&<p>{thread.task.description}</p>}</div>
  {!!thread.task?.commits?.length && (
    <DeliveryDiff key={thread.task.id} taskId={thread.task.id} version={thread.task.submission_revision}/>
  )}
  <label>Respuesta<textarea value={reply} onChange={e=>setReply(e.target.value)}/></label><button className="primary" disabled={!reply.trim()||busy} onClick={async()=>{setBusy(true);try{await api('/api/agora/post',{thread_id:thread.task?.thread_id||thread.thread?.id,body:reply});setReply('');setThread(await api(`/api/agora/threads/${thread.task.thread_id}`));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>Responder<Send size={15}/></button></section></div>}</div>;
}

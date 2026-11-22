import {invoke, isTauri} from '@tauri-apps/api/core';
import {registerDecisionPieceOpener} from '../../../hoard_link/hub/ui/production_decisions_card.js';
export const spaceId = new URLSearchParams(location.search).get('space_id') || 'ecosystem';
export const embedded = new URLSearchParams(location.search).get('embedded') === '1';
export const hubUrl = 'http://127.0.0.1:8810/?space_id=' + encodeURIComponent(spaceId) + '#agora';
export const lang = (new URLSearchParams(location.search).get('lang') ||
  localStorage.getItem('hub.lang') || navigator.language).toLowerCase().startsWith('es') ? 'es' : 'en';

async function native<T>(command: string, args: Record<string, unknown> = {}): Promise<T> {
  try {
    return await invoke<T>(command, {...args,lang});
  } catch (error) {
    throw error instanceof Error ? error : new Error(String(error));
  }
}

if (isTauri()) {
  registerDecisionPieceOpener((path, filename) => native('open_piece', {path, filename}));
}

export async function openWriteSession(): Promise<void> {
  if (isTauri()) await native('open_write_session');
}

export async function openHub(event:{preventDefault:()=>void}, decision?: string){
  if(isTauri()){event.preventDefault();await native('open_hub',{spaceId,decision});}
}
export async function previewSource(path: string): Promise<string> {
  return isTauri() ? (await native<{image: string}>('hub_request', {path, body: null})).image : path;
}
export async function api<T=any>(path:string,body?:unknown):Promise<T>{
  if(path.startsWith('/api/agora/') && spaceId !== 'ecosystem') {
    if(body===undefined) path+=(path.includes('?')?'&':'?')+'space_id='+encodeURIComponent(spaceId);
    else body={space_id:spaceId,...(body as Record<string,unknown>)};
  }
  let data:any;
  if(isTauri()) data=await native('hub_request',{path,body:body===undefined?null:body});
  else {const response=await fetch(path,{headers:body===undefined?{}:{'Content-Type':'application/json'},method:body===undefined?'GET':'POST',body:body===undefined?undefined:JSON.stringify(body)});data=await response.json();if(!response.ok)throw Object.assign(new Error(data.error || `Error ${response.status}`),{code:data.code});}
  if(data.ok===false)throw new Error(data.error || 'No se pudo completar la operación');
  return data as T;
}
export type Model={id:string;name?:string;efforts?:string[]};
export type OrphanProcess={pid:number;started_at:number;age_seconds:number;last_activity_at:number|null};
export type Worker={id:string;parent_id:string;agent_id?:string;model?:string;state:string;task?:string;result?:string;effort?:string;outcome?:string;workspace?:string;faustus_url?:string;detail?:string;verification_warnings?:string[];tool_success_count?:number;tool_failure_count?:number;can_continue?:boolean;retry_count?:number;resume_issue?:string;orphan_process?:OrphanProcess;replacement?:{worker_id?:string;task_id?:number;state?:string;detail?:string}};
export type LiveState={state:string;label?:string;last_activity_at?:string|null;source?:string;detail?:string;open_tasks?:number[];idle_with_work?:boolean};
export type Leader={id:string;name:string;state:string;detail?:string;model?:string;effort?:string;catalog?:Model[];workers?:Worker[];color?:string|null;live_state?:LiveState|null;idle_with_work?:boolean};
export type Message={id:number;agent?:string;author?:string;body:string;kind?:string;created?:number;ts?:number};
export type Task={id:number;title:string;state:string;status?:string;owner?:string;repo?:string;thread_id?:number;waiting_on?:number[]};
export const providerNames:Record<string,string>={claude:'Claude',codex:'Codex',cursor:'Cursor',faustus:'Faustus'};

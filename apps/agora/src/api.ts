import {invoke, isTauri} from '@tauri-apps/api/core';
export async function openHub(event:{preventDefault:()=>void}){
  if(isTauri()){event.preventDefault();await invoke('open_hub');}
}
export async function api<T=any>(path:string,body?:unknown):Promise<T>{
  let data:any;
  if(isTauri()) data=await invoke('hub_request',{path,body:body===undefined?null:body});
  else {const response=await fetch(path,{headers:body===undefined?{}:{'Content-Type':'application/json'},method:body===undefined?'GET':'POST',body:body===undefined?undefined:JSON.stringify(body)});data=await response.json();if(!response.ok)throw new Error(data.error || `Error ${response.status}`);}
  if(data.ok===false)throw new Error(data.error || 'No se pudo completar la operación');
  return data as T;
}
export type Model={id:string;name?:string;efforts?:string[]};
export type Worker={id:string;parent_id:string;model?:string;state:string;task?:string;result?:string;effort?:string;outcome?:string};
export type Leader={id:string;name:string;state:string;detail?:string;model?:string;effort?:string;catalog?:Model[];workers?:Worker[];color?:string|null};
export type Message={id:number;agent?:string;author?:string;body:string;kind?:string;created?:number;ts?:number};
export type Task={id:number;title:string;state:string;status?:string;owner?:string;repo?:string;thread_id?:number};
export const providerNames:Record<string,string>={claude:'Claude',codex:'Codex',cursor:'Cursor',faustus:'Faustus'};

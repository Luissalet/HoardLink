// Execute the actual mailbox client against an asynchronous DOM/API boundary.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
class Element {
  constructor(tag='',cls='',text=''){this.tagName=tag.toUpperCase();this.className=cls;this.children=[];this.dataset={};this.attributes={};this.style={};this.hidden=false;this.value='';this._text=String(text);}
  appendChild(n){n.parent=this;this.children.push(n);return n;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
  set innerHTML(v){assert.equal(v,'');this.children=[];this._text='';}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set textContent(v){this._text=String(v);this.children=[];}
  get options(){return this.children;}
  get lastChild(){return this.children.at(-1);}
  setAttribute(k,v){this.attributes[k]=String(v);}
  focus(){this.focused=true;}
  scrollIntoView(){}
  querySelectorAll(s){let [tag,cls]=s.split('.');if(s.startsWith('.'))tag='';return this.children.flatMap(c=>[...(c.tagName&&(!tag||c.tagName.toLowerCase()===tag)&&(!cls||c.className.split(' ').includes(cls))?[c]:[]),...c.querySelectorAll(s)]);}
  querySelector(s){return this.querySelectorAll(s)[0]||null;}
}
const message=(id,sphere='personal')=>({id,sphere,subject:'Subject '+id,from_name:'Sender '+id,from_addr:'sender@example.org',date_ts:1,category:'promo',snippet:'Snippet '+id,text:'Hello\n\n<script>never execute</script>\n\n[Read](https://example.org/'+ 'a'.repeat(1000)+')',attachments:[{name:'Attachment.pdf',url:'/api/mail/attachments/test',size:2048}],links:[{url:'javascript:alert(1)',label:'Unsafe link'}]});
function harness(){
  let facet; const requests=[],pending=[];
  const state={holdLists:false,holdDetail:false,failLists:false,failDetail:false,items:Array.from({length:42},(_,i)=>message(i+1)),config:{enabled:false,interval_min:10,retention_days:60,owner:''}};
  const document={documentElement:{dataset:{sphere:'personal'}},getElementById:()=>null,createTextNode:t=>new Element('', '',t)};
  const respond=(url)=>{
    if(url==='/api/spheres')return {ok:true,active:'personal',spheres:[{id:'personal',name:{en:'Personal'}},{id:'work',name:{en:'Work'}}]};
    if(url.startsWith('/api/mail/status'))return {ok:true,enabled:false,counts:{total:state.items.length},accounts:[]};
    if(url==='/api/mail/config')return {ok:true,config:{...state.config}};
    if(url==='/api/mail/interests')return {ok:true,interests:[{app:'ledger'}]};
    if(url.startsWith('/api/mail/messages/'))return state.failDetail?{ok:false,error:'detail unavailable'}:{ok:true,message:message(Number(url.split('/').at(-1)))};
    if(url==='/api/mail/dismiss')return {ok:true};
    if(state.failLists)return {ok:false,error:'offline'};
    let scope=new URL('http://qa'+url).searchParams.get('sphere');
    return {ok:true,messages:url.startsWith('/api/mail/attention?')?[]:state.items.filter(m=>!scope||m.sphere===scope)};
  };
  const api=async(url,body)=>{requests.push({url,body});if(body&&url==='/api/mail/config')Object.assign(state.config,body);
    if((state.holdLists&&/\/api\/mail\/(attention|unclaimed|messages)\?/.test(url))||(state.holdDetail&&/\/api\/mail\/messages\//.test(url)))return new Promise(resolve=>pending.push({url,resolve}));return respond(url);};
  const window={HubFacets:{ctx:{el:(...a)=>new Element(...a),api,toast:()=>{},L:v=>v?.en??v,fmtWhen:()=> 'today',bus:{addEventListener(){}},changedFields:(before,after)=>Object.fromEntries(Object.entries(after).filter(([k,v])=>v!==before[k]))},register:(_,f)=>facet=f}};
  vm.runInNewContext(readFileSync(new URL('../../hoard_link/hub/ui/mail.js',import.meta.url),'utf8'),{window,document,console,URL,location:{href:'http://qa/'},confirm:()=>true});
  const root=new Element('section');facet.mount(root);
  return {state,root,facet,requests,pending,respond,load:()=>facet.copilot.refresh(),button:(text)=>root.querySelectorAll('button').find(b=>b.textContent===text),settle:async()=>{await new Promise(r=>setImmediate(r));}};
}
{
  const h=harness();await h.load();await h.settle();
  assert.equal(h.root.querySelectorAll('.mg-message').length,20,'One page of one active queue');
  assert.equal(h.root.querySelector('.mg-queue').attributes['aria-pressed'],'false','Empty Attention is not the default');
  const search=h.root.querySelector('.mg-search');search.value='Subject 2';search.oninput();
  assert.equal(h.root.querySelectorAll('.mg-message').length,11);
  await h.root.querySelector('.mg-message').onclick();
  assert.equal(h.root.querySelector('.mg-list').hidden,true);
  const reader=h.root.querySelector('.mg-reader');assert.equal(reader.hidden,false);
  assert.equal(reader.querySelectorAll('script').length,0,'Message markup is literal text');
  const anchors=reader.querySelectorAll('a');assert.ok(anchors.some(a=>a.href==='http://qa/api/mail/attachments/test'),'Relative attachments stay available');
  assert.ok(anchors.every(a=>/^https?:/.test(a.href)),'No executable message URLs');
  assert.equal(h.facet.copilot.context().active_objects[0].object_id,'2');
  h.button('Back to inbox').onclick();assert.equal(h.root.querySelector('.mg-search').value,'Subject 2');
  assert.equal(h.root.querySelectorAll('.mg-message').length,11,'Reader return preserves filter');
  h.button('Settings').onclick();const field=h.root.querySelector('.mg-form').querySelectorAll('input')[0];field.value=33;
  await h.facet.tick();assert.equal(h.facet.copilot.context().pending_forms[0].fields.interval_min,33,'Polling preserves unsaved configuration');
  await h.root.querySelector('.mg-message').onclick();assert.equal(h.facet.copilot.context().pending_forms.length,0,'Reader excludes hidden settings draft');
}
{
  const h=harness();await h.load();h.state.holdDetail=true;
  const open=h.root.querySelector('.mg-message').onclick();await h.settle();
  const scope=h.root.querySelector('select');scope.value='work';await scope.onchange();await h.settle();
  h.pending.shift().resolve({ok:true,message:message(1)});await open;
  assert.equal(h.root.querySelector('.mg-reader').hidden,true,'Late reader response never crosses sphere');
  assert.equal(h.root.querySelectorAll('.mg-message').length,0);
}
{
  const h=harness();await h.load();h.state.failLists=true;await h.load();
  assert.equal(h.root.querySelectorAll('.mg-message').length,20,'Transient error retains current list');
  assert.ok(h.root.querySelector('.mg-error').textContent.includes('offline'));
  h.state.failLists=false;await h.button('Try again').onclick();assert.equal(h.root.querySelector('.mg-error'),null);
  h.state.failDetail=true;await h.root.querySelector('.mg-message').onclick();assert.ok(h.root.querySelector('.mg-reader').textContent.includes('detail unavailable'));
  h.state.failDetail=false;await h.button('Try again').onclick();assert.ok(h.root.querySelector('.mg-body'));
}
{
  const h=harness();await h.load();h.state.holdLists=true;
  const old=h.load();await h.settle();const oldPending=h.pending.splice(0);
  const scope=h.root.querySelector('select');scope.value='work';scope.onchange();await h.settle();
  for(const p of h.pending.splice(0))p.resolve({ok:true,messages:[message(99,'work')]});await h.settle();
  for(const p of oldPending)p.resolve({ok:true,messages:[message(1)]});await old;
  assert.ok(h.root.querySelector('.mg-message').textContent.includes('Subject 99'),'Late list response never replaces newer scope');
}
console.log('PASS mailbox: one queue, pagination/search, reader/back, safe content/attachments, draft retention, error recovery, two scope races');

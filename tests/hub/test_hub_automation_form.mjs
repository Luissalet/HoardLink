// Round-trip real automation documents through the native form, with a small DOM boundary.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.value=''; this.classList={add(){}}; this.events={}; }
  append(...items) { for (const item of items) {item.parentElement=this;this.children.push(item);} }
  replaceChildren() { this.children=[]; }
  setAttribute() {}
  addEventListener(name,fn) {this.events[name]=fn;}
  remove() {this.parentElement.children=this.parentElement.children.filter(x=>x!==this);}
  all(tag) {return this.children.flatMap(x=>[...(x.tagName===tag?[x]:[]),...x.all(tag)]);}
}
const window={};
vm.runInNewContext(readFileSync(new URL('../../hoard_link/hub/ui/automation_form.js',import.meta.url),'utf8'),{window,document:{createElement:tag=>new Element(tag)}});
function mount(value,kind) {
  const root=new Element('div');
  const form=window.HubAutomationForm.mount(root,value,kind,{text:(es,en)=>en});
  return {root,read:()=>JSON.parse(JSON.stringify(form.read())),field:name=>root.all('input').find(x=>x.name===name)};
}
{
  const rule={name:'Export',enabled:false,when:{type:'writer.export.done',source:'writer',match:{format:'pdf'}},cooldown_s:5,custom:'preserve',then:[{kind:'tool',app:'atlas',tool:'store',args:{uri:'${event.uri}'},on_error:'continue'}]};
  const h=mount(rule,'rules'); assert.deepEqual(h.read(),rule);
  h.field('name').value='Renamed'; h.field('event_type').value='writer.save.done';
  assert.deepEqual(h.read().when,{...rule.when,type:'writer.save.done'});
  assert.deepEqual(h.read().then,rule.then); assert.equal(h.read().enabled,false);
  const type=h.root.all('select').find(x=>x.children.some(o=>o.value==='backup')); type.value='start_app';type.events.change();
  assert.deepEqual(h.read().then,[{kind:'start_app',app:'atlas'}]);
  const add=h.root.all('button').find(x=>x.textContent==='Add action');add.onclick();
  assert.equal(h.read().then.length,2);
  h.root.all('button').find(x=>x.textContent==='Remove action').onclick();
  assert.deepEqual(h.read().then,[{kind:'hub',tool:'hub_backup_run',args:{}}]);
}
{
  const h=mount({name:'Backup',enabled:false,at:'04:30',days:['mon','fri'],then:[{kind:'hub',tool:'hub_backup_run',args:{}}]},'jobs');
  h.root.all('select')[0].value='every';h.field('every').value='30m';
  assert.equal(h.read().at,null);assert.equal(h.read().every,'30m');assert.deepEqual(h.read().days,['mon','fri']);
}
{
  const h=mount({when:{type:'test'},then:[{kind:'event',type:'result',data:{keep:true}}]},'rules');
  h.root.all('textarea')[0].value='{broken';assert.throws(()=>h.read(),{name:'SyntaxError'});
}
{
  const action={kind:'tool',app:'writer',tool:'export',args:{format:'pdf'},on_error:'continue'};
  const h=mount({when:{type:'test'},then:[action]},'rules');
  const type=h.root.all('select').find(x=>x.children.some(o=>o.value==='backup'));
  type.value='backup';type.events.change();
  type.value='tool';type.events.change();
  assert.deepEqual(h.read().then,[action],'Changing type and returning must preserve the tool draft');
  type.value='event';type.events.change();h.field('type').value='my.event';h.root.all('textarea')[0].value='{unfinished';
  type.value='backup';type.events.change();type.value='event';type.events.change();
  assert.equal(h.field('type').value,'my.event');assert.equal(h.root.all('textarea')[0].value,'{unfinished');
}
{
  const h=mount({when:{type:'links.watch.new',source:'links'},then:[{kind:'hub',tool:'hub_backup_run',args:{}}]},'rules');
  const trigger=h.root.all('select').find(x=>x.children.some(o=>o.value==='hub.app.started'));
  trigger.value='hub.app.started';trigger.events.change();
  const affected=h.root.all('select').find(x=>x.name==='affected_app');affected.value='writer';affected.events.change();
  assert.deepEqual(h.read().when,{type:'hub.app.started',source:'hub',where:{'data.app':'writer'}});
  trigger.value='links.watch.new';trigger.events.change();
  assert.deepEqual(h.read().when,{type:'links.watch.new',source:'links'});
}
console.log('PASS automation form: preserved advanced fields, disabled state, action replacement/removal, schedule switch, invalid parameters');

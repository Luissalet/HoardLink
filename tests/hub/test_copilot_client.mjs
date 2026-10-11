import assert from 'node:assert/strict';
import { createCopilotFetch } from '../../js/hoard-copilot.js';

let received;
const fetch = createCopilotFetch({hubUrl:'http://hub.example',token:'fixture-token',fetch:async(url,init)=>{
  received={url,init,envelope:JSON.parse(init.body)};
  return new Response('data: {"delta":"ámbito"}\n\n', {headers:{'Content-Type':'text/event-stream','X-Odysseus-Run-Id':'run-one'}});
}});
let response=await fetch('http://faustus.example/api/hoard-copilot/conversations',{
  method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({context:{origin:{app_id:'writer',data_space_id:'qa'}}}),
});
assert.equal(received.envelope.body.context.origin.app_id,'writer');
assert.equal(received.url,'http://hub.example/api/link/copilot');
assert.equal(received.init.headers.Authorization,'Bearer fixture-token');
assert.equal(response.headers.get('X-Odysseus-Run-Id'),'run-one');
assert.ok((await response.text()).includes('ámbito'));
const form=new FormData();form.set('session','one');form.set('message','fragmento');form.set('attachments','["upload-one"]');
await fetch('http://faustus.example/api/chat_stream',{method:'POST',body:form});
assert.ok(received.envelope.content_type.startsWith('multipart/form-data; boundary='));
assert.ok(Buffer.from(received.envelope.raw_body,'base64').toString().includes('upload-one'));
assert.equal(received.envelope.path,'/api/chat_stream');
const abort=new AbortController();
await fetch('http://faustus.example/api/chat/resume/one?cursor=12',{signal:abort.signal,headers:{'X-Odysseus-Run-Id':'run-one'}});
assert.equal(received.envelope.path,'/api/chat/resume/one?cursor=12');
assert.equal(received.envelope.headers['X-Odysseus-Run-Id'],'run-one');
console.log('PASS: Node transport JSON/SSE/multipart/cursor/run identity');

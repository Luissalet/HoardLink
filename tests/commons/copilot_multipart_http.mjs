import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {createRequire} from 'node:module';
const {installCopilot} = await import(process.env.COPILOT_MODULE);
const express = createRequire(path.join(process.env.HOARD_TEST_NODE_MODULES, 'package.json'))('express');
const data = fs.mkdtempSync(path.join(os.tmpdir(), 'copilot-multipart-'));
let captured;
const upstream = http.createServer(async (req, res) => {
  if (req.url.endsWith('/capabilities')) {
    res.writeHead(200, {'Content-Type':'application/json'}).end('{"contract_version":"1.0"}');
    return;
  }
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  captured = Buffer.concat(chunks);
  res.writeHead(200, {'Content-Type':'application/json'}).end('{"ok":true}');
});
await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
const app = express();
installCopilot(app, {appId:'multipart-qa', dataDir:data, token:'',
  faustusUrls:['http://127.0.0.1:'+upstream.address().port]});
const server = await new Promise(resolve => {
  const current = app.listen(0, '127.0.0.1', () => resolve(current));
});
const boundary = 'QA409Boundary';
const files = [
  Buffer.from('prefix--QA409Boundary\r\nContent-Disposition: form-data; name="hoard_context"\r\n\r\n'+
    '{"origin":{"app_id":"uploaded-example"}}\r\n--QA409Boundarysuffix\r\n\x00\xff', 'latin1'),
  Buffer.from('{"origin":{"app_id":"uploaded-json"}}'),
];
try {
  for (const formName of ['"hoard_context"', 'hoard_context']) for (const file of files) {
    const prefix = Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; filename="context"; name="file"\r\n`+
      'Content-Type: application/octet-stream\r\n\r\n');
    const suffix = Buffer.from(`\r\n--${boundary}\r\nContent-Disposition: form-data; name=${formName}\r\n\r\n`+
      `{"origin":{"app_id":"spoof"}}\r\n--${boundary}--\r\n`);
    const source = Buffer.concat([prefix, file, suffix]);
    const result = await fetch('http://127.0.0.1:'+server.address().port+'/api/copilot/proxy', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:'/api/chat_stream',method:'POST',
        content_type:'multipart/form-data; boundary='+boundary,raw_body:source.toString('base64')})});
    assert.equal(result.status, 200);
    await result.text();
    assert.ok(captured.subarray(0, prefix.length+file.length).equals(Buffer.concat([prefix,file])),
      'HTTP relay changed an uploaded file or its headers');
    const start = captured.indexOf(Buffer.from('name='+formName), prefix.length+file.length);
    const value = captured.indexOf(Buffer.from('\r\n\r\n'), start)+4;
    const end = captured.indexOf(Buffer.from('\r\n--'+boundary+'--'), value);
    const context = JSON.parse(captured.subarray(value,end));
    assert.equal(context.origin.app_id, 'multipart-qa');
    assert.ok(context.origin.data_space_id);
  }
  console.log(JSON.stringify({passed:true, files:files.length}));
} finally {
  await new Promise(resolve => server.close(resolve));
  await new Promise(resolve => upstream.close(resolve));
  fs.rmSync(data, {recursive:true, force:true});
}

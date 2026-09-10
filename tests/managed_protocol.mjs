// Real Bun listener and subprocess boundary. Synthetic profile supplied by unittest.
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
const [root, module] = process.argv.slice(2);
const profile = JSON.parse(readFileSync(join(root, 'profile.json')));
const secret = readFileSync(join(root, 'state/component-token'), 'utf8').trim();
const token = readFileSync(join(root, 'state/bridge-token'), 'utf8').trim();
const base = `http://127.0.0.1:${profile.bridge.hub_port}`;
const origin = 'https://example.test', version = 'tap.bridge/v1';
const headers = {'x-tap-component-token': secret, 'x-tap-probe-token': token, 'x-tap-probe-origin': origin, origin};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let hub, checks = 0;
const sockets = [];
writeFileSync(join(root, 'state/bridge.json'), JSON.stringify({configuration:'a'.repeat(64)}));
async function start() {
  hub = Bun.spawn([process.execPath, module, root], {stdout: 'ignore', stderr: 'inherit'});
  for (let i=0; i<100; i++) {
    try { if ((await fetch(base+'/health', {headers: {authorization: 'Bearer '+secret}})).ok) return; } catch {}
    await sleep(50);
  }
  throw new Error('Hub readiness timeout');
}
async function stop() { hub.kill(); await hub.exited; }
async function connect(page = crypto.randomUUID()) {
  const ws = new WebSocket(base.replace('http:', 'ws:')+'/__tap/probe/ws', {headers});
  sockets.push(ws);
  const messages = [];
  ws.addEventListener('message', event => messages.push(JSON.parse(event.data)));
  await new Promise((resolve, reject) => {ws.onopen=resolve; ws.onerror=reject;});
  function send(value) { ws.send(JSON.stringify(value)); }
  async function next(kind, id) {
    for (let i=0; i<150; i++) {
      const index = messages.findIndex(v => v.kind===kind && (!id || v.id===id));
      if (index>=0) return messages.splice(index, 1)[0];
      await sleep(50);
    }
    throw new Error('Missing '+kind);
  }
  send({version,kind:'Hello',page,origin});
  const welcome = await next('Welcome');
  assert.equal(welcome.page, page);
  const session = welcome.session;
  function request(handler, args = {}) {
    const id = crypto.randomUUID();
    send({version,kind:'Request',session,id,handler,args});
    return {id, result: () => next('Result', id)};
  }
  return {ws, messages, send, next, session, page, request, welcome};
}
try {
  await start();
  for (const overrides of [{'x-tap-component-token':''}, {'x-tap-component-token':token},
      {'x-tap-probe-token':'wrong'}, {'x-tap-probe-origin':'https://excluded.test'},
      {'x-tap-probe-origin':'https://foreign.test'}]) {
    assert.equal((await fetch(base+'/__tap/probe/runtime.js', {headers:{...headers,...overrides}})).status,403); checks++;
  }
  assert.equal((await fetch(base+'/health')).status,403); checks++;
  assert.equal((await fetch(base+'/__tap/probe/runtime.js',{headers})).status,200); checks++;
  const liveOrigin = 'https://live.example';
  const liveHeaders = {...headers, 'x-tap-probe-origin':liveOrigin, origin:liveOrigin};
  assert.equal((await fetch(base+'/__tap/probe/runtime.js',{headers:liveHeaders})).status,403);
  const effective = JSON.parse(readFileSync(join(root, 'state/effective-runtime.json')));
  effective.bridge.allow_origins.push(liveOrigin);
  writeFileSync(join(root, 'state/effective-runtime.json'), JSON.stringify(effective));
  assert.equal((await fetch(base+'/__tap/probe/runtime.js',{headers:liveHeaders})).status,200); checks++;
  assert.equal((await fetch(base+'/__tap/probe/runtime.js',{headers,method:'POST'})).status,405); checks++;
  const a = await connect(), b = await connect();
  assert.equal((await fetch(base+'/v1/pages')).status,403); checks++;
  const listed=await (await fetch(base+'/v1/pages',{headers:{authorization:'Bearer '+secret}})).json();
  assert.equal(listed.pages.length,2); assert(listed.pages.some(page=>page.page===a.page)); checks++;
  const controlled=fetch(base+'/v1/pages/'+encodeURIComponent(a.page)+'/commands',{method:'POST',headers:{authorization:'Bearer '+secret,'content-type':'application/json'},body:JSON.stringify({operation:'fixture.inspect',args:{selector:'main'}})}).then(response=>response.json());
  const command=await a.next('Command');
  assert.equal(command.operation,'fixture.inspect'); assert.deepEqual(command.args,{selector:'main'});
  a.send({version,kind:'CommandResult',session:a.session,id:command.id,ok:true,value:{matches:1}});
  assert.deepEqual(await controlled,{ok:true,value:{matches:1}}); checks++;
  assert.equal(a.welcome.revision, 'a'.repeat(64));
  writeFileSync(join(root, 'state/bridge.json'), JSON.stringify({configuration:'b'.repeat(64)}));
  assert.equal((await a.next('PlanChanged')).revision, 'b'.repeat(64));
  assert.equal((await b.next('PlanChanged')).revision, 'b'.repeat(64)); checks++;
  const result = await a.request('echo', {exact:'stdin reaches handler'}).result();
  assert.equal(result.value.args.exact,'stdin reaches handler');
  assert.equal(result.session,a.session); assert.equal(result.value.page,a.page);
  assert.notEqual(a.session,b.session); assert.equal(b.messages.length,0); checks++;
  for (const [name, code] of [['absent','handler_denied'],['__proto__','handler_denied'],['denied','handler_denied'],
      ['invalid','handler_protocol'],['failed','handler_failed'],['missing','handler_failed'],
      ['oversize','handler_failed'],['hang','handler_timeout']]) {
    const failure = await a.request(name).result();
    assert.equal(failure.ok,false,name); assert.equal(failure.error.code,code,name);
    assert.equal(failure.error.completion,'unknown'); checks++;
  }
  const late = a.request('delayed');
  await sleep(100); a.ws.close();
  const reconnect = await connect(a.page);
  assert.notEqual(reconnect.session,a.session);
  await sleep(800);
  assert.equal(reconnect.messages.some(v=>v.id===late.id),false);
  assert.equal(b.messages.some(v=>v.id===late.id),false); checks++;
  const mismatch=await connect(); mismatch.send({version:'unknown',kind:'Request'});
  assert.equal((await mismatch.next('Error')).error.code,'protocol_mismatch'); checks++;
  const forged=await connect(); forged.send({version,kind:'Request',session:a.session,id:crypto.randomUUID(),handler:'echo',args:{}});
  assert.equal((await forged.next('Error')).error.code,'invalid_request'); checks++;
  const duplicate=await connect(); const first=duplicate.request('echo'); await first.result();
  duplicate.send({version,kind:'Request',session:duplicate.session,id:first.id,handler:'echo',args:{}});
  assert.equal((await duplicate.next('Error')).error.code,'duplicate_or_limit'); checks++;
  const binary=await connect(); binary.ws.send(new Uint8Array([123,125]));
  assert.equal((await binary.next('Error')).error.code,'text_required'); checks++;
  await stop(); await sleep(200);
  assert.notEqual(b.ws.readyState,WebSocket.OPEN); checks++;
  profile.bridge.exclude_origins.push(origin);
  writeFileSync(join(root,'profile.json'),JSON.stringify(profile));
  await start();
  assert.equal((await fetch(base+'/__tap/probe/runtime.js',{headers})).status,403); checks++;
  console.log(JSON.stringify({checks,protocol:version,synthetic:true}));
} finally {
  for (const ws of sockets) ws.close();
  if (hub && hub.exitCode===null) await stop();
}

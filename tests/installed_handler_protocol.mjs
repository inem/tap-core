// Real loopback exercise of an installed handler and the startup snapshot.
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const [root, hubFile, expected] = process.argv.slice(2);
const profile = JSON.parse(readFileSync(join(root, 'profile.json'), 'utf8'));
const secret = readFileSync(join(root, 'state/component-token'), 'utf8').trim();
const token = readFileSync(join(root, 'state/bridge-token'), 'utf8').trim();
const base = `http://127.0.0.1:${profile.bridge.hub_port}`;
const headers = origin => ({origin, 'x-tap-component-token': secret,
  'x-tap-probe-token': token, 'x-tap-probe-origin': origin});
const hub = Bun.spawn([process.execPath, hubFile, root], {stdout:'pipe', stderr:'pipe'});
let ws;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try {
      const response = await fetch(base + '/health', {headers:{authorization:'Bearer ' + secret}});
      if (response.ok) { ready = true; break; }
    } catch {}
    await Bun.sleep(30);
  }
  assert(ready, 'Hub starts from the effective snapshot');
  assert.equal((await fetch(base + '/__tap/probe/runtime.js')).status, 403);
  assert.equal((await fetch(base + '/__tap/probe/runtime.js',
    {headers:headers('https://excluded.example')})).status, 403);
  const pageResponse = await fetch(base + '/__tap/probe/runtime.js',
    {headers:headers('https://fixture.example')});
  if (expected === 'disabled') {
    assert.equal(pageResponse.status, 403, 'disabled pack loses expanded origin');
  } else {
    assert.equal(pageResponse.status, 200, 'pack origin reaches Hub');
    const queue = [];
    ws = new WebSocket(base.replace('http:', 'ws:') + '/__tap/probe/ws',
      {headers:headers('https://fixture.example')});
    ws.addEventListener('message', event => queue.push(JSON.parse(event.data)));
    await new Promise((resolve,reject) => {ws.onopen=resolve; ws.onerror=reject;});
    async function next(kind) {
      for (let i=0; i<100; i++) {
        const index = queue.findIndex(value => value.kind===kind);
        if (index>=0) return queue.splice(index,1)[0];
        await Bun.sleep(30);
      }
      throw new Error('Missing ' + kind);
    }
    const version='tap.bridge/v1', page=crypto.randomUUID();
    ws.send(JSON.stringify({version,kind:'Hello',page,origin:'https://fixture.example'}));
    const welcome=await next('Welcome');
    for (const [args, ok] of [[{text:'hi'},true], [{text:42},false]]) {
      const id=crypto.randomUUID();
      ws.send(JSON.stringify({version,kind:'Request',session:welcome.session,id,
        handler:'fixture.installed-linked',args}));
      const result=await next('Result');
      assert.equal(result.id,id); assert.equal(result.ok,ok);
      if (ok) assert.deepEqual(result.value,{text:expected+'hi'});
      else assert.equal(result.error.code,'bad_request');
    }
  }
  console.log(JSON.stringify({ok:true, expected}));
} finally {
  ws?.close();
  hub.kill();
  await hub.exited;
}

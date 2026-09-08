// Hub protocol probes for linked-http error/timeout paths.
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import assert from 'node:assert/strict';

const [root, mode] = process.argv.slice(2);
const profile = JSON.parse(readFileSync(join(root, 'profile.json'), 'utf8'));
let effective = null;
try {
  effective = JSON.parse(readFileSync(join(root, 'state/effective-runtime.json'), 'utf8'));
} catch {}
const bridge = effective?.bridge ?? profile.bridge;
const secret = readFileSync(join(root, 'state/component-token'), 'utf8').trim();
const token = readFileSync(join(root, 'state/bridge-token'), 'utf8').trim();
const origin = 'http://127.0.0.1:18998';
const base = `http://127.0.0.1:${bridge.hub_port}`;
const headers = {
  origin,
  'x-tap-component-token': secret,
  'x-tap-probe-token': token,
  'x-tap-probe-origin': origin,
};

let ready = false;
for (let i = 0; i < 100; i++) {
  try {
    const response = await fetch(base + '/health', {headers: {authorization: 'Bearer ' + secret}});
    if (response.ok) {
      ready = true;
      break;
    }
  } catch {}
  await Bun.sleep(30);
}
assert(ready, 'Hub health');

const queue = [];
const ws = new WebSocket(base.replace('http:', 'ws:') + '/__tap/probe/ws', {headers});
ws.addEventListener('message', (event) => queue.push(JSON.parse(event.data)));
await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = reject;
});

async function next(kind, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const index = queue.findIndex((value) => value.kind === kind);
    if (index >= 0) return queue.splice(index, 1)[0];
    await Bun.sleep(30);
  }
  throw new Error('Missing ' + kind);
}

const version = 'tap.bridge/v1';
const page = crypto.randomUUID();
ws.send(JSON.stringify({version, kind: 'Hello', page, origin}));
const welcome = await next('Welcome');
const id = crypto.randomUUID();
ws.send(JSON.stringify({
  version, kind: 'Request', session: welcome.session, id,
  handler: 'example.linked-http', args: {},
}));
const result = await next('Result', mode === 'timeout' ? 12000 : 8000);
assert.equal(result.id, id);
ws.close();
if (mode === 'timeout') {
  assert.equal(result.ok, false);
  assert.equal(result.error.code, 'handler_timeout');
  console.log(JSON.stringify({ok: false, error: result.error}));
} else {
  throw new Error('unknown mode ' + mode);
}

// Smoke HTTP/WS against an already-running installer-managed Hub.
// Usage: bun smoke_hub.mjs <profile-root>
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const root = process.argv[2];
const profile = JSON.parse(readFileSync(join(root, 'profile.json'), 'utf8'));
const secret = readFileSync(join(root, 'state/component-token'), 'utf8').trim();
const token = readFileSync(join(root, 'state/bridge-token'), 'utf8').trim();
const origin = profile.bridge.allow_origins[0];
const base = `http://127.0.0.1:${profile.bridge.hub_port}`;
const version = 'tap.bridge/v1';
const headers = {
  'x-tap-component-token': secret,
  'x-tap-probe-token': token,
  'x-tap-probe-origin': origin,
  origin,
};

const health = await fetch(base + '/health', { headers: { authorization: 'Bearer ' + secret } });
assert.equal(health.status, 200, 'Hub /health');
const body = await health.json();
assert.equal(body.version, version);

const runtime = await fetch(base + '/__tap/probe/runtime.js', { headers });
assert.equal(runtime.status, 200, 'page runtime over Hub HTTP');

const ws = new WebSocket(base.replace('http:', 'ws:') + '/__tap/probe/ws', { headers });
const messages = [];
ws.addEventListener('message', (event) => messages.push(JSON.parse(event.data)));
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
const page = crypto.randomUUID();
ws.send(JSON.stringify({ version, kind: 'Hello', page, origin }));
async function next(kind) {
  for (let i = 0; i < 100; i++) {
    const index = messages.findIndex((value) => value.kind === kind);
    if (index >= 0) return messages.splice(index, 1)[0];
    await Bun.sleep(50);
  }
  throw new Error('missing ' + kind);
}
const welcome = await next('Welcome');
assert.equal(welcome.page, page);
const id = crypto.randomUUID();
ws.send(JSON.stringify({
  version, kind: 'Request', session: welcome.session, id, handler: 'echo',
  args: { smoke: 'installer-managed' },
}));
const result = await next('Result');
assert.equal(result.id, id);
assert.equal(result.ok, true);
assert.equal(result.value.args.smoke, 'installer-managed');
ws.close();
console.log(JSON.stringify({ ok: true, hub_port: profile.bridge.hub_port, protocol: version }));

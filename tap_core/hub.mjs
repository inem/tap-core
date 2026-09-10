// Generic volatile request/result transport. No Actions/Needs/Flows or journal.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { timingSafeEqual } from 'node:crypto';
const root = process.argv[2];
const profile = JSON.parse(readFileSync(join(root, 'profile.json'), 'utf8'));
let effective = null;
try { effective = JSON.parse(readFileSync(join(root, 'state/effective-runtime.json'), 'utf8')); } catch {}
const bridge = effective?.bridge ?? profile.bridge;
const components = effective?.components ?? profile.components;
const secret = readFileSync(join(root, 'state/component-token'), 'utf8').trim();
const pageToken = readFileSync(join(root, 'state/bridge-token'), 'utf8').trim();
const here = dirname(fileURLToPath(import.meta.url));
const VERSION = 'tap.bridge/v1';
const peers = new Set();
const pages = new Map(), commands = new Map();
let active = 0;
let planRevision = null;
const equal = (a, b) => typeof a === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));
const allowed = origin => bridge.enabled
  && bridge.allow_origins.includes(origin)
  && !(profile.bridge?.exclude_origins ?? bridge.exclude_origins).includes(origin);
const encode = value => JSON.stringify(value);
const fail = (code, message) => ({ok: false, error: {code, message, completion: 'unknown'}});
function send(ws, value) { if (!ws.data.closed) ws.send(encode({version: VERSION, session: ws.data.session, ...value})); }
function controllerAllowed(request) { return equal(request.headers.get('authorization'), 'Bearer ' + secret); }
function publicPage(ws) { return {page:ws.data.page, origin:ws.data.origin, connected:true}; }
async function controller(request, url) {
  if (!controllerAllowed(request)) return new Response('Denied', {status:403});
  if (request.method === 'GET' && url.pathname === '/v1/pages')
    return Response.json({version:VERSION, pages:[...pages.values()].map(publicPage)});
  const match = url.pathname.match(/^\/v1\/pages\/([^/]+)\/commands$/);
  if (request.method !== 'POST' || !match) return new Response('Not found', {status:404});
  const ws = pages.get(decodeURIComponent(match[1]));
  if (!ws || ws.data.closed) return Response.json({ok:false,error:{code:'page_not_found'}},{status:404});
  if (commands.size >= 8 || [...commands.values()].filter(command => command.ws === ws).length >= 4)
    return Response.json({ok:false,error:{code:'busy'}},{status:503});
  let body;
  try { const text = await request.text(); if (Buffer.byteLength(text) > 65536) throw new Error('input_limit'); body = JSON.parse(text); }
  catch (cause) { return Response.json({ok:false,error:{code:cause.message === 'input_limit' ? 'input_limit' : 'invalid_json'}},{status:400}); }
  if (!body || typeof body.operation !== 'string' || !/^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+$/.test(body.operation) || !Object.hasOwn(body,'args'))
    return Response.json({ok:false,error:{code:'invalid_command'}},{status:400});
  const id = crypto.randomUUID();
  const outcome = await new Promise(resolve => {
    const timer = setTimeout(() => { commands.delete(id); resolve({ok:false,error:{code:'command_timeout'}}); }, 7000);
    commands.set(id, {ws, timer, resolve});
    send(ws, {kind:'Command', id, operation:body.operation, args:body.args});
  });
  return Response.json(outcome, {status:outcome.ok ? 200 : 400});
}
function readPlanRevision() {
  try {
    const value = JSON.parse(readFileSync(join(root, 'state/bridge.json'), 'utf8'))?.configuration;
    return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : null;
  } catch { return null; }
}
planRevision = readPlanRevision();
setInterval(() => {
  const current = readPlanRevision();
  if (current && planRevision && current !== planRevision) {
    planRevision = current;
    for (const ws of peers) if (ws.data.page) send(ws, {kind: 'PlanChanged', revision: current});
  } else if (current) planRevision = current;
}, 500);
function reject(ws, code) { send(ws, {kind: 'Error', ...fail(code, code)}); ws.close(1008, code); }
async function bounded(stream, limit) {
  let count = 0, chunks = [];
  for await (const chunk of stream) {
    count += chunk.byteLength;
    if (count > limit) throw new Error('output_limit');
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString('utf8');
}
async function invoke(ws, request) {
  const binding = Object.hasOwn(components.handlers, request.handler) ? components.handlers[request.handler] : null;
  if (!binding || !binding.origins.includes(ws.data.origin)) return fail('handler_denied', 'Handler is not granted to this origin');
  if (active >= 8 || ws.data.pending >= 4) return fail('busy', 'Handler capacity exceeded');
  active++; ws.data.pending++;
  let child, timer;
  try {
    const context = {version: 1, handler_id: request.handler, origin: ws.data.origin, page_id: ws.data.page,
      session_id: ws.data.session, request_id: request.id, config: binding.config, profile_root: root,
      state_dir: join(root, 'state/handlers', request.handler), output_dir: join(root, 'data/handlers', request.handler),
      log_dir: join(root, 'logs/handlers', request.handler)};
    child = Bun.spawn([components.python, '-B', join(here, 'guardian.py'), String(process.pid), ...binding.command], {
      cwd: root, detached: true, stdin: 'pipe', stdout: 'pipe', stderr: 'pipe',
      env: {...process.env, TAP_PACK_CONTEXT: encode(context)},
    });
    ws.data.children.add(child);
    child.stdin.write(encode({version: 1, request_id: request.id, args: request.args}) + '\n');
    child.stdin.end();
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('handler_timeout')), 5000); });
    const [output, diagnostic, code] = await Promise.race([Promise.all([bounded(child.stdout, 262144), bounded(child.stderr, 65536), child.exited]), timeout]);
    if (code !== 0) {
      writeFileSync(join(context.log_dir, 'last-failure.json'), encode({request_id: request.id, exit_code: code, stderr: diagnostic}), {mode: 0o600});
      return fail('handler_failed', 'Handler exited unsuccessfully');
    }
    let result;
    try { result = JSON.parse(output); } catch { return fail('handler_protocol', 'Expected JSON handler result'); }
    if (!result || typeof result !== 'object' || typeof result.ok !== 'boolean') return fail('handler_protocol', 'Expected JSON result with boolean ok');
    if (result.ok) return {ok: true, value: result.value ?? null};
    if (typeof result.error?.code !== 'string' || typeof result.error?.message !== 'string') return fail('handler_protocol', 'Expected typed handler error');
    return {ok: false, error: {code: result.error.code.slice(0, 64), message: result.error.message.slice(0, 1024), completion: 'unknown'}};
  } catch (error) { return fail(error.message === 'handler_timeout' ? 'handler_timeout' : 'handler_failed', 'Handler failed or completion is uncertain'); }
  finally {
    clearTimeout(timer);
    if (child) { try { process.kill(-child.pid, 'SIGKILL'); } catch {} ws.data.children.delete(child); }
    active--; ws.data.pending--;
  }
}
const server = Bun.serve({
  hostname: '127.0.0.1', port: bridge.hub_port,
  async fetch(request, server) {
    const url = new URL(request.url);
    if (url.pathname.startsWith('/v1/')) return controller(request, url);
    if (request.method !== 'GET') return new Response('Method not allowed', {status: 405});
    if (url.pathname === '/health') return equal(request.headers.get('authorization'), 'Bearer ' + secret)
      ? Response.json({version: VERSION, pid: process.pid, sessions: peers.size, active_handlers: active}) : new Response('Denied', {status: 403});
    const origin = request.headers.get('x-tap-probe-origin');
    if (!equal(request.headers.get('x-tap-component-token'), secret) || !equal(request.headers.get('x-tap-probe-token'), pageToken) || !allowed(origin))
      return new Response('Denied', {status: 403});
    if (url.pathname === '/__tap/probe/runtime.js') return new Response(Bun.file(join(here, 'page-runtime.js')), {headers: {'Content-Type': 'application/javascript', 'Cache-Control': 'no-store'}});
    if (url.pathname !== '/__tap/probe/ws' || request.headers.get('origin') !== origin) return new Response('Denied', {status: 403});
    if (peers.size >= 64) return new Response('Busy', {status: 503});
    if (server.upgrade(request, {data: {origin, session: crypto.randomUUID(), page: null, closed: false, pending: 0, seen: new Set(), children: new Set()}})) return;
    return new Response('Expected WebSocket', {status: 400});
  },
  websocket: {
    maxPayloadLength: 65536, idleTimeout: 30,
    open(ws) { peers.add(ws); ws.data.timer = setTimeout(() => reject(ws, 'hello_timeout'), 3000); },
    async message(ws, raw) {
      if (typeof raw !== 'string') return reject(ws, 'text_required');
      let value;
      try { value = JSON.parse(raw); } catch { return reject(ws, 'invalid_json'); }
      if (!value || value.version !== VERSION) return reject(ws, 'protocol_mismatch');
      if (!ws.data.page) {
        if (value.kind !== 'Hello' || typeof value.page !== 'string' || !/^[a-f0-9-]{36}$/.test(value.page) || value.origin !== ws.data.origin)
          return reject(ws, 'invalid_hello');
        clearTimeout(ws.data.timer); ws.data.page = value.page;
        const previous = pages.get(value.page);
        if (previous && previous !== ws) previous.close(1000, 'replaced');
        pages.set(value.page, ws);
        return send(ws, {kind: 'Welcome', page: value.page, revision: planRevision});
      }
      if (value.kind === 'CommandResult' && value.session === ws.data.session && typeof value.id === 'string') {
        const command = commands.get(value.id);
        if (!command) return;
        if (command.ws !== ws || typeof value.ok !== 'boolean') return reject(ws, 'invalid_command_result');
        commands.delete(value.id); clearTimeout(command.timer);
        command.resolve(value.ok ? {ok:true,value:value.value ?? null} : {ok:false,error:value.error || {code:'operation_failed'}});
        return;
      }
      if (value.kind !== 'Request' || value.session !== ws.data.session || typeof value.id !== 'string' || !/^[a-f0-9-]{36}$/.test(value.id)
          || typeof value.handler !== 'string' || !Object.hasOwn(value, 'args')) return reject(ws, 'invalid_request');
      if (ws.data.seen.has(value.id) || ws.data.seen.size >= 1024) return reject(ws, 'duplicate_or_limit');
      ws.data.seen.add(value.id);
      const result = await invoke(ws, value);
      // A result belongs to this socket/session only, never a lookup by page ID.
      send(ws, {kind: 'Result', id: value.id, ...result});
    },
    close(ws) {
      clearTimeout(ws.data.timer); ws.data.closed = true; peers.delete(ws);
      if (ws.data.page && pages.get(ws.data.page) === ws) pages.delete(ws.data.page);
      for (const [id, command] of commands) if (command.ws === ws) {
        commands.delete(id); clearTimeout(command.timer); command.resolve({ok:false,error:{code:'disconnected'}});
      }
      for (const child of ws.data.children) { try { process.kill(-child.pid, 'SIGKILL'); } catch {} }
    },
  },
});
console.log(encode({ready: true, pid: process.pid, version: VERSION, port: server.port}));

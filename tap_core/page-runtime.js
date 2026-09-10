// Generic page requests; volatile connection epochs, no action/outbox replay.
(() => {
  'use strict';
  if (window.top !== window || window.TapBridge) return;
  const bootstrap = document.currentScript;
  const token = bootstrap.dataset.tapToken;
  const websocketEnabled = bootstrap.dataset.tapWs !== 'false';
  let appliedMode = bootstrap.dataset.tapMode === 'development' ? 'development' : 'installed';
  const version = 'tap.bridge/v1', page = crypto.randomUUID();
  const planVersion = 'tap.page-plan/v1';
  let appliedPlan = bootstrap.dataset.tapPlan, appliedPacks = [];
  const pending = new Map();
  const exposed = new Map(), inbound = new Set();
  const devExecutions = new Map();
  let activitySequence = 0;
  let socket, session, timer, planTimer, checkingPlan = false, reloading = false, planState = 'current';
  let closed = false, attempt = 0, paused = !websocketEnabled;
  let state = websocketEnabled ? 'connecting' : 'disabled';
  const error = code => Object.assign(new Error(code), {code, completion: 'unknown'});
  function send(value) {
    if (session && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({version, session, ...value}));
  }
  function commandResult(id, ok, value) {
    let result = {kind:'CommandResult', id, ok, ...(ok ? {value: value ?? null} : {error:value})};
    let encoded;
    try { encoded = JSON.stringify({version, session, ...result}); }
    catch { result = {kind:'CommandResult', id, ok:false, error:{code:'output_invalid', message:'Command result is not JSON'}}; encoded = JSON.stringify({version, session, ...result}); }
    if (new TextEncoder().encode(encoded).length > 262144)
      encoded = JSON.stringify({version, session, kind:'CommandResult', id, ok:false, error:{code:'output_limit', message:'Command result exceeds 256 KiB'}});
    if (socket?.readyState === WebSocket.OPEN) socket.send(encoded);
  }
  function inspectPage(args) {
    if (!args || typeof args.selector !== 'string' || !args.selector || args.selector.length > 2048
        || !Number.isInteger(args.limit) || args.limit < 1 || args.limit > 100)
      throw Object.assign(new Error('Expected selector and limit from 1 to 100'), {code:'invalid_arguments'});
    let selected;
    try { selected = document.querySelectorAll(args.selector); }
    catch { throw Object.assign(new Error('Selector is invalid'), {code:'invalid_selector'}); }
    const nodes = Array.from(selected).slice(0, args.limit).map(node => {
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return {
        tag:String(node.tagName || '').toLowerCase(), id:node.id || null,
        classes:Array.from(node.classList || []).slice(0, 32), role:node.getAttribute?.('role') || null,
        text:String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1024),
        rect:{x:rect.x, y:rect.y, width:rect.width, height:rect.height},
        visible:style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
        html:String(node.outerHTML || '').slice(0, 4096),
      };
    });
    return {url:location.href, title:document.title, selector:args.selector, total:selected.length, nodes};
  }
  const devCallback = '__tapDevResult_' + page.replaceAll('-', '');
  Object.defineProperty(window, devCallback, {enumerable:false, configurable:false, value:(id, outcome) => {
    const task = devExecutions.get(id);
    if (!task) return;
    Promise.resolve(outcome).then(task.resolve, task.reject).finally(() => {
      clearTimeout(task.timer); devExecutions.delete(id);
    });
  }});
  function executeDevelopmentSource(args) {
    if (!args || typeof args.source !== 'string' || !args.source
        || new TextEncoder().encode(args.source).length > 65536)
      throw Object.assign(new Error('Expected 1 to 65536 UTF-8 bytes of source'), {code:'invalid_arguments'});
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        devExecutions.delete(id);
        reject(Object.assign(new Error('Development source did not complete'), {code:'execution_timeout'}));
      }, 5000);
      devExecutions.set(id, {resolve, reject, timer});
      const script = document.createElement('script');
      if (bootstrap.nonce) script.nonce = bootstrap.nonce;
      script.textContent = `window[${JSON.stringify(devCallback)}](${JSON.stringify(id)},(async()=>{\n${args.source}\n})())`;
      try { (document.head || document.documentElement).appendChild(script); }
      catch (cause) {
        clearTimeout(timer); devExecutions.delete(id); reject(cause);
      } finally { script.remove(); }
    });
  }
  exposed.set('tap.dev.inspect', inspectPage);
  exposed.set('tap.dev.execute', executeDevelopmentSource);
  async function runCommand(value) {
    if (inbound.size >= 4) return commandResult(value.id, false, {code:'busy', message:'Page command capacity exceeded'});
    const handler = exposed.get(value.operation);
    if (!handler) return commandResult(value.id, false, {code:'operation_unavailable', message:'Page operation is not exposed'});
    inbound.add(value.id); activitySequence++;
    try { commandResult(value.id, true, await handler(value.args)); }
    catch (cause) { commandResult(value.id, false, {code:cause?.code || 'operation_failed', message:String(cause?.message || cause).slice(0,1024)}); }
    finally { inbound.delete(value.id); }
  }
  function schedulePlan(delay = 2000) {
    clearTimeout(planTimer);
    if (!closed && !reloading) planTimer = setTimeout(checkPlan, delay);
  }
  async function checkPlan() {
    if (closed || checkingPlan || reloading) return;
    checkingPlan = true; planState = 'checking';
    try {
      const url = new URL('/__tap/probe/plan.json', location.href);
      url.searchParams.set('token', token);
      const response = await fetch(url, {cache: 'no-store', credentials: 'omit'});
      if (!response.ok) throw new Error('plan_unavailable');
      const plan = await response.json();
      if (!plan || plan.version !== planVersion || !/^[a-f0-9]{64}$/.test(plan.revision)
          || !Array.isArray(plan.scripts) || !['current','revoked'].includes(plan.access)
          || !['installed','development'].includes(plan.mode)
          || !Array.isArray(plan.packs) || plan.packs.some((pack, index) => !pack
            || typeof pack.id !== 'string' || !/^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$/.test(pack.id)
            || typeof pack.version !== 'string' || !/^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/.test(pack.version)
            || pack.features !== undefined && (!Array.isArray(pack.features) || pack.features.length > 32
              || pack.features.some(feature => !feature || typeof feature.id !== 'string'
                || !/^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$/.test(feature.id)
                || typeof feature.label !== 'string' || !feature.label || feature.label.length > 80
                || typeof feature.value !== 'string' || !feature.value || feature.value.length > 160
                || feature.folder !== undefined && (typeof feature.folder !== 'string'
                  || !feature.folder || feature.folder.length > 240
                  || !/^data(?:\/(?!\.{1,2}(?:\/|$))[^/\\\0]+)*$/.test(feature.folder))))
            || plan.packs.findIndex(other => other.id === pack.id) !== index)
          || plan.application !== 'reload') throw new Error('plan_invalid');
      if (appliedPlan && plan.revision !== appliedPlan) {
        reloading = true; planState = 'reloading';
        const target = new URL(location.href);
        target.searchParams.set('tap-ui', plan.revision.slice(0, 12));
        location.replace(target.href);
        return;
      }
      appliedPlan = plan.revision; appliedMode = plan.mode; appliedPacks = plan.packs.map(pack => Object.freeze({
        ...pack, features:Object.freeze((pack.features || []).map(feature => Object.freeze({...feature}))),
      })); planState = plan.access;
      schedulePlan();
    } catch { planState = 'unavailable'; schedulePlan(5000); }
    finally { checkingPlan = false; }
  }
  function connect() {
    if (!websocketEnabled || closed || paused || socket && socket.readyState < 2) return;
    clearTimeout(timer); state = 'connecting';
    const url = new URL('/__tap/probe/ws', location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('token', token);
    const current = new WebSocket(url);
    socket = current;
    current.onopen = () => { if (socket === current) current.send(JSON.stringify({version, kind: 'Hello', page, origin: location.origin})); };
    current.onmessage = event => {
      if (socket !== current) return;
      let value;
      try { value = JSON.parse(event.data); } catch { current.close(); return; }
      if (value.version !== version || socket !== current) { current.close(); return; }
      if (value.kind === 'Welcome' && value.page === page) { session = value.session; attempt = 0; state = 'ready'; checkPlan(); return; }
      if (value.session !== session) return;
      if (value.kind === 'PlanChanged') { checkPlan(); return; }
      if (value.kind === 'Command' && typeof value.id === 'string' && typeof value.operation === 'string') {
        void runCommand(value); return;
      }
      if (value.kind === 'Result') {
        const task = pending.get(value.id);
        if (task) { pending.delete(value.id); clearTimeout(task.timer); value.ok ? task.resolve(value.value) : task.reject(Object.assign(error(value.error?.code || 'handler_error'), value.error)); }
      }
    };
    current.onclose = () => {
      if (socket !== current) return;
      session = null;
      for (const task of pending.values()) { clearTimeout(task.timer); task.reject(error('disconnected')); }
      pending.clear();
      socket = null;
      if (!closed && !paused && attempt < 8) { state = 'retrying'; timer = setTimeout(connect, Math.min(5000, 200 * 2 ** attempt++)); }
      else state = paused ? 'paused' : closed ? 'suspended' : 'unavailable';
    };
  }
  function stop() {
    clearTimeout(timer);
    const old = socket; socket = null; session = null;
    for (const task of pending.values()) { clearTimeout(task.timer); task.reject(error('disconnected')); }
    pending.clear(); old?.close();
  }
  window.TapBridge = Object.freeze({
    status: () => ({state, scope:'document', pending:pending.size,
      activity:{pending:pending.size + inbound.size, outbound:pending.size, inbound:inbound.size, sequence:activitySequence},
      plan: appliedPlan, plan_state: planState, mode: appliedMode, packs: appliedPacks,
      actions: !websocketEnabled ? [] : paused ? ['connect'] : state === 'unavailable' ? ['reconnect','disconnect'] : ['disconnect']}),
    disconnect() { if (!websocketEnabled) return; paused = true; stop(); state = 'paused'; },
    connect() { if (!websocketEnabled || closed) return; paused = false; attempt = 0; connect(); },
    reconnect() { if (!websocketEnabled || closed) return; paused = false; attempt = 0; stop(); connect(); },
    isReady: () => !!session && socket?.readyState === WebSocket.OPEN,
    expose(operation, handler) {
      if (typeof operation !== 'string' || !/^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+$/.test(operation) || typeof handler !== 'function')
        throw new TypeError('invalid_operation');
      if (exposed.has(operation)) throw new Error('operation_already_exposed');
      exposed.set(operation, handler);
      return () => { if (exposed.get(operation) === handler) exposed.delete(operation); };
    },
    request(handler, args) {
      if (!session || socket?.readyState !== WebSocket.OPEN) return Promise.reject(error('not_connected'));
      if (pending.size >= 4) return Promise.reject(error('busy'));
      const id = crypto.randomUUID(), value = JSON.stringify({version, kind: 'Request', session, id, handler, args});
      if (new TextEncoder().encode(value).length > 65536) return Promise.reject(error('input_limit'));
      activitySequence++;
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => { pending.delete(id); reject(error('request_timeout')); }, 6500);
        pending.set(id, {resolve, reject, timer: timeout});
        socket.send(value);
      });
    },
  });
  addEventListener('pagehide', () => { closed = true; clearTimeout(planTimer); stop(); state = websocketEnabled ? (paused ? 'paused' : 'suspended') : 'disabled'; });
  addEventListener('pageshow', event => { if (event.persisted) { closed = false; attempt = 0; schedulePlan(0); if (!paused) connect(); } });
  schedulePlan(0);
  connect();
})();

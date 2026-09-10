// Generic page requests; volatile connection epochs, no action/outbox replay.
(() => {
  'use strict';
  if (window.top !== window || window.TapBridge) return;
  const bootstrap = document.currentScript;
  const token = bootstrap.dataset.tapToken;
  const websocketEnabled = bootstrap.dataset.tapWs !== 'false';
  const version = 'tap.bridge/v1', page = crypto.randomUUID();
  const planVersion = 'tap.page-plan/v1';
  let appliedPlan = bootstrap.dataset.tapPlan, appliedPacks = [];
  const pending = new Map();
  let socket, session, timer, planTimer, checkingPlan = false, reloading = false, planState = 'current';
  let closed = false, attempt = 0, paused = !websocketEnabled;
  let state = websocketEnabled ? 'connecting' : 'disabled';
  const error = code => Object.assign(new Error(code), {code, completion: 'unknown'});
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
          || !Array.isArray(plan.packs) || plan.packs.some((pack, index) => !pack
            || typeof pack.id !== 'string' || !/^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$/.test(pack.id)
            || typeof pack.version !== 'string' || !/^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/.test(pack.version)
            || pack.features !== undefined && (!Array.isArray(pack.features) || pack.features.length > 32
              || pack.features.some(feature => !feature || typeof feature.id !== 'string'
                || !/^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$/.test(feature.id)
                || typeof feature.label !== 'string' || !feature.label || feature.label.length > 80
                || typeof feature.value !== 'string' || !feature.value || feature.value.length > 160))
            || plan.packs.findIndex(other => other.id === pack.id) !== index)
          || plan.application !== 'reload') throw new Error('plan_invalid');
      if (appliedPlan && plan.revision !== appliedPlan) {
        reloading = true; planState = 'reloading';
        const target = new URL(location.href);
        target.searchParams.set('tap-ui', plan.revision.slice(0, 12));
        location.replace(target.href);
        return;
      }
      appliedPlan = plan.revision; appliedPacks = plan.packs.map(pack => Object.freeze({
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
      plan: appliedPlan, plan_state: planState, packs: appliedPacks,
      actions: !websocketEnabled ? [] : paused ? ['connect'] : state === 'unavailable' ? ['reconnect','disconnect'] : ['disconnect']}),
    disconnect() { if (!websocketEnabled) return; paused = true; stop(); state = 'paused'; },
    connect() { if (!websocketEnabled || closed) return; paused = false; attempt = 0; connect(); },
    reconnect() { if (!websocketEnabled || closed) return; paused = false; attempt = 0; stop(); connect(); },
    isReady: () => !!session && socket?.readyState === WebSocket.OPEN,
    request(handler, args) {
      if (!session || socket?.readyState !== WebSocket.OPEN) return Promise.reject(error('not_connected'));
      if (pending.size >= 4) return Promise.reject(error('busy'));
      const id = crypto.randomUUID(), value = JSON.stringify({version, kind: 'Request', session, id, handler, args});
      if (new TextEncoder().encode(value).length > 65536) return Promise.reject(error('input_limit'));
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

// Generic page requests; volatile connection epochs, no action/outbox replay.
(() => {
  'use strict';
  if (window.top !== window || window.TapBridge) return;
  const token = document.currentScript.dataset.tapToken;
  const version = 'tap.bridge/v1', page = crypto.randomUUID();
  const pending = new Map();
  let socket, session, timer, closed = false, attempt = 0, paused = false, state = 'connecting';
  const error = code => Object.assign(new Error(code), {code, completion: 'unknown'});
  function connect() {
    if (closed || paused || socket && socket.readyState < 2) return;
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
      if (value.kind === 'Welcome' && value.page === page) { session = value.session; attempt = 0; state = 'ready'; return; }
      if (value.session !== session) return;
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
      actions: paused ? ['connect'] : state === 'unavailable' ? ['reconnect','disconnect'] : ['disconnect']}),
    disconnect() { paused = true; stop(); state = 'paused'; },
    connect() { if (closed) return; paused = false; attempt = 0; connect(); },
    reconnect() { if (closed) return; paused = false; attempt = 0; stop(); connect(); },
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
  addEventListener('pagehide', () => { closed = true; stop(); state = paused ? 'paused' : 'suspended'; });
  addEventListener('pageshow', event => { if (event.persisted) { closed = false; attempt = 0; if (!paused) connect(); } });
  connect();
})();

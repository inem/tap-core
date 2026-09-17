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
  // Pages that enforce `require-trusted-types-for 'script'` reject assigning a
  // plain string to script.textContent. Mint a TrustedScript through our own
  // policy so the development channel works there too. If the page's CSP does
  // not allow creating this policy (a `trusted-types` allowlist, or a locked
  // default policy), createPolicy throws; fall back to the raw string and let
  // the resulting page-side error surface with its message instead of a bare
  // operation_failed. (This is the script-sink counterpart to packs building
  // DOM via createElement to survive a default innerHTML-sanitizing policy.)
  let devScriptPolicy, devScriptPolicyTried = false;
  function developmentScript(source) {
    const trusted = window.trustedTypes;
    if (!trusted || typeof trusted.createPolicy !== 'function') return source;
    if (!devScriptPolicyTried) {
      devScriptPolicyTried = true;
      try { devScriptPolicy = trusted.createPolicy('tap-dev-execute', {createScript: value => value}); }
      catch { devScriptPolicy = null; }
    }
    return devScriptPolicy ? devScriptPolicy.createScript(source) : source;
  }
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
      const source = `window[${JSON.stringify(devCallback)}](${JSON.stringify(id)},(async()=>{\n${args.source}\n})())`;
      try {
        script.textContent = developmentScript(source);
        (document.head || document.documentElement).appendChild(script);
      } catch (cause) {
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
    clearTimeout(timer); state = 'connecting'; updateIndicator();
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
      if (value.kind === 'Welcome' && value.page === page) { session = value.session; attempt = 0; state = 'ready'; updateIndicator(); checkPlan(); return; }
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
      if (!closed && !paused && attempt < 8) { state = 'retrying'; updateIndicator(); timer = setTimeout(connect, Math.min(5000, 200 * 2 ** attempt++)); }
      else { state = paused ? 'paused' : closed ? 'suspended' : 'unavailable'; updateIndicator(); }
    };
  }
  function stop() {
    clearTimeout(timer);
    const old = socket; socket = null; session = null;
    for (const task of pending.values()) { clearTimeout(task.timer); task.reject(error('disconnected')); }
    pending.clear(); old?.close();
  }
  let indicatorHost, indicatorButton, indicatorDot, indicatorPanel, indicatorStatusDot, indicatorStatusTitle, indicatorHostname, indicatorPacks, indicatorObserver;
  function htmlText(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    })[char]);
  }
  function hasPackInspector() {
    return !!window.__tapInspector || !!document.querySelector?.('[data-tap-inspector]');
  }
  function runtimeTitle() {
    return !window.TapBridge ? 'Unavailable'
      : planState === 'revoked' ? 'Inactive'
      : planState === 'checking' || planState === 'reloading' ? 'Updating'
      : appliedMode === 'development' ? 'Development'
      : state === 'ready' ? 'Active'
      : state === 'retrying' || state === 'connecting' ? 'Connecting'
      : 'Unavailable';
  }
  function renderIndicatorPacks() {
    if (!indicatorPacks) return;
    indicatorPacks.replaceChildren?.();
    indicatorPacks.innerHTML = appliedPacks.map(pack => {
      const features = (pack.features || []).map(feature => `<li class="feature-item"><div class="feature-row"><span>${htmlText(feature.label)}</span><span>${htmlText(feature.value)}</span></div></li>`).join('');
      return `<li class="pack"><div class="pack-row"><span>${htmlText(pack.id)}</span><span>${htmlText(pack.version)}</span></div>${features ? `<ul class="pack-features">${features}</ul>` : ''}</li>`;
    }).join('');
  }
  function setIndicatorOpen(open) {
    if (!indicatorPanel || !indicatorButton) return;
    indicatorPanel.hidden = !open;
    indicatorButton.setAttribute('aria-expanded', String(open));
    if (open) renderIndicatorPacks();
  }
  function updateIndicator() {
    if (!indicatorHost) return;
    if (hasPackInspector()) { indicatorHost.hidden = true; return; }
    indicatorHost.hidden = false;
    const label = `TAP ${state}${appliedMode === 'development' ? ' development' : ''}`;
    indicatorHost.dataset.tapState = state;
    indicatorHost.dataset.tapMode = appliedMode;
    indicatorHost.title = label;
    if (indicatorHostname) indicatorHostname.textContent = location.hostname;
    if (indicatorStatusTitle) indicatorStatusTitle.textContent = runtimeTitle();
    if (indicatorStatusDot) indicatorStatusDot.className = `status-dot ${appliedMode === 'development' ? 'development' : state === 'ready' ? 'active' : planState === 'unavailable' ? 'warning' : ''}`;
    if (indicatorDot) indicatorDot.className = `dot ${appliedMode === 'development' ? 'development' : state === 'ready' ? 'active' : planState === 'unavailable' ? 'warning' : ''}`;
    indicatorButton?.setAttribute('aria-label', 'Open TAP page context');
    renderIndicatorPacks();
  }
  function renderIndicator() {
    if (indicatorHost || !document.documentElement || typeof document.createElement !== 'function') return;
    try {
      const host = document.createElement('div');
      if (typeof host.attachShadow !== 'function') return;
      host.setAttribute('data-tap-core-indicator', '');
      host.style.cssText = 'position:fixed!important;left:0!important;bottom:0!important;z-index:2147483645!important;font:13px/1.45 system-ui,-apple-system,sans-serif!important;color:#eef1f3!important;color-scheme:dark!important;';
      const root = host.attachShadow({mode:'open'});
      root.innerHTML = `<style>
        *{box-sizing:border-box}button{font:inherit;color:inherit;cursor:pointer}
        .lamp{border:0;background:transparent;width:20px;height:20px;padding:6px;display:flex;align-items:center;justify-content:center;pointer-events:auto}
        .dot,.status-dot{width:6px;height:6px;border-radius:50%;border:1px solid #9aa4ae;flex:none}.lamp .dot{width:8px;height:8px}.active{background:#77d8ac;border-color:#77d8ac}.lamp .active{box-shadow:0 0 6px #77d8acaa}.development,.transport{background:#5aa9ff;border-color:#5aa9ff}.lamp .development{box-shadow:0 0 6px #5aa9ffaa}.warning{background:#e5b567;border-color:#e5b567}.lamp .warning{box-shadow:0 0 6px #e5b567aa}
        section{position:absolute;bottom:24px;left:8px;width:min(320px,calc(100vw - 24px));max-height:65vh;overflow:auto;border:1px solid #505a65;border-radius:14px;background:#20262d;box-shadow:0 8px 32px #0005;padding:16px}
        [hidden]{display:none}header{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;gap:10px;align-items:center;margin-bottom:7px}header>strong{font-size:14px}header>button{border:0;background:none;font-size:20px;padding:3px 4px}.hostname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#adb6bf}
        .status{display:flex;gap:6px;align-items:center;font-size:12px;color:#cbd2d8}
        h2{font-size:11px;line-height:1.3;text-transform:uppercase;letter-spacing:.08em;color:#8f9aa5;margin:17px 0 5px}
        ul{list-style:none;padding:0;margin:0}.pack{padding:0;border-top:1px solid #ffffff14}.pack-row,.feature-row{display:flex;justify-content:space-between;gap:12px}.pack-row{padding:9px 0}.pack-row span:last-child,.feature-row span:last-child{color:#adb6bf;text-align:right}.pack-features{margin:0 0 7px 9px;padding-left:10px;border-left:1px solid #ffffff18}.feature-item{padding:5px 0}.feature-row{font-size:12px}.feature-row span:first-child{color:#cbd2d8}
        button:focus-visible{outline:2px solid #77d8ac;outline-offset:3px}
      </style>
      <section id="panel" role="region" aria-label="TAP page context" hidden>
        <header><strong>TAP</strong><small class="hostname"></small><div class="status"><span class="status-dot"></span><strong></strong></div><button type="button" class="close" aria-label="Close">×</button></header>
        <div class="packs"><h2>Packs</h2><ul></ul></div>
      </section>
      <button type="button" class="lamp" aria-label="Open TAP page context" aria-expanded="false" aria-controls="panel"><span class="dot"></span></button>`;
      indicatorHost = host;
      indicatorButton = root.querySelector('.lamp');
      indicatorDot = root.querySelector('.dot');
      indicatorPanel = root.querySelector('#panel');
      indicatorStatusDot = root.querySelector('.status-dot');
      indicatorStatusTitle = root.querySelector('.status strong');
      indicatorHostname = root.querySelector('.hostname');
      indicatorPacks = root.querySelector('.packs ul');
      indicatorButton.onclick = () => setIndicatorOpen(indicatorPanel.hidden);
      root.querySelector('.close').onclick = () => { setIndicatorOpen(false); indicatorButton.focus?.(); };
      if (typeof MutationObserver === 'function') {
        indicatorObserver = new MutationObserver(updateIndicator);
        indicatorObserver.observe(document.documentElement, {childList:true, subtree:true});
      }
      updateIndicator();
      (document.body || document.documentElement).appendChild(host);
    } catch { indicatorHost = indicatorButton = indicatorDot = indicatorPanel = indicatorStatusDot = indicatorStatusTitle = indicatorHostname = indicatorPacks = null; }
  }
  const ready = () => !!session && socket?.readyState === WebSocket.OPEN;
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function waitForReady(timeoutMs = 3000) {
    if (ready()) return true;
    if (!websocketEnabled || closed || paused) return false;
    if (!socket || state === 'unavailable') { attempt = 0; stop(); connect(); }
    const deadline = performance.now() + timeoutMs;
    while (!ready() && performance.now() < deadline) await delay(50);
    return ready();
  }
  window.TapBridge = Object.freeze({
    status: () => ({state, scope:'document', pending:pending.size,
      activity:{pending:pending.size + inbound.size, outbound:pending.size, inbound:inbound.size, sequence:activitySequence},
      plan: appliedPlan, plan_state: planState, mode: appliedMode, packs: appliedPacks,
      actions: !websocketEnabled ? [] : paused ? ['connect'] : state === 'unavailable' ? ['reconnect','disconnect'] : ['disconnect']}),
    disconnect() { if (!websocketEnabled) return; paused = true; stop(); state = 'paused'; updateIndicator(); },
    connect() { if (!websocketEnabled || closed) return; paused = false; attempt = 0; connect(); },
    reconnect() { if (!websocketEnabled || closed) return; paused = false; attempt = 0; stop(); connect(); },
    isReady: ready,
    expose(operation, handler) {
      if (typeof operation !== 'string' || !/^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+$/.test(operation) || typeof handler !== 'function')
        throw new TypeError('invalid_operation');
      if (exposed.has(operation)) throw new Error('operation_already_exposed');
      exposed.set(operation, handler);
      return () => { if (exposed.get(operation) === handler) exposed.delete(operation); };
    },
    async request(handler, args, options = {}) {
      const timeoutMs = Number.isInteger(options.timeout_ms) && options.timeout_ms >= 1000 && options.timeout_ms <= 60000
        ? options.timeout_ms : 6500;
      if (!ready() && !await waitForReady(Math.min(3000, timeoutMs))) throw error('not_connected');
      if (pending.size >= 4) throw error('busy');
      const id = crypto.randomUUID(), value = JSON.stringify({version, kind: 'Request', session, id, handler, args});
      if (new TextEncoder().encode(value).length > 65536) throw error('input_limit');
      activitySequence++;
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => { pending.delete(id); reject(error('request_timeout')); }, timeoutMs);
        pending.set(id, {resolve, reject, timer: timeout});
        socket.send(value);
      });
    },
  });
  addEventListener('pagehide', () => { closed = true; clearTimeout(planTimer); indicatorObserver?.disconnect(); stop(); state = websocketEnabled ? (paused ? 'paused' : 'suspended') : 'disabled'; updateIndicator(); });
  addEventListener('pageshow', event => { if (event.persisted) { closed = false; attempt = 0; schedulePlan(0); if (!paused) connect(); } });
  renderIndicator();
  schedulePlan(0);
  connect();
})();

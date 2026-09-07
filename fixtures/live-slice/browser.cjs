// Playwright is an explicit development dependency, not a core runtime.
const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const { chromium } = require(config.playwright);
let activeBrowser;
process.once('SIGTERM', async () => {
  try { if (activeBrowser) await activeBrowser.close(); }
  finally { process.exit(143); }
});
(async () => {
  const browser = await chromium.launch({
    executablePath: config.chrome, headless: true,
    proxy: { server: `http://127.0.0.1:${config.proxy_port}`, bypass: '<-loopback>' },
    args: ['--disable-background-networking', '--disable-component-update'],
  });
  activeBrowser = browser;
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      return [config.origin, config.second_origin, config.denied_origin].includes(url.origin)
        ? route.continue() : route.abort();
    });
    if (config.disabled) {
      for (const origin of [config.origin, config.second_origin, config.denied_origin]) {
        const page = await context.newPage();
        await page.goto(origin, { waitUntil: 'domcontentloaded' });
        if (await page.locator('#tap-probe-bootstrap').count()) throw new Error('disabled injection');
      }
      fs.writeFileSync(config.root + '/disabled-result.json', JSON.stringify({ not_injected: true }));
      return;
    }
    const page = await context.newPage();
    const frames = { sent: [], received: [] };
    let sockets = 0;
    page.on('websocket', ws => {
      if (new URL(ws.url()).pathname !== '/__tap/probe/ws') return;
      sockets++;
      ws.on('framesent', frame => { try { frames.sent.push(JSON.parse(frame.payload).kind); } catch {} });
      ws.on('framereceived', frame => { try { frames.received.push(JSON.parse(frame.payload).kind); } catch {} });
    });
    await page.goto(config.origin, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => window.TapProbe?.isReady(), null, { timeout: 15000 });
    if (await page.locator('#tap-probe-bootstrap').count() !== 1) throw new Error('bootstrap count');
    const second = await context.newPage();
    await second.goto(config.second_origin, { waitUntil: 'domcontentloaded' });
    await second.waitForFunction(() => window.TapProbe?.isReady(), null, { timeout: 15000 });
    const denied = await context.newPage();
    await denied.goto(config.denied_origin, { waitUntil: 'domcontentloaded' });
    if (await denied.locator('#tap-probe-bootstrap').count() !== 0) throw new Error('denied injection');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => window.TapProbe?.isReady(), null, { timeout: 15000 });
    if (await page.locator('#tap-probe-bootstrap').count() !== 1) throw new Error('reload bootstrap count');
    await page.locator('#load').click();
    await page.waitForFunction(() => document.querySelector('#result').textContent !== 'waiting', null, { timeout: 30000 });
    const displayed = JSON.parse(await page.locator('#result').textContent());
    if (await second.locator('#result').textContent() !== 'waiting') throw new Error('wrong tab received result');
    // Keep the page alive until its command Result reaches the local controller.
    await page.waitForFunction(() => document.querySelector('#confirmed').textContent === 'confirmed', null, { timeout: 15000 });
    fs.writeFileSync(config.root + '/browser-result.json', JSON.stringify({
      browser: browser.version(), displayed, sockets, frames,
      injected: true, denied_origin_unchanged: true, other_tab_unchanged: true,
    }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

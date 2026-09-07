const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = require(config.playwright);
(async () => {
  const browser = await chromium.launch({executablePath: config.chrome, headless: true,
    proxy: {server: `http://127.0.0.1:${config.proxy_port}`, bypass: '<-loopback>'},
    args: ['--disable-background-networking', '--disable-component-update']});
  try {
    const context = await browser.newContext();
    let leaked = 0;
    await context.route('**/*', route => {
      const u = new URL(route.request().url());
      if (u.searchParams.has('token') && !config.origins.slice(0,2).includes(u.origin)) leaked++;
      return config.origins.includes(u.origin) ? route.continue() : route.abort();
    });
    const pages = [];
    for (const origin of config.origins) {
      const page = await context.newPage(); pages.push(page);
      await page.goto(origin, {waitUntil: 'domcontentloaded'});
    }
    for (const page of pages.slice(0,2)) await page.waitForFunction(() => window.TapBridge?.isReady());
    if (await pages[2].locator('#tap-probe-bootstrap').count()) throw new Error('excluded page injected');
    const first = await pages[0].evaluate(() => TapBridge.request('echo', {tab: 1}));
    const second = await pages[1].evaluate(() => TapBridge.request('echo', {tab: 2}));
    if (first.session === second.session || first.page === second.page || first.args.tab !== 1 || second.args.tab !== 2) throw new Error('session addressing');
    const denied = await pages[1].evaluate(() => TapBridge.request('projection', {}).then(() => 'allowed', e => e.code));
    if (denied !== 'handler_denied') throw new Error('origin grant');
    await pages[0].locator('#load').click();
    await pages[0].waitForFunction(() => document.querySelector('#result').textContent !== 'waiting');
    const displayed = JSON.parse(await pages[0].locator('#result').textContent());
    if (await pages[1].locator('#result').textContent() !== 'waiting') throw new Error('cross-tab delivery');
    if (leaked) throw new Error('foreign base token request');
    fs.writeFileSync(config.output, JSON.stringify({displayed, browser: browser.version(),
      two_sessions: true, handler_origin_grant: true, other_tab_unchanged: true, excluded: true, foreign_token_requests: leaked}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});

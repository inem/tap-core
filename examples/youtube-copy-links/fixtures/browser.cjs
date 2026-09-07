const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = require(config.playwright);

(async () => {
  const browser = await chromium.launch({
    executablePath: config.chrome,
    headless: true,
    proxy: {server: `http://127.0.0.1:${config.proxy_port}`},
    args: ['--disable-background-networking', '--disable-component-update'],
  });
  let page;
  const pageErrors = [];
  try {
    const context = await browser.newContext({ignoreHTTPSErrors: true});
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], {
      origin: 'https://www.youtube.com',
    });
    page = await context.newPage();
    page.on('pageerror', error => {
      const message = String(error);
      if (pageErrors.length < 20 && !pageErrors.includes(message)) pageErrors.push(message);
    });
    const response = await page.goto(config.url, {waitUntil: 'domcontentloaded', timeout: 60000});
    await page.waitForFunction(() => window.__tapYoutubeCopyLinks, null, {timeout: 30000});
    const bootstrap = await page.locator('#tap-probe-bootstrap').count();
    const cspHeader = response ? (await response.allHeaders())['content-security-policy'] : null;
    const bootstrapNonce = await page.locator('#tap-probe-bootstrap').getAttribute('nonce');
    const button = page.getByRole('button', {name: /^Copy video URL https:\/\/youtu\.be\//}).first();
    await button.waitFor({state: 'visible', timeout: 10000});
    const label = await button.getAttribute('aria-label');
    await button.click();
    await page.waitForFunction(() => {
      const target = document.querySelector('[data-state="success"]');
      return Boolean(target && /Link copied/.test(target.getAttribute('aria-label') || ''));
    }, null, {timeout: 10000});
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    if (!/^https:\/\/youtu\.be\/[A-Za-z0-9_-]+$/.test(clipboard)) {
      throw new Error(`unexpected clipboard value: ${clipboard}`);
    }
    fs.writeFileSync(config.output, JSON.stringify({
      browser: browser.version(),
      url: page.url(),
      bootstrap_count: bootstrap,
      pack_runtime: true,
      copy_button_label: label,
      clipboard,
      visible_success_flash: true,
      csp_header_present: Boolean(cspHeader),
      bootstrap_nonce_present: Boolean(bootstrapNonce),
      tls: 'accepted only with Playwright ignoreHTTPSErrors; CA trust not established',
    }, null, 2));
  } catch (error) {
    const diagnostics = {error: String(error), browser: browser.version()};
    if (page) {
      diagnostics.url = page.url();
      diagnostics.title = await page.title().catch(() => '');
      diagnostics.bootstrap_count = await page.locator('#tap-probe-bootstrap').count().catch(() => -1);
      diagnostics.youtube_ui = await page.evaluate(() => Boolean(window.YouTubeUI)).catch(() => false);
      diagnostics.pack_runtime = await page.evaluate(() => Boolean(window.__tapYoutubeCopyLinks)).catch(() => false);
      diagnostics.video_id = await page.evaluate(() => window.YouTubeUI?.getVideoId(location.href) || null).catch(() => null);
      diagnostics.watch_page_count = await page.locator('ytd-watch-flexy').count().catch(() => -1);
      diagnostics.action_row_count = await page.locator('ytd-watch-metadata #actions').count().catch(() => -1);
      diagnostics.custom_elements = await page.locator('[data-youtube-ui-id]').evaluateAll(elements =>
        elements.map(element => ({id: element.dataset.youtubeUiId,
          label: element.getAttribute('aria-label'), connected: element.isConnected,
          display: getComputedStyle(element).display, visibility: getComputedStyle(element).visibility,
          rect: element.getBoundingClientRect().toJSON()}))).catch(() => []);
      diagnostics.watch_actions = await page.locator('ytd-watch-metadata #actions').evaluate(element => ({
        html: element.outerHTML.slice(0, 2000),
        visible: Boolean(element.offsetWidth || element.offsetHeight || element.getClientRects().length),
      })).catch(() => null);
      diagnostics.remembered_watch_actions = await page.evaluate(() =>
        window.YouTubeUI?._watchVideoActions?.size ?? null).catch(() => null);
      diagnostics.manual_add = await page.evaluate(() => {
        try {
          const button = window.YouTubeUI?.addEngagementButton({
            id: 'tap-live-debug', label: 'Debug', ariaLabel: 'TAP live debug',
          });
          return {returned: Boolean(button), connected: Boolean(button?.isConnected),
            html: button?.outerHTML || null};
        } catch (error) {
          return {error: String(error), stack: error?.stack || null};
        }
      }).catch(error => ({evaluation_error: String(error)}));
      diagnostics.page_errors = pageErrors;
      diagnostics.body_text = (await page.locator('body').innerText().catch(() => '')).slice(0, 500);
    }
    fs.writeFileSync(config.output, JSON.stringify(diagnostics, null, 2));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

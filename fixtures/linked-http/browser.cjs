const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {chromium} = require(config.playwright);

(async () => {
  const browser = await chromium.launch({
    executablePath: config.chrome,
    headless: true,
    proxy: {server: `http://127.0.0.1:${config.proxy_port}`, bypass: '<-loopback>'},
    args: ['--disable-background-networking', '--disable-component-update', '--ignore-certificate-errors'],
  });
  try {
    const context = await browser.newContext({ignoreHTTPSErrors: true});
    const page = await context.newPage();
    await page.goto(config.origin, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => window.TapBridge?.isReady(), null, {timeout: 20000});
    await page.locator('#load').click();
    await page.waitForFunction(() => {
      const text = document.querySelector('#result')?.textContent || '';
      return text.startsWith('{') && text.includes('"value"');
    }, null, {timeout: 20000});
    const displayed = JSON.parse(await page.locator('#result').textContent());
    if (displayed.value !== config.expected_value) {
      throw new Error('displayed value mismatch: ' + JSON.stringify(displayed));
    }
    fs.writeFileSync(config.output, JSON.stringify({
      displayed,
      browser: browser.version(),
      origin: config.origin,
      expected_value: config.expected_value,
    }));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

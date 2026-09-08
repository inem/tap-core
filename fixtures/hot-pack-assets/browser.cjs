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
    await page.goto(config.origin, {waitUntil: 'domcontentloaded', timeout: 20000});
    await page.waitForFunction(
      (marker) => window[marker] === true,
      config.marker,
      {timeout: 20000},
    );
    const scripts = await page.$$eval('script[src*="/__tap/probe/core/"]', (nodes) =>
      nodes.map((node) => node.getAttribute('src')));
    fs.writeFileSync(config.output, JSON.stringify({
      marker: config.marker,
      marker_true: true,
      scripts,
      browser: browser.version(),
      origin: config.origin,
    }));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import { chromium } from '@playwright/test';
import { htmlAttackVectors } from './html-preview-fixtures.mjs';

// Uses the actual BASE/HEAD helpers served by Vite and browser-native parsing.
// Every vector gets an unsandboxed, CSP-free top-level control before preview.
const [origin, baseModule, out] = process.argv.slice(2);
assert.equal(new URL(origin).hostname, '127.0.0.1');
const nginx = await fs.readFile(new URL('../nginx.conf', import.meta.url), 'utf8');
const parentCsp = nginx.match(/add_header Content-Security-Policy "([^"]+)"/)[1];
const rows = [];
let requests = [], connections = 0;
const listener = http.createServer((req, res) => {
  requests.push({ url: req.url, cookie: req.headers.cookie || '' });
  res.setHeader('Content-Type', 'text/html');
  res.end(req.url.endsWith('/child') ? 'child' : `<p>Destination</p><img src="${req.url}/child">`);
});
listener.on('connection', () => connections++);
await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
const targetPort = listener.address().port;
const run = Date.now().toString(36);
try {
  for (const [browserName, launch] of [['Chrome', { channel: 'chrome' }], ['Chromium', {}]]) {
    const netlog = `${out}/${browserName}-netlog-${run}.json`;
    const browser = await chromium.launch({ headless: true, ...launch, args: [
      `--log-net-log=${netlog}`,
      '--host-resolver-rules=MAP *.d3b-preview.test 127.0.0.1', '--no-proxy-server',
    ] });
    const version = browser.version();
    const vectors = htmlAttackVectors(`http://127.0.0.1:${targetPort}`);
    const link = (target, attributes = '') => `<a id="click" href="${target}/clicked" ${attributes}>Former link</a>`;
    const clickVectors = {
      linkHtml: target => link(target), linkTop: target => link(target, 'target="_top"'),
      linkBlank: target => link(target, 'target="_blank"'), linkPing: target => link(target, `ping="${target}/ping"`),
      linkSvg: target => `<svg width="300" height="80"><a id="click" href="${target}/svg-click"><text x="10" y="30">Former link</text></a></svg>`,
      linkXlink: target => `<svg xmlns:xlink="http://www.w3.org/1999/xlink" width="300" height="80"><a id="click" xlink:href="${target}/xlink-click"><text x="10" y="30">Former link</text></a></svg>`,
      linkSvgSet: target => `<svg width="300" height="80"><a id="click" href="${target}/old"><set attributeName="href" to="${target}/set-click"/><text x="10" y="30">Former link</text></a></svg>`,
      linkSvgAnimate: target => `<svg width="300" height="80"><a id="click" href="${target}/old"><animate attributeName="href" values="${target}/animate-click" dur="1s" fill="freeze"/><text x="10" y="30">Former link</text></a></svg>`,
    };
    const cases = [...Object.keys(vectors), ...Object.keys(clickVectors)];
    try {
    for (const vector of cases) {
      const click = vector in clickVectors;
      for (const policy of click ? ['none', 'nginx'] : ['none']) {
        const row = { browser: browserName, version, vector, policy, click };
        for (const mode of ['control', 'BASE', 'HEAD']) {
          const hostname = `${browserName}-${mode}-${vector}-${policy}-${run}`.toLowerCase() + '.d3b-preview.test';
          const target = `http://${hostname}:${targetPort}`;
          // Prerender must remain same-site to reproduce Chrome 156's leak.
          const html = click ? clickVectors[vector](`http://127.0.0.1:${targetPort}`) : /dns|preconnect/i.test(vector) ? htmlAttackVectors(target)[vector] : vectors[vector];
          const context = await browser.newContext({ viewport: { width: 800, height: 600 },
            javaScriptEnabled: !(mode === 'control' && vector === 'prerenderNoscript') });
          await context.addCookies([{ name: 'd3b-session', value: 'synthetic-canary', url: origin, sameSite: 'Lax' }]);
          const page = await context.newPage();
          page.on('dialog', dialog => dialog.dismiss().catch(() => {}));
          try {
            await page.goto(`${origin}/__d3b`, { waitUntil: 'load' });
            if (mode !== 'control') await page.evaluate(async baseModule => {
              window.d3bHelpers = {
                HEAD: (await import('/src/pages/agent-detail/HtmlArtifactPreview.tsx')).htmlPreviewDocument,
                BASE: (await import(baseModule)).htmlPreviewDocument,
              };
            }, baseModule);
            // Count from before DOMParser, including any parse-time effects.
            requests = []; connections = 0;
            if (mode === 'control') {
              await page.evaluate(html => { document.open(); document.write(`<!doctype html>${html}`); document.close(); }, html).catch(() => {});
            } else {
              await page.evaluate(({ html, mode, policy, parentCsp }) => {
                if (policy === 'nginx') {
                  const meta = document.createElement('meta'); meta.httpEquiv = 'Content-Security-Policy'; meta.content = parentCsp; document.head.appendChild(meta);
                }
                const doc = window.d3bHelpers[mode](html);
                if (doc === null) throw new Error('Unexpected source fallback for ' + html);
                window.d3bMessages = 0;
                window.addEventListener('message', () => window.d3bMessages++);
                localStorage.setItem('d3b-secret', 'synthetic-storage-canary');
                const frame = document.createElement('iframe'); frame.title = 'preview'; frame.setAttribute('sandbox', '');
                frame.referrerPolicy = 'no-referrer'; frame.style.cssText = 'width:700px;height:500px'; frame.srcdoc = doc;
                document.body.replaceChildren(frame);
              }, { html, mode, policy, parentCsp });
            }
            await page.waitForTimeout(/dns|preconnect/i.test(vector) ? 1500 : 800);
            if (click) {
              const scope = mode === 'control' ? page : page.frameLocator('iframe');
              await scope.locator('#click').hover();
              await scope.locator('#click').click();
              await page.waitForTimeout(700);
            } else if (vector === 'cssCursor') {
              await page.mouse.move(30, 30); await page.waitForTimeout(200);
            } else if (vector === 'imgLazy') {
              const scope = mode === 'control' ? page : page.frameLocator('iframe');
              await scope.locator('img').scrollIntoViewIfNeeded(); await page.waitForTimeout(300);
            }
            const state = mode === 'control' ? {} : await page.evaluate(() => ({
              messages: window.d3bMessages, storage: localStorage.getItem('d3b-secret'), cookie: document.cookie,
              frameDocument: document.querySelector('iframe').contentDocument,
            }));
            row[mode] = { hostname, requests: [...requests], connections, pages: context.pages().length, ...state };
          } finally { await context.close(); listener.closeAllConnections(); }
        }
        rows.push(row);
        console.log(`${browserName} ${vector} ${policy}: control=${row.control.requests.length}/${row.control.connections} BASE=${row.BASE.requests.length}/${row.BASE.connections} HEAD=${row.HEAD.requests.length}/${row.HEAD.connections}`);
      }
    }
    } finally { await browser.close(); }
    const log = JSON.parse(await fs.readFile(netlog, 'utf8'));
    const resolverTypes = new Set(Object.entries(log.constants.logEventTypes).filter(([name]) => /HOST_RESOLVER|DNS_TRANSACTION/.test(name)).map(([, id]) => id));
    for (const row of rows.filter(row => row.browser === browserName)) {
      for (const mode of ['control', 'BASE', 'HEAD']) {
        row[mode].lookups = log.events.filter(event => resolverTypes.has(event.type) && JSON.stringify(event.params || {}).includes(row[mode].hostname)).length;
      }
      row.observable = Boolean(row.control.requests.length || row.control.connections || row.control.lookups);
    }
  }
  await fs.writeFile(`${out}/listener-results.json`, JSON.stringify(rows, null, 2));
  const leaks = rows.filter(row => row.HEAD.requests.length || row.HEAD.connections || row.HEAD.lookups);
  const unavailable = rows.filter(row => !row.observable).map(row => `${row.browser}/${row.vector}/${row.policy}`);
  for (const row of rows) {
    assert.equal(row.HEAD.messages, 0); assert.equal(row.HEAD.storage, 'synthetic-storage-canary');
    assert.ok(row.HEAD.cookie.includes('d3b-session=synthetic-canary')); assert.equal(row.HEAD.frameDocument, null);
    assert.equal(row.HEAD.pages, 1);
  }
  console.log(JSON.stringify({ rows: rows.length, observable: rows.length - unavailable.length, unavailable, leaks: leaks.map(row => `${row.browser}/${row.vector}/${row.policy}`) }));
  assert.deepEqual(leaks, [], 'HEAD preview reached listener/resolver');
} finally {
  await fs.writeFile(`${out}/listener-partial.json`, JSON.stringify(rows, null, 2));
  listener.closeAllConnections(); await new Promise(resolve => listener.close(resolve));
}

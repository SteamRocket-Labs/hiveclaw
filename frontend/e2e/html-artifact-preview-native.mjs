import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import { chromium, expect } from '@playwright/test';

// Real member entry against an owned API/PG/Redis/Vite stack; no network mocks.
const fixture = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const out = process.argv[3];
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
assert.equal(new URL(fixture.target).hostname, '127.0.0.1');
const requests = [], connections = [], results = [];
const listener = http.createServer((req, res) => { requests.push(req.url); res.end('Unexpected preview request'); });
listener.on('connection', socket => connections.push(socket.remoteAddress));
await new Promise(resolve => listener.listen(Number(new URL(fixture.target).port), '127.0.0.1', resolve));
try {
  for (const [browserName, launch] of [['Chrome', { channel: 'chrome' }], ['Chromium', {}]]) {
    const browser = await chromium.launch({ headless: true, ...launch });
    try {
      const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 1000 } });
      await context.addInitScript(({ token, tenantId }) => {
        localStorage.setItem('token', token);
        localStorage.setItem('current_tenant_id', tenantId);
        localStorage.setItem('i18nextLng', 'zh');
        localStorage.setItem('d3b-secret', 'parent-storage-canary');
        document.cookie = 'd3b-session=parent-cookie-canary; SameSite=Lax';
        window.d3bMessages = 0;
        window.addEventListener('message', () => window.d3bMessages++);
      }, fixture);
      const page = await context.newPage();
      const appURL = `${fixture.origin}/agents/${fixture.agentId}/sessions/${fixture.sessionId}`;
      await page.goto(appURL);
      const panel = page.getByTestId('session-artifact-preview-inspector');
      const open = async name => {
        const start = Date.now();
        const row = page.getByRole('button', { name: `打开 ${name}`, exact: true });
        await expect(row).toBeVisible({ timeout: 30000 });
        if (name.endsWith('.html') || name.endsWith('.htm')) await expect(row.locator('.chat-artifact-meta')).toContainText('预览');
        await row.click(); await expect(panel).toBeVisible(); return start;
      };
      const start = await open('季度简报.html');
      const iframe = panel.locator('iframe');
      const frame = panel.frameLocator('iframe');
      await expect(frame.getByRole('heading', { name: '季度项目简报' })).toBeVisible();
      const openToPreviewMs = Date.now() - start;
      await expect(frame.getByRole('table')).toBeVisible();
      assert.equal(await frame.locator('h1').evaluate(el => getComputedStyle(el).color), 'rgb(33, 84, 166)');
      await expect(frame.locator('#script-status')).toHaveText('页面脚本未运行');
      assert.equal(await iframe.getAttribute('sandbox'), '');
      assert.equal(await iframe.getAttribute('src'), null);
      const addresses = await frame.locator('a').evaluateAll(links => links.map(link => ({ title: link.title, href: link.getAttribute('href'), target: link.getAttribute('target') })));
      assert.equal(addresses.length, 3);
      for (const link of addresses) { assert.equal(link.href, null); assert.equal(link.target, null); assert.ok(link.title.startsWith(fixture.target)); }
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        const link = frame.locator('a').first();
        await link.hover(); await page.waitForTimeout(1200);
        await page.screenshot({ path: `${out}/${browserName}-links-${width}.png` });
      }
      for (const link of await frame.locator('a').all()) await link.click();
      await page.waitForTimeout(800);
      assert.equal(requests.length, 0); assert.equal(connections.length, 0);
      assert.equal(context.pages().length, 1); assert.equal(page.url(), appURL);
      await expect(frame.getByRole('heading', { name: '季度项目简报' })).toBeVisible();
      await panel.getByRole('button', { name: '查看源码', exact: true }).click();
      const source = await panel.locator('pre').textContent();
      assert.ok(source.includes('<script>')); assert.ok(source.includes('href='));
      await expect(iframe).toHaveCount(0);
      await panel.getByRole('button', { name: '预览', exact: true }).click();
      await expect(frame.getByRole('heading', { name: '季度项目简报' })).toBeVisible();
      const downloading = page.waitForEvent('download');
      await panel.getByRole('button', { name: '下载 季度简报.html', exact: true }).click();
      const download = await downloading;
      assert.equal(download.suggestedFilename(), '季度简报.html');
      assert.equal(await fs.readFile(await download.path(), 'utf8'), source);
      await panel.getByRole('button', { name: '关闭', exact: true }).click();
      await open('较大页面.html');
      await expect(panel.getByText('文件较大，已显示源码。你也可以下载查看。')).toBeVisible();
      await expect(iframe).toHaveCount(0);
      await panel.getByRole('button', { name: '关闭', exact: true }).click();
      await open('自动访问探测.htm');
      await expect(frame.getByRole('heading', { name: '自动访问拦截测试' })).toBeVisible();
      await page.waitForTimeout(1000);
      assert.equal(requests.length, 0); assert.equal(connections.length, 0);
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        await page.screenshot({ path: `${out}/${browserName}-hostile-${width}.png` });
      }
      await panel.getByRole('button', { name: '关闭', exact: true }).click();
      await open('趋势图.svg');
      await expect(panel.getByRole('img', { name: '趋势图.svg' })).toBeVisible();
      await expect(panel.getByTitle('新窗口打开')).toHaveCount(0);
      await expect(panel.locator('a[target="_blank"]')).toHaveCount(0);
      await expect(panel.getByRole('button', { name: '下载 趋势图.svg', exact: true })).toBeVisible();
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        await page.screenshot({ path: `${out}/${browserName}-svg-${width}.png` });
      }
      const parent = await page.evaluate(() => ({ cookie: document.cookie, storage: localStorage.getItem('d3b-secret'), messages: window.d3bMessages, svgExecuted: localStorage.getItem('d3b-svg-executed') }));
      assert.ok(parent.cookie.includes('d3b-session=parent-cookie-canary'));
      assert.equal(parent.storage, 'parent-storage-canary'); assert.equal(parent.messages, 0); assert.equal(parent.svgExecuted, null);
      assert.equal(requests.length, 0); assert.equal(connections.length, 0);
      results.push({ browser: browserName, version: browser.version(), openToPreviewMs, addresses, requests: 0, connections: 0, parent, sourceDownloadMatch: true, largeFallback: true, svgNewTab: false });
      console.log(`PASS ${browserName} ${browser.version()}: real member static render/source/download/large fallback; inert hover/title/click=0/0; SVG no new tab; openToPreviewMs=${openToPreviewMs}`);
    } finally { await browser.close(); }
  }
} finally {
  await fs.writeFile(`${out}/member-browser-result.json`, JSON.stringify({ results, requests, connections }, null, 2));
  listener.closeAllConnections(); await new Promise(resolve => listener.close(resolve));
}

import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import { chromium, expect } from '@playwright/test';
import { htmlAttackVectors } from './html-preview-fixtures.mjs';

// Run against an owned API/PG/Redis/Vite stack. No API or browser network mocks.
const fixture = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const mode = process.argv[3];
const out = process.argv[4];
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
assert.equal(new URL(fixture.target).hostname, '127.0.0.1');
const requests = [], connections = [], results = [];
const listener = http.createServer((req, res) => { requests.push(req.url); res.setHeader('Content-Type', 'text/html'); res.end('<p>User-clicked destination</p>'); });
listener.on('connection', socket => connections.push(socket.remoteAddress));
await new Promise(resolve => listener.listen(Number(new URL(fixture.target).port), '127.0.0.1', resolve));
try {
  for (const [browserName, launch] of mode === 'red' ? [['Chrome', { channel: 'chrome' }]] : [['Chrome', { channel: 'chrome' }], ['Chromium', {}]]) {
    const browser = await chromium.launch({ headless: true, ...launch });
    let page;
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
      page = await context.newPage();
      const appURL = `${fixture.origin}/agents/${fixture.agentId}/sessions/${fixture.sessionId}`;
      await page.goto(appURL);
      const panel = page.getByTestId('session-artifact-preview-inspector');
      const open = async name => {
        const start = Date.now();
        await page.getByRole('button', { name: `打开 ${name}`, exact: true }).click();
        await expect(panel).toBeVisible();
        return start;
      };
      const start = await open('季度简报.html');
      if (mode === 'red') {
        await expect(panel.locator('pre')).toContainText('<!doctype html>');
        await page.screenshot({ path: `${out}/red-source-1440.png` });
        console.log(`RED HTML_SOURCE_VISIBLE iframe=${await panel.locator('iframe').count()} openToSourceMs=${Date.now() - start}`);
        await expect(panel.locator('iframe')).toHaveCount(1, { timeout: 1000 });
      }
      const iframe = panel.locator('iframe');
      const frame = panel.frameLocator('iframe');
      await expect(frame.getByRole('heading', { name: '季度项目简报' })).toBeVisible();
      const openToPreviewMs = Date.now() - start;
      await expect(frame.getByRole('table')).toBeVisible();
      assert.equal(await frame.locator('h1').evaluate(el => getComputedStyle(el).color), 'rgb(33, 84, 166)');
      await expect(frame.locator('#script-status')).toHaveText('页面脚本未运行');
      assert.equal(await iframe.getAttribute('sandbox'), '');
      assert.equal(await iframe.getAttribute('src'), null);
      const benignDocument = await iframe.getAttribute('srcdoc');
      assert.ok(benignDocument.includes('x-dns-prefetch-control'));
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        await page.screenshot({ path: `${out}/${browserName}-benign-${width}.png` });
      }
      await panel.getByRole('button', { name: '查看源码', exact: true }).click();
      await expect(panel.locator('pre')).toContainText('<!doctype html>');
      await expect(iframe).toHaveCount(0);
      await panel.getByRole('button', { name: '预览', exact: true }).click();
      await expect(frame.getByRole('heading', { name: '季度项目简报' })).toBeVisible();
      const downloaded = page.waitForEvent('download');
      await panel.getByRole('button', { name: '下载 季度简报.html', exact: true }).click();
      const download = await downloaded;
      assert.equal(download.suggestedFilename(), '季度简报.html');
      const downloadedBytes = await fs.readFile(await download.path(), 'utf8');
      assert.equal(benignDocument.slice(benignDocument.indexOf('<!doctype html>', 1)), downloadedBytes);
      await panel.getByRole('button', { name: '关闭', exact: true }).click();
      await open('较大页面.html');
      await expect(panel.getByText('文件较大，已显示源码。你也可以下载查看。')).toBeVisible();
      await expect(iframe).toHaveCount(0);
      await panel.getByRole('button', { name: '关闭', exact: true }).click();
      await open('自动访问探测.htm');
      await expect(frame.getByRole('heading', { name: '自动访问拦截测试' })).toBeVisible();
      const hostileDocument = await iframe.getAttribute('srcdoc');
      const prefix = hostileDocument.slice(0, hostileDocument.indexOf('<!doctype html>', 1));
      // Exercise each vector on the production frame and its production policy.
      for (const [vector, html] of Object.entries(htmlAttackVectors(fixture.target))) {
        await iframe.evaluate((el, document) => { el.srcdoc = document; }, prefix + html);
        await page.waitForTimeout(150);
        assert.equal(await frame.locator('html').getAttribute('data-executed'), null);
        assert.equal(requests.length, 0, vector);
        assert.equal(connections.length, 0, vector);
        assert.equal(page.url(), appURL);
        results.push({ browser: browserName, vector, requests: 0, connections: 0 });
        console.log(`PASS ${browserName} ${vector}: requests=0 connections=0`);
      }
      await iframe.evaluate((el, document) => { el.srcdoc = document; }, hostileDocument);
      await expect(frame.getByRole('heading', { name: '自动访问拦截测试' })).toBeVisible();
      await page.waitForTimeout(600);
      assert.equal(requests.length, 0);
      assert.equal(connections.length, 0);
      const parent = await page.evaluate(() => ({ cookie: document.cookie, storage: localStorage.getItem('d3b-secret'), messages: window.d3bMessages }));
      assert.ok(parent.cookie.includes('d3b-session=parent-cookie-canary'));
      assert.equal(parent.storage, 'parent-storage-canary');
      assert.equal(parent.messages, 0);
      await expect(page.getByRole('button', { name: '打开 季度简报.html', exact: true })).toBeVisible();
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        await page.evaluate(({ requests, connections }) => {
          let log = document.querySelector('#d3b-listener-log');
          if (!log) { log = document.createElement('pre'); log.id = 'd3b-listener-log'; document.body.appendChild(log); }
          log.textContent = `本机监听器：${requests} 请求 / ${connections} TCP 连接`;
          log.style.cssText = 'position:fixed;top:4px;left:4px;z-index:10000;background:#e6ffef;color:#14532d;padding:6px;font:12px monospace;pointer-events:none';
        }, { requests: requests.length, connections: connections.length });
        await expect(page.locator('#d3b-listener-log')).toBeVisible();
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await page.screenshot({ path: `${out}/${browserName}-hostile-${width}.png` });
      }
      // User-initiated links are outside the automatic-network requirement.
      await iframe.evaluate((el, document) => { el.srcdoc = document; }, benignDocument);
      await frame.getByRole('link', { name: '顶层链接', exact: true }).click();
      await frame.getByRole('link', { name: '新窗口链接', exact: true }).click();
      assert.equal(requests.length, 0);
      assert.equal(context.pages().length, 1);
      await frame.getByRole('link', { name: '打开链接', exact: true }).click();
      await expect(frame.getByText('User-clicked destination')).toBeVisible();
      assert.equal(page.url(), appURL);
      assert.deepEqual(requests, ['/clicked']);
      console.log(`PASS ${browserName}: static render/source/download/large fallback; automatic=0/0; user link=self frame only; openToPreviewMs=${openToPreviewMs}`);
      results.push({ browser: browserName, version: browser.version(), openToPreviewMs, automaticRequests: 0, automaticConnections: 0, userClickedRequests: [...requests], topUnchanged: true, parentMessages: parent.messages });
      requests.length = 0; connections.length = 0;
    } catch (error) {
      if (page) {
        await page.screenshot({ path: `${out}/${mode}-${browserName}-failure.png` }).catch(() => {});
        console.log(`PAGE ${page.url()}\n${await page.locator('body').innerText()}`);
      }
      throw error;
    } finally { await browser.close(); }
  }
} finally {
  await fs.writeFile(`${out}/${mode}-browser-result.json`, JSON.stringify({ results, requests, connections }, null, 2));
  listener.closeAllConnections();
  await new Promise(resolve => listener.close(resolve));
}

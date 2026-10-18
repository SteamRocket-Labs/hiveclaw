/** Consume a real group permission over HTTP; native Python owns same-Run recovery. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { chromium, expect } from '@playwright/test';

const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
const controls = createInterface({ input: process.stdin });
const control = controls[Symbol.asyncIterator]();
const errors = [];
let page;
async function openWork(token) {
  const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1440, height: 1000 } });
  await context.route('**/*', route => new URL(route.request().url()).origin === fixture.origin ? route.continue() : route.abort());
  await context.addInitScript(({ token, tenantId }) => {
    localStorage.setItem('token', token);
    localStorage.setItem('current_tenant_id', tenantId);
    localStorage.setItem('i18nextLng', 'en');
  }, { token, tenantId: fixture.tenantId });
  page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}/sessions/${fixture.sessionId}`);
  return context;
}
try {
  const reader = await openWork(fixture.readerToken);
  await expect(page.getByText('Waiting for the original requester to decide.', { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('button', { name: 'Allow once', exact: true })).toHaveCount(0);
  await page.screenshot({ path: fixture.readonlyScreenshot, fullPage: true });
  await reader.close();
  process.stdout.write(JSON.stringify({ event: 'readonly_checked' }) + String.fromCharCode(10));
  assert.equal(JSON.parse((await control.next()).value).event, 'requester_ready');
  const requester = await openWork(fixture.requesterToken);
  const allow = page.getByRole('button', { name: 'Allow once', exact: true });
  await expect(allow).toBeVisible({ timeout: 30000 });
  const url = `${fixture.origin}/api/agents/${fixture.agentId}/sessions/${fixture.sessionId}/permissions/${fixture.permissionId}/resolve`;
  await page.route(url, route => route.abort('failed'), { times: 1 });
  await allow.click();
  await expect(page.getByRole('alert').last()).toBeVisible();
  await expect(allow).toBeEnabled();
  await page.screenshot({ path: fixture.unconfirmedScreenshot, fullPage: true });
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route(url, async route => { await pending; await route.continue(); }, { times: 1 });
  const submitted = page.waitForResponse(response => response.request().method() === 'POST' && response.url() === url);
  await allow.click();
  await expect(allow).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Deny', exact: true })).toBeDisabled();
  release();
  const response = await submitted;
  assert.equal(response.status(), 200);
  const receipt = await response.json();
  assert.equal(receipt.status, 'resolved');
  assert.equal(receipt.run_status, 'resumable');
  await writeFile(fixture.receiptPath, JSON.stringify(receipt, null, 2));
  process.stdout.write(JSON.stringify({ event: 'permission_recorded' }) + String.fromCharCode(10));
  await expect(allow).toHaveCount(0, { timeout: 20000 });
  await page.reload();
  await expect(page.getByText('shared-report.md', { exact: false }).first()).toBeVisible({ timeout: 20000 });
  await expect(page.getByRole('button', { name: 'Allow once', exact: true })).toHaveCount(0);
  await expect(page.locator('[data-thread-item-type="approval_decision"]').getByText('Approval decision', { exact: true })).toBeVisible();
  await expect(page.getByText('permission_resolved', { exact: true })).toHaveCount(0);
  await expect(page.getByText('allow_once', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Some source content is unavailable to you.', { exact: false })).toHaveCount(0);
  await page.screenshot({ path: fixture.completedScreenshot, fullPage: true });
  assert.deepEqual(errors, []);
  await requester.close();
} catch (error) {
  await writeFile(fixture.errorPath, JSON.stringify({ error: String(error), errors, body: page ? await page.locator('body').innerText() : null }, null, 2));
  if (page && !page.isClosed()) await page.screenshot({ path: fixture.errorScreenshot, fullPage: true });
  throw error;
} finally {
  controls.close();
  await browser.close();
}

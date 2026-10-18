/** Exercise group Stop authority, failed transport, admission, and native terminal consumption. */
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
  const stop = page.getByTestId('session-composer-stop');
  await expect(stop).toBeVisible({ timeout: 30000 });
  return { context, stop };
}
try {
  const reader = await openWork(fixture.readerToken);
  await expect(reader.stop).toBeDisabled();
  await page.screenshot({ path: fixture.readonlyScreenshot, fullPage: true });
  await reader.context.close();
  process.stdout.write(JSON.stringify({ event: 'readonly_checked' }) + String.fromCharCode(10));
  assert.equal(JSON.parse((await control.next()).value).event, 'requester_ready');
  const requester = await openWork(fixture.requesterToken);
  await expect(requester.stop).toBeEnabled();
  const cancelUrl = `${fixture.origin}/api/agents/${fixture.agentId}/sessions/${fixture.sessionId}/runs/${fixture.runId}/cancel`;
  await page.route(cancelUrl, route => route.abort('failed'), { times: 1 });
  await requester.stop.click();
  await expect(page.getByText('The stop request could not be confirmed. Check this work or try Stop again.', { exact: true })).toBeVisible();
  await expect(requester.stop).toBeVisible();
  await expect(requester.stop).toBeEnabled();
  await page.screenshot({ path: fixture.unconfirmedScreenshot, fullPage: true });
  const submitted = page.waitForResponse(response => response.request().method() === 'POST' && response.url() === cancelUrl);
  await requester.stop.click();
  const response = await submitted;
  assert.equal(response.status(), 200);
  const receipt = await response.json();
  assert.equal(receipt.status, 'applying');
  assert.equal(receipt.accepted, true);
  await expect(page.getByText('Stop request recorded. Waiting for the run to settle.', { exact: true })).toBeVisible();
  await expect(requester.stop).toBeVisible();
  await writeFile(fixture.receiptPath, JSON.stringify(receipt, null, 2));
  process.stdout.write(JSON.stringify({ event: 'cancel_recorded' }) + String.fromCharCode(10));
  await expect(requester.stop).toHaveCount(0, { timeout: 30000 });
  await expect(page.getByText('Stop request recorded. Waiting for the run to settle.', { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(/^(cancelled|stopped)$/i).first()).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('session-composer-stop')).toHaveCount(0);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: fixture.completedScreenshot, fullPage: true });
  process.stdout.write(JSON.stringify({ event: 'work_stopped' }) + String.fromCharCode(10));
} catch (error) {
  await writeFile(fixture.errorPath, JSON.stringify({ error: String(error), errors, body: page ? await page.locator('body').innerText() : null }, null, 2));
  if (page && !page.isClosed()) await page.screenshot({ path: fixture.errorScreenshot, fullPage: true });
  throw error;
} finally {
  controls.close();
  await browser.close();
}

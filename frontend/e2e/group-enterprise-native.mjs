/** Read and decide an actual company ticket; native Python owns its claimed execution and return. */
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
  await expect(page.getByText('Company approval', { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(page.getByText('Write a workspace file', { exact: true })).toBeVisible();
  await expect(page.getByTestId('chat-inline-plan-tool-call').getByText('workspace/shared-report.md', { exact: true })).toBeVisible();
  await expect(page.getByText('Waiting for the responsible approver.', { exact: true })).toBeVisible();
  await expect(page.getByText('Waiting for approval', { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId('run-disclosure-block').filter({ hasText: 'Thinking' })).toHaveCount(0);
  await expect(page.locator('[data-testid="run-disclosure-block"][data-status="running"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="active-tail-status"]:not([data-phase="awaiting_approval"])')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Allow once', exact: true })).toHaveCount(0);
  await page.screenshot({ path: fixture.readonlyScreenshot, fullPage: true });
  await reader.close();
  process.stdout.write(JSON.stringify({ event: 'readonly_checked' }) + String.fromCharCode(10));
  assert.equal(JSON.parse((await control.next()).value).event, 'requester_ready');
  const approver = await openWork(fixture.requesterToken);
  const approve = page.getByRole('button', { name: 'Approve', exact: true });
  await expect(approve).toBeVisible({ timeout: 30000 });
  const url = `${fixture.origin}/api/agents/${fixture.agentId}/sessions/${fixture.sessionId}/approvals/${fixture.permissionId}/resolve`;
  await page.route(url, route => route.abort('failed'), { times: 1 });
  await approve.click();
  await expect(page.getByRole('alert').last()).toBeVisible();
  await expect(approve).toBeEnabled();
  await page.screenshot({ path: fixture.unconfirmedScreenshot, fullPage: true });
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route(url, async route => { await pending; await route.continue(); }, { times: 1 });
  const submitted = page.waitForResponse(response => response.request().method() === 'POST' && response.url() === url);
  await approve.click();
  await expect(approve).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Reject', exact: true })).toBeDisabled();
  release();
  const response = await submitted;
  assert.equal(response.status(), 200);
  const receipt = await response.json();
  assert.equal(receipt.status, 'approved');
  assert.equal(receipt.execution_status, 'queued');
  await writeFile(fixture.receiptPath, JSON.stringify(receipt, null, 2));
  await expect(approve).toHaveCount(0, { timeout: 20000 });
  await page.reload();
  await expect(page.getByText('Company approval', { exact: true })).toBeVisible({ timeout: 20000 });
  await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Allow once', exact: true })).toHaveCount(0);
  await expect(page.getByTestId('chat-inline-plan-tool-call').getByText('Queued', { exact: true })).toBeVisible();
  await page.screenshot({ path: fixture.completedScreenshot, fullPage: true });
  process.stdout.write(JSON.stringify({ event: 'permission_recorded' }) + String.fromCharCode(10));
  assert.equal(JSON.parse((await control.next()).value).event, 'work_returned');
  let finalContext = approver;
  if (fixture.editSaved) {
    await approver.close();
    finalContext = await openWork(fixture.readerToken);
  } else {
    await page.reload();
  }
  await expect(page.getByText('The approved report is ready.', { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(page.getByText('Runtime result integration page.', { exact: false })).toHaveCount(0);
  const previewResponse = page.waitForResponse(response => response.url().includes('/files/artifacts/') && response.url().endsWith('/content'));
  await page.getByTestId('chat-artifact-row-open').filter({ hasText: 'shared-report.md' }).last().click();
  assert.equal((await previewResponse).status(), 200);
  await expect(page.getByTestId('session-artifact-preview-inspector')).toContainText('GROUP_REPORT');
  const downloaded = page.waitForEvent('download');
  await page.getByTestId('session-artifact-preview-inspector').getByRole('button', { name: 'Download shared-report.md', exact: true }).click();
  assert.equal(await readFile(await (await downloaded).path(), 'utf8'), 'GROUP_REPORT');
  if (fixture.editSaved) {
    const inspector = page.getByTestId('session-artifact-preview-inspector');
    const current = page.waitForResponse(response => response.url().includes('/files/content') && response.request().method() === 'GET');
    await inspector.getByRole('button', { name: 'Open current file', exact: true }).click();
    assert.equal((await current).status(), 200);
    await inspector.getByRole('button', { name: 'Edit', exact: true }).click();
    await inspector.getByRole('textbox', { name: 'Your draft', exact: true }).fill('USER_EDITED_SAVED_REPORT');
    const saved = page.waitForResponse(response => response.url().includes('/files/content') && response.request().method() === 'PUT');
    await inspector.getByRole('button', { name: 'Save', exact: true }).click();
    assert.equal((await saved).status(), 200);
    await expect(inspector).toContainText('USER_EDITED_SAVED_REPORT');
    await inspector.getByRole('button', { name: 'Back to delivery', exact: true }).click();
    await expect(inspector.getByText('GROUP_REPORT', { exact: true })).toBeVisible();
    await page.reload();
    await page.getByTestId('chat-artifact-row-open').filter({ hasText: 'shared-report.md' }).last().click();
    await inspector.getByRole('button', { name: 'Open current file', exact: true }).click();
    await expect(inspector).toContainText('USER_EDITED_SAVED_REPORT');
  }
  await page.screenshot({ path: fixture.completedScreenshot, fullPage: true });
  assert.deepEqual(errors, []);
  await finalContext.close();
} catch (error) {
  await writeFile(fixture.errorPath, JSON.stringify({ error: String(error), errors, body: page ? await page.locator('body').innerText() : null }, null, 2));
  if (page && !page.isClosed()) await page.screenshot({ path: fixture.errorScreenshot, fullPage: true });
  throw error;
} finally {
  controls.close();
  await browser.close();
}

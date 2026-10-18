/** The mounted team UI decides a real ticket; Python runs its claimed native worker. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { chromium, expect } from '@playwright/test';

const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
const controls = createInterface({ input: process.stdin });
const control = controls[Symbol.asyncIterator]();
let context, page;
const errors = [];
async function openAs(token) {
  context = await browser.newContext({ locale: 'en-US', viewport: { width: 1280, height: 900 } });
  await context.route('**/*', route => new URL(route.request().url()).origin === fixture.origin ? route.continue() : route.abort());
  await context.addInitScript(({ token, tenantId }) => {
    localStorage.setItem('token', token);
    localStorage.setItem('current_tenant_id', tenantId);
    localStorage.setItem('i18nextLng', 'en');
  }, { token, tenantId: fixture.tenantId });
  page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
}
const listPath = '/api/enterprise/approvals';
const resolveUrl = `${fixture.origin}${listPath}/${fixture.approvalId}/resolve`;
async function openQueue() {
  await page.goto(`${fixture.origin}/team`);
  await page.getByRole('button', { name: /Delivery/ }).click();
  const response = page.waitForResponse(response => response.url().includes(listPath) && response.url().includes(`business_team_id=${fixture.teamId}`));
  await page.getByRole('button', { name: 'Approvals', exact: true }).click();
  const result = await response;
  assert.equal(result.status(), 200);
  const records = await result.json();
  assert.deepEqual(records.map(row => row.id), [fixture.approvalId]);
  assert.ok(!JSON.stringify(records).includes('PRIVATE_APPROVAL_CANARY'));
  return records[0];
}
try {
  await openAs(fixture.ownerToken);
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}#approvals`);
  const grantUrl = `${fixture.origin}${listPath}/${fixture.approvalId}/reviewers`;
  async function grantReviewer() {
    await page.getByRole('combobox', { name: 'Team reviewer', exact: true }).selectOption(fixture.leadId);
    const response = page.waitForResponse(response => response.url() === grantUrl && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Authorize for this approval', exact: true }).click();
    assert.equal((await response).status(), 201);
    await expect(page.getByRole('button', { name: 'Remove authorization for Team Lead', exact: true })).toBeVisible();
  }
  await grantReviewer();
  const revoked = page.waitForResponse(response => response.url().startsWith(grantUrl + '/') && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: 'Remove authorization for Team Lead', exact: true }).click();
  assert.equal((await revoked).status(), 200);
  await grantReviewer();
  await context.close();
  process.stdout.write(JSON.stringify({ event: 'reviewer_granted' }) + '\n');
  assert.equal(JSON.parse((await control.next()).value).event, 'reviewer_ready');
  await openAs(fixture.token);
  assert.equal((await openQueue()).status, 'pending');
  await expect(page.getByText('workspace/notes.md', { exact: true })).toBeVisible();
  await page.route(resolveUrl, route => route.abort('failed'), { times: 1 });
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await expect(page.getByRole('alert').last()).toBeVisible();
  await page.getByRole('alert').last().getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  const submitted = page.waitForResponse(response => response.url() === resolveUrl && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  const result = await submitted;
  assert.equal(result.status(), 200);
  const decision = await result.json();
  assert.equal(decision.execution_status, 'queued');
  assert.ok(!JSON.stringify(decision).includes('PRIVATE_APPROVAL_CANARY'));
  await writeFile(fixture.receiptPath, JSON.stringify({ decision, errors }, null, 2));
  process.stdout.write(JSON.stringify({ event: 'decision_recorded' }) + '\n');
  assert.equal(JSON.parse((await control.next()).value).event, 'work_completed');
  assert.equal((await openQueue()).execution_status, 'succeeded');
  await page.screenshot({ path: fixture.screenshotPath, fullPage: true });
  assert.deepEqual(errors, []);
} catch (error) {
  await page.screenshot({ path: fixture.errorScreenshot, fullPage: true }).catch(() => {});
  throw error;
} finally {
  controls.close();
  await browser.close();
}

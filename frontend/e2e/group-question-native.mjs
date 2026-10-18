/** Exercise a real group question in the built app and its native successor. */
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
const frames = [];
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
  page.on('websocket', socket => socket.on('framereceived', frame => {
    try { frames.push(JSON.parse(String(frame.payload))); } catch { /* Only JSON frames carry protocol facts. */ }
  }));
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}/sessions/${fixture.sessionId}`);
  const card = page.getByTestId('ask-user-question-card').filter({ hasText: 'Which audience?' });
  await expect(card).toBeVisible({ timeout: 30000 });
  return { context, card };
}
try {
  const reader = await openWork(fixture.readerToken);
  await expect(reader.card.getByText('Waiting for requester', { exact: true })).toBeVisible();
  await expect(reader.card.getByText('Only the original requester can answer this question.', { exact: true })).toBeVisible();
  await expect(reader.card.getByText('Needs your input', { exact: true })).toHaveCount(0);
  await expect(reader.card.getByRole('radio').first()).toBeDisabled();
  await expect(reader.card.getByRole('button', { name: 'Submit answer', exact: true })).toHaveCount(0);
  await page.screenshot({ path: fixture.readonlyScreenshot, fullPage: true });
  await reader.context.close();
  process.stdout.write(JSON.stringify({ event: 'readonly_checked' }) + '\n');
  assert.equal(JSON.parse((await control.next()).value).event, 'requester_ready');

  const requester = await openWork(fixture.requesterToken);
  await requester.card.getByRole('radio', { name: 'Team', exact: true }).check();
  const submitted = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith(`/sessions/${fixture.sessionId}/inputs`));
  await requester.card.getByRole('button', { name: 'Submit answer', exact: true }).click();
  const response = await submitted;
  assert.equal(response.status(), 200);
  const body = response.request().postDataJSON();
  const receipt = await response.json();
  assert.equal(body.kind, 'answer_request');
  assert.equal(body.request_item_id, fixture.questionId);
  assert.equal(receipt.admission_state, 'admitted');
  assert(!['rejected', 'cancelled', 'needs_reconciliation'].includes(receipt.status));
  await writeFile(fixture.answerPath, JSON.stringify({ body, receipt }, null, 2));
  process.stdout.write(JSON.stringify({ event: 'answer_recorded', inputId: body.input_id, commandId: receipt.command_id }) + '\n');
  await expect(page.getByText('The Team report is ready.', { exact: false }).first()).toBeVisible({ timeout: 45000 });
  await page.reload();
  await expect(page.getByText('The Team report is ready.', { exact: false }).first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId('ask-user-question-card').getByRole('radio').first()).toBeDisabled();
  await expect(page.getByTestId('ask-user-question-card').getByText('Answered', { exact: true })).toBeVisible();
  await expect(page.getByTestId('ask-user-question-card').getByText('This question has been answered.', { exact: true })).toBeVisible();
  await expect(page.getByTestId('ask-user-question-card').getByText('Needs your input', { exact: true })).toHaveCount(0);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: fixture.completedScreenshot, fullPage: true });
  await writeFile(fixture.framesPath, JSON.stringify(frames, null, 2));
  process.stdout.write(JSON.stringify({ event: 'work_completed' }) + '\n');
} catch (error) {
  await writeFile(fixture.errorPath, JSON.stringify({ error: String(error), errors, body: page ? await page.locator('body').innerText() : null, frames }, null, 2));
  if (page && !page.isClosed()) await page.screenshot({ path: fixture.errorScreenshot, fullPage: true });
  throw error;
} finally {
  controls.close();
  await browser.close();
}

/** Actual owner disclosure -> team handover -> independently authorized private work. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { chromium, expect } from '@playwright/test';

const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
const controls = createInterface({ input: process.stdin });
const messages = controls[Symbol.asyncIterator]();
const errors = [], commands = [];
let context, page;
const shareUrl = `${fixture.origin}/api/agents/${fixture.agentId}/tasks/${fixture.taskId}/team-sharing`;
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
  page.on('request', request => { if (request.url() === shareUrl && request.method() === 'POST') commands.push(request.postDataJSON()); });
}
async function handoff(event, expected, extra = {}) {
  process.stdout.write(JSON.stringify({ event, ...extra }) + '\n');
  assert.equal(JSON.parse((await messages.next()).value).event, expected);
}
async function openTasks() {
  await page.goto(`${fixture.origin}/team`);
  await page.getByRole('button', { name: /Delivery/ }).click();
  const response = page.waitForResponse(response => response.url().includes(`/business-teams/${fixture.teamId}/tasks`) && response.request().method() === 'GET');
  await page.getByRole('button', { name: 'Shared team tasks', exact: true }).click();
  const result = await response;
  assert.equal(result.status(), 200);
  const body = await result.json();
  assert.ok(!JSON.stringify(body).includes('PRIVATE_TASK_CANARY'));
  return body;
}
try {
  await openAs(fixture.ownerToken);
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}#tasks`);
  await page.getByRole('button', { name: 'Share task with a team', exact: true }).click();
  await page.getByRole('combobox', { name: 'Team', exact: true }).selectOption(fixture.teamId);
  await page.getByRole('combobox', { name: 'Responsible member', exact: true }).selectOption(fixture.ownerId);
  await page.getByRole('textbox', { name: 'Reason', exact: true }).fill('Share this reviewed team goal');
  await page.route(shareUrl, route => route.abort('failed'), { times: 1 });
  await page.getByRole('button', { name: 'Share reviewed task details', exact: true }).click();
  await expect(page.getByRole('alert').last()).toBeVisible();
  const shared = page.waitForResponse(response => response.url() === shareUrl && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Share reviewed task details', exact: true }).click();
  assert.equal((await shared).status(), 200);
  assert.equal(commands.length, 2);
  assert.deepEqual(commands[0], commands[1]);
  await expect(page.getByRole('button', { name: 'Review and update shared details', exact: true })).toBeVisible();
  await context.close();
  await handoff('task_shared', 'lead_ready');
  await openAs(fixture.leadToken);
  assert.equal((await openTasks()).tasks[0].description, 'Explicit shared goal');
  await page.getByRole('button', { name: 'Hand over responsibility', exact: true }).click();
  await page.getByRole('combobox', { name: 'New responsible member', exact: true }).selectOption(fixture.successorId);
  await page.getByRole('textbox', { name: 'Reason', exact: true }).fill('Continue with the next responsible teammate');
  const moved = page.waitForResponse(response => response.url().endsWith(`/tasks/${fixture.taskId}/handover`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Confirm responsibility handover', exact: true }).click();
  const decision = await moved;
  assert.equal(decision.status(), 200);
  assert.equal((await decision.json()).paused_routine_count, 1);
  await page.screenshot({ path: `${fixture.outputDir}/team-handover.png`, fullPage: true });
  await context.close();
  await handoff('task_transferred', 'successor_ready');
  await openAs(fixture.successorToken);
  assert.equal((await openTasks()).tasks[0].responsible_user_id, fixture.successorId);
  const denied = page.waitForResponse(response => response.url().endsWith(`/agents/${fixture.agentId}/sessions/runs`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Open my own work', exact: true }).click();
  await page.getByRole('textbox', { name: 'Your instructions', exact: true }).fill('Continue with the reviewed shared goal.');
  await page.getByRole('button', { name: 'Start in my own work', exact: true }).click();
  assert.equal((await denied).status(), 403);
  await handoff('access_required', 'access_ready');
  await page.getByRole('alert').getByRole('button', { name: 'Refresh', exact: true }).click();
  const opened = page.waitForResponse(response => response.url().endsWith(`/agents/${fixture.agentId}/sessions/runs`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Open my own work', exact: true }).click();
  await page.getByRole('textbox', { name: 'Your instructions', exact: true }).fill('Continue with the reviewed shared goal.');
  await page.getByRole('button', { name: 'Start in my own work', exact: true }).click();
  const result = await opened;
  assert.equal(result.status(), 201);
  await page.getByRole('link', { name: 'Open submitted work', exact: true }).click();
  await expect(page).toHaveURL(/session_id=/);
  const sessionId = new URL(page.url()).searchParams.get('session_id');
  assert.ok(sessionId);
  await context.close();
  await handoff('private_work_opened', 'owner_ready', { session_id: sessionId });
  await openAs(fixture.ownerToken);
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}#tasks`);
  await page.getByRole('button', { name: 'Make private and return responsibility to the creator', exact: true }).click();
  await page.getByRole('textbox', { name: 'Reason', exact: true }).fill('Return this assignment to private work');
  const revoked = page.waitForResponse(response => response.url() === shareUrl && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Confirm private work and creator responsibility', exact: true }).click();
  assert.equal((await revoked).status(), 200);
  await context.close();
  await handoff('task_unshared', 'revocation_ready');
  await openAs(fixture.successorToken);
  assert.deepEqual((await openTasks()).tasks, []);
  await expect(page.getByText('Deliver analysis', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: `${fixture.outputDir}/team-after-revocation.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(`${fixture.outputDir}/browser-receipt.json`, JSON.stringify({ commands, session_id: sessionId, errors }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${fixture.outputDir}/browser-error.png`, fullPage: true }).catch(() => {});
  throw error;
} finally {
  controls.close();
  await browser.close();
}

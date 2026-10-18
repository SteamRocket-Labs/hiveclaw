/** Real team request -> admin decision -> original native task claim. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { chromium, expect } from '@playwright/test';

const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
const controls = createInterface({ input: process.stdin });
const messages = controls[Symbol.asyncIterator]();
let context, page;
const errors = [], commands = [];
const requestUrl = `${fixture.origin}/api/business-teams/${fixture.teamId}/runtime-budget-runs/${fixture.runId}/increase-requests`;
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
  page.on('request', request => { if (request.url() === requestUrl && request.method() === 'POST') commands.push(request.postDataJSON()); });
}
async function openTeam() {
  await page.goto(`${fixture.origin}/team`);
  await page.getByRole('button', { name: /Delivery/ }).click();
  const response = page.waitForResponse(response => response.url().includes(`/business-teams/${fixture.teamId}/runtime-budget-runs`) && response.request().method() === 'GET');
  await page.getByRole('button', { name: 'Team runtime budgets', exact: true }).click();
  const result = await response;
  assert.equal(result.status(), 200);
  const body = await result.json();
  assert.equal(body.runs.length, 1);
  assert.ok(!JSON.stringify(body).includes('PRIVATE_BUDGET_CANARY'));
  return body.runs[0];
}
async function handoff(event, expected) {
  process.stdout.write(JSON.stringify({ event }) + '\n');
  assert.equal(JSON.parse((await messages.next()).value).event, expected);
}
try {
  await openAs(fixture.token);
  assert.equal((await openTeam()).limits.max_tokens, 10);
  await page.getByRole('spinbutton', { name: 'Total token limit', exact: true }).fill('30');
  await page.getByRole('spinbutton', { name: 'Max subagents', exact: true }).fill('3');
  await page.getByRole('textbox', { name: 'Reason', exact: true }).fill('Finish the analysis');
  await page.route(requestUrl, route => route.abort('failed'), { times: 1 });
  await page.getByRole('button', { name: 'Request an increase', exact: true }).click();
  await expect(page.getByRole('alert').last()).toBeVisible();
  const submitted = page.waitForResponse(response => response.url() === requestUrl && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Request an increase', exact: true }).click();
  const result = await submitted;
  assert.equal(result.status(), 201);
  const request = await result.json();
  assert.equal(request.status, 'pending');
  assert.equal(commands.length, 2);
  assert.deepEqual(commands[0], commands[1]);
  await expect(page.getByText('Awaiting administrator decision', { exact: true })).toBeVisible();
  await page.screenshot({ path: `${fixture.outputDir}/team-pending.png`, fullPage: true });
  await context.close();
  await handoff('increase_requested', 'admin_ready');
  await openAs(fixture.adminToken);
  await page.goto(`${fixture.origin}/enterprise/runtime-budgets`);
  await expect(page.getByText('Finish the analysis', { exact: false })).toBeVisible();
  const approved = page.waitForResponse(response => response.url().endsWith(`/runs/${fixture.runId}/approve-overrun`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Approve requested increase', exact: true }).click();
  const decision = await approved;
  assert.equal(decision.status(), 200);
  assert.equal(decision.request().postDataJSON().increase_request_id, request.id);
  assert.equal(decision.request().postDataJSON().max_tokens, 30);
  assert.equal(decision.request().postDataJSON().max_subagents, 3);
  await context.close();
  await handoff('increase_approved', 'lead_ready');
  await openAs(fixture.token);
  const resumed = await openTeam();
  assert.equal(resumed.limits.max_tokens, 30);
  assert.equal(resumed.limits.max_subagents, 3);
  assert.equal(resumed.increase_requests[0].status, 'approved');
  await expect(page.getByText('Requested increase approved', { exact: true })).toBeVisible();
  await page.screenshot({ path: `${fixture.outputDir}/team-resumed.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(`${fixture.outputDir}/browser-receipt.json`, JSON.stringify({ request, commands, resumed, errors }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${fixture.outputDir}/browser-error.png`, fullPage: true }).catch(() => {});
  throw error;
} finally {
  controls.close();
  await browser.close();
}

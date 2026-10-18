/** Actual team/owner entry, native worker receipt, safe retry and cancellation. */
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
async function openAs(token, path) {
  context = await browser.newContext({ locale: 'en-US', viewport: { width: 1280, height: 900 } });
  await context.route('**/*', route => new URL(route.request().url()).origin === fixture.origin ? route.continue() : route.abort());
  await context.addInitScript(({ token, tenantId }) => {
    localStorage.setItem('token', token);
    localStorage.setItem('current_tenant_id', tenantId);
    localStorage.setItem('i18nextLng', 'en');
  }, { token, tenantId: fixture.tenantId });
  page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto(`${fixture.origin}${path}`);
}
async function handoff(event, expected) {
  process.stdout.write(JSON.stringify({ event }) + '\n');
  assert.equal(JSON.parse((await messages.next()).value).event, expected);
}
try {
  await openAs(fixture.leadToken, '/team');
  await page.getByRole('button', { name: /Delivery/ }).click();
  await expect(page.getByRole('heading', { name: 'Employees assigned to this team' })).toBeVisible();
  await page.getByRole('button', { name: 'Employee readiness', exact: true }).click();
  const section = page.getByRole('region', { name: 'Employee readiness' });
  const url = `${fixture.origin}/api/business-teams/${fixture.teamId}/agents/${fixture.agentId}/readiness-checks`;
  page.on('request', request => { if (request.url() === url && request.method() === 'POST') commands.push(request.postDataJSON()); });
  await page.route(url, route => route.abort('failed'), { times: 1 });
  await section.getByRole('button', { name: 'Run a new check', exact: true }).click();
  await expect(section.getByRole('alert')).toBeVisible();
  const started = page.waitForResponse(response => response.url() === url && response.request().method() === 'POST');
  await section.getByRole('button', { name: 'Retry this check request', exact: true }).click();
  assert.equal((await started).status(), 202);
  assert.equal(commands.length, 2);
  assert.deepEqual(commands[0], commands[1]);
  await expect(section.getByText('Waiting for a worker; an administrator may need to check worker capacity.')).toBeVisible();
  await expect(section.getByText('Verified', { exact: true })).toHaveCount(0);
  await expect(section.getByRole('button', { name: 'Run a new check', exact: true })).toBeDisabled();
  await page.screenshot({ path: `${fixture.outputDir}/readiness-queued.png`, fullPage: true });
  await handoff('team_check_queued', 'worker_started');
  await expect(section.getByText('Checks recorded. Review each result.')).toBeVisible({ timeout: 30000 });
  await expect(section.getByText(/^Verified(?: · Approved model)?$/)).toHaveCount(4);
  await expect(section.getByText('Not verified by this check', { exact: true })).toHaveCount(2);
  await expect(section.getByText('Verified · Approved model', { exact: true })).toBeVisible();
  assert.ok(!(await page.locator('body').innerText()).includes('SYNTHETIC_PRIVATE_KEY_CANARY'));
  await page.screenshot({ path: `${fixture.outputDir}/readiness-checked.png`, fullPage: true });
  await context.close();
  await handoff('team_check_recorded', 'owner_ready');
  await openAs(fixture.ownerToken, `/agents/${fixture.agentId}#settings`);
  await page.getByRole('button', { name: 'Employee readiness', exact: true }).click();
  const ownerSection = page.getByRole('region', { name: 'Employee readiness' });
  await ownerSection.getByRole('button', { name: 'Run a new check', exact: true }).click();
  await expect(ownerSection.getByText('Waiting for a worker; an administrator may need to check worker capacity.')).toBeVisible();
  await ownerSection.getByRole('button', { name: 'Cancel my check', exact: true }).click();
  await expect(ownerSection.getByText('Check cancelled.').first()).toBeVisible();
  await expect(ownerSection.getByRole('button', { name: 'Run a new check', exact: true })).toBeEnabled();
  await page.screenshot({ path: `${fixture.outputDir}/readiness-owner-cancelled.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(`${fixture.outputDir}/browser-receipt.json`, JSON.stringify({ commands, errors, teamRecorded: true, ownerCancelled: true }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${fixture.outputDir}/browser-error.png`, fullPage: true }).catch(() => {});
  throw error;
} finally {
  controls.close();
  await browser.close();
}

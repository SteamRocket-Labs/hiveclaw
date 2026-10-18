/** Actual control-plane opening uses canonical company configuration and worker receipts. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { chromium, expect } from '@playwright/test';
const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
const lines = createInterface({ input: process.stdin });
const messages = lines[Symbol.asyncIterator]();
const errors = [];
const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1280, height: 900 } });
await context.route('**/*', route => new URL(route.request().url()).origin === fixture.origin ? route.continue() : route.abort());
await context.addInitScript(({ token, tenantId }) => {
  localStorage.setItem('token', token); localStorage.setItem('current_tenant_id', tenantId); localStorage.setItem('i18nextLng', 'en');
}, fixture);
const page = await context.newPage();
page.on('pageerror', error => errors.push(String(error)));
try {
  await page.goto(`${fixture.origin}/enterprise/dashboard`);
  const guide = page.locator('details').filter({ has: page.locator('summary', { hasText: 'Get started with your company' }) }).first();
  await guide.locator(':scope > summary').click();
  await expect(guide.getByRole('heading', { name: 'Readiness', exact: true })).toBeVisible();
  await expect(guide.getByRole('combobox', { name: 'Employee to check' })).toHaveValue(fixture.agentId);
  await guide.getByRole('link', { name: 'Models for native employees' }).click();
  await expect(page).toHaveURL(/\/enterprise\/llm$/);
  await expect(page.getByRole('heading', { name: 'Models & Budget', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Back to overview', exact: true }).click();
  await guide.locator(':scope > summary').click();
  await expect(guide.getByRole('heading', { name: 'Readiness', exact: true })).toBeVisible();
  await guide.getByRole('button', { name: 'Employee readiness', exact: true }).click();
  await guide.getByRole('button', { name: 'Run a new check', exact: true }).click();
  await expect(guide.getByText('Waiting for a worker; an administrator may need to check worker capacity.')).toBeVisible();
  await expect(guide.getByText('Verified', { exact: true })).toHaveCount(0);
  process.stdout.write(JSON.stringify({ event: 'opening_check_queued' }) + '\n');
  assert.equal(JSON.parse((await messages.next()).value).event, 'worker_started');
  await expect(guide.getByText('Checks recorded. Review each result.')).toBeVisible({ timeout: 30000 });
  await expect(guide.getByText('Not verified by this check', { exact: true })).toHaveCount(2);
  await expect(guide.getByText('Verified · Approved model', { exact: true })).toBeVisible();
  await page.screenshot({ path: `${fixture.outputDir}/company-opening-checked.png`, fullPage: true });
  const url = `${fixture.origin}/api/enterprise/onboarding-status?tenant_id=${fixture.tenantId}`;
  await page.route(url, route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'onboarding_status_unavailable' } }) }));
  await guide.getByRole('button', { name: 'Refresh', exact: true }).last().click();
  await expect(guide.getByRole('alert')).toBeVisible({ timeout: 15000 });
  await expect(guide.getByRole('combobox', { name: 'Employee to check' })).toHaveCount(0);
  await expect(guide.getByText('Verified · Approved model', { exact: true })).toHaveCount(0);
  await page.unroute(url);
  await guide.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(guide.getByRole('combobox', { name: 'Employee to check' })).toHaveValue(fixture.agentId);
  await guide.getByRole('link', { name: 'Open employee work', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/agents/${fixture.agentId}$`));
  await expect(page.getByRole('heading', { name: 'Analyst', exact: true })).toBeVisible();
  assert.ok(!(await page.locator('body').innerText()).includes('SYNTHETIC_PRIVATE_KEY_CANARY'));
  assert.deepEqual(errors, []);
  await writeFile(`${fixture.outputDir}/browser-receipt.json`, JSON.stringify({ errors, modelsRouteOpened: true, queueObserved: true, workerObserved: true, unavailableFactsHidden: true, employeeWorkOpened: true }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${fixture.outputDir}/browser-error.png`, fullPage: true }).catch(() => {});
  throw error;
} finally {
  lines.close(); await browser.close();
}

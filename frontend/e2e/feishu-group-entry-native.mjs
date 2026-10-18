/** Real settings component -> HTTP -> non-owner PostgreSQL shared-group selection. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { chromium, expect } from '@playwright/test';
const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1280, height: 900 } });
await context.route('**/*', route => new URL(route.request().url()).origin === fixture.origin ? route.continue() : route.abort());
await context.addInitScript(({ token, tenantId }) => {
  localStorage.setItem('token', token); localStorage.setItem('current_tenant_id', tenantId); localStorage.setItem('i18nextLng', 'en');
}, fixture);
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(String(error)));
try {
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}?manage=true#settings`);
  const panel = page.locator('details').filter({ has: page.locator('summary', { hasText: 'Shared group work' }) });
  await panel.locator(':scope > summary').click();
  const field = panel.getByLabel('Group IDs, one per line');
  await expect(field).toHaveValue('');
  await field.fill('oc_selected');
  const saved = page.waitForResponse(response => response.url().endsWith('/channel/shared-groups') && response.request().method() === 'PUT');
  await panel.getByRole('button', { name: 'Save', exact: true }).click();
  assert.equal((await saved).status(), 200);
  await expect(panel.getByRole('status')).toHaveText('Group entry setting saved.');
  await expect(panel.getByText('Saved groups: oc_selected', { exact: true })).toBeVisible();
  await panel.screenshot({ path: `${fixture.outputDir}/group-entry-1280.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(field).toHaveValue('oc_selected');
  assert.ok(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 2));
  await panel.screenshot({ path: `${fixture.outputDir}/group-entry-390.png` });
  assert.ok(!(await page.locator('body').innerText()).includes('synthetic-group-entry-secret'));
  assert.deepEqual(errors, []);
  await writeFile(`${fixture.outputDir}/group-browser-receipt.json`, JSON.stringify({ savedThroughCurrentSettings: true,
    actualApiAndDatabase: true, widths: [1280, 390], errors }, null, 2));
} catch (error) {
  await page.screenshot({ path: `${fixture.outputDir}/group-browser-error.png`, fullPage: true }).catch(() => {});
  throw error;
} finally {
  await browser.close();
}

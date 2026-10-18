/** Exercise the built application against the isolated native group fixture. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { chromium, expect } from '@playwright/test';

const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
assert.equal(new URL(fixture.origin).hostname, '127.0.0.1');
const browser = await chromium.launch({
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  headless: true,
});
const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1440, height: 1000 } });
const failures = [];
const page = await context.newPage();
page.on('pageerror', error => failures.push(String(error)));
const socketFrames = [];
page.on('websocket', socket => socket.on('framereceived', frame => {
  try { socketFrames.push(JSON.parse(String(frame.payload))); } catch { /* Non-JSON transport evidence. */ }
}));
await page.route('**/*', route => {
  const url = new URL(route.request().url());
  return url.origin === fixture.origin ? route.continue() : route.abort();
});
await page.addInitScript(({ token, tenantId }) => {
  localStorage.setItem('token', token);
  localStorage.setItem('current_tenant_id', tenantId);
  localStorage.setItem('i18nextLng', 'en');
}, fixture);
try {
  const groupsPromise = page.waitForResponse(response => response.url().endsWith('/sessions?scope=groups'));
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}/sessions/${fixture.sessionId}`);
  assert.equal((await groupsPromise).status(), 200);
  await expect(page.getByText('Shared group work', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('The independent report is ready.', { exact: false }).first()).toBeVisible({ timeout: 30000 });
  await expect(page.locator('textarea:visible')).toHaveCount(1);
  assert(!await page.locator('body').innerText().then(text => text.includes(fixture.privateCanary)));
  await expect.poll(() => socketFrames.find(frame => frame.type === 'session.ready'), { timeout: 15000 }).toBeTruthy();
  assert(socketFrames.some(frame => frame.type === 'session.ready' && frame.active_run_id === fixture.activeRunId));
  await expect(page.getByText('Runtime error', { exact: true })).toHaveCount(0);
  await expect(page.getByTestId('thread-item-retry-turn')).toHaveCount(0);
  await expect(page.getByText('Source access', { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: fixture.entryScreenshot, fullPage: true });

  const responsePromise = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith(`/sessions/${fixture.sessionId}/runs`));
  await page.locator('textarea:visible').fill('NATIVE_BROWSER_GROUP_ADDENDUM');
  await page.locator('textarea:visible').press('Enter');
  const response = await responsePromise;
  assert.equal(response.status(), 202);
  const receipt = await response.json();
  assert.equal(receipt.schema, 'hive.human_input_receipt');
  assert.equal(receipt.intent, 'queue_next_turn');
  assert.equal(receipt.run, null);
  await expect(page.getByText(/Your input is recorded|补充已入账/).first()).toBeVisible();
  await expect(page.locator('textarea:visible')).toHaveValue('');
  assert(!await page.locator('body').innerText().then(text => text.includes(fixture.privateCanary)));
  await writeFile(fixture.receiptPath, JSON.stringify(receipt, null, 2));
  process.stdout.write(JSON.stringify({ event: 'input_recorded', inputId: receipt.input_id, commandId: receipt.command_id }) + '\n');

  await expect.poll(() => socketFrames.some(frame => frame.type === 'session.error'
    && frame.error?.code === 'session_forbidden'), { timeout: 15000 }).toBe(true);
  await expect(page.getByText('The independent report is ready.', { exact: false })).toHaveCount(0);
  await expect(page.locator('textarea:visible')).toHaveCount(0);
  assert(page.url().includes(`/agents/${fixture.agentId}/sessions/${fixture.sessionId}`));
  const denied = await page.request.get(`${fixture.origin}/api/agents/${fixture.agentId}/sessions/${fixture.sessionId}/transcript?schema_version=2`, {
    headers: { Authorization: `Bearer ${fixture.token}`, 'X-Tenant-Id': fixture.tenantId },
  });
  assert.equal(denied.status(), 403);
  await page.screenshot({ path: fixture.deniedScreenshot, fullPage: true });
  await writeFile(fixture.socketPath, JSON.stringify(socketFrames, null, 2));
  process.stdout.write(JSON.stringify({ event: 'read_revoked', renderedBodyCleared: true }) + '\n');
  const control = JSON.parse(await new Promise(resolve => process.stdin.once('data', chunk => resolve(String(chunk)))));
  assert.equal(control.event, 'provider_offline');
  const unavailableGroups = page.waitForResponse(response => response.url().endsWith('/sessions?scope=groups'));
  await page.goto(`${fixture.origin}/agents/${fixture.agentId}/sessions/${fixture.privateSessionId}`);
  assert.equal((await unavailableGroups).status(), 503);
  await expect(page.locator('textarea:visible')).toBeEnabled({ timeout: 15000 });
  await expect(page.getByText(/Shared group conversations are unavailable|群会话暂不可用/).first()).toBeVisible();
  const privateResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && response.url().endsWith(`/sessions/${fixture.privateSessionId}/runs`));
  await page.locator('textarea:visible').fill('NATIVE_PRIVATE_CONTINUATION');
  await page.locator('textarea:visible').press('Enter');
  assert([201, 202].includes((await privateResponse).status()));
  await expect(page.locator('textarea:visible')).toHaveValue('');
  await expect(page.getByText('NATIVE_PRIVATE_CONTINUATION', { exact: false }).first()).toBeVisible();
  assert(!await page.locator('body').innerText().then(text => text.includes(fixture.privateCanary)));
  await page.screenshot({ path: fixture.privateScreenshot, fullPage: true });
  await writeFile(fixture.socketPath, JSON.stringify(socketFrames, null, 2));
  assert.deepEqual(failures, []);
  process.stdout.write(JSON.stringify({ event: 'private_available', groupInventoryStatus: 503 }) + '\n');
} catch (error) {
  await writeFile(fixture.errorPath, JSON.stringify({ error: String(error), url: page.url(), pageErrors: failures,
    body: await page.locator('body').innerText(), frames: socketFrames }, null, 2));
  await page.screenshot({ path: fixture.errorScreenshot, fullPage: true });
  throw error;
} finally {
  await browser.close();
}

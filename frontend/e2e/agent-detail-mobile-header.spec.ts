/**
 * §9 P12 E2E: Agent detail header must not overflow the document on narrow
 * screens (production repro: 390×844 light theme, collapsed 68px rail,
 * long agent name + long role description). Chat/expiry actions must stay
 * reachable and inline name editing must stay inside the viewport.
 *
 * Browser-level on the real Vite app with /api mocked via page.route()
 * (deterministic, backend-free).
 */

import { expect, test, type Page } from '@playwright/test';

const AGENT_ID = '7e57a9e7-0000-4000-8000-0000000000b4';
const LONG_NAME = 'WRC-FUNCTIONAL-B4-20260909-Analyst';
const LONG_ROLE =
  'Senior functional release analyst covering narrow-screen layout verification, '
  + 'mobile workbench navigation, expiry lifecycle evidence and long role description overflow';

async function bootstrapAgentDetail(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem(
      'auth-storage',
      JSON.stringify({
        state: {
          token: 'e2e-token',
          user: { id: 'u-1', username: 'e2e', display_name: 'E2E', role: 'admin', tenant_id: 't-1' },
        },
        version: 0,
      }),
    );
  });

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith('/api/')) return route.fallback();
    const method = route.request().method();

    if (path.endsWith('/auth/me') && method === 'GET') {
      return route.fulfill({
        json: {
          id: 'u-1',
          username: 'e2e',
          email: 'e2e@test.local',
          display_name: 'E2E',
          role: 'admin',
          tenant_id: 't-1',
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}`) && method === 'GET') {
      return route.fulfill({
        json: {
          id: AGENT_ID,
          name: LONG_NAME,
          status: 'idle',
          agent_type: 'native',
          access_level: 'manage',
          role_description: LONG_ROLE,
          expires_at: '2026-12-31T00:00:00Z',
          action_capabilities: { can_manage: true },
        },
      });
    }
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#workflows`);
}

async function documentOverflow(page: Page) {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
}

test('mobile 390px: header stays in viewport, chat/expiry reachable, edit name in bounds', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await bootstrapAgentDetail(page);

  await expect(page.getByRole('heading', { name: LONG_NAME })).toBeVisible();

  const { scrollWidth, clientWidth } = await documentOverflow(page);
  expect(scrollWidth, `document overflows: scrollWidth ${scrollWidth} > clientWidth ${clientWidth}`)
    .toBeLessThanOrEqual(clientWidth);

  // Header actions must be visible and horizontally within the viewport.
  const chatButton = page.locator('.agent-detail-header-actions button', { hasText: 'Chat' }).first();
  await expect(chatButton).toBeVisible();
  const chatBox = await chatButton.boundingBox();
  expect(chatBox, 'chat button must have a box').not.toBeNull();
  expect(chatBox!.x + chatBox!.width).toBeLessThanOrEqual(390);

  const expiryButton = page.locator('.agent-detail-expiry-edit');
  await expect(expiryButton).toBeVisible();
  const expiryBox = await expiryButton.boundingBox();
  expect(expiryBox!.x + expiryBox!.width).toBeLessThanOrEqual(390);

  // All workbench area tabs remain present (internal scrolling is allowed,
  // but the tab strip must not widen the document).
  const tablist = page.locator('.agent-workbench-areas');
  await expect(tablist).toBeVisible();
  const tabCount = await tablist.locator('button').count();
  expect(tabCount).toBeGreaterThan(1);

  // Editing mode: the inline name input must fit inside the viewport and
  // must not push the document wider.
  await page.getByRole('heading', { name: LONG_NAME }).click();
  const nameInput = page.locator('.agent-detail-name-input');
  await expect(nameInput).toBeVisible();
  const inputBox = await nameInput.boundingBox();
  expect(inputBox!.x + inputBox!.width).toBeLessThanOrEqual(390);
  const afterEdit = await documentOverflow(page);
  expect(afterEdit.scrollWidth).toBeLessThanOrEqual(afterEdit.clientWidth);
  await nameInput.press('Escape');
});

test('desktop 1280px: no overflow with the same long name', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await bootstrapAgentDetail(page);

  await expect(page.getByRole('heading', { name: LONG_NAME })).toBeVisible();
  const { scrollWidth, clientWidth } = await documentOverflow(page);
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
});

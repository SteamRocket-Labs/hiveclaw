import { expect, test, type Page } from '@playwright/test';

/**
 * Entry-recovery acceptance: the collapsed sidebar Settings menu and the
 * company-backend Back to App link are real navigation entry points, verified
 * with pointer, keyboard, and live geometry — not aria flags alone.
 */

type Role = 'member' | 'org_admin' | 'platform_admin';

async function bootstrapApp(page: Page, options: {
  role: Role;
  collapsed?: boolean;
  lang?: string;
  theme?: string;
  path?: string;
}) {
  const { role, collapsed = false, lang = 'en', theme = 'light', path = '/home' } = options;
  await page.addInitScript(({ role: initialRole, collapsed: initialCollapsed, lang: initialLang, theme: initialTheme }) => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', initialLang);
    localStorage.setItem('theme', initialTheme);
    localStorage.setItem('sidebar_collapsed', String(initialCollapsed));
    localStorage.setItem('current_tenant_id', 't-1');
    localStorage.setItem(
      'auth-storage',
      JSON.stringify({
        state: {
          token: 'e2e-token',
          user: { id: 'u-1', username: 'e2e', display_name: 'E2E Admin', role: initialRole, tenant_id: 't-1' },
        },
        version: 0,
      }),
    );
  }, { role, collapsed, lang, theme });
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith('/api/')) return route.fallback();
    if (path.endsWith('/auth/me')) {
      return route.fulfill({ json: { id: 'u-1', username: 'e2e', display_name: 'E2E Admin', role, tenant_id: 't-1' } });
    }
    if (route.request().method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto(path);
  await expect(page.locator('nav.sidebar')).toBeVisible();
}

const SETTINGS_DROPDOWN = '.account-dropdown.sidebar-settings-dropdown';
const SETTINGS_TRIGGER = '.sidebar-settings-row';

async function dropdownGeometry(page: Page) {
  return page.evaluate((selector) => {
    const dropdown = document.querySelector(selector);
    const sidebar = document.querySelector('nav.sidebar');
    if (!dropdown || !sidebar) return null;
    const dropdownRect = dropdown.getBoundingClientRect();
    const sidebarRect = sidebar.getBoundingClientRect();
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      dropdown: { left: dropdownRect.left, right: dropdownRect.right, top: dropdownRect.top, bottom: dropdownRect.bottom },
      sidebar: { left: sidebarRect.left, right: sidebarRect.right, top: sidebarRect.top, bottom: sidebarRect.bottom },
      display: getComputedStyle(dropdown).display,
    };
  }, SETTINGS_DROPDOWN);
}

test('collapsed sidebar Settings opens a visible actionable menu with the pointer', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapApp(page, { role: 'org_admin', collapsed: true });
  const trigger = page.locator(SETTINGS_TRIGGER);
  await expect(trigger).toBeVisible();

  await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  const dropdown = page.locator(SETTINGS_DROPDOWN);
  await expect(dropdown).toBeVisible();

  // The menu must paint beside the 68px rail and fully inside the viewport:
  // the sidebar's overflow clip must not hide it.
  const geometry = await dropdownGeometry(page);
  expect(geometry).toBeTruthy();
  expect(geometry!.display).not.toBe('none');
  expect(geometry!.dropdown.left).toBeGreaterThanOrEqual(geometry!.sidebar.right);
  expect(geometry!.dropdown.right).toBeLessThanOrEqual(geometry!.viewport.width);
  expect(geometry!.dropdown.top).toBeGreaterThanOrEqual(0);
  expect(geometry!.dropdown.bottom).toBeLessThanOrEqual(geometry!.viewport.height);

  // Every row is actionable, with role boundaries intact: a company
  // administrator reaches the company backend and never sees platform settings.
  await expect(dropdown.getByRole('button', { name: 'Account Settings' })).toBeVisible();
  await expect(dropdown.getByRole('link', { name: 'Company Admin' })).toBeVisible();
  await expect(dropdown.getByRole('link', { name: 'Platform Settings' })).toHaveCount(0);
  await expect(dropdown.getByRole('button', { name: 'Theme' })).toBeVisible();
  await expect(dropdown.getByRole('button', { name: 'Sign Out' })).toBeVisible();
});

test('collapsed sidebar Settings works with keyboard only and returns focus', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapApp(page, { role: 'org_admin', collapsed: true });
  const trigger = page.locator(SETTINGS_TRIGGER);

  await trigger.focus();
  await expect(trigger).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  const dropdown = page.locator(SETTINGS_DROPDOWN);
  await expect(dropdown).toBeVisible();
  // Focus moves into the menu so Tab walks the actions.
  await expect(dropdown.getByRole('button', { name: 'Account Settings' })).toBeFocused();

  // Escape closes the menu and returns focus to the trigger.
  await page.keyboard.press('Escape');
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await expect(dropdown).toHaveCount(0);
  await expect(trigger).toBeFocused();

  // Re-open and dismiss with an outside pointer press.
  await page.keyboard.press('Enter');
  await expect(dropdown).toBeVisible();
  await page.locator('.main-content').click({ position: { x: 400, y: 300 }, force: true });
  await expect(page.locator(SETTINGS_DROPDOWN)).toHaveCount(0);
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
});

test('collapsed sidebar Settings closes on in-app route change', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapApp(page, { role: 'org_admin', collapsed: true });
  const trigger = page.locator(SETTINGS_TRIGGER);

  await trigger.click();
  await expect(page.locator(SETTINGS_DROPDOWN)).toBeVisible();

  // Browser history navigation carries no outside mousedown; the menu must
  // still close rather than linger over a different page.
  await page.locator('nav.sidebar').getByRole('link', { name: 'Knowledge' }).click();
  await expect(page).toHaveURL(/\/knowledge/);
  await expect(page.locator(SETTINGS_DROPDOWN)).toHaveCount(0);
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
});

test('collapsed sidebar Settings keeps translated labels and theme tokens', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapApp(page, { role: 'org_admin', collapsed: true, lang: 'zh', theme: 'dark' });
  await page.locator(SETTINGS_TRIGGER).click();
  const dropdown = page.locator(SETTINGS_DROPDOWN);
  await expect(dropdown).toBeVisible();
  await expect(dropdown.getByRole('button', { name: '账户设置' })).toBeVisible();
  const background = await dropdown.evaluate((element) => getComputedStyle(element).backgroundColor);
  expect(background).not.toBe('rgba(0, 0, 0, 0)');
  expect(background).not.toBe('transparent');
});

test('expanded sidebar Settings menu keeps its in-sidebar flyout', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapApp(page, { role: 'org_admin', collapsed: false });
  const trigger = page.locator(SETTINGS_TRIGGER);
  await trigger.click();
  const dropdown = page.locator(SETTINGS_DROPDOWN);
  await expect(dropdown).toBeVisible();

  // Expanded mode is unchanged: the menu opens upward inside the sidebar.
  const geometry = await dropdownGeometry(page);
  expect(geometry).toBeTruthy();
  expect(geometry!.dropdown.left).toBeGreaterThanOrEqual(geometry!.sidebar.left);
  expect(geometry!.dropdown.right).toBeLessThanOrEqual(geometry!.sidebar.right);
  const triggerRect = await page.locator(SETTINGS_TRIGGER).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { top: rect.top };
  });
  expect(geometry!.dropdown.bottom).toBeLessThanOrEqual(triggerRect.top + 1);
});

test('narrow compact sidebar Settings opens a visible actionable menu', async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 720 });
  await bootstrapApp(page, { role: 'org_admin' });
  // Compact viewports start with the rail collapsed.
  await expect(page.locator('nav.sidebar')).toHaveClass(/collapsed/);
  await page.locator(SETTINGS_TRIGGER).click();
  const dropdown = page.locator(SETTINGS_DROPDOWN);
  await expect(dropdown).toBeVisible();
  const geometry = await dropdownGeometry(page);
  expect(geometry).toBeTruthy();
  expect(geometry!.dropdown.left).toBeGreaterThanOrEqual(geometry!.sidebar.right);
  expect(geometry!.dropdown.bottom).toBeLessThanOrEqual(geometry!.viewport.height);
  await expect(dropdown.getByRole('button', { name: 'Account Settings' })).toBeVisible();
});

for (const role of ['platform_admin', 'org_admin'] as const) {
  for (const path of ['/enterprise/dashboard', '/enterprise/info']) {
    test(`Back to App returns ${role} from ${path} with context intact`, async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 720 });
      await bootstrapApp(page, { role, path });
      await expect(page.locator('nav.sidebar')).toBeVisible();

      const backToApp = page.locator('nav.sidebar').getByRole('link', { name: 'Back to App' });
      await expect(backToApp).toBeVisible();
      await backToApp.click();

      await expect(page).toHaveURL(/\/home$/);
      // The app surface chrome is back and the authenticated session plus the
      // selected company survive the return.
      await expect(page.locator('nav.sidebar').getByRole('link', { name: 'Home' })).toBeVisible();
      const storage = await page.evaluate(() => ({
        token: localStorage.getItem('token'),
        tenant: localStorage.getItem('current_tenant_id'),
      }));
      expect(storage.token).toBe('e2e-token');
      expect(storage.tenant).toBe('t-1');
    });
  }
}

test('shipped platform-settings Back to App keeps working', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await bootstrapApp(page, { role: 'platform_admin', path: '/admin/platform-settings' });
  const backToApp = page.locator('nav.sidebar').getByRole('link', { name: 'Back to App' });
  await expect(backToApp).toBeVisible();
  await backToApp.click();
  await expect(page).toHaveURL(/\/home$/);
});

import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

/**
 * PDEC-013 three-role UI acceptance (mocked /api surface — UI wiring only;
 * these tests do not prove backend RLS or production behavior).
 *
 * - Scoped administrators (org_admin, platform_admin with a selected company)
 *   reach the managed Agent/session inventory as themselves: no fabricated
 *   operator reason, no Operator View chrome, real sender/actor rendering.
 * - Employees keep the own/public projection: a direct URL to a foreign
 *   Session is denied by the (mocked) server and the denial survives reload.
 * - A tenantless platform administrator gets a plain company selector on the
 *   HR creation path instead of a generic retry loop.
 * - A stale/disabled selected company on cold start clears only the selection
 *   (never the authenticated user) and lands on the active-company selector; a
 *   genuine 401 still logs out; an HR session authorization denial surfaces as
 *   a truthful error, not company re-selection.
 * - Clearing the selection in another tab clears this shell too — a platform
 *   admin's home tenant is never silently revived — and a successful empty
 *   company inventory says plainly that no active company is available.
 */

const AGENT_ID = '7e57a9e7-0000-4000-8000-0000000000a1';
const SESSION_ID = '8e57a9e7-0000-4000-8000-0000000000b1';
const HR_AGENT_ID = '7e57a9e7-0000-4000-8000-0000000000c1';
const HR_SESSION_ID = '8e57a9e7-0000-4000-8000-0000000000c2';

type Role = 'member' | 'org_admin' | 'platform_admin';

function threadItem(sequence: number, role: 'user' | 'assistant', content: string) {
  const id = `00000000-0000-4000-8000-${String(sequence).padStart(12, '0')}`;
  return {
    schema: 'hive.thread_item.v1',
    schema_version: 1,
    id,
    sequence,
    thread_id: SESSION_ID,
    session_id: SESSION_ID,
    run_id: null,
    turn_id: `turn-${sequence}`,
    correlation_id: null,
    item_type: role === 'user' ? 'user_message' : 'agent_message',
    item_status: 'succeeded',
    actor_type: role === 'user' ? 'user' : 'agent',
    event_type: role === 'user' ? 'user_message' : 'assistant_message',
    type: role === 'user' ? 'user_message' : 'assistant_message',
    role,
    visibility_scope: 'direct_user',
    listed_surface: 'chat',
    content,
    parts: [],
    metadata: { status: 'succeeded' },
    evidence_refs: [],
    created_at: `2026-09-05T10:0${sequence}:00Z`,
    item_data: {},
    audience: 'user',
    user_summary: content,
    user_action: null,
    operator_details: null,
  };
}

const managedSessionRow = {
  id: SESSION_ID,
  agent_id: AGENT_ID,
  user_id: 'u-employee',
  username: 'E2E Employee',
  title: 'Payroll reconciliation thread',
  source_channel: 'web',
  listed_surface: 'chat',
  session_kind: 'human_chat',
  permission_mode: 'default',
  is_current_user_session: false,
  read_only: true,
  authority_source: 'scoped_business_admin',
  operator_view: false,
  message_count: 2,
  created_at: '2026-09-05T10:00:00Z',
  updated_at: '2026-09-05T10:05:00Z',
};

function managedAgentProjection(role: Role) {
  const admin = role !== 'member';
  return {
    id: AGENT_ID,
    name: 'Payroll Clerk',
    status: 'idle',
    agent_type: 'native',
    role_description: 'Employee-private payroll agent',
    access_level: admin ? 'manage' : 'use',
    is_owner: false,
    action_capabilities: {
      can_use: true,
      can_manage: admin,
      can_manage_schedule: admin,
      can_manage_channel: admin,
      can_manage_permissions: admin,
      can_operator_inspect: false,
      can_transfer_ownership: false,
    },
    created_at: '2026-09-01T00:00:00Z',
  };
}

async function bootstrap(page: Page, options: {
  role: Role;
  path: string;
  tenantless?: boolean;
  sessionDenyStatus?: 403 | 404;
  hrSessionDeny?: { status: number; detail: string };
}) {
  const { role, path, tenantless = false, sessionDenyStatus, hrSessionDeny } = options;
  const scopedCalls: string[] = [];
  const user = {
    id: role === 'member' ? 'u-member' : 'u-admin',
    username: 'e2e',
    display_name: role === 'member' ? 'E2E Member' : 'E2E Admin',
    role,
    tenant_id: tenantless ? null : 't-1',
  };

  await page.addInitScript(({ tenantless: skipTenant, user: initialUser }) => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', 'en');
    localStorage.setItem('theme', 'light');
    if (!skipTenant) localStorage.setItem('current_tenant_id', 't-1');
    // A tenantless bootstrap must be genuinely selection-less even when an
    // earlier bootstrap on the same page left a selection behind — cold start
    // now preserves a valid selected company instead of wiping it.
    else localStorage.removeItem('current_tenant_id');
    localStorage.setItem(
      'auth-storage',
      JSON.stringify({ state: { token: 'e2e-token', user: initialUser }, version: 0 }),
    );
  }, { tenantless, user });

  page.on('request', (request) => {
    const url = request.url();
    if (url.includes('/sessions') && url.includes('scope=all')) scopedCalls.push(url);
  });

  await page.routeWebSocket('**/ws/chat/**', () => {});

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const reqPath = url.pathname;
    if (!reqPath.startsWith('/api/')) return route.fallback();
    const method = route.request().method();

    if (reqPath.endsWith('/auth/me')) return route.fulfill({ json: user });
    if (reqPath.endsWith('/admin/companies')) {
      return route.fulfill({ json: [{ id: 't-1', name: 'Acme Corp', slug: 'acme', is_active: true, created_at: '2026-01-01T00:00:00Z' }] });
    }
    if (reqPath.endsWith('/agents/system/hr')) {
      if (tenantless && !route.request().headers()['x-tenant-id']) {
        return route.fulfill({ status: 400, json: { detail: 'No tenant assigned' } });
      }
      return route.fulfill({ json: { id: HR_AGENT_ID, name: 'HR Agent', status: 'running' } });
    }
    if (reqPath === '/api/agents/' || reqPath === '/api/agents') {
      if (tenantless && !route.request().headers()['x-tenant-id']) {
        return route.fulfill({ status: 400, json: { detail: 'No tenant assigned' } });
      }
      return route.fulfill({ json: [managedAgentProjection(role)] });
    }
    if (reqPath.endsWith(`/agents/${AGENT_ID}`)) {
      return route.fulfill({ json: managedAgentProjection(role) });
    }
    if (reqPath.endsWith(`/agents/${AGENT_ID}/sessions`) && method === 'GET') {
      if (url.searchParams.get('scope') === 'all') {
        // The finalized backend contract: a scoped administrator needs no
        // operator reason; a member without an operator grant is denied.
        if (role === 'member') {
          return route.fulfill({ status: 403, json: { detail: 'Operator inspection authority is required' } });
        }
        return route.fulfill({ json: [managedSessionRow] });
      }
      return route.fulfill({ json: [] });
    }
    if (reqPath.includes(`/sessions/${SESSION_ID}/transcript`)) {
      if (sessionDenyStatus) {
        return route.fulfill({ status: sessionDenyStatus, json: { detail: sessionDenyStatus === 403 ? 'Session access denied' : 'Session not found' } });
      }
      return route.fulfill({ json: [
        threadItem(1, 'user', 'Reconcile the September payroll export.'),
        threadItem(2, 'assistant', 'The payroll export is reconciled and the summary is ready.'),
      ] });
    }
    if (reqPath.endsWith(`/sessions/${SESSION_ID}/messages`)) {
      if (sessionDenyStatus) {
        return route.fulfill({ status: sessionDenyStatus, json: { detail: 'Session access denied' } });
      }
      return route.fulfill({ json: [] });
    }
    if (reqPath.endsWith(`/sessions/${SESSION_ID}/lineage`)) return route.fulfill({ json: [managedSessionRow] });
    if (reqPath.endsWith(`/sessions/${SESSION_ID}/branches`)) return route.fulfill({ json: [] });
    if (reqPath.endsWith(`/sessions/${SESSION_ID}/runs/active`)) return route.fulfill({ status: 404, json: { detail: 'No active run' } });
    if (reqPath.endsWith(`/chat/sessions/${SESSION_ID}/runtime-summary`)) {
      return route.fulfill({ json: { activated_tool_groups: [], used_tools: [], blocked_capabilities: [], compaction_count: 0 } });
    }
    if (reqPath.endsWith(`/agents/${HR_AGENT_ID}`)) {
      return route.fulfill({ json: {
        id: HR_AGENT_ID,
        name: 'HR Agent',
        status: 'running',
        agent_type: 'native',
        agent_class: 'internal_system',
        role_description: 'HR onboarding',
        access_level: 'manage',
        is_owner: false,
        action_capabilities: { can_use: true, can_manage: true, can_manage_permissions: true },
        created_at: '2026-01-01T00:00:00Z',
      } });
    }
    if (reqPath.endsWith(`/agents/${HR_AGENT_ID}/sessions`) && method === 'POST') {
      if (hrSessionDeny) {
        return route.fulfill({ status: hrSessionDeny.status, json: { detail: hrSessionDeny.detail } });
      }
      return route.fulfill({ json: { id: HR_SESSION_ID, agent_id: HR_AGENT_ID, title: 'New Conversation', created_at: '2026-09-05T11:00:00Z', updated_at: '2026-09-05T11:00:00Z' } });
    }
    if (reqPath === '/api/users/' || reqPath.startsWith('/api/users/?')) {
      return route.fulfill({ json: [{
        id: 'u-member', username: 'employee', display_name: 'E2E Employee', email: 'employee@example.com',
        role: 'member', is_active: true, created_at: '2026-01-02T00:00:00Z', source: 'registered',
        agents_count: 1, tokens_used_today: 0, tokens_used_month: 0,
      }] });
    }
    if (reqPath.includes('/external-principals')) return route.fulfill({ json: [] });
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(path);
  return { scopedCalls };
}

test('scoped administrator opens an employee-private Session as a normal audited business actor', async ({ page }) => {
  const { scopedCalls } = await bootstrap(page, { role: 'org_admin', path: `/agents/${AGENT_ID}#chat` });

  // The managed inventory browser is available without any operator reason UI.
  const browser = page.getByTestId('detail-session-browser');
  await expect(browser).toBeVisible();
  await expect(browser.getByText('Payroll reconciliation thread')).toBeVisible();
  await expect(browser.getByText(/E2E Employee/)).toBeVisible();
  await expect(page.getByLabel('Operator inspection reason')).toHaveCount(0);
  // The listing call carried no fabricated operator authority.
  await expect.poll(() => scopedCalls.length).toBeGreaterThan(0);
  for (const call of scopedCalls) {
    expect(call).not.toContain('operator_reason');
    expect(call).not.toContain('operator_view');
  }

  // Opening the managed row presents the truthful read-only business view with
  // the real owner identified (the detail browser selection intentionally keeps
  // the address bar on the Agent page).
  await browser.getByText('Payroll reconciliation thread').click();
  await expect(page.getByTestId('session-workbench')).toBeVisible();
  await expect(page.getByText('Read-only · E2E Employee')).toBeVisible();
  await expect(page.getByText('Reconcile the September payroll export.')).toBeVisible();
  await expect(page.getByTestId('session-operator-view')).toHaveCount(0);

  // The canonical direct URL renders the same server-backed view…
  await page.goto(`/agents/${AGENT_ID}/sessions/${SESSION_ID}`);
  await expect(page.getByTestId('session-workbench')).toBeVisible();
  await expect(page.getByText('Read-only · E2E Employee')).toBeVisible();
  await expect(page.getByText('Reconcile the September payroll export.')).toBeVisible();
  await expect(page.getByTestId('session-operator-view')).toHaveCount(0);

  // …and a reload keeps it — no cached authority drift.
  await page.reload();
  await expect(page.getByTestId('session-workbench')).toBeVisible();
  await expect(page.getByText('Read-only · E2E Employee')).toBeVisible();
  await expect(page.getByTestId('session-operator-view')).toHaveCount(0);
  for (const call of scopedCalls) {
    expect(call).not.toContain('operator_reason');
  }
});

test('employee direct URL to a foreign Session is denied and stays denied on reload', async ({ page }) => {
  const { scopedCalls } = await bootstrap(page, {
    role: 'member',
    path: `/agents/${AGENT_ID}/sessions/${SESSION_ID}`,
    sessionDenyStatus: 403,
  });

  const denial = page.getByTestId('session-route-error');
  await expect(denial).toBeVisible();
  await expect(denial).toContainText('Session access denied');
  // The member never enters the all-users inventory lane and never sees an
  // operator control.
  expect(scopedCalls).toHaveLength(0);
  await expect(page.getByLabel('Operator inspection reason')).toHaveCount(0);
  await expect(page.getByText('Reconcile the September payroll export.')).toHaveCount(0);

  await page.reload();
  await expect(page.getByTestId('session-route-error')).toBeVisible();
  await expect(denial).toContainText('Session access denied');
  expect(scopedCalls).toHaveLength(0);
});

test('platform administrator without a selected company gets an explicit company selector on the HR path', async ({ page }) => {
  await bootstrap(page, { role: 'platform_admin', tenantless: true, path: '/agents/new' });

  // Plain, actionable selection — not a generic retry loop.
  await expect(page.getByText('Select a company first')).toBeVisible();
  await expect(page.getByRole('button', { name: /Use HR Agent for guided creation/i })).toHaveCount(0);
  // The sidebar keeps the company business entries for a platform administrator.
  await expect(page.locator('nav.sidebar').getByRole('link', { name: 'Agent Circle' })).toBeVisible();

  await page.getByRole('combobox', { name: 'Company', exact: true }).selectOption('t-1');
  await page.getByRole('button', { name: /Continue with this company/i }).click();

  // The canonical HR conversational flow opens in the selected company.
  await expect(page).toHaveURL(new RegExp(`/agents/${HR_AGENT_ID}\\?session_id=${HR_SESSION_ID}`));
  const tenant = await page.evaluate(() => localStorage.getItem('current_tenant_id'));
  expect(tenant).toBe('t-1');
});

test('platform administrator reaches the company member management surface in the selected company', async ({ page }) => {
  await bootstrap(page, { role: 'platform_admin', path: '/enterprise/users' });

  await expect(page.getByText('E2E Employee')).toBeVisible();
  // Both administrator roles get the existing role-assignment control;
  // the server stays authoritative for the mutation.
  await expect(page.locator('.user-mgmt-role-select')).toBeVisible();
  await expect(page.locator('nav.sidebar').getByRole('link', { name: 'Back to App' })).toBeVisible();
});

test('administrator sees managed labels instead of company-shared on employee-private Agents', async ({ page }) => {
  await bootstrap(page, { role: 'org_admin', path: '/agents' });

  const card = page.locator('.employee-card', { hasText: 'Payroll Clerk' });
  await expect(card).toBeVisible();
  await expect(card.getByText('Managed')).toBeVisible();
  await expect(card.getByText('Company shared')).toHaveCount(0);
  // Sidebar tree badge agrees with the directory chip.
  const sidebarRow = page.locator('nav.sidebar .sidebar-agent-item', { hasText: 'Payroll Clerk' });
  await expect(sidebarRow.getByText('Managed')).toBeVisible();
});

test('managed session browser and company picker have no serious accessibility violations', async ({ page }) => {
  await bootstrap(page, { role: 'org_admin', path: `/agents/${AGENT_ID}#chat` });
  await expect(page.getByTestId('detail-session-browser')).toBeVisible();
  const browserScan = await new AxeBuilder({ page })
    .include('[data-testid="detail-session-browser"]')
    .withTags(['wcag2a', 'wcag2aa'])
    .analyze();
  const browserBlocking = browserScan.violations
    .filter((violation) => violation.impact === 'critical' || violation.impact === 'serious')
    .map((violation) => violation.id);
  expect(browserBlocking).toEqual([]);

  await bootstrap(page, { role: 'platform_admin', tenantless: true, path: '/agents/new' });
  await expect(page.locator('.platform-company-picker')).toBeVisible();
  const pickerScan = await new AxeBuilder({ page })
    .include('.platform-company-picker')
    .withTags(['wcag2a', 'wcag2aa'])
    .analyze();
  const pickerBlocking = pickerScan.violations
    .filter((violation) => violation.impact === 'critical' || violation.impact === 'serious')
    .map((violation) => violation.id);
  expect(pickerBlocking).toEqual([]);
});

test('an invalid selected company on cold start recovers to the active-company selector without logging out', async ({ page }) => {
  const user = { id: 'u-admin', username: 'e2e', display_name: 'E2E Admin', role: 'platform_admin', tenant_id: null };
  const authTenantHeaders: Array<string | undefined> = [];
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', 'en');
    localStorage.setItem('theme', 'light');
    // Seed the stale selection only on the first load so a later valid
    // selection survives the reload below.
    if (!localStorage.getItem('current_tenant_id')) {
      localStorage.setItem('current_tenant_id', 'disabled-tenant');
    }
  });
  await page.route('**/api/**', (route) => {
    const request = route.request();
    const reqPath = new URL(request.url()).pathname;
    if (!reqPath.startsWith('/api/')) return route.fallback();
    if (reqPath.endsWith('/auth/me')) {
      // The bearer itself is valid; only the stale/disabled selection is
      // rejected — the typed X-Tenant-Id refusal the real backend answers.
      authTenantHeaders.push(request.headers()['x-tenant-id']);
      return request.headers()['x-tenant-id'] === 'disabled-tenant'
        ? route.fulfill({ status: 403, json: { detail: 'Target tenant is disabled' } })
        : route.fulfill({ json: user });
    }
    if (reqPath.endsWith('/admin/companies')) {
      return route.fulfill({ json: [{ id: 't-1', name: 'Acme Corp', slug: 'acme', is_active: true, created_at: '2026-01-01T00:00:00Z' }] });
    }
    if (request.method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto('/agents');

  // Still authenticated, still in the app: only the invalid selection is gone.
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByText('Sign In')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('token'))).toBe('e2e-token');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('current_tenant_id'))).toBeNull();
  // Exactly one rejected validation with the stale header, then the
  // bearer-only revalidation — no logout, no repeated selection attempts.
  expect(authTenantHeaders).toEqual(['disabled-tenant', undefined]);

  // The existing sidebar selector is the recovery surface and lists only the
  // active company — nothing is selected automatically.
  const selector = page.locator('nav.sidebar .sidebar-workspace-select');
  await expect(selector).toBeVisible();
  await expect(selector).toHaveValue('');
  await expect(selector.locator('option')).toHaveText(['Select a company…', 'Acme Corp']);

  // Choosing the active company survives a reload: a valid selection is
  // preserved and never silently replaced.
  await selector.selectOption('t-1');
  await page.reload();
  await expect(page).toHaveURL(/\/agents$/);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('current_tenant_id'))).toBe('t-1');
  await expect(page.locator('nav.sidebar .sidebar-workspace-name')).toHaveText('Acme Corp');
});

test('an explicitly invalid selection never silently falls back to the platform admin home tenant', async ({ page }) => {
  const user = { id: 'u-admin', username: 'e2e', display_name: 'E2E Admin', role: 'platform_admin', tenant_id: 'home-tenant' };
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', 'en');
    localStorage.setItem('theme', 'light');
    localStorage.setItem('current_tenant_id', 'stale-tenant');
  });
  await page.route('**/api/**', (route) => {
    const request = route.request();
    const reqPath = new URL(request.url()).pathname;
    if (!reqPath.startsWith('/api/')) return route.fallback();
    if (reqPath.endsWith('/auth/me')) {
      return request.headers()['x-tenant-id']
        ? route.fulfill({ status: 404, json: { detail: 'Target tenant no longer exists' } })
        : route.fulfill({ json: user });
    }
    if (reqPath.endsWith('/admin/companies')) {
      return route.fulfill({ json: [
        { id: 'home-tenant', name: 'Home Company', slug: 'home', is_active: true, created_at: '2026-01-01T00:00:00Z' },
        { id: 'other-tenant', name: 'Other Company', slug: 'other', is_active: true, created_at: '2026-01-01T00:00:00Z' },
      ] });
    }
    if (request.method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto('/agents');

  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByText('Sign In')).toHaveCount(0);
  // The invalidated explicit selection is cleared and NOT replaced by the
  // admin's home tenant — no silent different company.
  await expect.poll(() => page.evaluate(() => localStorage.getItem('current_tenant_id'))).toBeNull();
  const selector = page.locator('nav.sidebar .sidebar-workspace-select');
  await expect(selector).toBeVisible();
  await expect(selector).toHaveValue('');
  await expect(selector.locator('option')).toHaveText(['Select a company…', 'Home Company', 'Other Company']);
});

test('clearing the selected company in another tab clears the platform-admin shell instead of reviving the home tenant', async ({ page, context }) => {
  const user = { id: 'u-admin', username: 'e2e', display_name: 'E2E Admin', role: 'platform_admin', tenant_id: 'home-tenant' };
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', 'en');
    localStorage.setItem('theme', 'light');
    localStorage.setItem('current_tenant_id', 'home-tenant');
  });
  await context.route('**/api/**', (route) => {
    const request = route.request();
    const reqPath = new URL(request.url()).pathname;
    if (!reqPath.startsWith('/api/')) return route.fallback();
    if (reqPath.endsWith('/auth/me')) return route.fulfill({ json: user });
    if (reqPath.endsWith('/admin/companies')) {
      return route.fulfill({ json: [
        { id: 'home-tenant', name: 'Home Company', slug: 'home', is_active: true, created_at: '2026-01-01T00:00:00Z' },
        { id: 'other-tenant', name: 'Other Company', slug: 'other', is_active: true, created_at: '2026-01-01T00:00:00Z' },
      ] });
    }
    if (request.method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto('/agents');
  const selector = page.locator('nav.sidebar .sidebar-workspace-select');
  await expect(selector).toHaveValue('home-tenant');

  // The authoritative channel clears the selection from another tab…
  const secondTab = await context.newPage();
  await secondTab.goto('/login');
  await secondTab.evaluate(() => localStorage.removeItem('current_tenant_id'));

  // …and this shell follows it to the empty selection — the platform admin's
  // home tenant is never silently revived in React state.
  await expect.poll(() => page.evaluate(() => localStorage.getItem('current_tenant_id'))).toBeNull();
  await expect(selector).toHaveValue('');
  await expect(selector.locator('option')).toHaveText(['Select a company…', 'Home Company', 'Other Company']);
  await secondTab.close();
});

test('a successful empty company inventory says plainly that no active company is available', async ({ page }) => {
  const user = { id: 'u-admin', username: 'e2e', display_name: 'E2E Admin', role: 'platform_admin', tenant_id: null };
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', 'en');
    localStorage.setItem('theme', 'light');
    localStorage.removeItem('current_tenant_id');
  });
  await page.route('**/api/**', (route) => {
    const request = route.request();
    const reqPath = new URL(request.url()).pathname;
    if (!reqPath.startsWith('/api/')) return route.fallback();
    if (reqPath.endsWith('/auth/me')) return route.fulfill({ json: user });
    if (reqPath.endsWith('/admin/companies')) return route.fulfill({ json: [] });
    if (request.method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto('/agents');

  // Zero companies came back successfully: say that plainly instead of showing
  // a pending-selection prompt, and do not offer a dead selector.
  await expect(page.locator('nav.sidebar .sidebar-workspace-name')).toHaveText('No active company available');
  await expect(page.locator('nav.sidebar .sidebar-workspace-select')).toHaveCount(0);
  // Platform control stays reachable through the Settings menu.
  await page.locator('nav.sidebar').getByRole('button', { name: 'Settings' }).click();
  await expect(page.locator('nav.sidebar').getByRole('link', { name: 'Platform Settings' })).toBeVisible();
});

test('an HR session authorization denial stays an authorization error, not company re-selection', async ({ page }) => {
  await bootstrap(page, {
    role: 'platform_admin',
    path: '/agents/new',
    hrSessionDeny: { status: 403, detail: 'No access to this agent' },
  });

  await page.getByRole('button', { name: /Use HR Agent for guided creation/i }).click();

  // The typed per-Agent denial is shown as itself — never disguised as a
  // company-selection problem and never looped back into the picker.
  await expect(page.getByRole('alert')).toContainText('No access to this agent');
  await expect(page.locator('.platform-company-picker')).toHaveCount(0);
  await expect(page.getByText('Select a company first')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Use HR Agent for guided creation/i })).toBeVisible();
});

test('a genuinely expired bearer still logs out to the sign-in page', async ({ page }) => {
  await page.addInitScript(() => {
    // Arm the expired token only outside the login page so the post-logout
    // navigation does not re-arm it.
    if (window.location.pathname.startsWith('/login')) return;
    localStorage.setItem('token', 'e2e-expired-token');
    localStorage.setItem('i18nextLng', 'en');
  });
  await page.route('**/api/**', (route) => {
    const reqPath = new URL(route.request().url()).pathname;
    if (!reqPath.startsWith('/api/')) return route.fallback();
    if (reqPath.endsWith('/auth/me')) {
      return route.fulfill({ status: 401, json: { detail: 'Not authenticated' } });
    }
    if (route.request().method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto('/agents');

  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole('button', { name: 'Sign In' })).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('token'))).toBeNull();
});

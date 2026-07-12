import { expect, test, type Page, type Route } from '@playwright/test';


const AGENT_ID = '7e57a9e7-0000-4000-8000-000000000027';

type MockTask = Record<string, unknown> & {
  id: string;
  request_id: string;
  title: string;
  status: string;
  actions: { can_cancel: boolean; can_retry: boolean; can_reconcile: boolean };
};

function task(overrides: Partial<MockTask> = {}): MockTask {
  return {
    id: 'task-27',
    agent_id: AGENT_ID,
    title: 'Prepare board report',
    description: null,
    type: 'todo',
    status: 'pending',
    priority: 'medium',
    assignee: 'self',
    created_by: 'user-1',
    request_id: 'business-create-browser',
    request_hash: 'request-hash',
    execution_attempt: 1,
    created_at: '2026-07-12T00:00:00Z',
    updated_at: '2026-07-12T00:00:00Z',
    runtime_status: 'pending',
    runtime_phase: 'queued',
    runtime_request_id: 'business-create-browser',
    recovery_state: 'none',
    recovery_message: null,
    actions: { can_cancel: true, can_retry: false, can_reconcile: false },
    dependencies: [],
    stages: [
      { id: 'accepted', label: 'Assignment accepted', status: 'complete' },
      { id: 'authorized', label: 'Execution authorized', status: 'complete' },
      { id: 'queued', label: 'Durable run queued', status: 'complete' },
      { id: 'executing', label: 'Agent execution', status: 'pending' },
      { id: 'terminal', label: 'Final outcome', status: 'pending' },
    ],
    ...overrides,
  };
}

async function authenticate(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'business-task-e2e-token');
    localStorage.setItem(
      'auth-storage',
      JSON.stringify({
        state: {
          token: 'business-task-e2e-token',
          user: { id: 'user-1', username: 'owner', display_name: 'Owner', role: 'admin', tenant_id: 'tenant-1' },
        },
        version: 0,
      }),
    );
  });
}

async function commonRoute(route: Route): Promise<boolean> {
  const url = new URL(route.request().url());
  if (!url.pathname.startsWith('/api/')) {
    await route.fallback();
    return true;
  }
  if (url.pathname.endsWith('/auth/me')) {
    await route.fulfill({
      json: {
        id: 'user-1',
        username: 'owner',
        email: 'owner@example.com',
        display_name: 'Owner',
        role: 'admin',
        tenant_id: 'tenant-1',
      },
    });
    return true;
  }
  if (url.pathname.endsWith(`/agents/${AGENT_ID}`) && route.request().method() === 'GET') {
    await route.fulfill({
      json: {
        id: AGENT_ID,
        name: 'Business Task Agent',
        status: 'idle',
        agent_type: 'native',
        access_level: 'manage',
        role_description: 'Executes governed business assignments.',
      },
    });
    return true;
  }
  return false;
}

test('business assignment survives reload and starts exactly once from its confirmed plan', async ({ page }) => {
  await authenticate(page);
  let tasks: MockTask[] = [];
  let plans: Array<Record<string, unknown>> = [];
  const planBodies: Array<Record<string, unknown>> = [];
  const createBodies: Array<Record<string, unknown>> = [];

  await page.route('**/api/**', async (route) => {
    if (await commonRoute(route)) return;
    const url = new URL(route.request().url());
    const method = route.request().method();
    const tasksPath = `/api/agents/${AGENT_ID}/tasks/`;
    const plansPath = `/api/agents/${AGENT_ID}/plans`;

    if (url.pathname === tasksPath && method === 'GET') return route.fulfill({ json: tasks });
    if (url.pathname === plansPath && method === 'GET') return route.fulfill({ json: plans });
    if (url.pathname === plansPath && method === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}') as Record<string, unknown>;
      planBodies.push(body);
      plans = [{
        id: 'plan-27',
        agent_id: AGENT_ID,
        tenant_id: 'tenant-1',
        session_id: null,
        runtime_task_id: null,
        requested_by_user_id: 'user-1',
        source: 'business_task_workbench',
        intent_type: 'long_task',
        original_request: body.original_request,
        status: 'awaiting_confirmation',
        plan_version: 1,
        plan_hash: 'sha256-plan-27',
        plan_markdown_path: null,
        plan_json: {
          title: 'Board report execution plan',
          plan_markdown: '1. Verify figures\n2. Deliver the report',
          authorization_scopes: (body.fill as Record<string, unknown>).authorization_scopes,
        },
        handoff_status: 'not_started',
        handoff_payload: null,
        confirmed_by_user_id: null,
        confirmed_at: null,
        rejected_by_user_id: null,
        rejected_at: null,
        superseded_by_plan_id: null,
        expires_at: null,
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:00:00Z',
        metadata: body.metadata,
      }];
      return route.fulfill({ json: plans[0] });
    }
    if (url.pathname === `${plansPath}/plan-27/confirm` && method === 'POST') {
      plans[0] = { ...plans[0], status: 'confirmed' };
      return route.fulfill({ json: { ok: true, status: 'confirmed', plan_id: 'plan-27' } });
    }
    if (url.pathname === tasksPath && method === 'POST') {
      const body = JSON.parse(route.request().postData() || '{}') as Record<string, unknown>;
      createBodies.push(body);
      tasks = [task({ request_id: String(body.request_id), title: String(body.title) })];
      return route.fulfill({ json: tasks[0] });
    }
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#tasks`);
  await page.getByLabel('Title', { exact: true }).fill('Prepare board report');
  await page.getByRole('button', { name: 'Prepare execution plan' }).click();
  await expect(page.getByTestId('business-task-plan-queue')).toContainText('Board report execution plan');

  await page.reload();
  await expect(page.getByTestId('business-task-plan-queue')).toContainText('Needs confirmation');
  await page.getByRole('button', { name: 'Confirm and start' }).click();

  await expect(page.getByText('Prepare board report', { exact: true })).toBeVisible();
  expect(planBodies).toHaveLength(1);
  const scope = ((planBodies[0].fill as Record<string, unknown>).authorization_scopes as Array<Record<string, unknown>>)[0];
  expect(scope.arguments).toMatchObject({
    title: 'Prepare board report',
    description: null,
    type: 'todo',
    priority: 'medium',
    due_date: null,
  });
  expect(createBodies).toHaveLength(1);
  expect(createBodies[0]).toMatchObject({
    title: 'Prepare board report',
    description: null,
    confirmed_plan_id: 'plan-27',
    confirmed_plan_version: 1,
    confirmed_plan_hash: 'sha256-plan-27',
  });
  await expect(page.getByRole('button', { name: 'Confirm and start' })).toHaveCount(0);
});

test('running cancellation becomes an explicit reconciliation instead of a blind retry', async ({ page }) => {
  await authenticate(page);
  let active = task({
    status: 'doing',
    runtime_status: 'running',
    runtime_phase: 'invoking',
    stages: [{ id: 'executing', label: 'Agent execution', status: 'current' }],
  });
  const reconciliationBodies: Array<Record<string, unknown>> = [];

  await page.route('**/api/**', async (route) => {
    if (await commonRoute(route)) return;
    const url = new URL(route.request().url());
    const method = route.request().method();
    const taskPath = `/api/agents/${AGENT_ID}/tasks/${active.id}`;
    if (url.pathname === `/api/agents/${AGENT_ID}/tasks/` && method === 'GET') {
      return route.fulfill({ json: [active] });
    }
    if (url.pathname === `/api/agents/${AGENT_ID}/plans` && method === 'GET') {
      return route.fulfill({ json: [] });
    }
    if (url.pathname === `${taskPath}/cancel` && method === 'POST') {
      active = task({
        status: 'needs_reconciliation',
        runtime_status: 'needs_reconciliation',
        runtime_phase: 'terminal',
        recovery_state: 'needs_review',
        recovery_message: 'Execution may have crossed an external side-effect boundary.',
        actions: { can_cancel: false, can_retry: false, can_reconcile: true },
      });
      return route.fulfill({ json: { task: active, logs: [] } });
    }
    if (url.pathname === `${taskPath}/reconcile` && method === 'POST') {
      reconciliationBodies.push(JSON.parse(route.request().postData() || '{}') as Record<string, unknown>);
      active = task({
        status: 'failed',
        runtime_status: 'failed',
        runtime_phase: 'terminal',
        recovery_state: 'retry_available',
        actions: { can_cancel: false, can_retry: true, can_reconcile: false },
      });
      return route.fulfill({ json: { task: active, logs: [] } });
    }
    if (url.pathname === taskPath && method === 'GET') return route.fulfill({ json: { task: active, logs: [] } });
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#tasks`);
  await page.getByText('Stop assignment', { exact: true }).click();
  await page.getByPlaceholder('Reason (optional)').fill('Stop before publishing');
  await page.getByRole('button', { name: 'Stop safely' }).click();

  await expect(page.getByText('Review required', { exact: true })).toBeVisible();
  await expect(page.getByText('Review possible side effects', { exact: true })).toBeVisible();
  await page.getByPlaceholder('What did you verify?').fill('No external message or file was published.');
  await page.getByRole('button', { name: 'Record decision' }).click();

  await expect(page.getByRole('button', { name: 'Prepare retry plan' })).toBeVisible();
  expect(reconciliationBodies).toEqual([{
    decision: 'retry_safe',
    reason: 'No external message or file was published.',
  }]);
});

test('authorization denial is visible and never masquerades as an empty assignment list', async ({ page }) => {
  await authenticate(page);
  await page.route('**/api/**', async (route) => {
    if (await commonRoute(route)) return;
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.pathname === `/api/agents/${AGENT_ID}/tasks/` && method === 'GET') return route.fulfill({ json: [] });
    if (url.pathname === `/api/agents/${AGENT_ID}/plans` && method === 'GET') return route.fulfill({ json: [] });
    if (url.pathname === `/api/agents/${AGENT_ID}/plans` && method === 'POST') {
      return route.fulfill({ status: 403, json: { detail: 'You do not have permission to assign work to this Agent.' } });
    }
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#tasks`);
  await page.getByLabel('Title', { exact: true }).fill('Forbidden assignment');
  await page.getByRole('button', { name: 'Prepare execution plan' }).click();

  await expect(page.getByRole('alert')).toContainText('You do not have permission to assign work to this Agent.');
  await expect(page.getByTestId('business-task-plan-queue')).toHaveCount(0);
});

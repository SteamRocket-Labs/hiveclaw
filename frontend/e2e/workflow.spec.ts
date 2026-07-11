/**
 * §9 P12 E2E: workflow product surface — browser-level flows on the real Vite
 * app with the /api surface route-mocked per test (deterministic, backend-free;
 * full-stack E2E belongs to P15's deployment validation).
 *
 * Flow 1 (low risk):  paste definition → preview (low) → confirm → run completes.
 * Flow 2 (confirmation required): preview surfaces reasons → the explicit
 *                                Confirm and run action starts that immutable preview.
 */

import { expect, test, type Page } from '@playwright/test';

const AGENT_ID = '7e57a9e7-0000-4000-8000-000000000001';
const RUN_ID = 'a0a0a0a0-0000-4000-8000-00000000feed';

const LOW_RISK_DEFINITION = JSON.stringify({
  name: 'contract-review',
  args_schema: {},
  steps: [{ id: 'scan', type: 'agent_step', leaf: { name: 'scanner', type: 'explorer' }, task: 'Scan docs' }],
});

async function bootstrapApp(page: Page, options: { risk: 'low' | 'high' }) {
  const startBodies: Array<Record<string, unknown>> = [];
  // Authenticated session without a backend.
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
    // The glob also matches Vite module URLs like /src/api/domains/x.ts —
    // only the backend surface (paths starting with /api/) is mocked.
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
          name: 'E2E Agent',
          status: 'idle',
          agent_type: 'native',
          access_level: 'manage',
          role_description: 'tester',
        },
      });
    }
    if (path.endsWith('/workflows/preview') && method === 'POST') {
      const confirmationRequired = options.risk === 'high';
      return route.fulfill({
        json: {
          preview_id: confirmationRequired ? 'preview-high' : 'preview-low',
          session_id: 'session-workflow-e2e',
          preview_status: 'ready',
          artifact_version: 1,
          artifact_hash: confirmationRequired ? 'artifact-high' : 'artifact-low',
          definition_hash: confirmationRequired ? 'hash-high' : 'hash-low',
          args_hash: 'args-empty',
          confirmation_required: confirmationRequired,
          confirmation_reasons: confirmationRequired ? ["step 'send' has external effects"] : [],
          planned_leaf_calls: confirmationRequired ? 4 : 1,
          budget_tokens: confirmationRequired ? 900000 : 50000,
        },
      });
    }
    if (path.endsWith('/workflows/runs') && method === 'POST') {
      startBodies.push(JSON.parse(route.request().postData() || '{}') as Record<string, unknown>);
      return route.fulfill({
        json: { run_id: RUN_ID, status: 'completed', reason: null, definition_hash: 'hash-low', risk: 'low' },
      });
    }
    if (path.includes(`/workflows/runs/${RUN_ID}`) && method === 'GET') {
      return route.fulfill({
        json: {
          run_id: RUN_ID,
          status: 'completed',
          definition_hash: 'hash-low',
          definition_source: 'ephemeral',
          steps: [{ step_id: 'scan', step_type: 'agent_step', status: 'done', error: null }],
        },
      });
    }
    if (path.endsWith('/workflow-definitions') && method === 'GET') {
      return route.fulfill({ json: [] });
    }
    // Everything else the page asks for: harmless empty payloads.
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#workflows`);
  return { startBodies };
}

test('low-risk ephemeral: preview → confirm → run completes', async ({ page }) => {
  const { startBodies } = await bootstrapApp(page, { risk: 'low' });

  // The manual JSON flow now lives behind the advanced toggle (asset-view IA).
  await page.getByTestId('workflow-advanced-toggle').click();
  await page.getByTestId('workflow-definition-input').fill(LOW_RISK_DEFINITION);
  await page.getByTestId('workflow-preview-button').click();

  const previewCard = page.getByTestId('workflow-preview-card');
  await expect(previewCard).toBeVisible();
  await expect(previewCard).toContainText('No extra confirmation');
  await expect(previewCard).toContainText('1 planned worker call');

  const startButton = page.getByTestId('workflow-start-button');
  await expect(startButton).toBeEnabled();
  await startButton.click();

  const runPanel = page.getByTestId('workflow-run-panel');
  await expect(runPanel).toBeVisible();
  await expect(runPanel).toContainText('completed');
  await expect(page.getByTestId('workflow-step-scan')).toContainText('done');
  expect(startBodies).toEqual([{
    preview_id: 'preview-low',
    confirmed_plan_id: null,
    ledger_todo_id: null,
    plan_version: null,
    plan_hash: null,
  }]);
});

test('confirmation-required ephemeral: explicit action starts the immutable preview', async ({ page }) => {
  const { startBodies } = await bootstrapApp(page, { risk: 'high' });

  await page.getByTestId('workflow-advanced-toggle').click();
  await page.getByTestId('workflow-definition-input').fill(LOW_RISK_DEFINITION);
  await page.getByTestId('workflow-preview-button').click();

  await expect(page.getByTestId('workflow-preview-card')).toBeVisible();
  await expect(page.getByTestId('workflow-plan-required')).toBeVisible();
  await expect(page.getByTestId('workflow-plan-required')).toContainText('external effects');
  const confirmButton = page.getByTestId('workflow-start-button');
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();
  await expect(page.getByTestId('workflow-run-panel')).toContainText('completed');
  expect(startBodies).toEqual([{
    preview_id: 'preview-high',
    confirmed_plan_id: null,
    ledger_todo_id: null,
    plan_version: null,
    plan_hash: null,
  }]);
});

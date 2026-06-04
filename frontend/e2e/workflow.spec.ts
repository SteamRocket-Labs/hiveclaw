/**
 * §9 P12 E2E: workflow product surface — browser-level flows on the real Vite
 * app with the /api surface route-mocked per test (deterministic, backend-free;
 * full-stack E2E belongs to P15's deployment validation).
 *
 * Flow 1 (low risk):  paste definition → preview (low) → confirm → run completes.
 * Flow 2 (high risk): preview surfaces HIGH risk → start stays disabled and the
 *                     Plan-Mode-required notice is shown (fail-closed UX).
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
      return route.fulfill({
        json:
          options.risk === 'low'
            ? { definition_hash: 'hash-low', risk: 'low', risk_reasons: [], planned_leaf_calls: 1, budget_tokens: 50000 }
            : {
                definition_hash: 'hash-high',
                risk: 'high',
                risk_reasons: ["step 'send' has external effects"],
                planned_leaf_calls: 4,
                budget_tokens: 900000,
              },
      });
    }
    if (path.endsWith('/workflows/runs') && method === 'POST') {
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
}

test('low-risk ephemeral: preview → confirm → run completes', async ({ page }) => {
  await bootstrapApp(page, { risk: 'low' });

  await page.getByTestId('workflow-definition-input').fill(LOW_RISK_DEFINITION);
  await page.getByTestId('workflow-preview-button').click();

  const previewCard = page.getByTestId('workflow-preview-card');
  await expect(previewCard).toBeVisible();
  await expect(previewCard).toContainText('hash-low'.slice(0, 8));

  const startButton = page.getByTestId('workflow-start-button');
  await expect(startButton).toBeEnabled();
  await startButton.click();

  const runPanel = page.getByTestId('workflow-run-panel');
  await expect(runPanel).toBeVisible();
  await expect(runPanel).toContainText('completed');
  await expect(page.getByTestId('workflow-step-scan')).toContainText('done');
});

test('high-risk ephemeral: start is blocked behind Plan Mode', async ({ page }) => {
  await bootstrapApp(page, { risk: 'high' });

  await page.getByTestId('workflow-definition-input').fill(LOW_RISK_DEFINITION);
  await page.getByTestId('workflow-preview-button').click();

  await expect(page.getByTestId('workflow-preview-card')).toBeVisible();
  await expect(page.getByTestId('workflow-plan-required')).toBeVisible();
  await expect(page.getByTestId('workflow-plan-required')).toContainText('external effects');
  await expect(page.getByTestId('workflow-start-button')).toBeDisabled();
});

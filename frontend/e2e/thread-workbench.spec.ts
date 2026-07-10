import { expect, test, type Page } from '@playwright/test';

const AGENT_ID = '7e57a9e7-0000-4000-8000-000000000010';
const SESSION_ID = '8e57a9e7-0000-4000-8000-000000000020';

function threadItem(
  sequence: number,
  itemType: string,
  eventType: string,
  content: string,
  itemData: Record<string, unknown>,
  status = 'succeeded',
) {
  return {
    schema: 'hive.thread_item.v1',
    schema_version: 1,
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, '0')}`,
    sequence,
    thread_id: SESSION_ID,
    session_id: SESSION_ID,
    run_id: '9e57a9e7-0000-4000-8000-000000000030',
    turn_id: `turn-${sequence}`,
    correlation_id: '9e57a9e7-0000-4000-8000-000000000030',
    item_type: itemType,
    item_status: status,
    actor_type: itemType === 'user_message' ? 'user' : 'agent',
    event_type: eventType,
    type: eventType,
    role: itemType === 'user_message' ? 'user' : itemType === 'agent_message' ? 'assistant' : 'system',
    visibility_scope: 'direct_user',
    listed_surface: 'chat',
    content,
    parts: [],
    metadata: {},
    evidence_refs: [{ kind: 'transcript_event', id: `event-${sequence}` }],
    created_at: `2026-07-10T12:0${sequence}:00Z`,
    item_data: itemData,
  };
}

const SESSION = {
  id: SESSION_ID,
  agent_id: AGENT_ID,
  user_id: 'u-1',
  is_current_user_session: true,
  title: 'Typed workbench review',
  source_channel: 'web',
  listed_surface: 'chat',
  session_kind: 'human_chat',
  permission_mode: 'default',
  created_at: '2026-07-10T12:00:00Z',
  updated_at: '2026-07-10T12:10:00Z',
};

const TRANSCRIPT = [
  threadItem(1, 'user_message', 'user_message', 'Review the release evidence and prepare the final report.', {}),
  threadItem(2, 'agent_message', 'assistant_message', 'I will inspect the evidence, verify the workflow, and keep the result auditable.', {}),
  threadItem(3, 'workflow_activity', 'workflow_started', 'Release verification is running.', {
    workflow_run_id: 'workflow-1',
    workflow_step_id: 'verify',
    runtime_task_id: 'runtime-1',
    label: 'Release verification',
  }, 'running'),
  threadItem(4, 'context_compaction', 'session_compact', 'The active working state was preserved.', {
    original_message_count: 128,
    kept_message_count: 26,
    continuity_sections_injected: ['Task Ledger', 'Current Work'],
  }),
  threadItem(5, 'approval_request', 'permission_request', 'Tool approval is required.', {
    permission_request_id: 'permission-1',
    tool_name: 'write_file',
    tool_display_name: 'Write final report',
    arguments: { path: 'reports/final.md' },
    permission_mode: 'default',
    risk_class: 'controlled_write',
    expires_at: '2026-07-10T12:30:00Z',
    allow_session_allowed: false,
    destructive: false,
  }, 'waiting_user'),
  threadItem(6, 'error', 'runtime_error', 'The provider timed out before the turn completed.', {
    code: 'provider_timeout',
    reason: 'Provider timed out',
    retryable: true,
    retry_reason: 'The preceding user turn is safe to replay.',
  }, 'failed'),
];

async function bootstrap(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', 'en');
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

    if (path.endsWith('/auth/me')) {
      return route.fulfill({ json: { id: 'u-1', username: 'e2e', display_name: 'E2E', role: 'admin', tenant_id: 't-1' } });
    }
    if (path.endsWith(`/agents/${AGENT_ID}`)) {
      return route.fulfill({
        json: {
          id: AGENT_ID,
          name: 'Release Steward',
          status: 'idle',
          agent_type: 'native',
          access_level: 'manage',
          primary_model_id: 'gpt-test',
          role_description: 'Release governance and evidence review',
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}/sessions`) && method === 'GET') return route.fulfill({ json: [SESSION] });
    if (path.includes(`/sessions/${SESSION_ID}/transcript`)) return route.fulfill({ json: TRANSCRIPT });
    if (path.endsWith(`/sessions/${SESSION_ID}/messages`)) return route.fulfill({ json: [] });
    if (path.endsWith(`/sessions/${SESSION_ID}/lineage`)) return route.fulfill({ json: [SESSION] });
    if (path.endsWith(`/sessions/${SESSION_ID}/branches`)) return route.fulfill({ json: [] });
    if (path.endsWith(`/sessions/${SESSION_ID}/index`)) {
      return route.fulfill({
        json: {
          schema: 'hive.session_index.v1',
          thread_id: SESSION_ID,
          session_id: SESSION_ID,
          agent_id: AGENT_ID,
          dynamic_tools: [],
          checkpoints: [],
          event_count: TRANSCRIPT.length,
          t0_segments: [],
          active_projection: null,
          resume_health: { status: 'healthy' },
        },
      });
    }
    if (path.endsWith(`/sessions/${SESSION_ID}/workbench`)) {
      return route.fulfill({
        json: {
          session: SESSION,
          active_run: null,
          runtime_sections: {
            workflows: [{ id: 'workflow-1', runtime_kind: 'workflow', label: 'Release verification', status: 'running' }],
          },
          turn: { checkpoints: [] },
        },
      });
    }
    if (path.endsWith(`/sessions/${SESSION_ID}/runs/active`)) return route.fulfill({ json: null });
    if (path.endsWith(`/chat/sessions/${SESSION_ID}/runtime-summary`)) {
      return route.fulfill({
        json: {
          model: { label: 'GPT Test', provider: 'openai', context_window_tokens: 128000 },
          runtime: { connected: false, estimated_input_tokens: 18400, remaining_tokens_estimate: 109600 },
          activated_tool_groups: [],
          used_tools: ['write_file'],
          blocked_capabilities: [],
          compaction_count: 1,
        },
      });
    }
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#chat`);
  await expect(page.getByTestId('session-workbench')).toBeVisible();
  await expect(page.locator('[data-thread-item-type="approval_request"]')).toBeVisible();
  await expect(page.getByTestId('thread-item-retry-turn')).toBeVisible();
}

async function scrollTimelineToBottom(page: Page) {
  const timeline = page.locator('.session-tui-history');
  await timeline.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect.poll(async () => timeline.evaluate((element) => (
    element.scrollTop + element.clientHeight >= element.scrollHeight - 1
  ))).toBe(true);
}

test('typed session workbench desktop visual contract', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 980 });
  await bootstrap(page);
  await expect(page.getByTestId('thread-item-inspector')).toHaveCount(0);
  await scrollTimelineToBottom(page);
  await expect(page.getByTestId('session-workbench')).toHaveScreenshot('typed-workbench-desktop.png', {
    animations: 'disabled',
    caret: 'hide',
  });
});

test('technical evidence opens only from the explicit details control', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 980 });
  await bootstrap(page);
  const approval = page.locator('[data-thread-item-type="approval_request"]');
  await approval.hover();
  await approval.getByTestId('thread-item-technical-details').click();
  await expect(page.getByTestId('thread-item-inspector')).toBeVisible();
  await expect(page.getByTestId('session-runtime-deliverables')).toBeAttached();
});

test('typed session workbench narrow visual contract', async ({ page }) => {
  await page.setViewportSize({ width: 740, height: 920 });
  await bootstrap(page);
  const bounds = await page.getByTestId('session-workbench').boundingBox();
  expect(bounds?.width).toBeGreaterThan(620);
  await scrollTimelineToBottom(page);
  await expect(page.getByTestId('session-workbench')).toHaveScreenshot('typed-workbench-narrow.png', {
    animations: 'disabled',
    caret: 'hide',
  });
});

import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const AGENT_ID = '7e57a9e7-0000-4000-8000-000000000010';
const SESSION_ID = '8e57a9e7-0000-4000-8000-000000000020';
const RUN_ID = '9e57a9e7-0000-4000-8000-000000000030';

type Audience = 'user' | 'operator';
type Scenario = 'idle' | 'active';
type Theme = 'light' | 'dark';

type ThreadItemOptions = {
  sequence: number;
  itemType: string;
  eventType: string;
  content: string;
  userSummary: string;
  itemData?: Record<string, unknown>;
  status?: string;
  role?: string;
  parts?: Array<Record<string, unknown>>;
  userAction?: Record<string, unknown> | null;
};

function threadItem(audience: Audience, options: ThreadItemOptions) {
  const itemData = options.itemData || {};
  const id = `00000000-0000-4000-8000-${String(options.sequence).padStart(12, '0')}`;
  return {
    schema: 'hive.thread_item.v1',
    schema_version: 1,
    id,
    sequence: options.sequence,
    thread_id: SESSION_ID,
    session_id: SESSION_ID,
    run_id: RUN_ID,
    turn_id: `turn-${options.sequence}`,
    correlation_id: RUN_ID,
    item_type: options.itemType,
    item_status: options.status || 'succeeded',
    actor_type: options.itemType === 'user_message' ? 'user' : 'agent',
    event_type: options.eventType,
    type: options.eventType,
    role: options.role || (options.itemType === 'user_message' ? 'user' : options.itemType === 'agent_message' ? 'assistant' : 'system'),
    visibility_scope: 'direct_user',
    listed_surface: 'chat',
    content: options.content,
    parts: options.parts || [],
    metadata: audience === 'operator' ? { status: options.status || 'succeeded', tool_call_id: `tool-${options.sequence}` } : { status: options.status || 'succeeded' },
    evidence_refs: audience === 'operator' ? [{ kind: 'transcript_event', id: `event-${options.sequence}` }] : [],
    created_at: `2026-07-11T12:${String(options.sequence).padStart(2, '0')}:00Z`,
    item_data: itemData,
    audience,
    user_summary: options.userSummary,
    user_action: options.userAction || null,
    operator_details: audience === 'operator'
      ? {
          item_data: itemData,
          metadata: { status: options.status || 'succeeded', tool_call_id: `tool-${options.sequence}` },
          evidence_refs: [{ kind: 'transcript_event', id: `event-${options.sequence}` }],
          links: { id, session_id: SESSION_ID, run_id: RUN_ID, turn_id: `turn-${options.sequence}` },
        }
      : null,
  };
}

function transcriptFor(audience: Audience, scenario: Scenario) {
  const base = [
    threadItem(audience, {
      sequence: 1,
      itemType: 'user_message',
      eventType: 'user_message',
      content: scenario === 'active'
        ? 'Review the release evidence, coordinate the specialists, and prepare the final report.'
        : 'Summarize what is ready for the next release.',
      userSummary: scenario === 'active'
        ? 'Review the release evidence, coordinate the specialists, and prepare the final report.'
        : 'Summarize what is ready for the next release.',
    }),
    threadItem(audience, {
      sequence: 2,
      itemType: 'agent_message',
      eventType: 'assistant_message',
      content: scenario === 'active'
        ? 'I verified the evidence and prepared a reviewable release report.'
        : 'Everything is ready. Start a new request whenever you need me.',
      userSummary: scenario === 'active'
        ? 'I verified the evidence and prepared a reviewable release report.'
        : 'Everything is ready. Start a new request whenever you need me.',
      parts: scenario === 'active'
        ? [{
            type: 'artifact',
            id: 'artifact-release-report',
            artifact_id: 'artifact-release-report',
            name: 'release-report.md',
            filename: 'release-report.md',
            path: 'workspace/reports/release-report.md',
            mime_type: 'text/markdown',
            preview_kind: 'markdown',
            size: 4096,
            source: 'artifact_delivery',
            runtime_task_id: RUN_ID,
            snapshot_hash: 'sha256-release-report',
            source_agent_name: 'Release Steward',
          }]
        : [],
    }),
  ];
  if (scenario === 'idle') return base;
  return [
    ...base,
    threadItem(audience, {
      sequence: 3,
      itemType: 'plan',
      eventType: 'plan_confirmed',
      content: 'The release plan is confirmed.',
      userSummary: 'The release plan is confirmed.',
      itemData: { plan_id: audience === 'operator' ? 'plan-1' : null, plan_hash: audience === 'operator' ? 'sha256-plan' : null, phase: 'confirmed' },
    }),
    threadItem(audience, {
      sequence: 4,
      itemType: 'workflow_activity',
      eventType: 'workflow_started',
      content: 'Release verification workflow is running.',
      userSummary: 'Workflow: Release verification',
      status: 'running',
      itemData: { workflow_run_id: 'workflow-1', workflow_step_id: 'verify', runtime_task_id: 'runtime-1', label: 'Release verification' },
    }),
    threadItem(audience, {
      sequence: 5,
      itemType: 'subagent_activity',
      eventType: 'subagent_task_started',
      content: 'A one-shot critic is checking the evidence.',
      userSummary: 'Collaborating with Evidence critic',
      status: 'running',
      itemData: { runtime_task_id: 'subagent-1', child_session_id: 'child-session-1', target_agent_name: 'Evidence critic' },
    }),
    threadItem(audience, {
      sequence: 6,
      itemType: 'approval_request',
      eventType: 'permission_request',
      content: 'Approval is required before publishing the final report.',
      userSummary: 'Approval required: Publish final report',
      status: 'waiting_user',
      itemData: {
        permission_request_id: 'permission-1',
        tool_name: 'write_file',
        tool_display_name: 'Publish final report',
        arguments: audience === 'operator' ? { path: 'reports/final.md' } : {},
        permission_mode: audience === 'operator' ? 'default' : null,
        risk_class: audience === 'operator' ? 'controlled_write' : null,
        expires_at: '2026-07-11T12:30:00Z',
        allow_session_allowed: false,
        destructive: false,
      },
      userAction: {
        kind: 'resolve_approval',
        token: 'permission-1',
        label: 'Approve and continue',
        impact: 'Reversible workspace write',
        expires_at: '2026-07-11T12:30:00Z',
        details: [{ label: 'path', value: 'reports/final.md' }],
      },
    }),
    threadItem(audience, {
      sequence: 7,
      itemType: 'error',
      eventType: 'runtime_error',
      content: audience === 'operator' ? 'Provider timeout: request req-internal-7.' : 'The service was temporarily unavailable.',
      userSummary: 'The service was temporarily unavailable. This turn can be retried safely.',
      status: 'failed',
      itemData: {
        code: audience === 'operator' ? 'provider_timeout' : null,
        reason: audience === 'operator' ? 'Provider timed out' : null,
        retryable: true,
        retry_reason: audience === 'operator' ? 'The preceding turn is safe to replay.' : null,
      },
      userAction: { kind: 'retry_turn', label: 'Retry turn', details: [] },
    }),
  ];
}

function sessionFor(audience: Audience) {
  return {
    id: SESSION_ID,
    agent_id: AGENT_ID,
    user_id: audience === 'operator' ? 'u-owner' : 'u-1',
    is_current_user_session: audience === 'user',
    read_only: audience === 'operator',
    authority_source: audience === 'operator' ? 'manager_override' : 'session_owner',
    operator_view: audience === 'operator',
    title: audience === 'operator' ? 'Operator incident review' : 'Release readiness review',
    source_channel: 'web',
    listed_surface: 'chat',
    session_kind: 'human_chat',
    permission_mode: 'default',
    created_at: '2026-07-11T12:00:00Z',
    updated_at: '2026-07-11T12:10:00Z',
  };
}

function lineageFor(session: Record<string, unknown>, scenario: Scenario) {
  if (scenario === 'idle') return [session];
  return [
    session,
    {
      ...session,
      id: '8e57a9e7-0000-4000-8000-000000000021',
      parent_session_id: SESSION_ID,
      root_session_id: SESSION_ID,
      title: 'Evidence comparison branch',
      branch: { branch_mode: 'branch', anchor_event_id: '00000000-0000-4000-8000-000000000001' },
    },
  ];
}

function workbenchFor(session: Record<string, unknown>, audience: Audience, scenario: Scenario) {
  const active = scenario === 'active';
  return {
    schema: 'hive.ccplus.session_workbench.v1',
    audience,
    operator_details_available: true,
    agent_id: AGENT_ID,
    session,
    active_run: active ? { id: RUN_ID, task_type: 'web_chat_turn', status: 'running' } : null,
    runtime_tasks: active ? [{ id: RUN_ID, task_type: 'web_chat_turn', status: 'running', user_status: 'Working' }] : [],
    runtime_sections: active ? {
      agent_teams: [{
        id: 'team-1',
        runtime_kind: 'agent_team',
        label: 'Release review team',
        status: 'running',
        members: [{
          id: 'member-1',
          runtime_kind: 'team_member',
          label: 'Policy reviewer',
          status: 'completed',
          summary: 'Governance checks passed.',
          child_session_id: 'team-member-session-1',
          enterable: true,
        }],
      }],
      subagents: [{
        id: 'subagent-1',
        runtime_kind: 'subagent',
        label: 'Evidence critic',
        status: 'running',
        summary: 'Checking citations and artifact integrity.',
        child_session_id: 'child-session-1',
        enterable: true,
      }],
      workflows: [{
        id: 'workflow-1',
        runtime_kind: 'workflow',
        label: 'Release verification',
        status: 'running',
        summary: '3 of 4 checks complete',
        elapsed_seconds: 125,
        token_count: 4200,
        tool_count: 3,
        steps: [{ id: 'step-1', label: 'Verify deliverables', status: 'running' }],
        leaf_calls: [{ id: 'leaf-1', label: 'Citation check', status: 'completed', enterable: false }],
      }],
      runs: [{ id: RUN_ID, runtime_kind: 'runtime_task', label: 'Release review', status: 'running', elapsed_seconds: 42 }],
    } : {},
    goals: active ? [{ id: 'goal-1', objective: 'Ship a verified release report', status: 'active' }] : [],
    teams: [],
    controls: {},
    turn: { truth_source: 'chat_transcript_events', event_count: active ? 7 : 2, checkpoint_count: active ? 2 : 1 },
  };
}

async function bootstrap(page: Page, options: {
  audience: Audience;
  scenario: Scenario;
  theme?: Theme;
  transcriptDelayMs?: number;
}) {
  const { audience, scenario, theme = 'light', transcriptDelayMs = 0 } = options;
  const session = sessionFor(audience);
  const transcript = transcriptFor(audience, scenario);
  const lineage = lineageFor(session, scenario);
  const consoleErrors: string[] = [];
  let sessionSocketSend: ((payload: string) => void) | null = null;
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });

  await page.addInitScript((initialTheme) => {
    localStorage.setItem('token', 'e2e-token');
    localStorage.setItem('i18nextLng', 'en');
    localStorage.setItem('theme', initialTheme);
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
  }, theme);

  await page.routeWebSocket('**/ws/chat/**', (socket) => {
    socket.onMessage((message) => {
      let payload: Record<string, unknown> | null = null;
      try {
        payload = JSON.parse(String(message)) as Record<string, unknown>;
      } catch {
        payload = null;
      }
      if (payload?.type === 'ping') {
        socket.send(JSON.stringify({ type: 'pong' }));
        return;
      }
      if (payload?.type === 'session.subscribe') {
        sessionSocketSend = (eventPayload) => socket.send(eventPayload);
        socket.send(JSON.stringify({
          type: 'session.ready',
          schema_version: 2,
          session_id: SESSION_ID,
          subscription_id: 'e2e-session-subscription',
          accepted_after_sequence: Number(payload.after_sequence || 0),
          last_committed_sequence: transcript.length,
          connection_attempt_id: payload.connection_attempt_id,
        }));
      }
    });
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
          status: scenario === 'active' ? 'working' : 'idle',
          agent_type: 'native',
          access_level: 'manage',
          primary_model_id: 'gpt-test',
          role_description: 'Release governance and evidence review',
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}/sessions`) && method === 'GET') return route.fulfill({ json: [session] });
    if (path.includes(`/sessions/${SESSION_ID}/transcript`)) {
      if (transcriptDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, transcriptDelayMs));
      return route.fulfill({ json: transcript });
    }
    if (path.endsWith(`/sessions/${SESSION_ID}/messages`)) return route.fulfill({ json: [] });
    if (path.endsWith(`/sessions/${SESSION_ID}/lineage`)) return route.fulfill({ json: lineage });
    if (path.endsWith(`/sessions/${SESSION_ID}/branches`)) return route.fulfill({ json: lineage.slice(1) });
    if (path.endsWith(`/sessions/${SESSION_ID}/index`)) {
      return route.fulfill({
        json: {
          schema: 'hive.session_index.v1',
          thread_id: SESSION_ID,
          session_id: SESSION_ID,
          agent_id: AGENT_ID,
          dynamic_tools: [],
          checkpoints: [
            { checkpoint_event_id: '00000000-0000-4000-8000-000000000001', sequence: 1, role: 'user', content: 'Review the release.' },
            ...(scenario === 'active' ? [{ checkpoint_event_id: '00000000-0000-4000-8000-000000000002', sequence: 2, role: 'assistant', content: 'Report prepared.' }] : []),
          ],
          event_count: transcript.length,
          t0_segments: [],
          active_projection: null,
          resume_health: { status: 'healthy' },
        },
      });
    }
    if (path.endsWith(`/sessions/${SESSION_ID}/workbench`)) {
      return route.fulfill({ json: workbenchFor(session, audience, scenario) });
    }
    if (path.endsWith(`/sessions/${SESSION_ID}/runs/active`)) {
      return route.fulfill({
        json: scenario === 'active'
          ? { run_id: RUN_ID, status: 'running', created_at: '2026-07-11T12:00:00Z', started_at: '2026-07-11T12:00:01Z' }
          : null,
      });
    }
    if (path.endsWith(`/chat/sessions/${SESSION_ID}/runtime-summary`)) {
      return route.fulfill({
        json: {
          model: { label: 'GPT Test', provider: 'openai', context_window_tokens: 128000 },
          runtime: { connected: true, estimated_input_tokens: 18400, remaining_tokens_estimate: 109600 },
          activated_tool_groups: [],
          used_tools: scenario === 'active' ? ['write_file'] : [],
          blocked_capabilities: [],
          compaction_count: 1,
        },
      });
    }
    if (path.endsWith(`/sessions/${SESSION_ID}/work-ledger`)) {
      return route.fulfill({
        json: {
          schema: 'agent_work_ledger_view.v1',
          session_id: SESSION_ID,
          status: scenario === 'active' ? 'running' : 'completed',
          todo_items: scenario === 'active'
            ? [
                { id: 'todo-1', title: 'Verify release evidence', status: 'completed', required: true },
                { id: 'todo-2', title: 'Publish final report', status: 'running', required: true },
              ]
            : [],
          verification: [],
          progress: [],
          failures: [],
          findings: [],
          evidence_refs: [],
          counts: { todos_total: scenario === 'active' ? 2 : 0, todos_complete: scenario === 'active' ? 1 : 0, todos_open: scenario === 'active' ? 1 : 0 },
        },
      });
    }
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  const query = audience === 'operator'
    ? `?manage=true&session_id=${SESSION_ID}`
    : `?session_id=${SESSION_ID}`;
  await page.goto(`/agents/${AGENT_ID}${query}#chat`);
  await expect(page.getByTestId('session-workbench')).toBeVisible();
  await expect(page.locator('.vite-error-overlay')).toHaveCount(0);
  if ((page.viewportSize()?.width || 0) < 900 && await page.getByTestId('session-runtime-deliverables').count() === 0) {
    await page.getByTestId('session-runtime-collapse-toggle').click();
  }
  if (scenario === 'active') {
    await expect(page.getByTestId('session-runtime-deliverables')).toContainText('release-report.md');
    await expect(page.getByTestId('session-runtime-run-status')).toBeVisible();
    await expect(page.locator('[data-thread-item-type="approval_request"]')).toBeVisible();
    await expect(page.getByTestId('thread-item-retry-turn')).toBeVisible();
  } else {
    await expect(page.getByTestId('session-runtime-deliverables')).toContainText('No delivered artifacts');
  }
  return {
    consoleErrors,
    emitLiveSessionEvents(events: Array<Record<string, unknown>>) {
      if (sessionSocketSend === null) throw new Error('canonical Session websocket is not subscribed');
      for (const event of events) sessionSocketSend(JSON.stringify(event));
    },
  };
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

async function expectVisual(page: Page, name: string) {
  await scrollTimelineToBottom(page);
  await expect(page.getByTestId('session-workbench')).toHaveScreenshot(name, {
    animations: 'disabled',
    caret: 'hide',
    maxDiffPixelRatio: 0.01,
    threshold: 0.2,
  });
}

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .include('[data-testid="session-workbench"]')
    .withTags(['wcag2a', 'wcag2aa'])
    .analyze();
  const blocking = results.violations.filter((violation) => (
    violation.impact === 'critical' || violation.impact === 'serious'
  ));
  const blockingSummary = blocking.map((violation) => ({
    id: violation.id,
    targets: violation.nodes.map((node) => node.target.join(' > ')),
  }));
  expect(blockingSummary, JSON.stringify(blockingSummary, null, 2)).toEqual([]);
}

for (const viewport of [
  { name: 'desktop', width: 1440, height: 980 },
  { name: 'narrow', width: 740, height: 920 },
]) {
  test(`ordinary user active states ${viewport.name} visual contract`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const { consoleErrors } = await bootstrap(page, { audience: 'user', scenario: 'active' });
    await expect(page.getByTestId('thread-item-inspector')).toHaveCount(0);
    await expect(page.locator('[data-thread-item-type="workflow_activity"]')).toHaveCount(0);
    await expect(page.getByTestId('session-runtime-segment-team')).toContainText('1');
    await expect(page.getByTestId('session-runtime-segment-workers')).toContainText('1');
    await expect(page.getByTestId('session-runtime-segment-workflow')).toContainText('1');
    await expect(page.getByTestId('chat-work-ledger-dock')).toHaveAttribute('data-presentation', 'persistent');
    await expect(page.getByTestId('chat-work-ledger-panel')).toBeVisible();
    await expect(page.getByTestId('agent-task-list')).toContainText('Publish final report');
    await expectVisual(page, `workbench-user-active-${viewport.name}.png`);
    expect(consoleErrors).toEqual([]);
  });

  test(`ordinary user idle ${viewport.name} visual contract`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const { consoleErrors } = await bootstrap(page, { audience: 'user', scenario: 'idle' });
    await expect(page.getByTestId('session-runtime-summary-strip')).toHaveAttribute('data-runtime-state', 'idle');
    await expectVisual(page, `workbench-user-idle-${viewport.name}.png`);
    expect(consoleErrors).toEqual([]);
  });

  test(`operator evidence ${viewport.name} visual contract`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const { consoleErrors } = await bootstrap(page, { audience: 'operator', scenario: 'active' });
    if (viewport.width < 900) {
      await page.getByTestId('session-runtime-collapse-toggle').click();
    }
    const approval = page.locator('[data-thread-item-type="approval_request"]');
    await approval.getByTestId('thread-item-technical-details').click();
    if (viewport.width < 900) {
      await page.getByTestId('session-runtime-collapse-toggle').click();
    }
    await expect(page.getByTestId('thread-item-inspector')).toContainText('permission-1');
    await expect(page.locator('[data-thread-item-type="workflow_activity"]')).toBeVisible();
    await expect(page.getByTestId('chat-work-ledger-dock')).toHaveCount(0);
    await expectVisual(page, `workbench-operator-active-${viewport.name}.png`);
    expect(consoleErrors).toEqual([]);
  });
}

for (const audience of ['user', 'operator'] as const) {
  test(`${audience} workbench has no serious accessibility violations`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 980 });
    await bootstrap(page, { audience, scenario: 'active' });
    await expectNoSeriousAccessibilityViolations(page);
  });
}

test('ordinary user active dark desktop visual and accessibility contract', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 980 });
  const { consoleErrors } = await bootstrap(page, { audience: 'user', scenario: 'active', theme: 'dark' });
  await expect(page.getByTestId('chat-work-ledger-dock')).toHaveAttribute('data-presentation', 'persistent');
  await expect(page.getByTestId('chat-work-ledger-panel')).toBeVisible();
  await expectVisual(page, 'workbench-user-active-dark-desktop.png');
  await expectNoSeriousAccessibilityViolations(page);
  expect(consoleErrors).toEqual([]);
});

test('canonical assistant text becomes visible live without a refresh or a Thinking label', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 980 });
  const progress = 'I isolated the projection gap and am validating the live Session update path.';
  const common = {
    schema: 'hive.session_event',
    schema_version: 2,
    item_id: '00000000-0000-4000-8000-000000000088',
    item_kind: 'assistant_text',
    tenant_id: '00000000-0000-4000-8000-000000000099',
    scope: {
      level: 'round',
      session_id: SESSION_ID,
      thread_id: SESSION_ID,
      turn_id: 'turn-live-prose',
      run_id: RUN_ID,
      round_id: 'round-live-prose',
    },
    actor: { type: 'assistant', agent_id: AGENT_ID },
    visibility: { audience: 'direct_user' },
    occurred_at: '2026-07-17T12:08:00Z',
    persisted_at: '2026-07-17T12:08:00Z',
  };
  const { emitLiveSessionEvents } = await bootstrap(page, {
    audience: 'user',
    scenario: 'active',
  });
  emitLiveSessionEvents([
      {
        ...common,
        event_id: '00000000-0000-4000-8000-000000000088',
        sequence: 8,
        ordinal: 0,
        kind: 'assistant_text.snapshot',
        lifecycle: 'snapshot',
        payload_schema: 'hive.session.payload.assistant_text.snapshot.v2',
        payload: { phase: 'unknown', content: progress, block_index: 0 },
      },
      {
        ...common,
        event_id: '00000000-0000-4000-8000-000000000089',
        sequence: 9,
        ordinal: 1,
        kind: 'assistant_text.completed',
        lifecycle: 'completed',
        payload_schema: 'hive.session.payload.assistant_text.completed.v2',
        payload: { phase: 'unknown', content: '', block_index: 0 },
      },
    ]);

  await expect(page.getByTestId('run-disclosure-prose')).toContainText(progress);
  await expect(page.getByTestId('run-disclosure-prose')).not.toContainText('Thinking');
  await expect(page.getByTestId('chat-work-ledger-dock')).toHaveAttribute('data-presentation', 'persistent');
});

test('slow transcript and offline reconnect preserve the durable session projection', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 980 });
  const startedAt = Date.now();
  await bootstrap(page, {
    audience: 'user',
    scenario: 'active',
    transcriptDelayMs: 500,
  });
  expect(Date.now() - startedAt).toBeGreaterThanOrEqual(450);
  await expect(page.getByText('I verified the evidence and prepared a reviewable release report.')).toBeVisible();

  await page.context().setOffline(true);
  await expect(page.getByTestId('session-transport-status')).toHaveAttribute('data-transport-phase', 'offline');
  await expect(page.getByText('I verified the evidence and prepared a reviewable release report.')).toBeVisible();

  await page.context().setOffline(false);
  await expect(page.getByTestId('session-transport-status')).toHaveCount(0);
  await expect(page.getByText('I verified the evidence and prepared a reviewable release report.')).toBeVisible();
});

test('workspace renders a bounded DOM window for one thousand artifacts', async ({ page }) => {
  const files = Array.from({ length: 1000 }, (_, index) => ({
    name: `artifact-${String(index).padStart(4, '0')}.md`,
    path: `workspace/artifact-${String(index).padStart(4, '0')}.md`,
    type: 'file',
    is_dir: false,
    size: 1024,
  }));
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
    if (path.endsWith('/auth/me')) {
      return route.fulfill({ json: { id: 'u-1', username: 'e2e', role: 'admin', tenant_id: 't-1' } });
    }
    if (path.endsWith(`/agents/${AGENT_ID}`)) {
      return route.fulfill({
        json: {
          id: AGENT_ID,
          name: 'Artifact Steward',
          status: 'idle',
          agent_type: 'native',
          access_level: 'manage',
          role_description: 'Large workspace acceptance',
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}/files/`) && url.searchParams.get('path') === 'workspace') {
      return route.fulfill({ json: files });
    }
    if (route.request().method() === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#workspace`);
  await expect(page.locator('.file-browser-row')).toHaveCount(200);
  await expect(page.locator('.file-browser-list-more')).toContainText('Showing 200 of 1000 files');
  await page.locator('.file-browser-list-more button').click();
  await expect(page.locator('.file-browser-row')).toHaveCount(400);
  await expect(page.locator('.file-browser-list-more')).toContainText('Showing 400 of 1000 files');
});

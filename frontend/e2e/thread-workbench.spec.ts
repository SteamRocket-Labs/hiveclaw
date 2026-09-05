import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const AGENT_ID = '7e57a9e7-0000-4000-8000-000000000010';
const SESSION_ID = '8e57a9e7-0000-4000-8000-000000000020';
const RUN_ID = '9e57a9e7-0000-4000-8000-000000000030';
const OWN_BROWSER_SESSION_ID = '8e57a9e7-0000-4000-8000-000000000022';
const OPERATOR_REASON = 'Release evidence review';

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
  browserOwnSession?: boolean;
}) {
  const { audience, scenario, theme = 'light', transcriptDelayMs = 0, browserOwnSession = false } = options;
  const session = sessionFor(audience);
  // A managed browser lists every user session in scope; the operator's OWN
  // session stays a writable non-operator row with its real delete control.
  const browserSessions = browserOwnSession
    ? [session, {
      ...session,
      id: OWN_BROWSER_SESSION_ID,
      user_id: 'u-1',
      is_current_user_session: true,
      read_only: false,
      authority_source: 'session_owner',
      operator_view: false,
      title: 'Owned release checklist draft',
      created_at: '2026-07-11T12:02:00Z',
      updated_at: '2026-07-11T12:12:00Z',
    }]
    : [session];
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
        const lastCommittedSequence = transcript.length;
        const acceptedAfterSequence = payload.cursor_mode === 'live_tail'
          ? lastCommittedSequence
          : Number(payload.after_sequence || 0);
        socket.send(JSON.stringify({
          type: 'session.ready',
          schema_version: 2,
          session_id: SESSION_ID,
          subscription_id: 'e2e-session-subscription',
          accepted_after_sequence: acceptedAfterSequence,
          last_committed_sequence: lastCommittedSequence,
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
          access_level: audience === 'operator' ? 'use' : 'manage',
          action_capabilities: {
            can_use: true,
            can_manage: audience === 'user',
            can_manage_schedule: audience === 'user',
            can_manage_channel: audience === 'user',
            can_manage_permissions: audience === 'user',
            can_operator_inspect: audience === 'operator',
            can_transfer_ownership: audience === 'user',
          },
          primary_model_id: 'gpt-test',
          role_description: 'Release governance and evidence review',
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}/sessions`) && method === 'GET') {
      if (audience === 'operator' && url.searchParams.get('scope') !== 'all') {
        return route.fulfill({ json: [] });
      }
      if (audience === 'operator' && url.searchParams.get('operator_reason') !== OPERATOR_REASON) {
        return route.fulfill({ status: 422, json: { detail: 'Operator inspection reason is required' } });
      }
      return route.fulfill({ json: browserSessions });
    }
    if (
      audience === 'operator'
      && (path.includes(`/sessions/${SESSION_ID}`) || path.includes(`/chat/sessions/${SESSION_ID}`))
      && (url.searchParams.get('operator_view') !== 'true' || url.searchParams.get('operator_reason') !== OPERATOR_REASON)
    ) {
      return route.fulfill({ status: 403, json: { detail: 'Operator inspection authority is required' } });
    }
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
  if (audience === 'operator') {
    await page.getByLabel('Operator inspection reason').fill(OPERATOR_REASON);
    await expect(page.getByLabel('Operator inspection reason')).toHaveValue(OPERATOR_REASON);
    await page.getByTestId('agent-operator-reason').getByRole('button', { name: 'Begin inspection' }).click();
  }
  await expect(page.getByTestId('session-workbench')).toBeVisible();
  if (audience === 'operator') {
    await expect(page.getByTestId('session-operator-view')).toBeVisible();
  }
  await expect(page.locator('.vite-error-overlay')).toHaveCount(0);
  // Readiness is asserted on the primary reading flow only. The runtime rail is
  // never force-opened here: each test verifies the real first-render state and
  // then opens the panel explicitly when it exercises the expanded view.
  if (scenario === 'active') {
    await expect(page.locator('[data-thread-item-type="approval_request"]')).toBeVisible();
    if (audience === 'operator') {
      await expect(page.getByTestId('thread-item-retry-turn')).toHaveCount(0);
    } else {
      await expect(page.getByTestId('thread-item-retry-turn')).toBeVisible();
    }
  } else {
    await expect(page.getByText('Everything is ready. Start a new request whenever you need me.')).toBeVisible();
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

async function expectVisual(page: Page, name: string, options: {
  maxDiffPixelRatio?: number;
  capture?: 'element' | 'viewport';
} = {}) {
  await scrollTimelineToBottom(page);
  const screenshotOptions = {
    animations: 'disabled' as const,
    caret: 'hide' as const,
    maxDiffPixelRatio: options.maxDiffPixelRatio ?? 0.01,
    threshold: 0.2,
  };
  if (options.capture === 'viewport') {
    // The narrow expanded panel is an overlay that extends past the workbench
    // element's left edge; only a viewport capture records what the user sees.
    await expect(page).toHaveScreenshot(name, screenshotOptions);
    return;
  }
  await expect(page.getByTestId('session-workbench')).toHaveScreenshot(name, screenshotOptions);
}

// Nearly-empty idle scenes tolerate almost no drift: the old engineering
// dashboard differs from the task-first idle render by less than 1% of pixels,
// which slipped through the default 0.01 gate and let a stale baseline pass.
const IDLE_MAX_DIFF_PIXEL_RATIO = 0.002;

type BoxEdges = { left: number; right: number; top: number; bottom: number };

function intersectionArea(a: BoxEdges, b: BoxEdges): number {
  const width = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
  const height = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
  return width * height;
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
    // First render is task-first: task, answer, approval, and recovery stay in the
    // reading flow while the runtime rail starts collapsed with honest live counts.
    await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
    await expect(page.getByTestId('session-runtime-console')).toHaveCount(0);
    await expect(page.getByTestId('thread-item-inspector')).toHaveCount(0);
    await expect(page.locator('[data-thread-item-type="workflow_activity"]')).toHaveCount(0);
    await expect(page.getByTestId('session-runtime-collapsed-deliverables')).toContainText('1');
    await expect(page.getByTestId('session-runtime-collapsed-running')).toContainText('5');
    await expect(page.getByTestId('session-runtime-collapsed-attention')).toHaveCount(0);
    await expect(page.getByTestId('chat-work-ledger-dock')).toHaveAttribute('data-presentation', 'persistent');
    await expect(page.getByTestId('chat-work-ledger-panel')).toBeVisible();
    await expect(page.getByTestId('agent-task-list')).toContainText('Publish final report');
    await expectVisual(page, `workbench-user-active-${viewport.name}.png`);
    // An explicit open reveals the expanded view with real counts and states.
    await page.getByTestId('session-runtime-collapse-toggle').click();
    await expect(page.getByTestId('session-runtime-deliverables')).toContainText('release-report.md');
    await expect(page.getByTestId('session-runtime-run-status')).toBeVisible();
    await expect(page.getByTestId('session-runtime-summary-strip')).toHaveAttribute('data-runtime-state', 'running');
    await expect(page.getByTestId('session-runtime-segment-team')).toContainText('1');
    await expect(page.getByTestId('session-runtime-segment-workers')).toContainText('1');
    await expect(page.getByTestId('session-runtime-segment-workflow')).toContainText('1');
    await expect(page.getByTestId('session-runtime-console-empty')).toHaveCount(0);
    await expectVisual(page, `workbench-user-active-expanded-${viewport.name}.png`, {
      capture: viewport.name === 'narrow' ? 'viewport' : 'element',
    });
    expect(consoleErrors).toEqual([]);
  });

  test(`ordinary user idle ${viewport.name} visual contract`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const { consoleErrors } = await bootstrap(page, { audience: 'user', scenario: 'idle' });
    // Idle first render: no engineering dashboard, no zero-count badges.
    await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
    await expect(page.getByTestId('session-runtime-collapse-toggle')).toHaveAttribute('aria-label', 'Expand runtime panel');
    await expect(page.getByTestId('session-runtime-console')).toHaveCount(0);
    await expect(page.getByTestId('session-runtime-collapsed-deliverables')).toHaveCount(0);
    await expect(page.getByTestId('session-runtime-collapsed-attention')).toHaveCount(0);
    await expect(page.getByTestId('session-runtime-collapsed-running')).toHaveCount(0);
    await expectVisual(page, `workbench-user-idle-${viewport.name}.png`, {
      maxDiffPixelRatio: IDLE_MAX_DIFF_PIXEL_RATIO,
    });
    // An explicit open shows one quiet honest empty state instead of zero tabs.
    await page.getByTestId('session-runtime-collapse-toggle').click();
    await expect(page.getByTestId('session-runtime-deliverables')).toContainText('No delivered artifacts');
    await expect(page.getByTestId('session-runtime-run-status')).toBeVisible();
    await expect(page.getByTestId('session-runtime-console-empty')).toContainText(
      'No background agents, teams, or workflows in this session.',
    );
    await expect(page.getByTestId('session-runtime-summary-strip')).toHaveCount(0);
    await expect(page.getByTestId('session-runtime-segment-team')).toHaveCount(0);
    expect(consoleErrors).toEqual([]);
  });

  test(`operator evidence ${viewport.name} visual contract`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const { consoleErrors } = await bootstrap(page, { audience: 'operator', scenario: 'active' });
    if (viewport.width > 960) {
      // Operators explicitly came for evidence: the rail starts expanded on wide
      // workbenches. Collapsing it must not bury the inspector.
      await expect(page.getByTestId('session-runtime-panel')).not.toHaveClass(/is-collapsed/);
      await expect(page.getByTestId('session-runtime-summary-strip')).toBeVisible();
      await page.getByTestId('session-runtime-collapse-toggle').click();
      await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
    } else {
      await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
    }
    await expect(page.getByTestId('thread-item-inspector')).toHaveCount(0);
    // An explicit technical-details click always reveals its inspector, even with
    // the rail collapsed.
    const approval = page.locator('[data-thread-item-type="approval_request"]');
    await approval.getByTestId('thread-item-technical-details').click();
    await expect(page.getByTestId('session-runtime-panel')).not.toHaveClass(/is-collapsed/);
    await expect(page.getByTestId('thread-item-inspector')).toContainText('permission-1');
    await expect(page.locator('[data-thread-item-type="workflow_activity"]')).toBeVisible();
    await expect(page.getByTestId('chat-work-ledger-dock')).toHaveCount(0);
    await expect(page.getByTestId('message-action-like')).toHaveCount(0);
    await expect(page.getByTestId('message-action-dislike')).toHaveCount(0);
    await expect(page.getByTestId('message-action-branch')).toHaveCount(0);
    await expect(page.getByTestId('message-action-rewind')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Allow once' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Allow for this session' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Deny' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Resume', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Close team', exact: true })).toHaveCount(0);
    if (viewport.width <= 960) {
      // The narrow inspector overlay anchors to the shell's lower edge, which
      // extends below the fold under tall agent chrome. The close/return
      // controls must stay inside the viewport, and ordinary page scrolling
      // must reveal the drawer's below-fold content.
      const geometry = await page.evaluate(() => {
        const rectOf = (selector: string) => {
          const element = document.querySelector(selector);
          if (!element) return null;
          const rect = element.getBoundingClientRect();
          return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
        };
        return {
          viewportHeight: window.innerHeight,
          toggle: rectOf('[data-testid="session-runtime-collapse-toggle"]'),
          inspectorClose: rectOf('.session-technical-drawer [aria-label="Close"]'),
          drawer: rectOf('.session-technical-drawer'),
        };
      });
      expect(geometry.toggle).toBeTruthy();
      expect(geometry.inspectorClose).toBeTruthy();
      expect(geometry.toggle!.top).toBeGreaterThanOrEqual(0);
      expect(geometry.toggle!.bottom).toBeLessThanOrEqual(geometry.viewportHeight);
      expect(geometry.inspectorClose!.top).toBeGreaterThanOrEqual(0);
      expect(geometry.inspectorClose!.bottom).toBeLessThanOrEqual(geometry.viewportHeight);
      if (geometry.drawer && geometry.drawer.bottom > geometry.viewportHeight) {
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
        const drawerBottom = await page.evaluate(() => {
          const drawer = document.querySelector('.session-technical-drawer');
          return drawer ? drawer.getBoundingClientRect().bottom : null;
        });
        expect(drawerBottom).not.toBeNull();
        expect(drawerBottom!).toBeLessThanOrEqual(geometry.viewportHeight);
        await page.evaluate(() => window.scrollTo(0, 0));
      }
    }
    // Honest evidence at narrow: a viewport capture records exactly what the
    // operator sees; an element capture would stitch in below-fold content.
    await expectVisual(page, `workbench-operator-active-${viewport.name}.png`, {
      capture: viewport.name === 'narrow' ? 'viewport' : 'element',
    });
    if (viewport.width <= 960) {
      // The return path: inspector Close clears the selection, the collapse
      // toggle folds the overlay, and the reading flow is visible again.
      await page.locator('.session-technical-drawer [aria-label="Close"]').click();
      await expect(page.getByTestId('thread-item-inspector')).toHaveCount(0);
      await page.getByTestId('session-runtime-collapse-toggle').click();
      await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
      await expect(approval).toBeVisible();
    }
    expect(consoleErrors).toEqual([]);
  });
}

test('operator same-item technical-detail selection reopens a collapsed rail', async ({ page }) => {
  await page.setViewportSize({ width: 740, height: 920 });
  const { consoleErrors } = await bootstrap(page, { audience: 'operator', scenario: 'active' });
  const details = page.locator('[data-thread-item-type="approval_request"]').getByTestId('thread-item-technical-details');
  // The first explicit selection reveals the inspector even from the narrow
  // collapsed default.
  await details.click();
  await expect(page.getByTestId('session-runtime-panel')).not.toHaveClass(/is-collapsed/);
  await expect(page.getByTestId('thread-item-inspector')).toContainText('permission-1');
  // After the user collapses the rail to read, re-selecting the SAME item is a
  // new explicit action and must reveal the inspector again.
  await page.getByTestId('session-runtime-collapse-toggle').click();
  await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
  await details.click();
  await expect(page.getByTestId('session-runtime-panel')).not.toHaveClass(/is-collapsed/);
  await expect(page.getByTestId('thread-item-inspector')).toContainText('permission-1');
  expect(consoleErrors).toEqual([]);
});

for (const viewport of [
  { name: 'desktop', width: 1440, height: 980 },
  { name: 'narrow', width: 740, height: 920 },
]) {
  test(`runtime rail controls keep clear separate space ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const { consoleErrors } = await bootstrap(page, { audience: 'user', scenario: 'active' });
    // Collapsed: the expand toggle and the count badges are distinct actions
    // and must never paint over each other.
    const collapsed = await page.evaluate(() => {
      const rectOf = (selector: string) => {
        const element = document.querySelector(selector);
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
      };
      return {
        toggle: rectOf('[data-testid="session-runtime-collapse-toggle"]'),
        deliverables: rectOf('[data-testid="session-runtime-collapsed-deliverables"]'),
        running: rectOf('[data-testid="session-runtime-collapsed-running"]'),
      };
    });
    expect(collapsed.toggle).toBeTruthy();
    expect(collapsed.deliverables).toBeTruthy();
    expect(collapsed.running).toBeTruthy();
    expect(intersectionArea(collapsed.toggle!, collapsed.deliverables!)).toBe(0);
    expect(intersectionArea(collapsed.toggle!, collapsed.running!)).toBe(0);
    // Expanded: the toggle owns the panel's top strip and never covers the
    // first section header. Expand via keyboard: a count badge is a real
    // button, Enter activates it, and focus survives on the expanded panel's
    // collapse toggle.
    const deliverablesBadge = page.getByTestId('session-runtime-collapsed-deliverables');
    await deliverablesBadge.focus();
    await expect(deliverablesBadge).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.getByTestId('session-runtime-panel')).not.toHaveClass(/is-collapsed/);
    await expect(page.getByTestId('session-runtime-collapse-toggle')).toBeFocused();
    const expanded = await page.evaluate(() => {
      const toggle = document.querySelector('[data-testid="session-runtime-collapse-toggle"]');
      const header = document.querySelector('[data-testid="session-runtime-deliverables"] .session-runtime-section-header');
      if (!toggle || !header) return null;
      const toggleRect = toggle.getBoundingClientRect();
      const headerRect = header.getBoundingClientRect();
      return { toggleBottom: toggleRect.bottom, headerTop: headerRect.top };
    });
    expect(expanded).toBeTruthy();
    expect(expanded!.toggleBottom).toBeLessThanOrEqual(expanded!.headerTop);
    expect(consoleErrors).toEqual([]);
  });
}

// At and below the 960px narrow boundary the collapsed rail is an overlay; the
// reading flow must keep its own clear space so message text, approval status,
// and composer controls never render under the rail.
for (const viewport of [
  { name: 'narrow', width: 740, height: 920 },
  { name: 'intermediate', width: 860, height: 920 },
  { name: 'narrow-boundary', width: 960, height: 920 },
]) {
  for (const scenario of ['idle', 'active'] as const) {
    test(`collapsed rail never obscures reading text ${scenario} ${viewport.name}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      const { consoleErrors } = await bootstrap(page, { audience: 'user', scenario });
      await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
      await scrollTimelineToBottom(page);
      const overlaps = await page.evaluate(() => {
        const center = document.querySelector('[data-testid="session-workbench"] .session-tui-center');
        const rail = document.querySelector('[data-testid="session-runtime-panel"]');
        if (!center || !rail) return [{ text: 'session workbench did not render', area: -1 }];
        const railRect = rail.getBoundingClientRect();
        const overlapArea = (rect: { left: number; right: number; top: number; bottom: number }) => {
          const width = Math.max(0, Math.min(rect.right, railRect.right) - Math.max(rect.left, railRect.left));
          const height = Math.max(0, Math.min(rect.bottom, railRect.bottom) - Math.max(rect.top, railRect.top));
          return width * height;
        };
        const hits: Array<{ text: string; area: number }> = [];
        const walker = document.createTreeWalker(center, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          const value = (node.textContent || '').trim();
          if (!value) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          for (const rect of Array.from(range.getClientRects())) {
            const area = overlapArea(rect);
            if (area > 0) hits.push({ text: value, area });
          }
          range.detach();
        }
        return hits;
      });
      expect(overlaps).toEqual([]);
      expect(consoleErrors).toEqual([]);
    });
  }
}

// In managed mode at and below 960px the session browser stacks above the
// reading column while the collapsed rail pins to the shell's top-right over
// the browser. The browser's filter, rows, and row actions must reserve the
// same rail strip as the reading flow, so an own non-operator session's delete
// control stays visible on hover, hit-testable, and clickable.
for (const viewport of [
  { name: 'narrow', width: 740, height: 920 },
  { name: 'intermediate', width: 860, height: 920 },
  { name: 'narrow-boundary', width: 960, height: 920 },
]) {
  test(`collapsed rail never obscures managed session browser controls ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const deleteRequests: string[] = [];
    page.on('request', (request) => {
      if (request.method() === 'DELETE') deleteRequests.push(request.url());
    });
    const { consoleErrors } = await bootstrap(page, {
      audience: 'operator',
      scenario: 'active',
      browserOwnSession: true,
    });
    await expect(page.getByTestId('session-workbench')).toHaveClass(/session-tui-shell-managed/);
    await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
    const browser = page.getByTestId('detail-session-browser');
    await expect(browser).toBeVisible();
    const header = browser.locator('.detail-session-browser-header');
    const filter = browser.locator('.detail-session-filter');
    const ownRow = browser.locator('.detail-session-row', { hasText: 'Owned release checklist draft' });
    const rowMain = ownRow.locator('.detail-session-row-main');
    const action = ownRow.getByRole('button', { name: 'Delete session Owned release checklist draft' });
    await expect(header).toBeVisible();
    await expect(filter).toBeVisible();
    await expect(rowMain).toBeVisible();
    // First render must already present one complete session row inside the
    // list clip — no scrolling premise — so an ordinary user immediately sees
    // a whole session entry at every narrow width.
    const firstRow = browser.locator('.detail-session-row').first();
    await expect(firstRow).toBeVisible();
    const firstRender = await page.evaluate(() => {
      const rectOf = (element: Element | null | undefined) => {
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
      };
      const browserElement = document.querySelector('[data-testid="detail-session-browser"]');
      const list = browserElement?.querySelector('.detail-session-list');
      const row = list?.querySelector('.detail-session-row');
      return {
        listClientHeight: list?.clientHeight ?? null,
        listBottom: rectOf(list)?.bottom ?? null,
        row: rectOf(row),
        rowHeight: row?.getBoundingClientRect().height ?? null,
        rowMarginBottom: row ? Number.parseFloat(getComputedStyle(row).marginBottom) || 0 : 0,
      };
    });
    expect(firstRender.listClientHeight).toBeTruthy();
    expect(firstRender.rowHeight).toBeTruthy();
    expect(firstRender.row).toBeTruthy();
    expect(firstRender.listBottom).toBeTruthy();
    expect(firstRender.listClientHeight!).toBeGreaterThanOrEqual(firstRender.rowHeight! + firstRender.rowMarginBottom);
    expect(firstRender.row!.bottom + firstRender.rowMarginBottom).toBeLessThanOrEqual(firstRender.listBottom! + 0.5);
    // A real downward drag from the native bottom-right resize corner must
    // materially enlarge the browser; frozen max-height caps pinned the box at
    // its default and made this exact drag a no-op.
    const preDragBox = await browser.boundingBox();
    expect(preDragBox).toBeTruthy();
    const cornerX = preDragBox!.x + preDragBox!.width - 3;
    const cornerY = preDragBox!.y + preDragBox!.height - 3;
    await page.mouse.move(cornerX, cornerY);
    await page.mouse.down();
    await page.mouse.move(cornerX, cornerY + 80, { steps: 8 });
    await page.mouse.up();
    const postDragBox = await browser.boundingBox();
    expect(postDragBox).toBeTruthy();
    expect(postDragBox!.height - preDragBox!.height).toBeGreaterThanOrEqual(64);
    // After the resize the rail-clearance contract still holds for the browser
    // chrome and the own session row, and no horizontal overflow appears.
    const postDrag = await page.evaluate(() => {
      const rectOf = (element: Element | null | undefined) => {
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
      };
      const browserElement = document.querySelector('[data-testid="detail-session-browser"]');
      const rows = Array.from(browserElement?.querySelectorAll('.detail-session-row') ?? []);
      const ownRowElement = rows.find((row) => row.textContent?.includes('Owned release checklist draft'));
      return {
        viewportWidth: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        rail: rectOf(document.querySelector('[data-testid="session-runtime-panel"]')),
        browser: rectOf(browserElement),
        header: rectOf(browserElement?.querySelector('.detail-session-browser-header')),
        filter: rectOf(browserElement?.querySelector('.detail-session-filter')),
        ownRow: rectOf(ownRowElement),
        action: rectOf(ownRowElement?.querySelector('.detail-session-row-action')),
      };
    });
    expect(postDrag.rail).toBeTruthy();
    expect(postDrag.browser).toBeTruthy();
    expect(postDrag.header).toBeTruthy();
    expect(postDrag.filter).toBeTruthy();
    expect(postDrag.ownRow).toBeTruthy();
    expect(postDrag.action).toBeTruthy();
    expect(intersectionArea(postDrag.browser!, postDrag.rail!)).toBe(0);
    expect(intersectionArea(postDrag.header!, postDrag.rail!)).toBe(0);
    expect(intersectionArea(postDrag.filter!, postDrag.rail!)).toBe(0);
    expect(intersectionArea(postDrag.ownRow!, postDrag.rail!)).toBe(0);
    expect(intersectionArea(postDrag.action!, postDrag.rail!)).toBe(0);
    expect(postDrag.browser!.right).toBeLessThanOrEqual(postDrag.viewportWidth);
    expect(postDrag.scrollWidth).toBeLessThanOrEqual(postDrag.clientWidth);
    // The narrow browser caps the session list to a short scroll viewport, so a
    // real user scrolls a lower row's control into the clip and hovers it: the
    // delete control reveals on hover/focus, as in the product.
    await action.evaluate((element) => {
      element.scrollIntoView({ block: 'center', inline: 'nearest' });
    });
    const revealedBox = await action.boundingBox();
    expect(revealedBox).toBeTruthy();
    await page.mouse.move(
      revealedBox!.x + revealedBox!.width / 2,
      revealedBox!.y + revealedBox!.height / 2,
    );
    await expect(action).toBeVisible();
    const railBox = await page.getByTestId('session-runtime-panel').boundingBox();
    const browserBox = await browser.boundingBox();
    const headerBox = await header.boundingBox();
    const filterBox = await filter.boundingBox();
    const rowBox = await ownRow.boundingBox();
    const actionBox = await action.boundingBox();
    expect(railBox).toBeTruthy();
    expect(browserBox).toBeTruthy();
    expect(headerBox).toBeTruthy();
    expect(filterBox).toBeTruthy();
    expect(rowBox).toBeTruthy();
    expect(actionBox).toBeTruthy();
    const edges = (box: { x: number; y: number; width: number; height: number }) => ({
      left: box.x,
      right: box.x + box.width,
      top: box.y,
      bottom: box.y + box.height,
    });
    const rail = edges(railBox!);
    expect(intersectionArea(edges(headerBox!), rail)).toBe(0);
    expect(intersectionArea(edges(filterBox!), rail)).toBe(0);
    expect(intersectionArea(edges(rowBox!), rail)).toBe(0);
    expect(intersectionArea(edges(actionBox!), rail)).toBe(0);
    // The control itself is the hit target at its own center, not the rail.
    await expect.poll(async () => action.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return hit === element || element.contains(hit);
    })).toBe(true);
    // The stacked browser keeps its native vertical-resize affordance, and the
    // bottom-right corner that hosts the widget is hit-testable as the browser
    // rather than intercepted by the collapsed rail above it.
    await expect(browser).toHaveCSS('resize', 'vertical');
    await expect.poll(async () => browser.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.right - 3, rect.bottom - 3);
      if (!hit || hit.closest('[data-testid="session-runtime-panel"]')) return false;
      return hit === element || element.contains(hit);
    })).toBe(true);
    // The browser box itself must clear the rail: its native resize widget
    // lives at the border-box corner, outside any inner content reservation.
    expect(intersectionArea(edges(browserBox!), rail)).toBe(0);
    // A trial click proves actionability; a real safe click reaches the control
    // and opens the delete confirmation, dismissed here without any destructive
    // request.
    await action.click({ trial: true });
    await action.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('Owned release checklist draft');
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(rowMain).toBeVisible();
    // Keyboard parity: from the row body, Tab reaches the delete control, focus
    // reveals it, and Enter runs the same safe open/cancel flow.
    await rowMain.focus();
    await expect(rowMain).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(action).toBeFocused();
    await expect(action).toHaveCSS('opacity', '1');
    await page.keyboard.press('Enter');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('Owned release checklist draft');
    // The destructive confirm must never own the delayed initial focus: a
    // danger dialog parks focus on Cancel, so a reflexive second Enter cancels
    // instead of deleting.
    const cancelButton = dialog.getByRole('button', { name: 'Cancel' });
    await expect(cancelButton).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(dialog).toHaveCount(0);
    await expect(rowMain).toBeVisible();
    expect(deleteRequests).toEqual([]);
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
  await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
  await expect(page.getByTestId('session-runtime-collapsed-deliverables')).toContainText('1');
  await expect(page.getByTestId('chat-work-ledger-dock')).toHaveAttribute('data-presentation', 'persistent');
  await expect(page.getByTestId('chat-work-ledger-panel')).toBeVisible();
  await expectVisual(page, 'workbench-user-active-dark-desktop.png');
  await expectNoSeriousAccessibilityViolations(page);
  expect(consoleErrors).toEqual([]);
});

test('retryable pre-final failure replays the exact user checkpoint in an edit branch', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 980 });
  await bootstrap(page, { audience: 'user', scenario: 'active' });
  const requestPromise = page.waitForRequest((request) => (
    request.method() === 'POST'
    && new URL(request.url()).pathname.endsWith(`/agents/${AGENT_ID}/sessions/${SESSION_ID}/branches`)
  ));

  await page.getByTestId('thread-item-retry-turn').click();
  const request = await requestPromise;
  expect(request.postDataJSON()).toMatchObject({
    mode: 'edit',
    anchor_event_id: '00000000-0000-4000-8000-000000000001',
    content: 'Review the release evidence, coordinate the specialists, and prepare the final report.',
    display_content: 'Review the release evidence, coordinate the specialists, and prepare the final report.',
    start_run: true,
  });
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

  const liveDisclosure = page.getByTestId('run-disclosure-block').last();
  await expect(liveDisclosure).toBeVisible();
  await liveDisclosure.getByRole('button').click();
  await expect(liveDisclosure.getByTestId('run-disclosure-prose')).toContainText(progress);
  await expect(liveDisclosure.getByTestId('run-disclosure-prose')).not.toContainText('Thinking');
  await expect(page.getByTestId('chat-work-ledger-dock')).toHaveAttribute('data-presentation', 'persistent');
  // Unrelated background activity must not force the rail open or steal focus.
  await expect(page.getByTestId('session-runtime-panel')).toHaveClass(/is-collapsed/);
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

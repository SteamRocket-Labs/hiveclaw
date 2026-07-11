import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import WorkspaceRuntimeBudgetsSection from './WorkspaceRuntimeBudgetsSection';

const scheduledPolicy = {
  id: 'policy-1',
  tenant_id: 'tenant-1',
  name: 'Scheduled guard',
  enabled: true,
  priority: 1,
  scope_type: 'source_profile',
  source: 'scheduled',
  profile: 'scheduled',
  enforcement_mode: 'enforce',
  fail_mode: 'fail_closed',
  max_tokens: 1000000,
  max_cache_miss_tokens: 250000,
  max_subagents: 32,
  max_team_sessions: 0,
  max_delegations: 32,
  max_background_tasks: 32,
  max_continuation_wakes: 64,
  max_provider_calls: 128,
  default_child_token_reservation: 50000,
  default_llm_call_token_reservation: 50000,
  created_at: null,
  updated_at: null,
};

const agentTeamPolicy = {
  ...scheduledPolicy,
  id: 'policy-agent-team',
  name: 'Agent Team guard',
  source: 'agent_team',
  profile: 'agent_team',
  max_subagents: 16,
  max_team_sessions: 4,
  max_cache_miss_tokens: 16000000,
};

let policyData: unknown[] = [scheduledPolicy];
const exhaustedRun = {
  id: 'run-1',
  tenant_id: 'tenant-1',
  root_run_kind: 'trigger_fire',
  root_run_key: 'trigger:daily_scan',
  source: 'scheduled',
  profile: 'scheduled',
  status: 'exhausted',
  enforcement_mode: 'enforce',
  terminal_reason: 'runtime_budget_exhausted:subagents',
  user_status: 'Paused',
  user_reason: 'Run limit reached',
  user_next_action: 'Ask an admin to review',
  created_at: '2026-07-02T09:30:00Z',
  expires_at: null,
  completed_at: null,
};

let runData: unknown[] = [exhaustedRun];
let deliveryData: unknown[] = [];

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = String(queryKey[0]);
    if (key === 'runtime-budget-policies') {
      return { data: policyData };
    }
    if (key === 'runtime-budget-runs') {
      return { data: runData };
    }
    if (key === 'runtime-budget-transition-deliveries') {
      return { data: deliveryData };
    }
    return { data: [] };
  },
  useMutation: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

describe('WorkspaceRuntimeBudgetsSection', () => {
  beforeEach(() => {
    policyData = [scheduledPolicy];
    runData = [exhaustedRun];
    deliveryData = [];
  });

  it('renders tenant guardrails and user-facing protected run status', () => {
    const html = renderToStaticMarkup(<WorkspaceRuntimeBudgetsSection />);

    expect(html).toContain('Autonomous run protection');
    expect(html).toContain('Observe only');
    expect(html).toContain('Enforce protection');
    expect(html).toContain('Scheduled guard');
    expect(html).toContain('Run limit reached');
    expect(html).toContain('Approve');
  });

  it('shows built-in enforcement as active when no tenant override exists', () => {
    policyData = [];

    const html = renderToStaticMarkup(<WorkspaceRuntimeBudgetsSection />);

    expect(html).toContain('Built-in default protection is active.');
    expect(html).toContain('Subagents 32');
    expect(html).toContain('Cache miss 8,000,000');
    expect(html).toContain('Saving creates a company policy that takes priority over the platform default.');
    expect(html).toContain('Maximum child workers this run may start.');
    expect(html).toContain('Maximum teammate sessions for explicit Agent Team runs.');
    expect(html).toContain('Interactive');
    expect(html).toContain('Dynamic Workflow');
    expect(html).toContain('Agent Team');
    expect(html).toContain('Maximum times this run chain may resume after background signals.');
    expect(html).toContain('Maximum model calls allowed in this run chain.');
    expect(html).toContain('Maximum total tokens allowed for this run chain, including cached and non-cached tokens.');
    expect(html).toContain('Maximum non-cached input tokens allowed for this run chain.');
    expect(html).toContain('What the platform should do when this run hits the protection limit.');
    expect(html).toContain('Save company policy');
  });

  it('does not show another profile override as the active daily policy', () => {
    policyData = [agentTeamPolicy];

    const html = renderToStaticMarkup(<WorkspaceRuntimeBudgetsSection />);

    expect(html).toContain('Built-in default protection is active.');
    expect(html).toContain('Cache miss 8,000,000');
    expect(html).not.toContain('Agent Team guard');
  });

  it('shows approval and rejection actions only for a durable waiting run', () => {
    runData = [
      {
        ...exhaustedRun,
        status: 'waiting_budget_approval',
        user_status: 'Waiting for approval',
        user_reason: 'Run limit reached and approval is required',
        user_next_action: 'Approve to resume the exact queued task',
      },
    ];

    const html = renderToStaticMarkup(<WorkspaceRuntimeBudgetsSection />);

    expect(html).toContain('Waiting for approval');
    expect(html).toContain('Approve to resume the exact queued task');
    expect(html).toContain('Approve');
    expect(html).toContain('Reject');
  });

  it('keeps ambiguous delivery recovery in the company control plane', () => {
    deliveryData = [
      {
        id: 'delivery-1',
        tenant_id: 'tenant-1',
        budget_run_id: 'run-1',
        budget_event_id: 'event-1',
        transition: 'cancelled',
        channel: 'telegram',
        status: 'needs_reconciliation',
        attempt_count: 1,
        last_error: 'provider result unknown',
        created_at: '2026-07-11T10:00:00Z',
        delivered_at: null,
      },
    ];

    const html = renderToStaticMarkup(<WorkspaceRuntimeBudgetsSection />);

    expect(html).toContain('Delivery needs review');
    expect(html).toContain('telegram');
    expect(html).toContain('Confirm delivered');
    expect(html).toContain('Retry delivery');
    expect(html).not.toContain('provider result unknown');
  });
});

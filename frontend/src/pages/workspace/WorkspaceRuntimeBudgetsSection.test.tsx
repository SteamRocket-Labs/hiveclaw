import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceRuntimeBudgetsSection from './WorkspaceRuntimeBudgetsSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = String(queryKey[0]);
    if (key === 'runtime-budget-policies') {
      return {
        data: [
          {
            id: 'policy-1',
            tenant_id: 'tenant-1',
            name: 'Scheduled guard',
            enabled: true,
            priority: 1,
            scope_type: 'source_profile',
            source: 'scheduled',
            profile: 'scheduled',
            agent_id: null,
            trigger_id: null,
            enforcement_mode: 'enforce',
            fail_mode: 'fail_closed',
            max_tokens: 1000000,
            max_cache_miss_tokens: 250000,
            max_subagents: 32,
            max_delegations: 32,
            max_background_tasks: 32,
            max_continuation_wakes: 64,
            max_provider_calls: 128,
            default_child_token_reservation: 50000,
            default_llm_call_token_reservation: 50000,
            created_at: null,
            updated_at: null,
          },
        ],
      };
    }
    if (key === 'runtime-budget-runs') {
      return {
        data: [
          {
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
          },
        ],
      };
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
  it('renders tenant guardrails and user-facing protected run status', () => {
    const html = renderToStaticMarkup(<WorkspaceRuntimeBudgetsSection />);

    expect(html).toContain('Autonomous run protection');
    expect(html).toContain('Observe mode');
    expect(html).toContain('Enforce mode');
    expect(html).toContain('Scheduled guard');
    expect(html).toContain('Run limit reached');
    expect(html).toContain('Approve');
  });
});

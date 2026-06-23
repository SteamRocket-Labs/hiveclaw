import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { ToolFailureSummary } from '../api/domains/activity';
import type { Agent, Task } from '../types';
import { DashboardHomeShell, ToolFailureOverview, summarizeCrossAgentToolFailures } from './Dashboard';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      const values = (fallbackOrOptions as Record<string, unknown> | undefined) ?? options ?? {};
      if ('count' in values) {
        return String(values.count);
      }
      return key.split('.').pop() ?? key;
    },
  }),
}));

const makeSummary = (overrides: Partial<ToolFailureSummary> = {}): ToolFailureSummary => ({
  total_errors: 0,
  by_tool: [],
  by_provider: [],
  by_error_class: [],
  by_http_status: [],
  recent_errors: [],
  ...overrides,
});

describe('Dashboard tool failure overview', () => {
  it('aggregates cross-agent tool failures for dashboard triage', () => {
    const overview = summarizeCrossAgentToolFailures([
      {
        agentId: 'agent-1',
        agentName: 'Ops Bot',
        summary: makeSummary({
          total_errors: 3,
          by_tool: [
            { tool_name: 'firecrawl_fetch', count: 2 },
            { tool_name: 'web_search', count: 1 },
          ],
          by_provider: [{ provider: 'firecrawl', count: 2 }],
          by_error_class: [{ error_class: 'quota_or_billing', count: 1 }],
          by_http_status: [{ http_status: 402, count: 2 }],
        }),
      },
      {
        agentId: 'agent-2',
        agentName: 'Research Bot',
        summary: makeSummary({
          total_errors: 2,
          by_tool: [{ tool_name: 'web_search', count: 2 }],
          by_provider: [{ provider: 'duckduckgo', count: 2 }],
          by_error_class: [{ error_class: 'provider_error', count: 2 }],
          by_http_status: [{ http_status: 429, count: 2 }],
        }),
      },
    ]);

    expect(overview.totalErrors).toBe(5);
    expect(overview.byAgent[0]).toMatchObject({ agentId: 'agent-1', agentName: 'Ops Bot', count: 3 });
    expect(overview.byTool[0]).toMatchObject({ label: 'web_search', count: 3 });
    expect(overview.byProvider[0]).toMatchObject({ label: 'firecrawl', count: 2 });
    expect(overview.byErrorClass[0]).toMatchObject({ label: 'provider_error', count: 2 });
    expect(overview.byHttpStatus[0]).toMatchObject({ label: '402', count: 2 });
  });

  it('renders cross-agent failure summary card content', () => {
    const markup = renderToStaticMarkup(
      <ToolFailureOverview
        summaries={[
          {
            agentId: 'agent-1',
            agentName: 'Ops Bot',
            summary: makeSummary({
              total_errors: 3,
              by_tool: [{ tool_name: 'firecrawl_fetch', count: 2 }],
              by_provider: [{ provider: 'firecrawl', count: 2 }],
              by_error_class: [{ error_class: 'quota_or_billing', count: 2 }],
              by_http_status: [{ http_status: 402, count: 2 }],
            }),
          },
        ]}
        onSelectAgent={() => {}}
      />,
    );

    expect(markup).toContain('toolFailuresTitle');
    expect(markup).toContain('Ops Bot');
    expect(markup).toContain('firecrawl_fetch');
    expect(markup).toContain('quota_or_billing');
    expect(markup).toContain('402');
  });
});

const makeAgent = (overrides: Partial<Agent> = {}): Agent => ({
  id: 'agent-1',
  name: 'Atlas',
  role_description: 'Market and competitor research',
  status: 'running',
  creator_id: 'user-1',
  tokens_used_today: 128400,
  tokens_used_month: 690900,
  heartbeat_enabled: true,
  heartbeat_interval_minutes: 60,
  heartbeat_active_hours: '09:00-18:00',
  created_at: '2026-06-01T00:00:00Z',
  last_active_at: '2026-06-23T08:00:00Z',
  ...overrides,
});

const makeTask = (overrides: Partial<Task> = {}): Task => ({
  id: 'task-1',
  agent_id: 'agent-1',
  title: 'Q2 competitor report',
  type: 'todo',
  status: 'doing',
  priority: 'high',
  assignee: 'agent-1',
  created_by: 'user-1',
  created_at: '2026-06-23T07:00:00Z',
  updated_at: '2026-06-23T07:30:00Z',
  ...overrides,
});

describe('Dashboard workspace homepage', () => {
  it('renders the CC Design workspace home instead of the old management table', () => {
    const markup = renderToStaticMarkup(
      <DashboardHomeShell
        agents={[
          makeAgent(),
          makeAgent({ id: 'agent-2', name: 'Ledger', status: 'idle', tokens_used_today: 2200, tokens_used_month: 18000 }),
        ]}
        isLoading={false}
        allTasks={[
          makeTask(),
          makeTask({ id: 'task-2', agent_id: 'agent-2', title: 'Approve August ledger plan', status: 'pending', priority: 'urgent' }),
        ]}
        allActivities={[
          { id: 'act-1', agent_id: 'agent-1', summary: 'Saved Q2 research outline', created_at: '2026-06-23T08:05:00Z' },
        ]}
        agentActivities={{}}
        toolFailureSnapshots={[]}
        onNavigate={() => {}}
      />,
    );

    expect(markup).toContain('workspace-home');
    expect(markup).toContain('Create via HR');
    expect(markup).toContain('Assign task');
    expect(markup).toContain('Automation');
    expect(markup).toContain('Assets');
    expect(markup).toContain('Needs you');
    expect(markup).toContain('In progress');
    expect(markup).toContain('This month');
    expect(markup).toContain('Activity');
    expect(markup).toContain('Approve August ledger plan');
    expect(markup).toContain('Q2 competitor report');
    expect(markup).toContain('708.9K');
    expect(markup).not.toContain('Latest Activity</span>');
  });
});

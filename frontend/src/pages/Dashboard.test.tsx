import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { ToolFailureSummary } from '../api/domains/activity';
import { ToolFailureOverview, summarizeCrossAgentToolFailures } from './Dashboard';

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

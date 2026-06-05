import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AgentSubagentsSection, { toolFaceSummary } from './AgentSubagentsSection';
import type { SubagentRow } from '../../api/domains/subagents';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      if (options && 'count' in options) {
        return `${key.split('.').pop()}:${String(options.count)}`;
      }
      return key.split('.').pop() ?? key;
    },
    i18n: { language: 'en' },
  }),
}));

const listRows: SubagentRow[] = [
  {
    name: 'market-scout',
    description: 'Market research scout for DeFi landscape questions.',
    scope: 'agent',
    type: 'explorer',
    model: null,
    isolation: 'none',
    max_tool_rounds: 6,
    allowed_tools: ['web_search', 'web_fetch', 'read_file', 'grep_search'],
    excluded_tools: [],
  },
  {
    name: 'shared-critic',
    description: 'Company-wide adversarial reviewer.',
    scope: 'tenant',
    type: 'critic',
    model: 'claude-x',
    isolation: 'none',
    max_tool_rounds: null,
    allowed_tools: [],
    excluded_tools: [],
  },
  {
    name: 'explorer',
    description: 'Fast read-only agent specialized for exploring codebases.',
    scope: 'builtin',
    type: 'explorer',
    model: null,
    isolation: 'none',
    max_tool_rounds: null,
    allowed_tools: ['web_search'],
    excluded_tools: [],
  },
];

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey, enabled }: { queryKey: unknown[]; enabled?: boolean }) => {
    if (enabled === false) {
      return { data: undefined, isLoading: false };
    }
    const key = String(queryKey[0]);
    if (key === 'agent-subagents') {
      return { data: { subagents: listRows }, isLoading: false };
    }
    return { data: undefined, isLoading: false };
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

describe('AgentSubagentsSection', () => {
  it('renders the merged list with scope badges', () => {
    const html = renderToStaticMarkup(<AgentSubagentsSection agentId="agent-1" canManage={true} />);
    expect(html).toContain('market-scout');
    expect(html).toContain('shared-critic');
    // one row per scope, badge labels resolved through i18n keys
    expect(html).toContain('agent');
    expect(html).toContain('tenant');
    expect(html).toContain('builtin');
    // model surfaces on the row when set
    expect(html).toContain('claude-x');
    // whenToUse description surfaces on every row (CC parity)
    expect(html).toContain('Market research scout for DeFi landscape questions.');
    expect(html).toContain('Fast read-only agent specialized for exploring codebases.');
  });

  it('shows the new-definition button only with manage access', () => {
    const managed = renderToStaticMarkup(<AgentSubagentsSection agentId="agent-1" canManage={true} />);
    expect(managed).toContain('newButton');

    const readOnly = renderToStaticMarkup(<AgentSubagentsSection agentId="agent-1" canManage={false} />);
    expect(readOnly).not.toContain('newButton');
  });
});

describe('toolFaceSummary', () => {
  it('summarizes explicit allow-lists with overflow count', () => {
    expect(toolFaceSummary(listRows[0])).toBe('web_search, web_fetch, read_file +1');
  });

  it('returns empty for type-preset rows', () => {
    expect(toolFaceSummary(listRows[1])).toBe('');
  });
});

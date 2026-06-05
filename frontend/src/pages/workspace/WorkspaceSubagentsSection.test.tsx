import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceSubagentsSection from './WorkspaceSubagentsSection';
import type { SubagentRow } from '../../api/domains/subagents';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      if (options && 'name' in options) {
        return `${key.split('.').pop()}:${String(options.name)}`;
      }
      return key.split('.').pop() ?? key;
    },
    i18n: { language: 'en' },
  }),
}));

const tenantRows: SubagentRow[] = [
  {
    name: 'shared-critic',
    description: 'Company-wide adversarial reviewer.',
    scope: 'tenant',
    type: 'critic',
    model: null,
    isolation: 'none',
    max_tool_rounds: 4,
    allowed_tools: ['read_file'],
    excluded_tools: [],
  },
  {
    name: 'worker',
    description: 'General-purpose agent for multi-step tasks.',
    scope: 'builtin',
    type: 'worker',
    model: null,
    isolation: 'none',
    max_tool_rounds: null,
    allowed_tools: ['web_search'],
    excluded_tools: [],
  },
];

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: { subagents: tenantRows }, isLoading: false }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

describe('WorkspaceSubagentsSection', () => {
  it('renders the company library with tenant and builtin rows', () => {
    const html = renderToStaticMarkup(<WorkspaceSubagentsSection />);
    expect(html).toContain('shared-critic');
    expect(html).toContain('tenant');
    expect(html).toContain('builtin');
    // whenToUse description surfaces on every row (CC parity)
    expect(html).toContain('Company-wide adversarial reviewer.');
    expect(html).toContain('General-purpose agent for multi-step tasks.');
  });

  it('offers delete only on tenant rows and fork on builtin rows', () => {
    const html = renderToStaticMarkup(<WorkspaceSubagentsSection />);
    // tenant row → edit + delete; builtin row → fork (use-as-template), no delete
    expect(html.split('deleteButton').length - 1).toBe(1);
    expect(html).toContain('forkButton');
  });
});

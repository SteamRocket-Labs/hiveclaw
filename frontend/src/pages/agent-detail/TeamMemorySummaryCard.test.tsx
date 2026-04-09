import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mutationConfigs: Array<Record<string, unknown>> = [];
const queryKeys: unknown[][] = [];

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      const values = (fallbackOrOptions as Record<string, unknown> | undefined) ?? options ?? {};
      if ('count' in values) {
        return `${key.split('.').pop() ?? key}:${String(values.count)}`;
      }
      return key.split('.').pop() ?? key;
    },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (config: { queryKey: unknown[] }) => {
    queryKeys.push(config.queryKey);
    if (String(config.queryKey[0]) === 'team-memory-entry') {
      return {
        data: {
          key: 'deploy-playbook',
          title: 'Deploy Playbook',
          workspace_key: 'workspace',
          updated_at: '2026-03-27T09:00:00Z',
          snippet: 'Use a canary rollout and verify logs before promoting globally.',
          content: 'Use a canary rollout and verify logs before promoting globally.\nDocument rollback before the final promotion.',
          path: 'shared_memory/tenant/workspace/deploy-playbook.md',
          absolute_path: '/tmp/shared_memory/tenant/workspace/deploy-playbook.md',
        },
      };
    }
    return {
      data: [
        {
          key: 'deploy-playbook',
          title: 'Deploy Playbook',
          workspace_key: 'workspace',
          updated_at: '2026-03-27T09:00:00Z',
          snippet: 'Use a canary rollout and verify logs before promoting globally.',
          path: 'shared_memory/tenant/workspace/deploy-playbook.md',
          absolute_path: '/tmp/shared_memory/tenant/workspace/deploy-playbook.md',
        },
      ],
    };
  },
  useMutation: (config: Record<string, unknown>) => {
    mutationConfigs.push(config);
    return {
      mutate: vi.fn(),
      isPending: false,
    };
  },
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

describe('TeamMemorySummaryCard helpers', () => {
  beforeEach(() => {
    mutationConfigs.length = 0;
    queryKeys.length = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders shared memory details and exposes guarded mutation hooks', async () => {
    const { default: TeamMemorySummaryCard } = await import('./TeamMemorySummaryCard');

    const markup = renderToStaticMarkup(<TeamMemorySummaryCard agentId="agent-1" section="workspace" />);

    expect(markup).toContain('Deploy Playbook');
    expect(markup).toContain('Document rollback before the final promotion.');
    expect(mutationConfigs).toHaveLength(2);
    expect(mutationConfigs.every((config) => typeof config.onError === 'function')).toBe(true);
  });

  it('builds query keys without agent-specific cache fragments and guards delete confirmation', async () => {
    const {
      buildTeamMemoryEntryQueryKey,
      buildTeamMemoryQueryKey,
      buildTeamMemoryUpsertRequest,
      confirmTeamMemoryDelete,
      formatTeamMemoryMutationError,
    } = await import('./TeamMemorySummaryCard');

    expect(buildTeamMemoryQueryKey('workspace')).toEqual(['team-memory', 'workspace']);
    expect(buildTeamMemoryEntryQueryKey('workspace', 'deploy-playbook')).toEqual([
      'team-memory-entry',
      'workspace',
      'deploy-playbook',
    ]);
    expect(
      buildTeamMemoryUpsertRequest(
        'workspace',
        {
          key: 'deploy-playbook',
          title: 'Deploy Playbook',
          content: 'Use a canary rollout.',
          mode: 'append',
        },
        {
          revision: 3,
        },
      ),
    ).toMatchObject({
      base_revision: 3,
      mode: 'append',
    });
    expect(confirmTeamMemoryDelete(() => false, 'Delete entry?')).toBe(false);
    expect(confirmTeamMemoryDelete(() => true, 'Delete entry?')).toBe(true);
    expect(
      formatTeamMemoryMutationError({ status: 409, detail: 'Team memory entry revision conflict.' }, 'fallback'),
    ).toContain('conflict');
  });
});

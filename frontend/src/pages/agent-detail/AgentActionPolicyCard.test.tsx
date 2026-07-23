import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { autonomyApi, type OwnerActionPolicyActions } from '../../api/domains/autonomy';
import AgentActionPolicyCard, {
  persistOwnerActionPolicy,
  restorePreviousOwnerActionPolicy,
} from './AgentActionPolicyCard';

const policy = {
  schema: 'hive.owner_action_policy.v1' as const,
  actions: {
    'tool.external_effect': 'confirm_first',
    'tool.local_read': 'full_authority',
    'tool.local_write': 'never_do',
  } satisfies OwnerActionPolicyActions,
  version: 2,
  revision_id: 'revision-2',
  content_hash: 'hash-v2',
  source: 'user',
  valid: true,
  error_code: null,
  can_manage: true,
};

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: string[] }) => (
    queryKey[0] === 'owner-action-policy-history'
      ? {
        data: {
          items: [
            { version: 2, is_active: true, change_source: 'user', created_at: '2026-07-24T01:00:00Z' },
            { version: 1, is_active: false, change_source: 'migration', created_at: '2026-07-24T00:00:00Z' },
          ],
        },
        isLoading: false,
        error: null,
      }
      : { data: policy, isLoading: false, error: null }
  ),
  useQueryClient: () => ({ setQueryData: vi.fn(), invalidateQueries: vi.fn() }),
}));

vi.mock('../../api/domains/autonomy', async () => {
  const actual = await vi.importActual<typeof import('../../api/domains/autonomy')>(
    '../../api/domains/autonomy',
  );
  return {
    ...actual,
    autonomyApi: {
      ...actual.autonomyApi,
      getActionPolicy: vi.fn(),
      getActionPolicyHistory: vi.fn(),
      updateActionPolicy: vi.fn(),
      rollbackActionPolicy: vi.fn(),
    },
  };
});

describe('AgentActionPolicyCard', () => {
  beforeEach(() => {
    vi.mocked(autonomyApi.updateActionPolicy).mockResolvedValue(policy);
    vi.mocked(autonomyApi.rollbackActionPolicy).mockResolvedValue({
      ...policy,
      version: 3,
      source: 'rollback',
    });
  });

  it('shows business action boundaries without runtime implementation details', () => {
    const markup = renderToStaticMarkup(
      <AgentActionPolicyCard agentId="agent-1" canManage={false} />,
    );

    expect(markup).toContain('Action boundaries');
    expect(markup).toContain('External actions');
    expect(markup).toContain('Internal read-only work');
    expect(markup).toContain('Internal changes');
    expect(markup).toContain('Ask first');
    expect(markup).not.toContain('tool.external_effect');
    expect(markup).not.toContain('handler_name');
    expect(markup.toLowerCase()).not.toContain('hook');
    expect(markup).not.toContain('Save action policy');
  });

  it('offers typed choices only to managers', () => {
    const markup = renderToStaticMarkup(
      <AgentActionPolicyCard agentId="agent-1" canManage />,
    );

    expect(markup).toContain('Save action policy');
    expect(markup).toContain('Do directly');
    expect(markup).toContain('Ask first');
    expect(markup).toContain('Never do');
    expect(markup).toContain('name="owner-action-policy-tool.external_effect"');
    expect(markup).toContain('Restore previous policy');
    expect(markup).not.toContain('revision-2');
    expect(markup).not.toContain('Version 1');
  });

  it('persists the complete exact policy with optimistic version binding', async () => {
    await persistOwnerActionPolicy('agent-1', policy.actions, 2);

    expect(autonomyApi.updateActionPolicy).toHaveBeenCalledWith('agent-1', {
      actions: policy.actions,
      expected_version: 2,
    });
  });

  it('restores the previous business policy through optimistic version binding', async () => {
    await restorePreviousOwnerActionPolicy('agent-1', 1, 2);

    expect(autonomyApi.rollbackActionPolicy).toHaveBeenCalledWith('agent-1', {
      target_version: 1,
      expected_version: 2,
      reason: 'Restore previous action policy from employee settings.',
    });
  });
});

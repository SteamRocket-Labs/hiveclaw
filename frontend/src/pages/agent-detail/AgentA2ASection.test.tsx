import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { a2aApi } from '../../api/domains/a2a';

const queryCalls = vi.hoisted(() => [] as Array<{ queryKey: unknown[]; enabled?: boolean }>);

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('../../components/AppDialogs', () => ({
  showAppToast: vi.fn(),
  requestAppConfirm: vi.fn(async () => true),
}));

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
  useMutation: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
  useQuery: ({ queryKey, enabled }: { queryKey: unknown[]; enabled?: boolean }) => {
    queryCalls.push({ queryKey, enabled });
    if (queryKey[0] === 'a2a-collaborators') {
      return {
        data: {
          same_owner_agents: [],
          public_agents: [],
          collaboration_groups: [],
        },
        isLoading: false,
        isError: false,
      };
    }
    if (queryKey[0] === 'a2a-management') {
      return {
        data: {
          groups: [
            {
              group_id: 'group-1',
              group_name: 'Launch room',
              purpose: 'Ship together',
              status: 'active',
              can_invite: true,
              members: [
                {
                  member_id: 'membership-secret-id',
                  agent_id: 'target-agent-id',
                  name: 'Risk Reviewer',
                  role_description: 'Reviews launch risk',
                  role: 'specialist',
                  status: 'pending_owner_confirmation',
                  owner_name: 'Target Owner',
                  owner_relation: 'you',
                  invitation_reason: 'Need risk sign-off',
                  capability_scope: {},
                  can_approve: true,
                  can_reject: true,
                  can_revoke: false,
                  moderation_reason_required: false,
                },
              ],
            },
          ],
        },
        isLoading: false,
        isError: false,
      };
    }
    return {
      data: { candidates: [] },
      isLoading: false,
      isError: false,
    };
  },
}));

import AgentA2ASection, { executeMemberAction } from './AgentA2ASection';

describe('AgentA2ASection management plane', () => {
  beforeEach(() => {
    queryCalls.length = 0;
  });

  it('does not load or expose group management to a use-only employee viewer', () => {
    const markup = renderToStaticMarkup(<AgentA2ASection agentId="agent-1" canManage={false} />);

    const managementQuery = queryCalls.find((call) => call.queryKey[0] === 'a2a-management');
    expect(managementQuery?.enabled).toBe(false);
    expect(markup).not.toContain('Manage collaboration groups');
    expect(markup).not.toContain('Create group');
  });

  it('shows pending approval and group creation controls to an Agent manager without raw ids', () => {
    const markup = renderToStaticMarkup(<AgentA2ASection agentId="agent-1" canManage />);

    const managementQuery = queryCalls.find((call) => call.queryKey[0] === 'a2a-management');
    expect(managementQuery?.enabled).toBe(true);
    expect(markup).toContain('Manage collaboration groups');
    expect(markup).toContain('Create group');
    expect(markup).toContain('Risk Reviewer');
    expect(markup).toContain('Awaiting owner confirmation');
    expect(markup).toContain('Approve');
    expect(markup).toContain('Reject');
    expect(markup).toContain('Invite an employee');
    expect(markup).not.toContain('membership-secret-id');
    expect(markup).not.toContain('target-agent-id');
  });

  it('routes each moderation action to its exact governed API', async () => {
    const approve = vi.spyOn(a2aApi, 'approveGroupMember').mockResolvedValue({
      status: 'ok',
      member_status: 'active',
    });
    const reject = vi.spyOn(a2aApi, 'rejectGroupMember').mockResolvedValue({
      status: 'ok',
      member_status: 'rejected',
    });
    const revoke = vi.spyOn(a2aApi, 'revokeGroupMember').mockResolvedValue({
      status: 'ok',
      member_status: 'revoked',
    });

    await executeMemberAction('agent-1', {
      action: 'approve',
      groupId: 'group-1',
      memberId: 'member-1',
      reason: 'Owner approved',
    });
    await executeMemberAction('agent-1', {
      action: 'reject',
      groupId: 'group-1',
      memberId: 'member-1',
      reason: 'Out of scope',
    });
    await executeMemberAction('agent-1', {
      action: 'revoke',
      groupId: 'group-1',
      memberId: 'member-1',
      reason: 'Project complete',
    });

    expect(approve).toHaveBeenCalledWith('agent-1', 'group-1', 'member-1', 'Owner approved');
    expect(reject).toHaveBeenCalledWith('agent-1', 'group-1', 'member-1', 'Out of scope');
    expect(revoke).toHaveBeenCalledWith('agent-1', 'group-1', 'member-1', 'Project complete');
  });
});

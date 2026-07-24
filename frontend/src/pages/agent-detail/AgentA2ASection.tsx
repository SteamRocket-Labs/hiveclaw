import { type FormEvent, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import {
  a2aApi,
  type A2ACollaboratorAgent,
  type A2ACollaborationGroup,
  type A2AGroupMemberRole,
  type A2AInviteCandidate,
  type A2AManagementGroup,
  type A2AManagementMember,
} from '../../api/domains/a2a';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import './AgentA2ASection.css';

type AgentA2ASectionProps = {
  agentId: string;
  canManage?: boolean;
};

type MemberAction = 'approve' | 'reject' | 'revoke';

type MemberActionInput = {
  action: MemberAction;
  groupId: string;
  memberId: string;
  reason: string;
};

type InviteInput = {
  groupId: string;
  targetAgentId: string;
  role: A2AGroupMemberRole;
  invitationReason: string;
};

function statusLabel(status: string | undefined, t: ReturnType<typeof useTranslation>['t']): string {
  const labels: Record<string, string> = {
    active: t('agent.a2a.status.active', 'Active'),
    pending_owner_confirmation: t('agent.a2a.status.pending', 'Awaiting owner confirmation'),
    rejected: t('agent.a2a.status.rejected', 'Rejected'),
    revoked: t('agent.a2a.status.revoked', 'Revoked'),
    expired: t('agent.a2a.status.expired', 'Expired'),
  };
  return labels[String(status || '')] || t('agent.a2a.status.unknown', 'Unavailable');
}

function normalizeMemberRole(role: string | undefined): A2AGroupMemberRole {
  if (role === 'coordinator' || role === 'specialist' || role === 'observer') return role;
  return 'member';
}

function memberRoleLabel(
  role: string | undefined,
  t: ReturnType<typeof useTranslation>['t'],
): string {
  const normalized = normalizeMemberRole(role);
  const fallback = normalized === 'member'
    ? 'Member'
    : normalized === 'coordinator'
      ? 'Coordinator'
      : normalized === 'specialist'
        ? 'Specialist'
        : 'Observer';
  return t(`agent.a2a.roles.${normalized}`, fallback);
}

export async function executeMemberAction(agentId: string, input: MemberActionInput) {
  if (input.action === 'approve') {
    return a2aApi.approveGroupMember(agentId, input.groupId, input.memberId, input.reason);
  }
  if (input.action === 'reject') {
    return a2aApi.rejectGroupMember(agentId, input.groupId, input.memberId, input.reason);
  }
  return a2aApi.revokeGroupMember(agentId, input.groupId, input.memberId, input.reason);
}

function AgentRow({
  agent,
  badge,
  noDescription,
}: {
  agent: A2ACollaboratorAgent;
  badge: string;
  noDescription: string;
}) {
  return (
    <div className="agent-a2a-row">
      <div className="agent-a2a-avatar">{agent.name?.charAt(0) || 'A'}</div>
      <div className="agent-a2a-row-body">
        <div className="agent-a2a-name">{agent.name}</div>
        <div className="agent-a2a-meta">{agent.role_description || noDescription}</div>
      </div>
      <div className="agent-a2a-badge">{badge}</div>
    </div>
  );
}

function AgentList({
  title,
  description,
  badge,
  agents,
  empty,
  noDescription,
}: {
  title: string;
  description: string;
  badge: string;
  agents: A2ACollaboratorAgent[];
  empty: string;
  noDescription: string;
}) {
  return (
    <section className="agent-a2a-list">
      <h4 className="agent-a2a-heading">{title}</h4>
      <p className="agent-a2a-desc">{description}</p>
      {agents.length > 0 ? (
        <div className="agent-a2a-agents">
          {agents.map((agent) => (
            <AgentRow key={agent.id} agent={agent} badge={badge} noDescription={noDescription} />
          ))}
        </div>
      ) : (
        <div className="agent-a2a-empty">{empty}</div>
      )}
    </section>
  );
}

function CallableGroupList({ groups }: { groups: A2ACollaborationGroup[] }) {
  const { t } = useTranslation();
  if (groups.length === 0) {
    return (
      <div className="agent-a2a-empty">
        {t('agent.a2a.noCollaborationGroups', 'No approved A2A collaboration groups.')}
      </div>
    );
  }
  return (
    <div className="agent-a2a-groups">
      {groups.map((group) => (
        <div key={group.group_id} className="agent-a2a-group">
          <div className="agent-a2a-group-header">
            <div>
              <div className="agent-a2a-group-name">
                {group.group_name || t('agent.a2a.unnamedGroup', 'Unnamed group')}
              </div>
              {group.purpose && <div className="agent-a2a-meta">{group.purpose}</div>}
            </div>
            <div className="agent-a2a-group-status is-active">
              {statusLabel(group.status, t)}
            </div>
          </div>
          <div className="agent-a2a-members">
            {(group.members || []).map((member) => (
              <div key={member.agent_id || member.id} className="agent-a2a-member">
                <div className="agent-a2a-member-avatar">{member.name?.charAt(0) || 'A'}</div>
                <div className="agent-a2a-member-body">
                  <div className="agent-a2a-member-name">{member.name}</div>
                  <div className="agent-a2a-member-role">
                    {member.role_description || t('agent.a2a.noDescription', 'No description')}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ManagedMemberRow({
  member,
  groupCanInvite,
  busy,
  onAction,
  onReinvite,
}: {
  member: A2AManagementMember;
  groupCanInvite: boolean;
  busy: boolean;
  onAction: (input: MemberActionInput) => void;
  onReinvite: (input: InviteInput) => void;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const hasModerationAction = member.can_approve || member.can_reject || member.can_revoke;
  const reasonMissing = member.moderation_reason_required && !reason.trim();
  const canReinvite = groupCanInvite && (member.status === 'rejected' || member.status === 'revoked');
  const action = async (nextAction: MemberAction) => {
    if (nextAction === 'reject' || nextAction === 'revoke') {
      const confirmed = await requestAppConfirm({
        title: nextAction === 'revoke'
          ? t('agent.a2a.revokeConfirmTitle', 'Revoke collaboration access?')
          : t('agent.a2a.rejectConfirmTitle', 'Reject this invitation?'),
        message: nextAction === 'revoke'
          ? t(
              'agent.a2a.revokeConfirmMessage',
              'This employee will immediately leave the group callable list. You can invite them again later.',
            )
          : t(
              'agent.a2a.rejectConfirmMessage',
              'The invitation will be rejected. The group can send a new invitation later.',
            ),
        confirmLabel: nextAction === 'revoke'
          ? t('agent.a2a.revoke', 'Revoke')
          : t('agent.a2a.reject', 'Reject'),
        cancelLabel: t('common.cancel', 'Cancel'),
        danger: true,
      });
      if (!confirmed) return;
    }
    onAction({
      action: nextAction,
      groupId: '',
      memberId: member.member_id,
      reason: reason.trim(),
    });
  };

  return (
    <div className="agent-a2a-managed-member">
      <div className="agent-a2a-managed-member-main">
        <div className="agent-a2a-member-avatar">{member.name?.charAt(0) || 'A'}</div>
        <div className="agent-a2a-member-body">
          <div className="agent-a2a-member-name">{member.name}</div>
          <div className="agent-a2a-member-role">
            {member.role_description || t('agent.a2a.noDescription', 'No description')}
            {' · '}
            {member.owner_relation === 'you'
              ? t('agent.a2a.ownerYou', 'Owned by you')
              : t('agent.a2a.ownerOther', 'Owned by {{name}}', { name: member.owner_name })}
            {' · '}
            {memberRoleLabel(member.role, t)}
          </div>
          {member.invitation_reason && (
            <div className="agent-a2a-invitation-reason">
              {t('agent.a2a.invitationReason', 'Invitation: {{reason}}', {
                reason: member.invitation_reason,
              })}
            </div>
          )}
        </div>
        <div className={`agent-a2a-group-status is-${member.status}`}>
          {statusLabel(member.status, t)}
        </div>
      </div>
      {(hasModerationAction || canReinvite) && (
        <div className="agent-a2a-member-controls">
          {hasModerationAction && (
            <input
              type="text"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={
                member.moderation_reason_required
                  ? t('agent.a2a.adminReasonRequired', 'Governance reason required')
                  : t('agent.a2a.actionReason', 'Reason (optional)')
              }
              aria-label={t('agent.a2a.actionReason', 'Reason (optional)')}
            />
          )}
          <div className="agent-a2a-member-actions">
            {member.can_approve && (
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={busy || reasonMissing}
                onClick={() => void action('approve')}
              >
                {t('agent.a2a.approve', 'Approve')}
              </button>
            )}
            {member.can_reject && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busy || reasonMissing}
                onClick={() => void action('reject')}
              >
                {t('agent.a2a.reject', 'Reject')}
              </button>
            )}
            {member.can_revoke && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busy || reasonMissing}
                onClick={() => void action('revoke')}
              >
                {t('agent.a2a.revoke', 'Revoke')}
              </button>
            )}
            {canReinvite && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busy}
                onClick={() =>
                  onReinvite({
                    groupId: '',
                    targetAgentId: member.agent_id,
                    role: normalizeMemberRole(member.role),
                    invitationReason: member.invitation_reason || '',
                  })
                }
              >
                {t('agent.a2a.reinvite', 'Invite again')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function InviteMemberControl({
  agentId,
  group,
  busy,
  onInvite,
}: {
  agentId: string;
  group: A2AManagementGroup;
  busy: boolean;
  onInvite: (input: InviteInput) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [role, setRole] = useState<A2AGroupMemberRole>('member');
  const [invitationReason, setInvitationReason] = useState('');
  const trimmedQuery = query.trim();
  const { data, isLoading } = useQuery({
    queryKey: ['a2a-invite-candidates', agentId, group.group_id, trimmedQuery],
    queryFn: () => a2aApi.searchInviteCandidates(agentId, group.group_id, trimmedQuery),
    enabled: group.can_invite && trimmedQuery.length >= 2,
  });
  const candidates = trimmedQuery.length >= 2 ? data?.candidates || [] : [];
  const invite = (candidate: A2AInviteCandidate) => {
    if (candidate.invite_action === 'pending' || candidate.invite_action === 'already_active') return;
    onInvite({
      groupId: group.group_id,
      targetAgentId: candidate.agent_id,
      role,
      invitationReason: invitationReason.trim(),
    });
  };

  return (
    <div className="agent-a2a-invite">
      <div className="agent-a2a-invite-title">{t('agent.a2a.inviteEmployee', 'Invite an employee')}</div>
      <p className="agent-a2a-desc">
        {t(
          'agent.a2a.inviteEmployeeDesc',
          'Search within your company. Another owner must approve before private cross-owner A2A becomes callable.',
        )}
      </p>
      <div className="agent-a2a-invite-fields">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('agent.a2a.searchEmployee', 'Search by employee name or role')}
          aria-label={t('agent.a2a.searchEmployee', 'Search by employee name or role')}
        />
        <select value={role} onChange={(event) => setRole(event.target.value as A2AGroupMemberRole)}>
          <option value="member">{t('agent.a2a.roles.member', 'Member')}</option>
          <option value="coordinator">{t('agent.a2a.roles.coordinator', 'Coordinator')}</option>
          <option value="specialist">{t('agent.a2a.roles.specialist', 'Specialist')}</option>
          <option value="observer">{t('agent.a2a.roles.observer', 'Observer')}</option>
        </select>
        <input
          type="text"
          value={invitationReason}
          onChange={(event) => setInvitationReason(event.target.value)}
          placeholder={t('agent.a2a.invitationReasonPlaceholder', 'Why this employee is needed')}
          aria-label={t('agent.a2a.invitationReasonPlaceholder', 'Why this employee is needed')}
        />
      </div>
      {isLoading && <div className="agent-a2a-empty">{t('common.loading', 'Loading...')}</div>}
      {!isLoading && trimmedQuery.length >= 2 && candidates.length === 0 && (
        <div className="agent-a2a-empty">{t('agent.a2a.noInviteCandidates', 'No matching employees.')}</div>
      )}
      {candidates.length > 0 && (
        <div className="agent-a2a-candidates">
          {candidates.map((candidate) => {
            const unavailable =
              candidate.invite_action === 'pending' || candidate.invite_action === 'already_active';
            return (
              <div key={candidate.agent_id} className="agent-a2a-candidate">
                <div>
                  <div className="agent-a2a-member-name">{candidate.name}</div>
                  <div className="agent-a2a-member-role">
                    {candidate.role_description || t('agent.a2a.noDescription', 'No description')}
                    {' · '}
                    {candidate.owner_relation === 'you'
                      ? t('agent.a2a.ownerYou', 'Owned by you')
                      : t('agent.a2a.ownerOther', 'Owned by {{name}}', { name: candidate.owner_name })}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={busy || unavailable}
                  onClick={() => invite(candidate)}
                >
                  {candidate.invite_action === 'reinvite'
                    ? t('agent.a2a.reinvite', 'Invite again')
                    : candidate.invite_action === 'pending'
                      ? t('agent.a2a.status.pending', 'Awaiting owner confirmation')
                      : candidate.invite_action === 'already_active'
                        ? t('agent.a2a.status.active', 'Active')
                        : t('agent.a2a.invite', 'Invite')}
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ManagementPanel({
  agentId,
  groups,
  loading,
  failed,
  actionBusy,
  inviteBusy,
  createBusy,
  onCreate,
  onMemberAction,
  onInvite,
}: {
  agentId: string;
  groups: A2AManagementGroup[];
  loading: boolean;
  failed: boolean;
  actionBusy: boolean;
  inviteBusy: boolean;
  createBusy: boolean;
  onCreate: (input: { name: string; purpose: string }) => Promise<unknown>;
  onMemberAction: (input: MemberActionInput) => void;
  onInvite: (input: InviteInput) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [purpose, setPurpose] = useState('');
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName || createBusy) return;
    try {
      await onCreate({ name: trimmedName, purpose: purpose.trim() });
      setName('');
      setPurpose('');
    } catch {
      // The mutation owns the user-facing error; preserve the form for retry.
    }
  };

  return (
    <section className="agent-a2a-management">
      <h4 className="agent-a2a-heading-spaced">
        {t('agent.a2a.manageGroups', 'Manage collaboration groups')}
      </h4>
      <p className="agent-a2a-desc">
        {t(
          'agent.a2a.manageGroupsDesc',
          'Pending and revoked memberships stay in this management view and never enter the callable employee list.',
        )}
      </p>
      <form className="agent-a2a-create-group" onSubmit={submit}>
        <input
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={t('agent.a2a.groupName', 'Group name')}
          aria-label={t('agent.a2a.groupName', 'Group name')}
          required
        />
        <input
          type="text"
          value={purpose}
          onChange={(event) => setPurpose(event.target.value)}
          placeholder={t('agent.a2a.groupPurpose', 'Purpose and collaboration boundary')}
          aria-label={t('agent.a2a.groupPurpose', 'Purpose and collaboration boundary')}
        />
        <button type="submit" className="btn btn-primary btn-sm" disabled={createBusy}>
          {createBusy ? t('common.saving', 'Saving...') : t('agent.a2a.createGroup', 'Create group')}
        </button>
      </form>
      {loading && <div className="agent-a2a-empty">{t('common.loading', 'Loading...')}</div>}
      {failed && (
        <div className="agent-a2a-empty is-error">
          {t('agent.a2a.managementLoadFailed', 'Failed to load collaboration group management.')}
        </div>
      )}
      {!loading && !failed && groups.length === 0 && (
        <div className="agent-a2a-empty">{t('agent.a2a.noManagedGroups', 'No collaboration groups yet.')}</div>
      )}
      <div className="agent-a2a-managed-groups">
        {groups.map((group) => {
          const bindGroupToAction = (input: MemberActionInput) =>
            onMemberAction({ ...input, groupId: group.group_id });
          const bindGroupToInvite = (input: InviteInput) =>
            onInvite({ ...input, groupId: group.group_id });
          return (
            <div key={group.group_id} className="agent-a2a-managed-group">
              <div className="agent-a2a-group-header">
                <div>
                  <div className="agent-a2a-group-name">{group.group_name}</div>
                  {group.purpose && <div className="agent-a2a-meta">{group.purpose}</div>}
                </div>
                <div className={`agent-a2a-group-status is-${group.status}`}>
                  {statusLabel(group.status, t)}
                </div>
              </div>
              <div className="agent-a2a-managed-members">
                {group.members.map((member) => (
                  <ManagedMemberRow
                    key={member.member_id}
                    member={member}
                    groupCanInvite={group.can_invite}
                    busy={actionBusy || inviteBusy}
                    onAction={bindGroupToAction}
                    onReinvite={bindGroupToInvite}
                  />
                ))}
              </div>
              {group.can_invite && (
                <InviteMemberControl
                  agentId={agentId}
                  group={group}
                  busy={inviteBusy}
                  onInvite={bindGroupToInvite}
                />
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

export default function AgentA2ASection({ agentId, canManage = false }: AgentA2ASectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const collaboratorsQuery = useQuery({
    queryKey: ['a2a-collaborators', agentId],
    queryFn: () => a2aApi.listCollaborators(agentId),
    enabled: !!agentId,
  });
  const managementQuery = useQuery({
    queryKey: ['a2a-management', agentId],
    queryFn: () => a2aApi.getManagement(agentId),
    enabled: !!agentId && canManage,
  });

  const refreshA2A = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['a2a-collaborators', agentId] }),
      queryClient.invalidateQueries({ queryKey: ['a2a-management', agentId] }),
      queryClient.invalidateQueries({ queryKey: ['a2a-invite-candidates', agentId] }),
    ]);
  };
  const createMutation = useMutation({
    mutationFn: (input: { name: string; purpose: string }) => a2aApi.createGroup(agentId, input),
    onSuccess: async () => {
      showAppToast(t('agent.a2a.groupCreated', 'Collaboration group created.'), 'success');
      await refreshA2A();
    },
    onError: (error: Error) =>
      showAppToast(error.message || t('agent.a2a.groupCreateFailed', 'Failed to create collaboration group.'), 'error'),
  });
  const memberMutation = useMutation({
    mutationFn: (input: MemberActionInput) => executeMemberAction(agentId, input),
    onSuccess: async () => {
      showAppToast(t('agent.a2a.membershipUpdated', 'Collaboration membership updated.'), 'success');
      await refreshA2A();
    },
    onError: (error: Error) =>
      showAppToast(error.message || t('agent.a2a.membershipUpdateFailed', 'Failed to update membership.'), 'error'),
  });
  const inviteMutation = useMutation({
    mutationFn: (input: InviteInput) =>
      a2aApi.inviteGroupMember(agentId, input.groupId, {
        target_agent_id: input.targetAgentId,
        role: input.role,
        invitation_reason: input.invitationReason,
      }),
    onSuccess: async (result) => {
      showAppToast(
        result.requires_owner_confirmation
          ? t('agent.a2a.invitationPending', 'Invitation sent. The other owner must approve it.')
          : t('agent.a2a.invitationActive', 'Employee added to the collaboration group.'),
        'success',
      );
      await refreshA2A();
    },
    onError: (error: Error) =>
      showAppToast(error.message || t('agent.a2a.invitationFailed', 'Failed to invite employee.'), 'error'),
  });

  const data = collaboratorsQuery.data;
  if (collaboratorsQuery.isLoading) {
    return <div className="card">{t('common.loading', 'Loading...')}</div>;
  }
  if (collaboratorsQuery.isError) {
    return <div className="card">{t('agent.a2a.loadFailed', 'Failed to load A2A collaborators.')}</div>;
  }

  return (
    <div className="card">
      <AgentList
        title={t('agent.a2a.sameOwnerAgents', 'Same-owner agents')}
        description={t('agent.a2a.sameOwnerAgentsDesc', 'Agents owned by the same user can collaborate directly.')}
        badge={t('agent.a2a.sameOwnerBadge', 'same owner')}
        agents={data?.same_owner_agents || []}
        empty={t('agent.a2a.noSameOwnerAgents', 'No same-owner collaborators.')}
        noDescription={t('agent.a2a.noDescription', 'No description')}
      />
      <AgentList
        title={t('agent.a2a.publicAgents', 'Public agents')}
        description={t(
          'agent.a2a.publicAgentsDesc',
          'Public same-tenant agents can collaborate directly while they remain public.',
        )}
        badge={t('agent.a2a.publicBadge', 'public')}
        agents={data?.public_agents || []}
        empty={t('agent.a2a.noPublicAgents', 'No public collaborators.')}
        noDescription={t('agent.a2a.noDescription', 'No description')}
      />
      <section>
        <h4 className="agent-a2a-heading-spaced">
          {t('agent.a2a.collaborationGroups', 'A2A Collaboration Groups')}
        </h4>
        <p className="agent-a2a-desc">
          {t(
            'agent.a2a.collaborationGroupsDesc',
            'Cross-owner private A2A requires an approved collaboration group.',
          )}
        </p>
        <CallableGroupList groups={data?.collaboration_groups || []} />
      </section>
      {canManage && (
        <ManagementPanel
          agentId={agentId}
          groups={managementQuery.data?.groups || []}
          loading={managementQuery.isLoading}
          failed={managementQuery.isError}
          actionBusy={memberMutation.isPending}
          inviteBusy={inviteMutation.isPending}
          createBusy={createMutation.isPending}
          onCreate={(input) => createMutation.mutateAsync(input)}
          onMemberAction={(input) => memberMutation.mutate(input)}
          onInvite={(input) => inviteMutation.mutate(input)}
        />
      )}
    </div>
  );
}

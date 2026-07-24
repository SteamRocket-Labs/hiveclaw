import { get, post } from '../core';

export interface A2ACollaboratorAgent {
  id: string;
  name: string;
  role_description?: string;
  status?: string;
  relation?: string;
  owner_user_id?: string;
}

export interface A2ACollaborationGroupMember {
  id?: string;
  agent_id: string;
  name: string;
  role_description?: string;
  status?: string;
  role?: string;
  owner_user_id?: string;
}

export interface A2ACollaborationGroup {
  group_id: string;
  group_name: string;
  purpose?: string;
  status?: string;
  members: A2ACollaborationGroupMember[];
}

export interface A2ACollaboratorsResponse {
  same_owner_agents: A2ACollaboratorAgent[];
  public_agents: A2ACollaboratorAgent[];
  collaboration_groups: A2ACollaborationGroup[];
}

export type A2AGroupMemberRole = 'coordinator' | 'member' | 'specialist' | 'observer';

export interface A2AManagementMember {
  member_id: string;
  agent_id: string;
  name: string;
  role_description?: string;
  agent_status?: string;
  role: string;
  status: string;
  owner_name: string;
  owner_relation: 'you' | 'another_owner';
  invitation_reason?: string;
  capability_scope?: Record<string, unknown>;
  can_approve: boolean;
  can_reject: boolean;
  can_revoke: boolean;
  moderation_reason_required: boolean;
}

export interface A2AManagementGroup {
  group_id: string;
  group_name: string;
  purpose?: string;
  status: string;
  visibility?: string;
  expires_at?: string | null;
  can_invite: boolean;
  members: A2AManagementMember[];
}

export interface A2AManagementResponse {
  groups: A2AManagementGroup[];
}

export interface A2AInviteCandidate {
  agent_id: string;
  name: string;
  role_description?: string;
  status: string;
  owner_name: string;
  owner_relation: 'you' | 'another_owner';
  membership_status?: string | null;
  invite_action: 'invite' | 'reinvite' | 'pending' | 'already_active';
}

export interface A2AInviteCandidatesResponse {
  candidates: A2AInviteCandidate[];
}

export interface A2AGroupCreateInput {
  name: string;
  purpose?: string;
  visibility?: string;
}

export interface A2AGroupInviteInput {
  target_agent_id: string;
  role?: A2AGroupMemberRole;
  invitation_reason?: string;
  capability_scope?: Record<string, unknown>;
}

export const a2aApi = {
  listCollaborators: (agentId: string) =>
    get<A2ACollaboratorsResponse>(`/agents/${agentId}/a2a/collaborators`),
  getManagement: (agentId: string) =>
    get<A2AManagementResponse>(`/agents/${agentId}/a2a/management`),
  searchInviteCandidates: (agentId: string, groupId: string, query: string) =>
    get<A2AInviteCandidatesResponse>(
      `/agents/${agentId}/a2a/groups/${groupId}/invite-candidates?q=${encodeURIComponent(query)}`,
    ),
  createGroup: (agentId: string, data: A2AGroupCreateInput) =>
    post<{ status: string; group_id: string; group_name: string }>(`/agents/${agentId}/a2a/groups`, data),
  inviteGroupMember: (agentId: string, groupId: string, data: A2AGroupInviteInput) =>
    post<{ status: string; member_id: string; member_status: string; requires_owner_confirmation: boolean }>(
      `/agents/${agentId}/a2a/groups/${groupId}/members`,
      data,
    ),
  approveGroupMember: (agentId: string, groupId: string, memberId: string, reason = '') =>
    post<{ status: string; member_status: string }>(
      `/agents/${agentId}/a2a/groups/${groupId}/members/${memberId}/approve`,
      { reason },
    ),
  rejectGroupMember: (agentId: string, groupId: string, memberId: string, reason = '') =>
    post<{ status: string; member_status: string }>(
      `/agents/${agentId}/a2a/groups/${groupId}/members/${memberId}/reject`,
      { reason },
    ),
  revokeGroupMember: (agentId: string, groupId: string, memberId: string, reason = '') =>
    post<{ status: string; member_status: string }>(
      `/agents/${agentId}/a2a/groups/${groupId}/members/${memberId}/revoke`,
      { reason },
    ),
};

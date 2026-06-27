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

export interface A2AGroupCreateInput {
  name: string;
  purpose?: string;
  visibility?: string;
}

export interface A2AGroupInviteInput {
  target_agent_id: string;
  role?: string;
  invitation_reason?: string;
  capability_scope?: Record<string, unknown>;
}

export const a2aApi = {
  listCollaborators: (agentId: string) =>
    get<A2ACollaboratorsResponse>(`/agents/${agentId}/a2a/collaborators`),
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

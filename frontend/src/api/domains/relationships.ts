import { get, put, del } from '../core';

export interface HumanRelationshipInput {
  member_id: string;
  relation: string;
  description: string;
}

export interface AgentRelationshipInput {
  target_agent_id: string;
  relation: string;
  description: string;
}

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
  collaboration_groups: A2ACollaborationGroup[];
}

export const relationshipsApi = {
  listHuman: (agentId: string) => get<unknown[]>(`/agents/${agentId}/relationships/`),
  saveHuman: (agentId: string, relationships: HumanRelationshipInput[]) =>
    put<{ status: string }>(`/agents/${agentId}/relationships/`, { relationships }),
  removeHuman: (agentId: string, relationshipId: string) =>
    del<{ status: string }>(`/agents/${agentId}/relationships/${relationshipId}`),

  listAgents: (agentId: string) => get<unknown[]>(`/agents/${agentId}/relationships/agents`),
  saveAgents: (agentId: string, relationships: AgentRelationshipInput[]) =>
    put<{ status: string }>(`/agents/${agentId}/relationships/agents`, { relationships }),
  removeAgent: (agentId: string, relationshipId: string) =>
    del<{ status: string }>(`/agents/${agentId}/relationships/agents/${relationshipId}`),
  listA2ACollaborators: (agentId: string) =>
    get<A2ACollaboratorsResponse>(`/agents/${agentId}/relationships/a2a-collaborators`),
};

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
};

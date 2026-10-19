import { get, post } from '../core';

export type MachineScope = 'agent.describe' | 'agent.execute' | 'task.read' | 'task.cancel';
export interface MachineIdentity {
  id: string; credential_id: string; agent_id: string; issue_request_id: string;
  name: string; status: 'active' | 'revoked' | 'expired' | 'needs_reissue' | 'unavailable'; scopes: string[];
  expires_at: string; last_used_at: string | null; revoked_at: string | null; created_at: string;
}
export interface MachineIdentityPage {
  agent_id: string; can_issue: boolean; supported_scopes: string[]; identities: MachineIdentity[];
}
export interface MachineFrame { expected_user_id: string; expected_tenant_id: string }
export interface MachineIssue extends MachineFrame {
  name: string; expires_at: string; scopes: MachineScope[]; issue_request_id: string;
}
export interface MachineIssueReceipt extends MachineIdentity {
  token: string | null; idempotent_replay: boolean;
  secret_status: 'available_once' | 'unavailable_after_issue'; next_action?: 'revoke_and_reissue';
}
export const machineIdentitiesApi = {
  list: (agentId: string) => get<MachineIdentityPage>(`/agents/${agentId}/machine-identities`),
  issue: (agentId: string, body: MachineIssue) => post<MachineIssueReceipt>(`/agents/${agentId}/machine-identities`, body),
  revoke: (agentId: string, id: string, body: MachineFrame) =>
    post<{ id: string; agent_id: string; status: 'revoked'; revoked_at: string }>(`/agents/${agentId}/machine-identities/${id}/revoke`, body),
  descriptionPath: (agentId: string) => `/api/agents/${encodeURIComponent(agentId)}/machine-description`,
  cardPath: (agentId: string) => `/api/agents/${encodeURIComponent(agentId)}/a2a/.well-known/agent-card.json`,
  taskPath: (agentId: string) => `/api/agents/${encodeURIComponent(agentId)}/a2a/rpc`,
};

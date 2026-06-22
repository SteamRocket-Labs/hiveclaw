import { del, get, post } from '../core';

export interface LocalBridgeConnection {
  id: string;
  tenant_id: string;
  agent_id: string;
  user_id: string;
  device_name: string;
  client_kind: string;
  status: string;
  scopes: string[];
  last_seen_at?: string | null;
  created_at?: string | null;
  revoked_at?: string | null;
}

export interface LocalBridgeWorkRequest {
  id: string;
  agent_id: string;
  tenant_id?: string | null;
  sender_user_id?: string | null;
  conversation_id?: string | null;
  content: string;
  status: string;
  result?: string | null;
  attachments: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
  created_at?: string | null;
  delivered_at?: string | null;
  completed_at?: string | null;
}

export const localBridgeApi = {
  listConnections: (agentId: string) =>
    get<{ connections: LocalBridgeConnection[] }>(`/agents/${agentId}/local-bridge/connections`),
  listWorkRequests: (agentId: string) =>
    get<{ work_requests: LocalBridgeWorkRequest[] }>(`/agents/${agentId}/local-bridge/work-requests`),
  getWorkRequest: (agentId: string, messageId: string) =>
    get<LocalBridgeWorkRequest>(`/agents/${agentId}/local-bridge/work-requests/${messageId}`),
  approvePairing: (agentId: string, userCode: string) =>
    post<{ status: string }>(`/agents/${agentId}/local-bridge/pairings/${encodeURIComponent(userCode)}/approve`),
  rejectPairing: (agentId: string, userCode: string) =>
    post<{ status: string }>(`/agents/${agentId}/local-bridge/pairings/${encodeURIComponent(userCode)}/reject`),
  revokeConnection: (agentId: string, connectionId: string) =>
    del<{ status: string }>(`/agents/${agentId}/local-bridge/connections/${connectionId}`),
  createWorkRequest: (agentId: string, content: string, metadata: Record<string, unknown> = {}) =>
    post<{ status: string; message_id: string; conversation_id?: string | null }>(
      `/agents/${agentId}/local-bridge/work-requests`,
      {
        content,
        metadata,
      },
    ),
};

import { get, post, put } from '../core';

export interface RemoteA2AConnection {
  agent_id: string; state: string; version: number | null; request_id: string | null;
  can_configure: boolean; can_disable: boolean; card_url: string | null; rpc_url: string | null;
  credential_configured: boolean; allowed_networks: string[]; max_response_bytes: number;
  timeout_seconds: number; discovered_at: string | null; discovery_error: string | null;
  agent_card: { name?: string; description?: string } | null;
  effect_control: 'unverified'; cost_metering: 'unknown'; runtime_activation: 'not_activated';
}
export interface RemoteA2AFrame {
  expected_user_id: string; expected_tenant_id: string; expected_version: number | null;
}
export interface RemoteA2AConfiguration extends RemoteA2AFrame {
  request_id: string; card_url: string; rpc_url: string; bearer_token?: string;
  allowed_networks: string[]; max_response_bytes: number; timeout_seconds: number;
}
const path = (agentId: string) => `/agents/${encodeURIComponent(agentId)}/remote-a2a`;
export const remoteA2AApi = {
  get: (agentId: string) => get<RemoteA2AConnection>(path(agentId)),
  configure: (agentId: string, body: RemoteA2AConfiguration) => put<RemoteA2AConnection>(path(agentId), body),
  discover: (agentId: string, body: RemoteA2AFrame) => post<RemoteA2AConnection>(`${path(agentId)}/discover`, body),
  disable: (agentId: string, body: RemoteA2AFrame) => post<RemoteA2AConnection>(`${path(agentId)}/disable`, body),
};

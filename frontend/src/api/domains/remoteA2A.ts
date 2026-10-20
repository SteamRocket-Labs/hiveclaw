import { get, post, put } from '../core';

export interface RemoteA2AConnection {
  agent_id: string; state: string; version: number | null; request_id: string | null;
  can_configure: boolean; can_disable: boolean; card_url: string | null; rpc_url: string | null;
  credential_configured: boolean; allowed_networks: string[]; max_response_bytes: number;
  timeout_seconds: number; discovered_at: string | null; discovery_error: string | null;
  agent_card: { name?: string; description?: string } | null;
  effect_control: 'unverified'; cost_metering: 'unknown';
  runtime_activation: 'not_activated' | 'activated' | 'authorization_required';
  runtime_revision?: number | null; runtime_request_id?: string | null; previous_runtime?: 'native' | 'local_agent' | null;
}
export interface RemoteA2AFrame {
  expected_user_id: string; expected_tenant_id: string; expected_version: number | null;
}
export interface RemoteA2AConfiguration extends RemoteA2AFrame {
  request_id: string; card_url: string; rpc_url: string; bearer_token?: string;
  allowed_networks: string[]; max_response_bytes: number; timeout_seconds: number;
}
export interface RemoteRuntimeSelection extends RemoteA2AFrame {
  request_id: string; expected_runtime_revision: number | null; selected_runtime: 'remote_a2a' | 'previous';
  uncontrolled_effects_acknowledged: boolean; unknown_cost_acknowledged: boolean;
}
const path = (agentId: string) => `/agents/${encodeURIComponent(agentId)}/remote-a2a`;
export const remoteA2AApi = {
  get: (agentId: string) => get<RemoteA2AConnection>(path(agentId)),
  configure: (agentId: string, body: RemoteA2AConfiguration) => put<RemoteA2AConnection>(path(agentId), body),
  discover: (agentId: string, body: RemoteA2AFrame) => post<RemoteA2AConnection>(`${path(agentId)}/discover`, body),
  disable: (agentId: string, body: RemoteA2AFrame) => post<RemoteA2AConnection>(`${path(agentId)}/disable`, body),
  selectRuntime: (agentId: string, body: RemoteRuntimeSelection) => post<RemoteA2AConnection>(`${path(agentId)}/runtime`, body),
  tasks: (agentId: string, sessionId: string) => get<RemoteA2ATasks>(`${path(agentId)}/tasks?source_session_id=${encodeURIComponent(sessionId)}`),
  task: (agentId: string, taskId: string) => get<RemoteA2ATask>(`${path(agentId)}/tasks/${encodeURIComponent(taskId)}`),
  startTask: (agentId: string, body: RemoteA2ATaskInput) => post<RemoteA2ATask>(`${path(agentId)}/tasks`, body),
  operateTask: (agentId: string, taskId: string, body: RemoteA2ATaskOperation) => post<RemoteA2ATask>(`${path(agentId)}/tasks/${encodeURIComponent(taskId)}/operations`, body),
  recoverArtifacts: (agentId: string, taskId: string, body: Pick<RemoteA2AFrame, 'expected_user_id' | 'expected_tenant_id'>) =>
    post<RemoteA2AFileReturn>(`${path(agentId)}/tasks/${encodeURIComponent(taskId)}/artifacts`, body),
};

export interface RemoteA2AFileReturn {
  agent_id: string; task_id: string; source_session_id: string;
  parts: Record<string, unknown>[];
  file_returns: { status: string; path?: string; reason_code?: string; recovery_action?: string }[];
}

export interface RemoteA2ATask {
  id: string; agent_id: string; source_session_id: string; request_id: string; latest_operation_id: string;
  configuration_version: number;
  latest_observation_command_id?: string | null;
  automatic_observation?: { status: 'active' | 'paused'; started: number; max_observations: number;
    deadline: string; next_at: string | null; reason: string | null } | null;
  status: string; remote_task_id: string | null; remote_context_id: string | null; remote_state: string | null;
  error_code: string | null; observation?: Record<string, unknown> | null; operations?: Record<string, unknown>[];
  cost_metering: 'unknown'; effect_control: 'unverified'; local_abandoned: boolean; recovery_actions: string[];
}
export interface RemoteA2ATasks {
  agent_id: string; source_session_id: string; connection: { ready: boolean; version: number | null };
  items: RemoteA2ATask[];
}
export interface RemoteA2ATaskInput extends RemoteA2AFrame {
  request_id: string; source_session_id: string; params: Record<string, unknown>;
  uncontrolled_effects_acknowledged: boolean; unknown_cost_acknowledged: boolean;
}
export interface RemoteA2ATaskOperation extends RemoteA2AFrame {
  operation_id: string; action: 'refresh' | 'cancel' | 'send_message' | 'abandon' | 'retry' | 'observe' | 'pause_observation'; params?: Record<string, unknown>;
}

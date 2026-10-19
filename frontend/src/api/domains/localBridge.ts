import { del, get, getBlob, post, upload } from '../core';

export interface LocalDeviceScope {
  expected_user_id: string;
  expected_tenant_id: string;
}

export interface LocalDeviceControl {
  schema: 'hive.local_connection_control.v1';
  connection_id: string;
  actor_user_id: string;
  request_counts: { cancelled_before_dispatch: number; needs_reconciliation: number };
  host_execution_state: 'unknown';
  stop_confirmation: 'unavailable';
  next_action: string;
  recorded_at: string;
}

export interface LocalDeviceRevocation {
  status: 'revoked';
  connection_id: string;
  work_control: LocalDeviceControl;
}

function deviceControlQuery(scope?: LocalDeviceScope) {
  return scope ? '?' + new URLSearchParams({ ...scope }).toString() : '';
}

export interface LocalBridgeConnection {
  id: string;
  tenant_id: string;
  agent_id?: string | null;
  user_id: string;
  device_name: string;
  client_kind: string;
  status: string;
  work_control?: LocalDeviceControl | null;
  presence_status?: 'online' | 'offline' | 'unknown' | string | null;
  presence_last_seen_at?: string | null;
  runtime_kind?: string | null;
  scopes: string[];
  last_seen_at?: string | null;
  created_at?: string | null;
  revoked_at?: string | null;
  expires_at?: string | null;
}

export interface LocalBridgeInstallGuide {
  product_name: string;
  skill_repo_url: string;
  skill_name: string;
  npm_package: string;
  binary_name: string;
  install_skill_command: string;
  install_cli_command: string;
  login_command: string;
  status_command: string;
  run_command: string;
  user_prompt: string;
  instructions: string[];
}

export interface LocalAgentChannelSession {
  id: string;
  chat_session_id: string | null;
  agent_id?: string | null;
  title?: string | null;
  source: string;
  source_channel?: string;
  session_kind?: string;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
  last_message_at?: string | null;
}

export interface LocalAgentChannelMessage {
  id: string;
  session_id: string;
  status: string;
  content: string;
  attachments: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
  idempotency_key: string;
  request_hash?: string | null;
  capability_snapshot_hash?: string | null;
  replay_key: string;
  approval_id?: string | null;
  receipt?: LocalAgentExecutionReceipt | null;
  delivery_attempt_count?: number;
  delivery_lease_expires_at?: string | null;
  created_at?: string | null;
}

export interface LocalAgentExecutionReceipt {
  schema: 'hive.execution_receipt.v1' | string;
  request_hash: string;
  capability_snapshot_hash: string;
  result_refs: string[];
  status: string;
  replay_key: string;
  trace_id: string;
  span_id: string;
}

export interface LocalAgentChannelEvent {
  id: string;
  sequence: number;
  session_id: string;
  message_id?: string | null;
  direction: string;
  type: string;
  payload: Record<string, unknown>;
  created_at?: string | null;
}

export interface LocalAgentChannelTimeline {
  session: LocalAgentChannelSession;
  events: LocalAgentChannelEvent[];
  next_cursor: number;
}

export interface LocalAgentRequestControl {
  schema: 'hive.local_message_control.v1';
  control_id: string;
  action: 'cancel' | 'revoke_connection';
  message_id: string;
  session_id: string;
  actor_user_id: string;
  status: 'cancelled' | 'needs_reconciliation' | 'already_terminal';
  execution_state: string;
  next_action: string;
  recorded_at: string;
}

export interface LocalWorkDisposition {
  schema: 'hive.local_work_disposition.v1';
  control_id: string;
  action: 'abandon';
  message_id: string;
  session_id: string;
  actor_user_id: string;
  request_hash: string | null;
  status: 'abandoned';
  execution_state: 'unknown';
  next_action: string;
  recorded_at: string;
}

export interface LocalSourceDelivery {
  status: 'queued' | 'needs_reconciliation';
  notification_id?: string;
  reason?: string;
  retryable?: boolean;
  recovery_action?: string;
}

export interface LocalSourceReturn {
  schema: 'hive.local_source_return.v1';
  message_id: string;
  session_id: string;
  actor_user_id: string;
  status: string;
  source_delivery: LocalSourceDelivery;
  reexecuted: false;
}

export interface LocalAgentRequestPage {
  scope: { actor_user_id: string; tenant_id: string; session_id: string };
  requests: Array<{
    id: string; request_hash: string | null; status: string; execution_state: string;
    preview: string; created_at: string; control: LocalAgentRequestControl | null;
    can_cancel: boolean; can_abandon: boolean; next_action: string | null;
    source_delivery: LocalSourceDelivery | null; can_retry_source_return: boolean; decision_reason: string | null;
    tracking_state: 'open' | 'settled' | 'abandoned'; disposition: LocalWorkDisposition | null;
  }>;
  next_cursor: string | null;
}

export interface LocalAgentBrowserWsTicket {
  ticket: string;
  expires_in: number;
  single_use: boolean;
}

export interface LocalAgentWorkspaceFile {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified_at?: string;
}

export interface LocalAgentWorkspaceContent {
  path: string;
  content: string;
}

export interface LocalAgentWorkspaceUpload {
  filename: string;
  saved_filename: string;
  size: number;
  workspace_path: string;
  extracted_text?: string;
  preview_text?: string;
  conversion?: Record<string, unknown> | null;
  is_image?: boolean;
  image_data_url?: string;
}

export const localBridgeApi = {
  listConnections: () =>
    get<{ connections: LocalBridgeConnection[] }>('/local-bridge/connections'),
  getInstallGuide: () =>
    get<LocalBridgeInstallGuide>('/local-bridge/install-guide'),
  approvePairing: (userCode: string) =>
    post<{ status: string }>(`/local-bridge/pairings/${encodeURIComponent(userCode)}/approve`),
  rejectPairing: (userCode: string) =>
    post<{ status: string }>(`/local-bridge/pairings/${encodeURIComponent(userCode)}/reject`),
  revokeConnection: (connectionId: string, scope?: LocalDeviceScope) =>
    del<LocalDeviceRevocation>(`/local-bridge/connections/${connectionId}${deviceControlQuery(scope)}`),
  getDefaultChannelSession: () =>
    post<LocalAgentChannelSession>('/local-agents/sessions/default'),
  getAgentDefaultChannelSession: (agentId: string) =>
    post<LocalAgentChannelSession>(`/agents/${agentId}/local-agent/sessions/default`),
  listAgentChannelSessions: (agentId: string) =>
    get<LocalAgentChannelSession[]>(`/agents/${agentId}/local-agent/sessions`),
  createAgentChannelSession: (agentId: string, input: { source?: string; title?: string } = {}) =>
    post<LocalAgentChannelSession>(`/agents/${agentId}/local-agent/sessions`, {
      source: input.source ?? 'web',
      title: input.title,
    }),
  getAgentChannelSession: (agentId: string, sessionId: string) =>
    get<LocalAgentChannelSession>(`/agents/${agentId}/local-agent/sessions/${encodeURIComponent(sessionId)}`),
  deleteAgentChannelSession: (agentId: string, sessionId: string) =>
    del(`/agents/${agentId}/local-agent/sessions/${encodeURIComponent(sessionId)}`),
  createChannelSession: (input: { source?: string; title?: string } = {}) =>
    post<LocalAgentChannelSession>('/local-agents/sessions', {
      source: input.source ?? 'web',
      title: input.title,
    }),
  sendChannelMessage: (
    sessionId: string,
    input: {
      content: string;
      attachments?: Array<Record<string, unknown>>;
      metadata?: Record<string, unknown>;
      idempotencyKey?: string;
    },
  ) =>
    post<LocalAgentChannelMessage>(`/local-agents/sessions/${sessionId}/messages`, {
      content: input.content,
      attachments: input.attachments ?? [],
      metadata: input.metadata ?? {},
      idempotency_key: input.idempotencyKey,
    }),
  sendAgentChannelMessage: (
    agentId: string,
    sessionId: string,
    input: {
      content: string;
      attachments?: Array<Record<string, unknown>>;
      metadata?: Record<string, unknown>;
      idempotencyKey?: string;
    },
  ) =>
    post<LocalAgentChannelMessage>(`/agents/${agentId}/local-agent/sessions/${sessionId}/messages`, {
      content: input.content,
      attachments: input.attachments ?? [],
      metadata: input.metadata ?? {},
      idempotency_key: input.idempotencyKey,
    }),
  getChannelTimeline: (sessionId: string, afterSequence = 0) => {
    const suffix = afterSequence > 0 ? `?after_sequence=${encodeURIComponent(String(afterSequence))}` : '';
    return get<LocalAgentChannelTimeline>(`/local-agents/sessions/${sessionId}/timeline${suffix}`);
  },
  listChannelRequests: (sessionId: string, before?: string) => {
    const params = new URLSearchParams({ limit: '50' });
    if (before) params.set('before', before);
    return get<LocalAgentRequestPage>(`/local-agents/sessions/${sessionId}/requests?${params}`);
  },
  cancelChannelRequest: (sessionId: string, messageId: string, command: {
    expected_user_id: string; expected_tenant_id: string; request_hash: string | null; idempotency_key: string;
  }) => post<LocalAgentRequestControl>(`/local-agents/sessions/${sessionId}/messages/${messageId}/cancel`, command),
  abandonChannelRequest: (sessionId: string, messageId: string, command: {
    expected_user_id: string; expected_tenant_id: string; request_hash: string | null; idempotency_key: string;
  }) => post<LocalWorkDisposition>(`/local-agents/sessions/${sessionId}/messages/${messageId}/abandon`, command),
  retryChannelSourceReturn: (sessionId: string, messageId: string, command: {
    expected_user_id: string; expected_tenant_id: string; request_hash: string | null;
  }) => post<LocalSourceReturn>(`/local-agents/sessions/${sessionId}/messages/${messageId}/source-return/retry`, command),
  createBrowserChannelWsTicket: (sessionId: string) =>
    post<LocalAgentBrowserWsTicket>(`/local-agents/sessions/${sessionId}/ws-ticket`),
  listChannelEvents: (sessionId: string, afterSequence = 0) => {
    const suffix = afterSequence > 0 ? `?after_sequence=${encodeURIComponent(String(afterSequence))}` : '';
    return get<{ events: LocalAgentChannelEvent[]; next_cursor: number }>(
      `/local-agents/sessions/${sessionId}/events${suffix}`,
    );
  },
  listWorkspaceFiles: (path = 'workspace') =>
    get<LocalAgentWorkspaceFile[]>(`/local-agents/workspace/files?path=${encodeURIComponent(path)}`),
  readWorkspaceFile: (path: string) =>
    get<LocalAgentWorkspaceContent>(`/local-agents/workspace/content?path=${encodeURIComponent(path)}`),
  downloadWorkspaceFile: (path: string) =>
    getBlob(`/local-agents/workspace/download?path=${encodeURIComponent(path)}`),
  downloadAgentSessionWorkspaceFile: (agentId: string, sessionId: string, path: string) =>
    getBlob(
      `/agents/${encodeURIComponent(agentId)}/local-agent/sessions/${encodeURIComponent(sessionId)}`
      + `/workspace/download?path=${encodeURIComponent(path)}`,
    ),
  uploadWorkspaceFile: (file: File) =>
    upload<LocalAgentWorkspaceUpload>('/local-agents/workspace/upload', file),

  // Legacy agent-scoped bridge workbench APIs remain for existing screens while
  // the Local Agent Channel moves to a user-scoped IM surface.
  listAgentConnections: (agentId: string) =>
    get<{ connections: LocalBridgeConnection[] }>(`/agents/${agentId}/local-bridge/connections`),
  approveAgentPairing: (agentId: string, userCode: string) =>
    post<{ status: string }>(`/agents/${agentId}/local-bridge/pairings/${encodeURIComponent(userCode)}/approve`),
  rejectAgentPairing: (agentId: string, userCode: string) =>
    post<{ status: string }>(`/agents/${agentId}/local-bridge/pairings/${encodeURIComponent(userCode)}/reject`),
  revokeAgentConnection: (agentId: string, connectionId: string, scope?: LocalDeviceScope) =>
    del<LocalDeviceRevocation>(`/agents/${agentId}/local-bridge/connections/${connectionId}${deviceControlQuery(scope)}`),
};

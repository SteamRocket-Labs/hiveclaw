import { del, get, getBlob, post, upload } from '../core';

export interface LocalBridgeConnection {
  id: string;
  tenant_id: string;
  agent_id?: string | null;
  user_id: string;
  device_name: string;
  client_kind: string;
  status: string;
  presence_status?: 'online' | 'offline' | 'unknown' | string | null;
  presence_last_seen_at?: string | null;
  runtime_kind?: string | null;
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
  created_at?: string | null;
}

export interface LocalAgentChannelEvent {
  id: string;
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
  listWorkRequests: (agentId: string) =>
    get<{ work_requests: LocalBridgeWorkRequest[] }>(`/agents/${agentId}/local-bridge/work-requests`),
  getWorkRequest: (agentId: string, messageId: string) =>
    get<LocalBridgeWorkRequest>(`/agents/${agentId}/local-bridge/work-requests/${messageId}`),
  approvePairing: (userCode: string) =>
    post<{ status: string }>(`/local-bridge/pairings/${encodeURIComponent(userCode)}/approve`),
  rejectPairing: (userCode: string) =>
    post<{ status: string }>(`/local-bridge/pairings/${encodeURIComponent(userCode)}/reject`),
  revokeConnection: (connectionId: string) =>
    del<{ status: string }>(`/local-bridge/connections/${connectionId}`),
  createWorkRequest: (agentId: string, content: string, metadata: Record<string, unknown> = {}) =>
    post<{ status: string; message_id: string; conversation_id?: string | null }>(
      `/agents/${agentId}/local-bridge/work-requests`,
      {
        content,
        metadata,
      },
    ),
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
    },
  ) =>
    post<LocalAgentChannelMessage>(`/local-agents/sessions/${sessionId}/messages`, {
      content: input.content,
      attachments: input.attachments ?? [],
      metadata: input.metadata ?? {},
    }),
  sendAgentChannelMessage: (
    agentId: string,
    sessionId: string,
    input: {
      content: string;
      attachments?: Array<Record<string, unknown>>;
      metadata?: Record<string, unknown>;
    },
  ) =>
    post<LocalAgentChannelMessage>(`/agents/${agentId}/local-agent/sessions/${sessionId}/messages`, {
      content: input.content,
      attachments: input.attachments ?? [],
      metadata: input.metadata ?? {},
    }),
  getChannelTimeline: (sessionId: string) =>
    get<LocalAgentChannelTimeline>(`/local-agents/sessions/${sessionId}/timeline`),
  createBrowserChannelWsTicket: (sessionId: string) =>
    post<LocalAgentBrowserWsTicket>(`/local-agents/sessions/${sessionId}/ws-ticket`),
  listChannelEvents: (sessionId: string) =>
    get<{ events: LocalAgentChannelEvent[] }>(`/local-agents/sessions/${sessionId}/events`),
  listWorkspaceFiles: (path = 'workspace') =>
    get<LocalAgentWorkspaceFile[]>(`/local-agents/workspace/files?path=${encodeURIComponent(path)}`),
  readWorkspaceFile: (path: string) =>
    get<LocalAgentWorkspaceContent>(`/local-agents/workspace/content?path=${encodeURIComponent(path)}`),
  downloadWorkspaceFile: (path: string) =>
    getBlob(`/local-agents/workspace/download?path=${encodeURIComponent(path)}`),
  uploadWorkspaceFile: (file: File) =>
    upload<LocalAgentWorkspaceUpload>('/chat/upload', file),

  // Legacy agent-scoped bridge workbench APIs remain for existing screens while
  // the Local Agent Channel moves to a user-scoped IM surface.
  listAgentConnections: (agentId: string) =>
    get<{ connections: LocalBridgeConnection[] }>(`/agents/${agentId}/local-bridge/connections`),
  approveAgentPairing: (agentId: string, userCode: string) =>
    post<{ status: string }>(`/agents/${agentId}/local-bridge/pairings/${encodeURIComponent(userCode)}/approve`),
  rejectAgentPairing: (agentId: string, userCode: string) =>
    post<{ status: string }>(`/agents/${agentId}/local-bridge/pairings/${encodeURIComponent(userCode)}/reject`),
  revokeAgentConnection: (agentId: string, connectionId: string) =>
    del<{ status: string }>(`/agents/${agentId}/local-bridge/connections/${connectionId}`),
};

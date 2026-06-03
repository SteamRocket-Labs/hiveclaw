/**
 * Chat domain adapter — history, sessions, file upload.
 */

import { get, post, del, upload } from '../core';
import type { RequestOptions } from '../core/request';
import type { ChatMessage } from '../../types';

export interface ChatSession {
  id: string;
  agent_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface UploadedChatFile {
  filename: string;
  saved_filename: string;
  size: number;
  extracted_text: string;
  workspace_path: string;
  is_image: boolean;
  image_data_url?: string;
}

export interface SessionRuntimeSummary {
  model?: {
    label?: string;
    provider?: string;
    name?: string;
    supports_vision?: boolean;
    context_window_tokens?: number | null;
  };
  runtime?: {
    connected?: boolean;
    estimated_input_tokens?: number | null;
    remaining_tokens_estimate?: number | null;
  };
  activated_tool_groups: string[];
  used_tools: string[];
  blocked_capabilities: Array<{
    tool?: string | null;
    status?: string | null;
    capability?: string | null;
  }>;
  compaction_count: number;
  permission_event_count?: number;
  team_memory_hit_count?: number;
  last_compaction?: {
    summary?: string | null;
    original_message_count?: number | null;
    kept_message_count?: number | null;
    continuity_sections_injected?: string[] | null;
    created_at?: string | null;
  } | null;
  last_team_memory_hit?: {
    workspace_key?: string | null;
    query?: string | null;
    matched_keys?: string[] | null;
    created_at?: string | null;
  } | null;
  last_tool_budget_event?: {
    reason?: string | null;
    tool_name?: string | null;
    created_at?: string | null;
  } | null;
  last_retry_reason?: string | null;
}

export interface StartSessionRunInput {
  content: string;
  display_content?: string;
  file_name?: string;
  plan_mode_requested?: boolean;
}

export interface SessionRun {
  run_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'killed' | 'skipped' | string;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  result_summary?: string | null;
}

export const chatApi = {
  getHistory: (agentId: string, conversationId?: string) => {
    const qs = conversationId ? `?conversation_id=${conversationId}` : '';
    return get<ChatMessage[]>(`/chat/${agentId}/history${qs}`);
  },
  uploadFile: (file: File, agentId?: string) =>
    upload<UploadedChatFile>('/chat/upload', file, agentId ? { agent_id: agentId } : undefined),

  /** Session management */
  listSessions: (agentId: string, scope?: 'mine' | 'all') =>
    get<ChatSession[]>(`/agents/${agentId}/sessions${scope ? `?scope=${scope}` : ''}`),
  createSession: (agentId: string, title?: string) => post<ChatSession>(`/agents/${agentId}/sessions`, { title }),
  deleteSession: (agentId: string, sessionId: string) => del(`/agents/${agentId}/sessions/${sessionId}`),
  getSessionMessages: (agentId: string, sessionId: string, options?: RequestOptions) =>
    get<ChatMessage[]>(`/agents/${agentId}/sessions/${sessionId}/messages`, options),
  getRuntimeSummary: (sessionId: string, options?: RequestOptions) =>
    get<SessionRuntimeSummary>(`/chat/sessions/${sessionId}/runtime-summary`, options),
  startSessionRun: (agentId: string, sessionId: string, input: StartSessionRunInput) =>
    post<SessionRun>(`/agents/${agentId}/sessions/${sessionId}/runs`, input),
  getActiveSessionRun: (agentId: string, sessionId: string) =>
    get<SessionRun | null>(`/agents/${agentId}/sessions/${sessionId}/runs/active`),
  cancelSessionRun: (agentId: string, sessionId: string, runId: string) =>
    post<SessionRun>(`/agents/${agentId}/sessions/${sessionId}/runs/${runId}/cancel`, {}),
};

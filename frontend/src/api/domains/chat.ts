/**
 * Chat domain adapter — history, sessions, file upload.
 */

import { get, post, patch, del, upload } from '../core';
import type { RequestOptions } from '../core/request';
import type { ChatMessage } from '../../types';

export interface ChatSession {
  id: string;
  agent_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  parent_session_id?: string | null;
  root_session_id?: string | null;
  runtime_task_id?: string | null;
  peer_agent_id?: string | null;
  participant_id?: string | null;
  participant_type?: string | null;
  username?: string | null;
  source_channel?: string | null;
  thread_source?: string | null;
  session_kind?: string | null;
  actor_type?: string | null;
  runtime_source?: string | null;
  visibility_scope?: string | null;
  listed_surface?: string | null;
  permission_mode?: string | null;
  is_current_user_session?: boolean;
  read_only?: boolean;
  permission_profile?: {
    mode?: string | null;
    allowed_tools?: string[];
    writable_roots?: string[];
    [key: string]: unknown;
  } | null;
  writable_roots?: string[] | null;
  transcript_metadata_json?: Record<string, unknown> | null;
}

export interface UploadedChatFile {
  filename: string;
  saved_filename: string;
  size: number;
  extracted_text: string;
  preview_text?: string;
  conversion?: {
    status: 'converted' | 'failed';
    markdown_path?: string;
    metadata_path?: string;
    engine?: string;
    used_ocr?: boolean;
    used_vision?: boolean;
    warnings?: string[];
    source_sha256?: string;
    error?: string;
  } | null;
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
  permission_mode?: 'auto' | 'default' | 'bypassPermissions';
  attachments?: Array<Record<string, unknown>>;
  parts?: Array<Record<string, unknown>>;
}

export interface UpdateSessionPermissionProfileInput {
  permission_mode: 'auto' | 'default' | 'bypassPermissions';
}

export type ConversationBranchMode =
	  | 'fork'
	  | 'branch'
	  | 'edit'
  | 'insert_before'
  | 'insert_after'
  | 'reply'
  | 'regenerate'
  | 'rewind'
  | 'side_question';

export interface RecordSessionFeedbackInput {
  label: 'useful' | 'misleading';
  reason?: string;
  message_id?: string | null;
  decision_id?: string | null;
}

export interface BranchSessionInput {
  mode: ConversationBranchMode;
  anchor_event_id: string;
  content?: string;
  display_content?: string;
  file_name?: string;
  title?: string;
  start_run?: boolean;
  permission_mode?: 'auto' | 'default' | 'bypassPermissions';
  attachments?: Array<Record<string, unknown>>;
  parts?: Array<Record<string, unknown>>;
}

export interface BranchSessionResponse {
  session: ChatSession;
  branch: Record<string, unknown>;
  run?: SessionRun | null;
}

export interface ChatTranscriptEvent {
  id: string;
  sequence: number;
  session_id?: string;
  run_id?: string | null;
  message_id?: string | null;
  actor_type?: string;
  event_type: string;
  type?: string;
  role?: string;
  visibility_scope?: string;
  listed_surface?: string;
  content?: string;
  parts?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
}

export interface SessionIndex {
  schema: string;
  thread_id: string;
  session_id: string;
  agent_id: string;
  tenant_id?: string | null;
  user_id?: string | null;
  title?: string | null;
  source?: string | null;
  thread_source?: string | null;
  listed_surface?: string | null;
  visibility_scope?: string | null;
  session_kind?: string | null;
  forked_from_id?: string | null;
  parent_thread_id?: string | null;
  root_thread_id?: string | null;
  runtime_task_id?: string | null;
  event_persistence_mode?: string | null;
  dynamic_tools: string[];
  checkpoints: Array<Record<string, unknown>>;
  event_count: number;
  last_event_sequence?: number | null;
  t0_segments: Array<Record<string, unknown>>;
  resume_health: Record<string, unknown>;
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
  getSessionTranscript: (agentId: string, sessionId: string, options?: RequestOptions & { afterSequence?: number }) => {
    const after = typeof options?.afterSequence === 'number' ? `?after_sequence=${options.afterSequence}` : '';
    const requestOptions = options ? { ...options } : undefined;
    if (requestOptions) delete requestOptions.afterSequence;
    return get<ChatTranscriptEvent[]>(`/agents/${agentId}/sessions/${sessionId}/transcript${after}`, requestOptions);
  },
  getRuntimeSummary: (sessionId: string, options?: RequestOptions) =>
    get<SessionRuntimeSummary>(`/chat/sessions/${sessionId}/runtime-summary`, options),
  startSessionRun: (agentId: string, sessionId: string, input: StartSessionRunInput) =>
    post<SessionRun>(`/agents/${agentId}/sessions/${sessionId}/runs`, input),
  branchSession: (agentId: string, sessionId: string, input: BranchSessionInput) =>
    post<BranchSessionResponse>(`/agents/${agentId}/sessions/${sessionId}/branches`, input),
  recordSessionFeedback: (agentId: string, sessionId: string, input: RecordSessionFeedbackInput) =>
    post<unknown>(`/agents/${agentId}/sessions/${sessionId}/feedback`, input),
  listSessionBranches: (agentId: string, sessionId: string) =>
    get<ChatSession[]>(`/agents/${agentId}/sessions/${sessionId}/branches`),
  getSessionLineage: (agentId: string, sessionId: string) =>
    get<Array<Record<string, unknown>>>(`/agents/${agentId}/sessions/${sessionId}/lineage`),
  getSessionIndex: (agentId: string, sessionId: string) =>
    get<SessionIndex>(`/agents/${agentId}/sessions/${sessionId}/index`),
  getActiveSessionRun: (agentId: string, sessionId: string) =>
    get<SessionRun | null>(`/agents/${agentId}/sessions/${sessionId}/runs/active`),
  cancelSessionRun: (agentId: string, sessionId: string, runId: string) =>
    post<SessionRun>(`/agents/${agentId}/sessions/${sessionId}/runs/${runId}/cancel`, {}),
  resolveSessionPermission: (
    agentId: string,
    sessionId: string,
    permissionRequestId: string,
    input: { action: 'allow_once' | 'allow_session' | 'deny'; feedback?: string },
  ) =>
    post<unknown>(`/agents/${agentId}/sessions/${sessionId}/permissions/${permissionRequestId}/resolve`, input),
  updateSessionPermissionProfile: (
    agentId: string,
    sessionId: string,
    input: UpdateSessionPermissionProfileInput,
  ) =>
    patch<unknown>(`/agents/${agentId}/sessions/${sessionId}/permissions/profile`, input),
};

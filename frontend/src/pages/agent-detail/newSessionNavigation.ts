export interface NewSessionDraftRequest {
  agentId: string;
  requestId: string;
}

export interface NewSessionDraftNavigation {
  to: string;
  state: {
    newSessionDraft: {
      agent_id: string;
      request_id: string;
    };
  };
}

export interface CreateDraftChatSessionInput {
  agentId: string;
  userId?: string;
  permissionMode: string;
  now?: Date;
  requestId?: string;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function createRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `new-session:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

export function buildNewSessionDraftNavigation(agentId: string): NewSessionDraftNavigation {
  return {
    to: `/agents/${encodeURIComponent(agentId)}#chat`,
    state: {
      newSessionDraft: {
        agent_id: agentId,
        request_id: createRequestId(),
      },
    },
  };
}

export function createDraftChatSession({
  agentId,
  userId,
  permissionMode,
  now = new Date(),
  requestId = createRequestId(),
}: CreateDraftChatSessionInput) {
  const pad = (value: number) => String(value).padStart(2, '0');
  const draftId = `draft:${requestId}`;
  return {
    id: draftId,
    draft_client_id: draftId,
    is_draft: true,
    agent_id: agentId,
    user_id: userId,
    title: `Session ${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    source_channel: 'web',
    session_kind: 'human_chat',
    actor_type: 'user',
    runtime_source: 'web_chat',
    visibility_scope: 'direct_user',
    listed_surface: 'chat',
    is_current_user_session: true,
    read_only: false,
    message_count: 0,
    permission_mode: permissionMode,
  };
}

export function readNewSessionDraftRequest(
  state: unknown,
  expectedAgentId: string | null | undefined,
): NewSessionDraftRequest | null {
  const request = recordValue(recordValue(state).newSessionDraft);
  const agentId = typeof request.agent_id === 'string' ? request.agent_id.trim() : '';
  const requestId = typeof request.request_id === 'string' ? request.request_id.trim() : '';
  if (!agentId || !requestId || !expectedAgentId || agentId !== String(expectedAgentId)) return null;
  return { agentId, requestId };
}

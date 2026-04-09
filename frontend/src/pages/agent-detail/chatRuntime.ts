import type { SessionRuntimeSummary } from '../../api/domains/chat';
import type { ToolCallMeta } from './toolResultEnvelope';

export const MIN_COMPOSER_HEIGHT = 44;
export const MAX_COMPOSER_HEIGHT = 160;

export type RuntimeEventType = 'permission' | 'session_compact' | 'pack_activation' | 'team_memory';

export interface AgentChatMessage {
  role: 'user' | 'assistant' | 'tool_call' | 'event';
  content: string;
  fileName?: string;
  imageUrl?: string;
  thinking?: string;
  sender_name?: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolStatus?: 'running' | 'done';
  toolResult?: string;
  toolRawResult?: string;
  toolMeta?: ToolCallMeta | null;
  timestamp?: string;
  participant_id?: string | null;
  id?: string;
  eventType?: RuntimeEventType;
  eventTitle?: string;
  eventStatus?: string;
  eventToolName?: string;
  eventApprovalId?: string;
  eventSecurityZone?: string;
  eventCapability?: string;
  eventApprovalRequired?: boolean;
  eventReason?: string;
  eventNextStep?: string;
  eventRetryable?: boolean;
  eventRetryReason?: string;
  originalMessageCount?: number;
  keptMessageCount?: number;
  continuitySectionsInjected?: string[];
  activatedPacks?: string[];
  skillName?: string;
  triggerTool?: string;
}

export type ChatRuntimeSummary = SessionRuntimeSummary;

type ActiveModelSummary = {
  label?: string;
  provider?: string;
  model?: string;
  supports_vision?: boolean;
  max_input_tokens?: number | null;
};

type BuildRuntimeSummaryInput = {
  persistedSummary?: Partial<ChatRuntimeSummary> | null;
  activeModel?: ActiveModelSummary | null;
  agentPrimaryModelId?: string | null;
  agentContextWindowSize?: number | null;
  messages: AgentChatMessage[];
  connected: boolean;
};

type EventPart = {
  type?: string;
  event_type?: RuntimeEventType;
  title?: string;
  text?: string;
  status?: string;
  tool_name?: string;
  approval_id?: string;
  security_zone?: string;
  capability?: string;
  approval_required?: boolean;
  reason?: string;
  next_step?: string;
  retryable?: boolean;
  retry_reason?: string;
  original_message_count?: number;
  kept_message_count?: number;
  continuity_sections_injected?: string[];
  packs?: Array<string | { name?: string }>;
  skill_name?: string;
  trigger_tool?: string;
};

const RUNTIME_EVENT_TYPES = new Set<RuntimeEventType>(['permission', 'session_compact', 'pack_activation', 'team_memory']);

function isRuntimeEventType(value: unknown): value is RuntimeEventType {
  return typeof value === 'string' && RUNTIME_EVENT_TYPES.has(value as RuntimeEventType);
}

function getEventPart(payload: any): EventPart | undefined {
  if (payload?.part && typeof payload.part === 'object') return payload.part as EventPart;
  if (Array.isArray(payload?.parts)) {
    return payload.parts.find((part: EventPart) => part?.type === 'event');
  }
  return undefined;
}

function normalizePackNames(packs: EventPart['packs'] | undefined): string[] | undefined {
  if (!Array.isArray(packs)) return undefined;
  const names = packs
    .map((pack) => (typeof pack === 'string' ? pack : pack?.name))
    .filter((name): name is string => Boolean(name));
  return names.length > 0 ? names : undefined;
}

export function computeComposerHeight(scrollHeight: number): number {
  return Math.min(MAX_COMPOSER_HEIGHT, Math.max(MIN_COMPOSER_HEIGHT, scrollHeight));
}

export function estimateRuntimeInputTokens(messages: AgentChatMessage[]): number {
  const totalChars = messages.reduce((total, message) => {
    const payload = [
      message.content,
      message.thinking,
      message.toolResult,
      message.toolName,
      message.fileName,
    ]
      .filter((part): part is string => typeof part === 'string' && part.length > 0)
      .join('\n');
    return total + payload.length;
  }, 0);
  return totalChars > 0 ? Math.max(1, Math.ceil(totalChars / 4)) : 0;
}

export function buildRuntimeSummary({
  persistedSummary,
  activeModel,
  agentPrimaryModelId,
  agentContextWindowSize,
  messages,
  connected,
}: BuildRuntimeSummaryInput): ChatRuntimeSummary {
  const fallbackContextWindow =
    activeModel?.max_input_tokens ??
    agentContextWindowSize ??
    null;
  const fallbackEstimatedTokens = estimateRuntimeInputTokens(messages);
  const backendModel = persistedSummary?.model || {};
  const backendRuntime = persistedSummary?.runtime || {};
  const contextWindowTokens =
    backendModel.context_window_tokens ??
    fallbackContextWindow;
  const estimatedInputTokens =
    backendRuntime.estimated_input_tokens ??
    fallbackEstimatedTokens;

  return {
    model: {
      label: backendModel.label || activeModel?.label || agentPrimaryModelId || 'Unknown model',
      provider: backendModel.provider || activeModel?.provider,
      name: backendModel.name || activeModel?.model,
      supports_vision: backendModel.supports_vision ?? activeModel?.supports_vision,
      context_window_tokens: contextWindowTokens,
    },
    runtime: {
      connected: backendRuntime.connected ?? connected,
      estimated_input_tokens: estimatedInputTokens,
      remaining_tokens_estimate:
        backendRuntime.remaining_tokens_estimate ??
        (typeof contextWindowTokens === 'number'
          ? Math.max(contextWindowTokens - estimatedInputTokens, 0)
          : null),
    },
    activated_packs: persistedSummary?.activated_packs || [],
    used_tools: persistedSummary?.used_tools || [],
    blocked_capabilities: persistedSummary?.blocked_capabilities || [],
    compaction_count: persistedSummary?.compaction_count || 0,
    permission_event_count: persistedSummary?.permission_event_count || 0,
    team_memory_hit_count: persistedSummary?.team_memory_hit_count || 0,
    last_compaction: persistedSummary?.last_compaction || null,
    last_team_memory_hit: persistedSummary?.last_team_memory_hit || null,
    last_tool_budget_event: persistedSummary?.last_tool_budget_event || null,
    last_retry_reason: persistedSummary?.last_retry_reason || null,
  };
}

export function getTransportNotice(payload: any): string | null {
  if (payload?.type !== 'info') return null;
  const text = payload?.content || payload?.message;
  return typeof text === 'string' && text.trim() ? text : null;
}

export function getRuntimeEventMessage(payload: any): AgentChatMessage | null {
  const eventType = payload?.eventType || payload?.event_type || payload?.type;
  if (!isRuntimeEventType(eventType)) return null;

  const part = getEventPart(payload);
  const activatedPacks = normalizePackNames(payload?.packs ?? part?.packs);
  const content =
    payload?.content ||
    payload?.message ||
    payload?.summary ||
    part?.text ||
    '';

  return {
    role: 'event',
    content,
    eventType,
    eventTitle: payload?.eventTitle || payload?.title || part?.title,
    eventStatus: payload?.eventStatus || payload?.status || part?.status || 'info',
    eventToolName: payload?.eventToolName || payload?.tool_name || part?.tool_name,
    eventApprovalId: payload?.eventApprovalId || payload?.approval_id || part?.approval_id,
    eventSecurityZone: payload?.eventSecurityZone || payload?.security_zone || part?.security_zone,
    eventCapability: payload?.eventCapability || payload?.capability || part?.capability,
    eventApprovalRequired:
      payload?.eventApprovalRequired ?? payload?.approval_required ?? part?.approval_required,
    eventReason: payload?.eventReason || payload?.reason || part?.reason,
    eventNextStep: payload?.eventNextStep || payload?.next_step || part?.next_step,
    eventRetryable: payload?.eventRetryable ?? payload?.retryable ?? part?.retryable,
    eventRetryReason: payload?.eventRetryReason || payload?.retry_reason || part?.retry_reason,
    originalMessageCount:
      payload?.originalMessageCount ??
      payload?.original_message_count ??
      part?.original_message_count,
    keptMessageCount:
      payload?.keptMessageCount ??
      payload?.kept_message_count ??
      part?.kept_message_count,
    continuitySectionsInjected:
      payload?.continuitySectionsInjected ??
      payload?.continuity_sections_injected ??
      part?.continuity_sections_injected,
    activatedPacks,
    skillName: payload?.skillName || payload?.skill_name || part?.skill_name,
    triggerTool: payload?.triggerTool || payload?.trigger_tool || part?.trigger_tool,
    timestamp: payload?.timestamp || payload?.created_at,
    sender_name: payload?.sender_name,
    participant_id: payload?.participant_id,
    id: payload?.id,
  };
}

export function normalizeStoredChatMessage(payload: any): AgentChatMessage {
  const eventMessage = getRuntimeEventMessage(payload);
  if (eventMessage) return eventMessage;

  if (payload?.role === 'tool_call') {
    return {
      role: 'tool_call',
      content: payload?.content || '',
      toolName: payload?.toolName,
      toolArgs: payload?.toolArgs,
      toolStatus: payload?.toolStatus,
      toolResult: payload?.toolResult,
      toolRawResult: payload?.toolRawResult ?? payload?.toolResult,
      toolMeta: payload?.toolMeta ?? null,
      thinking: payload?.thinking,
      timestamp: payload?.created_at || payload?.timestamp,
      sender_name: payload?.sender_name,
      participant_id: payload?.participant_id,
      id: payload?.id,
    };
  }

  return {
    role: payload?.role === 'assistant' ? 'assistant' : 'user',
    content: payload?.content || '',
    thinking: payload?.thinking,
    timestamp: payload?.created_at || payload?.timestamp,
    sender_name: payload?.sender_name,
    participant_id: payload?.participant_id,
    id: payload?.id,
  };
}

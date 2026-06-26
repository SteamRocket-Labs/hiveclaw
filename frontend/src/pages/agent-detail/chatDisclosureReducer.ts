import type { AgentChatMessage } from './chatRuntime';

export type RunStepKind =
  | 'reasoning'
  | 'tool'
  | 'search'
  | 'file'
  | 'command'
  | 'permission'
  | 'question'
  | 'plan'
  | 'compaction'
  | 'workflow'
  | 'subagent'
  | 'trigger'
  | 'deep_research'
  | 'artifact'
  | 'event';

export type RunStepStatus = 'queued' | 'running' | 'blocked' | 'done' | 'failed' | 'cancelled';

export interface RunStepSnapshot {
  id: string;
  toolCallId?: string | null;
  kind: RunStepKind;
  title: string;
  subtitle?: string;
  status: RunStepStatus;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  summary?: string;
  details?: unknown;
  visibility: 'visible' | 'collapsed' | 'debug';
  blocking?: boolean;
}

export interface RunTimelineSnapshot {
  id: string;
  status: 'idle' | 'running' | 'blocked' | 'done' | 'failed' | 'cancelled';
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  steps: RunStepSnapshot[];
  answerMessageId?: string;
}

type TimelineBuildOptions = {
  now?: Date;
};

const FILE_TOOL_PREFIXES = ['read_', 'write_', 'edit_', 'list_', 'delete_'];
const SEARCH_TOOL_PREFIXES = ['web_', 'search', 'firecrawl', 'xcrawl'];
const COMMAND_TOOLS = new Set(['execute_code', 'run_command']);
const SESSION_NATIVE_DISCLOSURE_EVENTS = new Set([
  'permission',
  'permission_request',
  'permission_resolved',
  'session_compact',
  'tool_group_activation',
  'pack_activation',
  'deferred_tools_delta',
  'team_memory',
  'hook_progress',
  'hook_summary',
  'hook_attachment',
  'hook_blocked',
  'workflow_run',
  'workflow_step',
  'dynamic_workflow',
  'deep_research',
  'child_session',
  'subagent',
  'team_member',
  'schedule',
  'schedule_fire',
  'goal',
  'once',
  'memory_candidate',
  'artifact_update',
  'artifact_delivery',
]);

function compactValue(value: unknown): string {
  if (typeof value === 'string') return value.length > 80 ? `${value.slice(0, 77)}...` : value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value == null) return String(value);
  try {
    const serialized = JSON.stringify(value);
    return serialized.length > 80 ? `${serialized.slice(0, 77)}...` : serialized;
  } catch {
    return String(value);
  }
}

function compactText(text: string, limit = 160): string {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, Math.max(0, limit - 3))}...`;
}

function stepIdForMessage(message: AgentChatMessage, index: number): string {
  if (message.toolMeta?.kind === 'runtime_step' && message.toolMeta.stepId) return message.toolMeta.stepId;
  return message.id || `step-${index}`;
}

function eventTitle(message: AgentChatMessage): string {
  return message.eventTitle || message.eventType || 'Runtime event';
}

function eventMetadataSummary(message: AgentChatMessage): string {
  const parts = [
    message.eventChildSessionId ? `child:${message.eventChildSessionId}` : '',
    message.eventRuntimeTaskId ? `run:${message.eventRuntimeTaskId}` : '',
    message.eventWorkflowRunId ? `workflow:${message.eventWorkflowRunId}` : '',
    message.eventWorkflowStepId ? `step:${message.eventWorkflowStepId}` : '',
    message.eventDeepResearchRunId ? `research:${message.eventDeepResearchRunId}` : '',
    message.eventScheduleFireId ? `fire:${message.eventScheduleFireId}` : '',
    message.eventScheduleId ? `schedule:${message.eventScheduleId}` : '',
    message.eventGoalId ? `goal:${message.eventGoalId}` : '',
    message.eventOnceId ? `once:${message.eventOnceId}` : '',
    message.eventArtifactId ? `artifact:${message.eventArtifactId}` : '',
    message.eventRevisionId ? `rev:${message.eventRevisionId}` : '',
  ].filter(Boolean);
  return parts.join(' · ');
}

export function getDisclosureStepSummary(message: AgentChatMessage): string {
  if (message.role === 'assistant') {
    return compactText(message.thinking || '');
  }

  if (message.role === 'tool_call') {
    if (message.toolName === 'tool_search') {
      return 'Checking available tools';
    }
    const entries = Object.entries(message.toolArgs || {});
    if (entries.length > 0) {
      return entries.map(([key, value]) => `${key}: ${compactValue(value)}`).join(', ');
    }
    if (message.toolMeta?.kind === 'user_clarification') {
      const count = message.toolMeta.questions.length;
      return count === 1 ? '1 question' : `${count} questions`;
    }
    if (message.toolMeta?.kind === 'plan_proposal') {
      return message.toolMeta.summary || message.toolMeta.nextAction || '';
    }
    if (message.toolMeta?.kind === 'deep_research') {
      return message.toolMeta.summary || message.toolMeta.status || '';
    }
    return '';
  }

  if (message.role === 'event') {
    return [compactText(message.content || message.eventReason || message.eventNextStep || ''), eventMetadataSummary(message)]
      .filter(Boolean)
      .join(' · ');
  }

  return '';
}

function kindForToolMessage(message: AgentChatMessage): RunStepKind {
  if (message.toolMeta?.kind === 'user_clarification') return 'question';
  if (message.toolMeta?.kind === 'plan_mode_request' || message.toolMeta?.kind === 'plan_proposal') return 'plan';
  if (message.toolMeta?.kind === 'deep_research') return 'deep_research';

  const name = (message.toolName || '').toLowerCase();
  if (COMMAND_TOOLS.has(name)) return 'command';
  if (FILE_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix))) return 'file';
  if (SEARCH_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix))) return 'search';
  if (name.includes('workflow')) return 'workflow';
  if (name.includes('subagent') || name.includes('delegate')) return 'subagent';
  if (name.includes('trigger') || name.includes('schedule')) return 'trigger';
  return 'tool';
}

function titleForToolMessage(message: AgentChatMessage): string {
  if (message.toolName === 'tool_search') return 'Loading tools';
  return message.toolName || 'Tool call';
}

function kindForEventMessage(message: AgentChatMessage): RunStepKind {
  if (message.eventType === 'session_compact') return 'compaction';
  if (message.eventType === 'permission') return 'permission';
  if (message.eventType === 'tool_group_activation' || message.eventType === 'pack_activation') return 'tool';
  if (message.eventType === 'workflow_run' || message.eventType === 'workflow_step' || message.eventType === 'dynamic_workflow') return 'workflow';
  if (message.eventType === 'child_session' || message.eventType === 'subagent' || message.eventType === 'team_member') return 'subagent';
  if (message.eventType === 'schedule' || message.eventType === 'schedule_fire' || message.eventType === 'once') return 'trigger';
  if (message.eventType === 'deep_research') return 'deep_research';
  if (message.eventType === 'artifact_update' || message.eventType === 'artifact_delivery') return 'artifact';
  return 'event';
}

function statusForMessage(message: AgentChatMessage): RunStepStatus {
  if (message.role === 'tool_call') {
    if (message.toolMeta?.kind === 'user_clarification' && message.toolMeta.blocking) return 'blocked';
    if (message.toolMeta?.kind === 'deep_research' && message.toolMeta.status === 'running') return 'running';
    if (message.toolMeta?.kind === 'runtime_step' && message.toolMeta.status === 'failed') return 'failed';
    return message.toolStatus === 'running' ? 'running' : 'done';
  }
  if (message.role === 'assistant') return 'done';
  if (message.role === 'event') {
    const status = String(message.eventStatus || '').toLowerCase();
    if (status === 'running' || status === 'in_progress' || status === 'pending') return 'running';
    if (status === 'blocked' || status === 'approval_required' || status === 'session_permission_required') return 'blocked';
    if (status === 'failed' || status === 'error') return 'failed';
    if (status === 'cancelled' || status === 'canceled') return 'cancelled';
  }
  return 'done';
}

export function isDisclosureStepMessage(message: AgentChatMessage): boolean {
  if (message.role === 'tool_call') return true;
  if (message.role === 'assistant') return Boolean(!message.content?.trim() && message.thinking?.trim());
  if (message.role === 'event') {
    return Boolean(message.eventType && SESSION_NATIVE_DISCLOSURE_EVENTS.has(message.eventType));
  }
  return false;
}

function buildStep(message: AgentChatMessage, index: number): RunStepSnapshot | null {
  if (!isDisclosureStepMessage(message)) return null;
  const status = statusForMessage(message);
  const summary = getDisclosureStepSummary(message);

  if (message.role === 'assistant') {
    return {
      id: stepIdForMessage(message, index),
      kind: 'reasoning',
      title: 'Thinking',
      status,
      startedAt: message.timestamp,
      completedAt: status === 'done' ? message.timestamp : undefined,
      summary,
      details: message.thinking,
      visibility: 'collapsed',
    };
  }

  if (message.role === 'tool_call') {
    return {
      id: stepIdForMessage(message, index),
      toolCallId: message.toolMeta?.kind === 'runtime_step' ? message.toolMeta.toolCallId : undefined,
      kind: kindForToolMessage(message),
      title: titleForToolMessage(message),
      status,
      startedAt: message.timestamp,
      completedAt: status === 'done' || status === 'blocked' ? message.timestamp : undefined,
      durationMs: message.toolMeta?.kind === 'runtime_step' ? message.toolMeta.durationMs ?? undefined : undefined,
      summary,
      details: {
        args: message.toolArgs,
        result: message.toolResult,
        rawResult: message.toolRawResult,
        meta: message.toolMeta,
      },
      visibility: message.toolMeta?.kind === 'runtime_step' && message.toolMeta.visibility !== 'redacted'
        ? message.toolMeta.visibility
        : 'collapsed',
      blocking: status === 'blocked',
    };
  }

  return {
    id: stepIdForMessage(message, index),
    kind: kindForEventMessage(message),
    title: eventTitle(message),
    status,
    startedAt: message.timestamp,
    completedAt: message.timestamp,
    summary,
    details: message,
    visibility: 'collapsed',
  };
}

function getTimestampMs(value?: string): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function getTimelineStatus(steps: RunStepSnapshot[], hasAnswer: boolean): RunTimelineSnapshot['status'] {
  if (steps.some((step) => step.status === 'failed')) return 'failed';
  if (steps.some((step) => step.status === 'blocked')) return 'blocked';
  if (steps.some((step) => step.status === 'running')) return 'running';
  if (steps.length > 0 || hasAnswer) return 'done';
  return 'idle';
}

export function buildRunTimelineFromMessages(
  messages: AgentChatMessage[],
  options: TimelineBuildOptions = {},
): RunTimelineSnapshot {
  const steps = messages
    .map((message, index) => buildStep(message, index))
    .filter((step): step is RunStepSnapshot => Boolean(step));
  const answerIndex = messages.findIndex((message) => message.role === 'assistant' && Boolean(message.content?.trim()));
  const answer = answerIndex >= 0 ? messages[answerIndex] : null;
  const status = getTimelineStatus(steps, Boolean(answer));
  const firstTime = steps.map((step) => getTimestampMs(step.startedAt)).find((time): time is number => time != null);
  const lastStepTime = [...steps].reverse().map((step) => getTimestampMs(step.completedAt || step.startedAt)).find((time): time is number => time != null);
  const completedAt = status === 'running' ? undefined : lastStepTime;
  const nowMs = options.now?.getTime();
  const durationEnd = completedAt ?? nowMs;

  return {
    id: 'timeline-0',
    status,
    startedAt: firstTime != null ? new Date(firstTime).toISOString() : undefined,
    completedAt: completedAt != null ? new Date(completedAt).toISOString() : undefined,
    durationMs: firstTime != null && durationEnd != null ? Math.max(0, durationEnd - firstTime) : undefined,
    steps,
    answerMessageId: answer ? answer.id || `answer-${answerIndex}` : undefined,
  };
}

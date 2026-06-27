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
  summary?: string;
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

function compactText(text: string, limit = 160): string {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, Math.max(0, limit - 3))}...`;
}

function stringArg(message: AgentChatMessage, keys: string[]): string {
  for (const key of keys) {
    const value = message.toolArgs?.[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function basename(path: string): string {
  return path.split('/').filter(Boolean).pop() || path;
}

function summarizeFileTool(message: AgentChatMessage): string {
  const target = stringArg(message, ['path', 'file_path', 'target_path', 'source_path', 'name']);
  return target ? compactText(basename(target), 120) : 'Workspace file operation';
}

function summarizeSearchTool(message: AgentChatMessage): string {
  const target = stringArg(message, ['query', 'url', 'domain']);
  return target ? compactText(target, 120) : 'Search request';
}

function summarizeCommandTool(message: AgentChatMessage): string {
  const command = stringArg(message, ['cmd', 'command']);
  if (!command) return 'Command execution';
  const head = command.split(/\s+/).filter(Boolean).slice(0, 3).join(' ');
  return head ? compactText(`${head}${command.length > head.length ? ' ...' : ''}`, 120) : 'Command execution';
}

function summarizeToolMessage(message: AgentChatMessage): string {
  const name = (message.toolName || '').toLowerCase();
  if (message.toolName === 'tool_search') return 'Checking available tools';
  if (message.toolMeta?.kind === 'user_clarification') {
    const count = message.toolMeta.questions.length;
    return count === 1 ? '1 question' : `${count} questions`;
  }
  if (message.toolMeta?.kind === 'plan_proposal') {
    return message.toolMeta.summary || message.toolMeta.nextAction || '';
  }
  if (message.toolMeta?.kind === 'dynamic_workflow_proposal') {
    return message.toolMeta.goal || message.toolMeta.nextAction || '';
  }
  if (COMMAND_TOOLS.has(name)) return summarizeCommandTool(message);
  if (FILE_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix))) return summarizeFileTool(message);
  if (SEARCH_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix))) return summarizeSearchTool(message);
  return message.toolArgs && Object.keys(message.toolArgs).length > 0 ? 'Details available' : '';
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
    return summarizeToolMessage(message);
  }

  if (message.role === 'event') {
    if (message.sessionPermissionRequest && message.eventStatus === 'session_permission_required') {
      const tool =
        message.sessionPermissionRequest.tool_display_name ||
        message.sessionPermissionRequest.tool_name ||
        message.eventToolName ||
        'this tool';
      return `Permission needed for ${tool}`;
    }
    return [compactText(message.content || message.eventReason || message.eventNextStep || ''), eventMetadataSummary(message)]
      .filter(Boolean)
      .join(' · ');
  }

  return '';
}

function kindForToolMessage(message: AgentChatMessage): RunStepKind {
  if (message.toolMeta?.kind === 'user_clarification') return 'question';
  if (message.toolMeta?.kind === 'plan_mode_request' || message.toolMeta?.kind === 'plan_proposal') return 'plan';
  if (message.toolMeta?.kind === 'dynamic_workflow_proposal') return 'workflow';

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
  const name = (message.toolName || '').toLowerCase();
  if (name === 'read_file') return 'Read file';
  if (name === 'write_file') return 'Write file';
  if (name === 'edit_file') return 'Edit file';
  if (name === 'delete_file') return 'Delete file';
  if (name === 'list_files') return 'List files';
  if (name === 'web_search') return 'Search web';
  if (name === 'web_fetch' || name === 'firecrawl_fetch' || name === 'xcrawl_scrape') return 'Fetch web page';
  if (COMMAND_TOOLS.has(name)) return 'Run command';
  if (name.includes('workflow')) return 'Workflow step';
  if (name.includes('subagent') || name.includes('delegate')) return 'Sub-agent step';
  if (name.includes('trigger') || name.includes('schedule')) return 'Schedule step';
  return message.toolName || 'Tool call';
}

function kindForEventMessage(message: AgentChatMessage): RunStepKind {
  if (message.eventType === 'session_compact') return 'compaction';
  if (message.eventType === 'permission') return 'permission';
  if (message.eventType === 'tool_group_activation' || message.eventType === 'pack_activation') return 'tool';
  if (message.eventType === 'workflow_run' || message.eventType === 'workflow_step' || message.eventType === 'dynamic_workflow') return 'workflow';
  if (message.eventType === 'child_session' || message.eventType === 'subagent' || message.eventType === 'team_member') return 'subagent';
  if (message.eventType === 'schedule' || message.eventType === 'schedule_fire' || message.eventType === 'once') return 'trigger';
  if (message.eventType === 'artifact_update' || message.eventType === 'artifact_delivery') return 'artifact';
  return 'event';
}

function statusForMessage(message: AgentChatMessage): RunStepStatus {
  if (message.role === 'tool_call') {
    if (message.toolMeta?.kind === 'user_clarification' && message.toolMeta.blocking) return 'blocked';
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

function formatCount(count: number, singular: string, plural: string): string {
  return count === 1 ? singular : plural;
}

function buildAggregateSummary(steps: RunStepSnapshot[]): string {
  const fileCount = steps.filter((step) => step.kind === 'file').length;
  const searchCount = steps.filter((step) => step.kind === 'search').length;
  const commandCount = steps.filter((step) => step.kind === 'command').length;
  const parts: string[] = [];

  if (fileCount > 0) parts.push(`Read ${fileCount} ${formatCount(fileCount, 'file', 'files')}`);
  if (searchCount > 0) parts.push(`Searched web ${searchCount} ${formatCount(searchCount, 'time', 'times')}`);
  if (commandCount > 0) parts.push(`Ran ${commandCount} ${formatCount(commandCount, 'command', 'commands')}`);

  return parts.join(' · ');
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
    summary: buildAggregateSummary(steps) || undefined,
    steps,
    answerMessageId: answer ? answer.id || `answer-${answerIndex}` : undefined,
  };
}

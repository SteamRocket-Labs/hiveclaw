import type { AgentChatMessage } from './chatRuntime';

export type RunStepKind =
  | 'reasoning'
  | 'commentary'
  | 'prose'
  | 'tool'
  | 'search'
  | 'file'
  | 'command'
  | 'permission'
  | 'question'
  | 'plan'
  | 'compaction'
  | 'workflow'
  | 'agent_team'
  | 'team_member'
  | 'subagent'
  | 'background_agent'
  | 'a2a'
  | 'trigger'
  | 'artifact'
  | 'event';

// `interrupted` is the typed honest state for transcript steps whose lifecycle
// never reached a terminal mark while no authoritative active run exists
// (crashed/legacy runs). It must never render as live "processing" and never
// fabricate a terminal outcome; duration freezes at the last durable step.
export type RunStepStatus =
  | 'queued'
  | 'running'
  | 'blocked'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

/**
 * Presentation is intentionally independent from the runtime kind.
 *
 * - process: turn-local reasoning/commentary/compaction; follows the turn disclosure.
 * - tool_history: low-risk retrieval/discovery calls; one-line current activity + expandable history.
 * - surface: lifecycle, mutation, command, failure, or recovery state; never hidden by the turn disclosure.
 * - external: an existing interactive/dedicated renderer owns the step (question, approval, plan, etc.).
 */
export type RunStepPresentation = 'process' | 'tool_history' | 'surface' | 'external';

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
  presentation?: RunStepPresentation;
}

export interface RunTimelineSnapshot {
  id: string;
  status: 'idle' | 'running' | 'blocked' | 'done' | 'failed' | 'cancelled' | 'interrupted';
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  summary?: string;
  steps: RunStepSnapshot[];
  answerMessageId?: string;
}

type TimelineBuildOptions = {
  now?: Date;
  answer?: AgentChatMessage;
  /** True only when the authoritative runtime (runs/active poll) reports a live run for this session. */
  activeRun?: boolean;
};

const FILE_TOOL_NAMES = new Set([
  'delete_file',
  'edit_file',
  'fs_list',
  'fs_read',
  'fs_write',
  'list_files',
  'read_document',
  'read_file',
  'write_file',
]);
const SEARCH_TOOL_PREFIXES = ['web_', 'search', 'firecrawl', 'xcrawl'];
const COMMAND_TOOLS = new Set(['execute_code', 'run_command']);
const A2A_TOOLS = new Set([
  'cancel_async_task',
  'check_async_task',
  'delegate_to_agent',
  'list_async_tasks',
  'send_agent_session_message',
  'send_message_to_agent',
]);
const SUBAGENT_TOOLS = new Set(['check_subagent', 'spawn_subagent']);
const TASK_LEDGER_MUTATION_TOOLS = new Set(['task_create', 'task_stop', 'task_update', 'track_todo']);

// Source-checked against the 145 canonical @tool registrations on 2026-07-17.
// This is deliberately an allowlist: new/unknown tools stay surfaced until a
// human classifies them as non-blocking retrieval. That prevents an interactive
// or side-effecting tool from silently disappearing into generic history.
export const FOLDABLE_RETRIEVAL_TOOL_NAMES: ReadonlySet<string> = new Set([
  'advanced_web_fetch',
  'advanced_web_search',
  'anysearch_batch_search',
  'anysearch_extract',
  'anysearch_get_sub_domains',
  'anysearch_search',
  'discover_resources',
  'exa_fetch',
  'exa_search',
  'feishu_approval_definition',
  'feishu_approval_get',
  'feishu_approval_query',
  'feishu_base_field_list',
  'feishu_base_record_list',
  'feishu_base_table_list',
  'feishu_calendar_list',
  'feishu_doc_read',
  'feishu_drive_file_read',
  'feishu_sheet_info',
  'feishu_sheet_read',
  'feishu_task_list',
  'feishu_url_read',
  'feishu_url_resolve',
  'feishu_user_search',
  'feishu_wiki_list',
  'firecrawl_fetch',
  'firecrawl_search',
  'fs_list',
  'fs_read',
  'get_current_time',
  'get_goal',
  'glob_search',
  'grep_search',
  'inspect_mcp_tool',
  'list_files',
  'list_mcp_tools',
  'load_memory',
  'load_skill',
  'mcp_auth_status',
  'mcp_get_prompt',
  'mcp_list_prompts',
  'mcp_list_resources',
  'mcp_read_resource',
  'office_document_dump',
  'office_document_query',
  'office_document_validate',
  'office_document_view',
  'plaza_get_new_posts',
  'read_context_resource',
  'read_document',
  'read_emails',
  'read_file',
  'read_ledger',
  'read_personal_kb',
  'read_runtime_result',
  'search_clawhub',
  'search_memory',
  'search_personal_kb',
  'task_get',
  'task_list',
  'task_output',
  'tavily_extract',
  'tavily_search',
  'tool_search',
  'web_fetch',
  'web_search',
  'xcrawl_scrape',
]);

const DEDICATED_TOOL_CARD_META_KINDS = new Set([
  'create_employee_success',
  'dynamic_workflow_proposal',
  'hr_preview',
  'plan_mode_request',
  'plan_proposal',
  'user_clarification',
  'workflow_preview',
]);

export function isDedicatedToolCardMessage(message: AgentChatMessage): boolean {
  if (message.role !== 'tool_call') return false;
  if (message.sessionPermissionRequest) return true;
  return Boolean(message.toolMeta && DEDICATED_TOOL_CARD_META_KINDS.has(message.toolMeta.kind));
}

export function getToolStepPresentation(
  message: AgentChatMessage,
  status: RunStepStatus,
): RunStepPresentation {
  if (isDedicatedToolCardMessage(message)) return 'external';
  if (status === 'failed' || status === 'blocked' || status === 'cancelled') return 'surface';
  const name = String(message.toolName || '').trim().toLowerCase();
  // CC/FreeCode and Codex consume successful task mutations through their
  // dedicated task/plan surface. Repeating the raw tool call in the Session
  // creates two conflicting product truths. Failures remain surfaced above.
  if (TASK_LEDGER_MUTATION_TOOLS.has(name)) return 'external';
  return FOLDABLE_RETRIEVAL_TOOL_NAMES.has(name) ? 'tool_history' : 'surface';
}
const SESSION_NATIVE_DISCLOSURE_EVENTS = new Set([
  'permission',
  'permission_request',
  'permission_resolved',
  'session_compact',
  'context_compaction',
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
  'agent_task_notification',
  'subagent',
  'team_member',
  'schedule',
  'schedule_fire',
  'goal',
  'once',
  'memory_candidate',
  'artifact_update',
  'artifact_delivery',
  'file_changes',
  'runtime_action_started',
  'runtime_action_progress',
  'runtime_action_completed',
  'runtime_action_blocked',
  'runtime_action_failed',
  'memory_context_degraded',
  'memory_context_unavailable',
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

function summarizeToolCompletion(message: AgentChatMessage): string {
  if (message.toolStatus !== 'done') return '';
  const result = message.toolResult;
  if (typeof result !== 'string') return '';
  const text = result
    .replace(/^[\s✅✓✔️☑️]+/u, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!text) return '';
  const name = (message.toolName || '').toLowerCase();
  if (COMMAND_TOOLS.has(name)) return '';

  const isFileTool = FILE_TOOL_NAMES.has(name);
  if (isFileTool) {
    const isOperationalReceipt =
      /^(written|wrote|created|updated|edited|patched|deleted|saved|copied|moved|renamed|applied|validated)\b/i.test(text)
      || /\bworkspace\/[^\s]+ \(\d+ chars\)/i.test(text);
    return isOperationalReceipt ? compactText(text, 160) : '';
  }
  return '';
}

function summarizeToolMessage(message: AgentChatMessage): string {
  const name = (message.toolName || '').toLowerCase();
  if (message.toolName === 'tool_search') return 'Checking available tools';
  if (TASK_LEDGER_MUTATION_TOOLS.has(name)) {
    const taskLabel = stringArg(message, [
      'activeForm',
      'active_form',
      'subject',
      'title',
      'content',
    ]);
    if (taskLabel) return compactText(taskLabel, 120);
    const taskId = stringArg(message, ['task_id', 'taskId', 'id']);
    return taskId ? `Task ${compactText(taskId, 100)}` : 'Task ledger updated';
  }
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
  const completionSummary = summarizeToolCompletion(message);
  if (completionSummary) return completionSummary;
  if (COMMAND_TOOLS.has(name)) return summarizeCommandTool(message);
  if (FILE_TOOL_NAMES.has(name)) return summarizeFileTool(message);
  if (SEARCH_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix))) return summarizeSearchTool(message);
  const path = stringArg(message, ['path', 'file_path', 'target_path', 'source_path']);
  if (path) return compactText(basename(path), 120);
  return message.toolArgs && Object.keys(message.toolArgs).length > 0 ? 'Details available' : '';
}

function stepIdForMessage(message: AgentChatMessage, index: number): string {
  if (message.toolMeta?.kind === 'runtime_step' && message.toolMeta.stepId) return message.toolMeta.stepId;
  return message.id || `step-${index}`;
}

function eventTitle(message: AgentChatMessage): string {
  if (String(message.eventType || '').startsWith('runtime_action_')) {
    const suffix = String(message.eventType).replace('runtime_action_', '');
    return `Action ${suffix.charAt(0).toUpperCase()}${suffix.slice(1)}`;
  }
  if (message.eventType === 'child_session' && String(message.eventReason || '').startsWith('delegation_')) {
    return 'A2A session';
  }
  return message.eventTitle || message.eventType || 'Runtime event';
}

export function getDisclosureStepSummary(message: AgentChatMessage): string {
  if (message.role === 'assistant') {
    return compactText(message.thinking || message.content || '');
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
    return compactText(message.content || message.eventReason || message.eventNextStep || '');
  }

  return '';
}

function kindForToolMessage(message: AgentChatMessage): RunStepKind {
  if (message.toolMeta?.kind === 'user_clarification') return 'question';
  if (message.toolMeta?.kind === 'plan_mode_request' || message.toolMeta?.kind === 'plan_proposal') return 'plan';
  if (message.toolMeta?.kind === 'dynamic_workflow_proposal' || message.toolMeta?.kind === 'workflow_preview') return 'workflow';

  const name = (message.toolName || '').toLowerCase();
  if (COMMAND_TOOLS.has(name)) return 'command';
  if (FILE_TOOL_NAMES.has(name)) return 'file';
  if (SEARCH_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix))) return 'search';
  if (name.includes('workflow')) return 'workflow';
  if (A2A_TOOLS.has(name) || name.includes('a2a')) return 'a2a';
  if (SUBAGENT_TOOLS.has(name) || name.includes('subagent')) return 'subagent';
  if (name.includes('trigger') || name.includes('schedule')) return 'trigger';
  if (name === 'request_shell_escalation') return 'permission';
  if (name.includes('plan')) return 'plan';
  return 'tool';
}

function titleForToolMessage(message: AgentChatMessage): string {
  if (message.toolName === 'tool_search') return 'Loading tools';
  const name = (message.toolName || '').toLowerCase();
  if (TASK_LEDGER_MUTATION_TOOLS.has(name)) return 'Update tasks';
  if (name === 'read_file' || name === 'fs_read' || name === 'read_document') return 'Read file';
  if (name === 'write_file' || name === 'fs_write') return 'Write file';
  if (name === 'edit_file') return 'Edit file';
  if (name === 'delete_file') return 'Delete file';
  if (name === 'list_files' || name === 'fs_list') return 'List files';
  if (name === 'grep_search' || name === 'glob_search') return 'Search files';
  if (name === 'web_search') return 'Search web';
  if (name === 'web_fetch' || name === 'firecrawl_fetch' || name === 'xcrawl_scrape') return 'Fetch web page';
  if (COMMAND_TOOLS.has(name)) return 'Run command';
  if (name === 'office_document_apply') return 'Edit document';
  if (name.includes('workflow')) return 'Workflow step';
  if (A2A_TOOLS.has(name) || name.includes('a2a')) return 'A2A step';
  if (SUBAGENT_TOOLS.has(name) || name.includes('subagent')) return 'Sub-agent step';
  if (name.includes('trigger') || name.includes('schedule')) return 'Schedule step';
  if (name === 'request_shell_escalation') return 'Permission request';
  if (name.includes('plan')) return 'Plan step';
  return message.toolName || 'Tool call';
}

function kindForEventMessage(message: AgentChatMessage): RunStepKind {
  if (message.eventType === 'session_compact' || message.eventType === 'context_compaction') return 'compaction';
  if (
    message.eventType === 'permission'
    || message.eventType === 'permission_request'
    || message.eventType === 'permission_resolved'
  ) return 'permission';
  if (String(message.eventType || '').startsWith('runtime_action_')) {
    const source = String(message.eventNotificationSource || '').toLowerCase();
    if (source.includes('workflow')) return 'workflow';
    if (source.includes('agent_team')) return 'agent_team';
    if (source.includes('team_member')) return 'team_member';
    if (source.includes('subagent')) return 'subagent';
    if (source.includes('background') || source.includes('long_task')) return 'background_agent';
    if (source.includes('a2a')) return 'a2a';
    return 'event';
  }
  if (
    message.eventType === 'tool_group_activation'
    || message.eventType === 'pack_activation'
    || message.eventType === 'deferred_tools_delta'
  ) return 'tool';
  if (message.eventType === 'workflow_run' || message.eventType === 'workflow_step' || message.eventType === 'dynamic_workflow') return 'workflow';
  if (
    message.eventType === 'child_session'
    && String(message.eventReason || '').startsWith('delegation_')
  ) return 'a2a';
  if (message.eventType === 'agent_task_notification') {
    const source = String(message.eventNotificationSource || '').toLowerCase();
    if (source.includes('workflow')) return 'workflow';
    if (source.includes('agent_team')) return 'agent_team';
    if (source.includes('team_member')) return 'team_member';
    if (source.includes('subagent')) return 'subagent';
    if (source.includes('background') || source.includes('long_task')) return 'background_agent';
    return 'a2a';
  }
  if (message.eventType === 'team_member') return 'team_member';
  if (message.eventType === 'subagent' || message.eventType === 'child_session') return 'subagent';
  if (message.eventType === 'schedule' || message.eventType === 'schedule_fire' || message.eventType === 'once') return 'trigger';
  if (
    message.eventType === 'artifact_update'
    || message.eventType === 'artifact_delivery'
    || message.eventType === 'file_changes'
  ) return 'artifact';
  return 'event';
}

const EXTERNALLY_RENDERED_THREAD_ITEM_TYPES = new Set(['approval_request', 'error', 'plan', 'warning']);

function eventHasDedicatedConversationSurface(message: AgentChatMessage): boolean {
  if (message.sessionPermissionRequest && message.eventStatus === 'session_permission_required') return true;
  const item = message.threadItem;
  if (!item) return false;
  if (EXTERNALLY_RENDERED_THREAD_ITEM_TYPES.has(item.item_type)) return true;
  return Boolean(item.user_action && item.user_action.kind !== 'open_artifact');
}

function getEventStepPresentation(
  message: AgentChatMessage,
  kind: RunStepKind,
  status: RunStepStatus,
): RunStepPresentation {
  if (eventHasDedicatedConversationSurface(message)) return 'external';
  if (kind === 'compaction') return 'process';
  if (kind === 'permission' && status === 'blocked') return 'external';
  if (kind === 'tool') return 'tool_history';
  if (
    message.eventType === 'hook_progress'
    || message.eventType === 'hook_summary'
    || message.eventType === 'hook_attachment'
  ) return 'process';
  return 'surface';
}

function statusForMessage(message: AgentChatMessage, allowLiveRunning: boolean): RunStepStatus {
  const live = (status: RunStepStatus): RunStepStatus =>
    status === 'running' && !allowLiveRunning ? 'interrupted' : status;
  if (message.role === 'tool_call') {
    if (message.toolMeta?.kind === 'user_clarification' && message.toolMeta.blocking) return 'blocked';
    if (message.toolMeta?.kind === 'runtime_step') {
      const status = String(message.toolMeta.status || '').toLowerCase();
      if (status === 'failed' || status === 'error') return 'failed';
      if (status === 'blocked' || status === 'approval_required') return 'blocked';
      if (status === 'cancelled' || status === 'canceled') return 'cancelled';
      if (status === 'running' || status === 'pending' || status === 'in_progress') return live('running');
    }
    return message.toolStatus === 'running' ? live('running') : 'done';
  }
  if (message.role === 'assistant') {
    if (message.sessionItem && !message.sessionItem.terminal) return live('running');
    const status = String(message.eventStatus || '').toLowerCase();
    if (status === 'started' || status === 'delta' || status === 'running' || status === 'in_progress' || status === 'pending') {
      return live('running');
    }
    if (status === 'failed' || status === 'error') return 'failed';
    if (status === 'cancelled' || status === 'canceled') return 'cancelled';
    return 'done';
  }
  if (message.role === 'event') {
    const status = String(message.eventStatus || '').toLowerCase();
    if (status === 'running' || status === 'in_progress' || status === 'pending') return live('running');
    if (status === 'blocked' || status === 'approval_required' || status === 'session_permission_required') return 'blocked';
    if (status === 'failed' || status === 'error') return 'failed';
    if (status === 'cancelled' || status === 'canceled') return 'cancelled';
  }
  return 'done';
}

export function isDisclosureStepMessage(message: AgentChatMessage): boolean {
  const canonicalKind = message.sessionItem?.kind;
  if (canonicalKind && (
    canonicalKind.startsWith('assistant_')
    || canonicalKind.startsWith('a2a_')
  )) return true;
  if (message.role === 'tool_call') return true;
  if (message.role === 'assistant') {
    return message.eventType === 'assistant_commentary' || Boolean(message.thinking?.trim());
  }
  if (message.role === 'event') {
    return Boolean(message.eventType && SESSION_NATIVE_DISCLOSURE_EVENTS.has(message.eventType));
  }
  return false;
}

function buildStep(message: AgentChatMessage, index: number, allowLiveRunning: boolean): RunStepSnapshot | null {
  if (!isDisclosureStepMessage(message)) return null;
  const status = statusForMessage(message, allowLiveRunning);
  const summary = getDisclosureStepSummary(message);
  const canonicalKind = message.sessionItem?.kind;

  if (canonicalKind === 'assistant_reasoning_private') {
    return {
      id: stepIdForMessage(message, index),
      kind: 'reasoning',
      title: 'Thinking',
      status,
      startedAt: message.timestamp,
      completedAt: status === 'done' ? message.timestamp : undefined,
      summary: message.content?.trim() ? compactText(message.content) : 'Provider-private reasoning was used.',
      details: message.content?.trim() || undefined,
      visibility: 'collapsed',
      presentation: 'process',
    };
  }

  if (canonicalKind === 'assistant_reasoning_summary') {
    return {
      id: stepIdForMessage(message, index),
      kind: 'reasoning',
      title: 'Thinking',
      status,
      startedAt: message.timestamp,
      completedAt: status === 'done' ? message.timestamp : undefined,
      summary,
      details: message.content || undefined,
      visibility: 'collapsed',
      presentation: 'process',
    };
  }

  if (canonicalKind === 'assistant_commentary' || (!canonicalKind && message.eventType === 'assistant_commentary')) {
    return {
      id: stepIdForMessage(message, index),
      kind: 'commentary',
      title: 'Progress update',
      status,
      startedAt: message.timestamp,
      completedAt: status === 'done' ? message.timestamp : undefined,
      summary,
      details: message.content || undefined,
      visibility: 'visible',
      presentation: 'process',
    };
  }

  if (canonicalKind === 'assistant_text') {
    return {
      id: stepIdForMessage(message, index),
      kind: 'prose',
      title: 'Assistant update',
      status,
      startedAt: message.timestamp,
      completedAt: status === 'done' ? message.timestamp : undefined,
      summary,
      details: message.content || undefined,
      visibility: 'visible',
      presentation: 'process',
    };
  }

  if (canonicalKind === 'assistant_final') {
    return null;
  }

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
      presentation: 'process',
    };
  }

  if (message.role === 'tool_call') {
    if (String(message.toolName || '').trim().toLowerCase() === 'report_progress') {
      const publicMessage = message.toolArgs?.message;
      if (typeof publicMessage !== 'string' || !publicMessage.trim()) return null;
      return {
        id: stepIdForMessage(message, index),
        toolCallId: message.toolMeta?.kind === 'runtime_step' ? message.toolMeta.toolCallId : undefined,
        kind: 'commentary',
        title: 'Progress update',
        status,
        startedAt: message.timestamp,
        completedAt: status === 'done' ? message.timestamp : undefined,
        durationMs: message.toolMeta?.kind === 'runtime_step' ? message.toolMeta.durationMs ?? undefined : undefined,
        summary: publicMessage,
        details: publicMessage,
        visibility: 'visible',
        presentation: 'process',
      };
    }
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
      presentation: getToolStepPresentation(message, status),
    };
  }

  const kind = kindForEventMessage(message);
  return {
    id: stepIdForMessage(message, index),
    kind,
    title: eventTitle(message),
    status,
    startedAt: message.timestamp,
    completedAt: message.timestamp,
    summary,
    details: message.sessionPermissionRequest && message.eventStatus === 'session_permission_required'
      ? undefined
      : message.content || undefined,
    visibility: 'collapsed',
    blocking: status === 'blocked',
    presentation: getEventStepPresentation(message, kind, status),
  };
}

function getTimestampMs(value?: string): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function getTimelineStatus(steps: RunStepSnapshot[], hasAnswer: boolean): RunTimelineSnapshot['status'] {
  if (hasAnswer) return 'done';
  if (steps.some((step) => step.status === 'failed')) return 'failed';
  if (steps.some((step) => step.status === 'blocked')) return 'blocked';
  if (steps.some((step) => step.status === 'running')) return 'running';
  if (steps.some((step) => step.status === 'interrupted')) return 'interrupted';
  if (steps.length > 0 || hasAnswer) return 'done';
  return 'idle';
}

function isAssistantAnswer(message: AgentChatMessage): boolean {
  if (!message.content?.trim()) return false;
  if (message.sessionItem) {
    return message.sessionItem.kind === 'assistant_final' && message.sessionItem.terminal;
  }
  if (message.eventType) {
    if (message.eventType === 'assistant_final' || message.eventType === 'assistant_message') return true;
    if (message.eventType.startsWith('assistant_')) return false;
  }
  return message.role === 'assistant';
}

function formatCount(count: number, singular: string, plural: string): string {
  return count === 1 ? singular : plural;
}

function buildAggregateSummary(steps: RunStepSnapshot[]): string {
  const hiddenProcessTools = steps.filter((step) => step.presentation === 'tool_history');
  const fileCount = hiddenProcessTools.filter((step) => step.kind === 'file').length;
  const searchCount = hiddenProcessTools.filter((step) => step.kind === 'search').length;
  const commandCount = hiddenProcessTools.filter((step) => step.kind === 'command').length;
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
  const allowLiveRunning = options.activeRun === true;
  const steps = messages
    .map((message, index) => buildStep(message, index, allowLiveRunning))
    .filter((step): step is RunStepSnapshot => Boolean(step));
  const answerIndex = messages.findIndex(isAssistantAnswer);
  const answer = options.answer || (answerIndex >= 0 ? messages[answerIndex] : null);
  const status = getTimelineStatus(steps, Boolean(answer));
  const firstTime = steps.map((step) => getTimestampMs(step.startedAt)).find((time): time is number => time != null);
  const lastStepTime = [...steps].reverse().map((step) => getTimestampMs(step.completedAt || step.startedAt)).find((time): time is number => time != null);
  const answerTime = getTimestampMs(answer?.timestamp);
  // `interrupted` must not fabricate a terminal completion: completedAt stays
  // undefined and duration freezes at the last durable step timestamp.
  const completedAt = status === 'running' || status === 'interrupted' ? undefined : answerTime ?? lastStepTime;
  const nowMs = options.now?.getTime();
  const durationEnd = completedAt ?? (status === 'interrupted' ? lastStepTime ?? nowMs : nowMs);

  return {
    id: 'timeline-0',
    status,
    startedAt: firstTime != null ? new Date(firstTime).toISOString() : undefined,
    completedAt: completedAt != null ? new Date(completedAt).toISOString() : undefined,
    durationMs: firstTime != null && durationEnd != null ? Math.max(0, durationEnd - firstTime) : undefined,
    summary: buildAggregateSummary(steps) || undefined,
    steps,
    answerMessageId: answer
      ? answer.id || (answerIndex >= 0 ? `answer-${answerIndex}` : 'answer-external')
      : undefined,
  };
}

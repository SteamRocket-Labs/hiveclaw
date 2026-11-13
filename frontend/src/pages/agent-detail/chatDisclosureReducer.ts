import { isStreamingAssistantMessage, type AgentChatMessage } from './chatRuntime';
import { memberFilename, memberHostFromUrl } from './memberToolSummary';

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
  /** This step's text is byte-identical to the final answer below the block;
   * renderers skip the repeat while step counts keep the record. */
  repeatsAnswer?: boolean;

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
  systemEvent?: { type: string; metadata: Record<string, unknown> };
  // Set only on render-time copies by the system step presenter.
  titleIsLocalized?: boolean;
}

/** Locale-neutral run aggregate: the renderer composes the sentence. */
export interface RunAggregateSummary {
  files: number;
  searches: number;
  commands: number;
}

export interface RunTimelineSnapshot {
  id: string;
  status: 'idle' | 'running' | 'blocked' | 'done' | 'failed' | 'cancelled' | 'interrupted';
  /** Pending input presentation; the run lifecycle keeps its existing status. */
  phase?: 'queued';
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  /** The run terminal or runtime row supplies the exact duration. */
  durationIsFinal?: boolean;
  /** Presentation-only (X2 r2 D): the loaded page starts mid-turn, so this
   * block's evidence is incomplete — render no status, count or clock until
   * the page that completes the turn arrives. The model status is unchanged. */
  statusUnknown?: boolean;
  /** Presentation-only (X2 r2 E): the block is done, but it still holds a
   * delegation whose colleague may be working for this reader — the header
   * shows the existing running label instead of the done label. The model
   * status is unchanged. */
  delegationInFlight?: boolean;
  summary?: RunAggregateSummary;
  steps: RunStepSnapshot[];
  answerMessageId?: string;
}

type TimelineBuildOptions = {
  now?: Date;
  answer?: AgentChatMessage;
  /** Durable timestamp of the user input that opened this turn. */
  turnStartedAt?: string;
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
// Prefix match for the step-level query/url summary display only. This must
// never feed the aggregate "Searched web" count: search_personal_kb,
// search_memory, and friends start with "search" but never touch the web.
const SEARCH_TOOL_PREFIXES = ['web_', 'search', 'firecrawl', 'xcrawl'];
// Exact web-search registry, source-checked against the @tool registrations in
// backend/app/tools/handlers/search.py. Only these count as web search; web
// fetches (web_fetch, firecrawl_fetch, xcrawl_scrape, …) and internal search
// surfaces (Personal KB, memory, clawhub) are excluded on purpose.
const WEB_SEARCH_TOOL_NAMES = new Set([
  'advanced_web_search',
  'anysearch_batch_search',
  'anysearch_search',
  'exa_search',
  'firecrawl_search',
  'tavily_search',
  'web_search',
]);
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
  'enterprise_approval',
  'employee_first_task',
  'create_employee_success',
  'dynamic_workflow_proposal',
  'hr_creation_handoff',
  'hr_preview',
  'employee_creation_refused',
  'plan_mode_request',
  'plan_proposal',
  'user_clarification',
  'workflow_preview',
]);

/** X2 r2 E: this step still reports a delegated task as in progress for this
 * reader — the colleague may be working. The typed terminal rewrite
 * (`projectMemberDelegationStates`) clears both signals once the child's
 * terminal receipt reaches this session, so the label never outlives real
 * evidence the reader can see. */
export function stepDelegationStillInProgress(step: RunStepSnapshot): boolean {
  // X2 r3: the label may only mark a delegation the projection can also CLEAR
  // — `projectMemberDelegationStates` keys rows by task id, so an untyped
  // started row or an in_progress summary without a task id must not set it.
  const summary = (step.details as { memberSummary?: { kind?: string; state?: string; taskId?: string } } | null)?.memberSummary;
  if (summary?.kind === 'delegation' && summary.state === 'in_progress'
    && typeof summary.taskId === 'string' && summary.taskId) return true;
  const event = step.systemEvent;
  if (!event || event.type !== 'runtime_action_started') return false;
  const metadata = event.metadata || {};
  if (metadata.action_kind !== 'a2a_delegation') return false;
  if (!String(metadata.runtime_task_id || metadata.task_id || '')) return false;
  return ['running', 'in_progress', 'pending', 'queued'].includes(String(metadata.status || step.status || ''));
}

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
  'runtime_action_unconfirmed',
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

function summarizeFileTool(message: AgentChatMessage): string {
  // X3c: native rows keep no args; the typed member summary owns the name.
  const fromSummary = message.toolMemberSummary?.filename;
  if (fromSummary) return compactText(fromSummary, 120);
  const target = stringArg(message, ['path', 'file_path', 'target_path', 'source_path', 'name']);
  const filename = memberFilename(target);
  return filename ? compactText(filename, 120) : '';
}

function summarizeSearchTool(message: AgentChatMessage): string {
  const url = stringArg(message, ['url']);
  if (url) return message.toolMemberSummary ? message.toolMemberSummary.host || '' : memberHostFromUrl(url) || '';
  const target = stringArg(message, ['query', 'domain']);
  return target ? compactText(target, 120) : '';
}

function summarizeToolMessage(message: AgentChatMessage): string {
  const name = (message.toolName || '').toLowerCase();
  if (message.toolMemberSummary?.kind === 'web_fetch') return message.toolMemberSummary.host || '';
  // X3c: the collapsed MCP row names the trusted connection label.
  if (message.toolMemberSummary?.kind === 'mcp_app') return message.toolMemberSummary.appLabel || '';
  // Step summaries carry only the member's own material (file names, queries,
  // model-authored labels). Command lines and tool receipts stay collapsed in
  // the step details (B2): the default layer shows the action and its state.
  if (message.toolName === 'tool_search') return '';
  if (TASK_LEDGER_MUTATION_TOOLS.has(name)) {
    const taskLabel = stringArg(message, [
      'activeForm',
      'active_form',
      'subject',
      'title',
      'content',
    ]);
    return taskLabel ? compactText(taskLabel, 120) : '';
  }
  if (message.toolMeta?.kind === 'user_clarification') return '';
  if (message.toolMeta?.kind === 'plan_proposal') {
    return message.toolMeta.summary || message.toolMeta.nextAction || '';
  }
  if (message.toolMeta?.kind === 'dynamic_workflow_proposal') {
    return message.toolMeta.goal || message.toolMeta.nextAction || '';
  }
  if (COMMAND_TOOLS.has(name)) return '';
  if (FILE_TOOL_NAMES.has(name)) return summarizeFileTool(message);
  if (SEARCH_TOOL_PREFIXES.some((prefix) => name.startsWith(prefix))) return summarizeSearchTool(message);
  const path = stringArg(message, ['path', 'file_path', 'target_path', 'source_path']);
  const filename = memberFilename(path);
  if (filename) return compactText(filename, 120);
  return '';
}

function stepIdForMessage(message: AgentChatMessage, index: number): string {
  if (message.toolMeta?.kind === 'runtime_step' && message.toolMeta.stepId) return message.toolMeta.stepId;
  return message.id || `step-${index}`;
}

function eventTitle(message: AgentChatMessage): string {
  // Titles are presentation sentinels localized at render time (B2); event
  // type suffixes and raw reasons never become user-facing title text.
  if (String(message.eventType || '').startsWith('runtime_action_')) {
    return 'Runtime event';
  }
  if (message.eventType === 'child_session' && String(message.eventReason || '').startsWith('delegation_')) {
    // The direction matters: a completed/blocked/failed receipt came BACK
    // from the colleague; only started/progress updates go the other way.
    // The event's terminal status decides (X1-r3 ruling 6): delegation_terminal
    // covers every non-completed terminal reason (failed/killed; skipped
    // projects to blocked) and a contract mismatch projects to blocked —
    // confirmed against direct_invocation_terminal_boundary_processor.py and
    // orchestrator.py, which never emit a completed status for these. The
    // reason literals stay as a fallback for events that carry no status.
    const terminalStatus = String(message.eventStatus || '').toLowerCase();
    return terminalStatus === 'completed'
      ? 'Delegation result'
      : ['blocked', 'failed', 'cancelled', 'canceled', 'killed'].includes(terminalStatus)
        || ['delegation_blocked', 'delegation_failed'].includes(String(message.eventReason))
        ? 'Delegation unfinished'
        : 'Delegation update';
  }
  return message.eventTitle || 'Runtime event';
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
      // The dedicated approval card owns this copy; the step row itself stays
      // a localized sentinel (B2).
      return 'Permission request';
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
  // Only registry web searches are "search" steps: the aggregate summary
  // counts this kind as "Searched web", so prefix matches must not leak
  // Personal KB / memory / clawhub lookups into a web-search claim.
  if (WEB_SEARCH_TOOL_NAMES.has(name)) return 'search';
  if (name.includes('workflow')) return 'workflow';
  if (A2A_TOOLS.has(name) || name.includes('a2a')) return 'a2a';
  if (SUBAGENT_TOOLS.has(name) || name.includes('subagent')) return 'subagent';
  if (name.includes('trigger') || name.includes('schedule')) return 'trigger';
  if (name === 'request_shell_escalation') return 'permission';
  if (name.includes('plan')) return 'plan';
  return 'tool';
}

export function titleForToolMessage(message: Pick<AgentChatMessage, 'toolName'>): string {
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
  if (A2A_TOOLS.has(name) || name.includes('a2a')) return 'Delegation step';
  // The sentinel must camel-case onto the `subagentStep` locale key.
  if (SUBAGENT_TOOLS.has(name) || name.includes('subagent')) return 'Subagent step';
  if (name.includes('trigger') || name.includes('schedule')) return 'Schedule step';
  if (name === 'request_shell_escalation') return 'Permission request';
  if (name.includes('plan')) return 'Plan step';
  // An unrecognized tool is presented by its localized class, never by its
  // raw snake_case id; the id stays available inside the step details.
  return 'Tool call';
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
    // The canonical outcome (denied/aborted/cancelled/unavailable/…) states
    // the action did not happen; the step must agree with the member
    // sentence instead of showing a completed action (X3 r2). X3 r3: the
    // typed member summary owns the same decision when a refusal page
    // arrives on a success frame — and approval_required keeps the existing
    // blocked mapping rather than reading as a failure.
    const outcome = String(message.eventStatus || '').toLowerCase();
    const summaryState = message.toolMemberSummary?.state;
    const envelopeStatus = message.toolMeta?.kind === 'runtime_step' ? message.toolMeta.status : '';
    if (message.toolMemberSummary?.status === 'cancelled' || ['killed', 'cancelled', 'canceled'].includes(String(envelopeStatus))) return 'cancelled';
    if (['blocked', 'skipped'].includes(message.toolMemberSummary?.status || '')) return 'blocked';
    if (outcome === 'approval_required') return 'blocked';
    const notDone = ['failed', 'error', 'denied', 'unavailable'].includes(outcome)
      || summaryState === 'not_done' || summaryState === 'unknown';
    if (notDone || ['cancelled', 'canceled', 'aborted'].includes(outcome)) {
      if (['cancelled', 'canceled', 'aborted'].includes(outcome)) return 'cancelled';
      // No existing member step status distinguishes "unconfirmed": the
      // R17b/R19b path renders the unconfirmed sentence on a step whose
      // status otherwise stays done/failed, so failed is the existing
      // not-done status used here.
      return 'failed';
    }
    // X3c: a command that ran (outcome success) still failed when its typed
    // execution result says so — a non-zero exit or a timeout is not done.
    // Decided from the typed payload field only, never from output text;
    // a zero exit stays done.
    const execution = message.sessionItem?.payload?.code_execution_result;
    if (execution && typeof execution === 'object' && !Array.isArray(execution)) {
      const record = execution as { exit_code?: unknown; timed_out?: unknown };
      if (record.timed_out === true
        || (typeof record.exit_code === 'number' && Number.isFinite(record.exit_code) && record.exit_code !== 0)) {
        return 'failed';
      }
    }
    if (message.toolMeta?.kind === 'user_clarification' && message.toolMeta.blocking && !message.toolMeta.answered) return 'blocked';
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
    if (isStreamingAssistantMessage(message)) return live('running');
    if (message.sessionItem && !message.sessionItem.terminal) return live('running');
    const status = String(message.eventStatus || '').toLowerCase();
    if (status === 'started' || status === 'delta' || status === 'running' || status === 'in_progress' || status === 'pending') {
      return live('running');
    }
    if (status === 'failed' || status === 'error') return 'failed';
    if (status === 'cancelled' || status === 'canceled' || status === 'killed') return 'cancelled';
    return 'done';
  }
  if (message.role === 'event') {
    const status = String(message.eventStatus || '').toLowerCase();
    if (status === 'running' || status === 'in_progress' || status === 'pending') return live('running');
    if (status === 'blocked' || status === 'skipped' || status === 'approval_required' || status === 'session_permission_required') return 'blocked';
    if (status === 'failed' || status === 'error') return 'failed';
    if (status === 'cancelled' || status === 'canceled' || status === 'killed') return 'cancelled';
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
    return isStreamingAssistantMessage(message)
      || message.eventType === 'assistant_commentary'
      || Boolean(message.thinking?.trim());
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
      // Private-provider reasoning contributes lifecycle state only. Its
      // bytes and provider implementation jargon never belong on the user
      // Session surface.
      summary: undefined,
      details: undefined,
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
      presentation: message.sessionItem?.actor?.type === 'runtime' ? 'surface' : 'process',
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
      details: message.thinking || message.content || undefined,
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
      // Operators opening the step keep the original tool id next to args and
      // results; only the default title is localized (B2 r2).
      details: {
        toolName: message.toolName,
        args: message.toolArgs,
        result: message.toolResult,
        rawResult: message.toolRawResult,
        memberSummary: message.toolMemberSummary,
        executionResult: message.sessionItem?.payload?.code_execution_result,
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
    systemEvent: { type: message.eventType || '', metadata: message.threadItem?.metadata || message.sessionItem?.payload || {} },
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
  if (steps.some((step) => step.status === 'cancelled')) return 'cancelled';
  if (steps.some((step) => step.status === 'interrupted')) return 'interrupted';
  if (steps.length > 0 || hasAnswer) return 'done';
  return 'idle';
}

export function isAssistantAnswer(message: AgentChatMessage): boolean {
  if (isStreamingAssistantMessage(message)) return false;
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

export function buildRunAggregateSummary(steps: RunStepSnapshot[]): RunAggregateSummary | undefined {
  // An interrupted call never returned a result, so it must not surface as a
  // counted completed action ("Searched the web N times") inside a stopped run.
  const completed = steps.filter((step) => step.status !== 'interrupted');
  const hiddenProcessTools = completed.filter((step) => step.presentation === 'tool_history');
  const files = completed.filter((step) => step.status === 'done' && step.presentation !== 'external'
    && ['read_file', 'fs_read', 'read_document'].includes(String((step.details as { toolName?: string } | null)?.toolName))).length;
  const searches = hiddenProcessTools.filter((step) => step.kind === 'search').length;
  const commands = hiddenProcessTools.filter((step) => step.kind === 'command').length;
  return files + searches + commands > 0 ? { files, searches, commands } : undefined;
}

function workflowEventKey(message: AgentChatMessage): string | null {
  if (message.role !== 'event' || !message.eventWorkflowRunId) return null;
  if (message.eventType === 'runtime_action_progress' || message.eventType === 'runtime_action_completed') {
    return JSON.stringify(message.eventWorkflowStepId
      ? [message.eventWorkflowRunId, message.eventWorkflowStepId]
      : [message.eventWorkflowRunId]);
  }
  if (message.eventType === 'workflow_run') return JSON.stringify([message.eventWorkflowRunId]);
  if (message.eventType === 'workflow_step' && message.eventWorkflowStepId) {
    return JSON.stringify([message.eventWorkflowRunId, message.eventWorkflowStepId]);
  }
  return null;
}

/** The runtime's receipt for a call it aborted because the member's approval
 * released a re-issue instead (`approved_tool_execution_not_released`). */
function isApprovedExecutionAbort(message: AgentChatMessage): boolean {
  if (message.role !== 'tool_call') return false;
  for (const value of [message.toolRawResult, message.toolResult]) {
    if (!value) continue;
    try {
      const parsed = typeof value === 'string' ? JSON.parse(value) : value;
      if (parsed && typeof parsed === 'object'
        && (parsed as Record<string, unknown>).reason_code === 'approved_tool_execution_not_released') {
        return true;
      }
    } catch {
      // Not the abort receipt's shape.
    }
  }
  return false;
}

export function buildRunTimelineFromMessages(
  messages: AgentChatMessage[],
  options: TimelineBuildOptions = {},
): RunTimelineSnapshot {
  const allowLiveRunning = options.activeRun === true;
  const latestWorkflowEvents = new Map<string, AgentChatMessage>();
  for (const message of messages) {
    const key = workflowEventKey(message);
    if (key) latestWorkflowEvents.set(key, message);
  }
  const steps = messages
    // One approved action, one row (B2 r3): the aborted invocation is
    // bookkeeping of the same action the resumed turn re-issues.
    .filter((message, index) => !(
      isApprovedExecutionAbort(message)
      && messages.slice(index + 1).some((later) => later.role === 'tool_call' && later.toolName === message.toolName)
    ))
    .map((message, index) => {
      const step = buildStep(message, index, allowLiveRunning);
      const key = workflowEventKey(message);
      const latest = key ? latestWorkflowEvents.get(key) : undefined;
      // A lifecycle transition is not an abandoned turn when the same exact
      // workflow entity has a later terminal receipt. Keep all event details.
      if (step && (step.status === 'running' || step.status === 'interrupted') && latest
        && ['done', 'completed', 'failed', 'error', 'cancelled', 'canceled'].includes(latest.eventStatus || '')) {
        return { ...step, status: statusForMessage(latest, allowLiveRunning), completedAt: latest.timestamp };
      }
      return step;
    })
    .filter((step): step is RunStepSnapshot => Boolean(step));
  const answerIndex = messages.findIndex(isAssistantAnswer);
  const answer = options.answer || (answerIndex >= 0 ? messages[answerIndex] : null);
  const status = getTimelineStatus(steps, Boolean(answer));
  const firstStepTime = steps.map((step) => getTimestampMs(step.startedAt)).find((time): time is number => time != null);
  const acceptedInputTime = getTimestampMs(options.turnStartedAt);
  const firstTime = acceptedInputTime != null && firstStepTime != null
    ? Math.min(acceptedInputTime, firstStepTime)
    : acceptedInputTime ?? firstStepTime;
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
    summary: buildRunAggregateSummary(steps),
    steps,
    answerMessageId: answer
      ? answer.id || (answerIndex >= 0 ? `answer-${answerIndex}` : 'answer-external')
      : undefined,
  };
}

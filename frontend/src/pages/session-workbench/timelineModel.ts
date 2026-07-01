import type { SessionIndex } from '../../api/domains/chat';
import type { SessionWorkbench } from '../../api/domains/ccParity';
import {
  buildRunTimelineFromMessages,
  isDisclosureStepMessage,
  type RunTimelineSnapshot,
} from '../agent-detail/chatDisclosureReducer';
import type { AgentChatMessage, ChatRuntimeSummary } from '../agent-detail/chatRuntime';

export type ThreadTimelineCell =
  | {
      kind: 'user_turn';
      id: string;
      message: AgentChatMessage;
      index: number;
    }
  | {
      kind: 'assistant_final';
      id: string;
      message: AgentChatMessage;
      index: number;
    }
  | {
      kind: 'active_run';
      id: string;
      timeline: RunTimelineSnapshot;
      sourceMessages: Array<{ message: AgentChatMessage; index: number }>;
      answer?: AgentChatMessage;
      answerIndex?: number;
    }
  | {
      kind: 'boundary';
      id: string;
      title: string;
      summary?: string;
      index: number;
    };

export type SessionWorkbenchStatus = 'idle' | 'running' | 'waiting' | 'streaming' | 'complete' | 'failed';

export interface SessionWorkbenchHeaderModel {
  sessionId: string | null;
  title: string;
  status: SessionWorkbenchStatus;
  modelLabel: string | null;
  providerLabel: string | null;
  resumeHealth: string;
  checkpointCount: number;
  branchDepth: number;
  compactionCount: number;
  contextWindowStatusLabel: string | null;
  contextWindowTitle: string | null;
  activeRunStatus: string | null;
}

export interface SessionWorkbenchInspectorModel {
  sessionEventCount: number | null;
  t0SegmentCount: number;
  latestCheckpointLabel: string | null;
  usedToolCount: number;
  blockedCapabilityCount: number;
  activatedToolGroupCount: number;
}

export interface ThreadTimelineModel {
  cells: ThreadTimelineCell[];
  header: SessionWorkbenchHeaderModel;
  inspector: SessionWorkbenchInspectorModel;
}

export interface BuildThreadTimelineInput {
  messages: AgentChatMessage[];
  activeSession?: Record<string, unknown> | null;
  runtimeSummary?: ChatRuntimeSummary | null;
  sessionIndex?: SessionIndex | null;
  sessionWorkbench?: SessionWorkbench | null;
  branchLineage?: Array<{ id: unknown; parent_session_id?: unknown }> | null;
  isWaiting: boolean;
  isStreaming: boolean;
  activeRunStatus?: string | null;
}

export type CompletionWakeState = 'pending' | 'running' | 'completed' | 'failed';

export interface CompletionWakeItemModel {
  id: string;
  kind: string;
  label: string;
  status: string;
  state: CompletionWakeState;
  summary: string;
  source: string;
}

export interface CompletionWakeModel {
  summary: {
    total: number;
    pending: number;
    running: number;
    completed: number;
    failed: number;
    terminal: number;
    needsParentObservation: number;
  };
  items: CompletionWakeItemModel[];
}

export type RuntimeSectionName =
  | 'agent_teams'
  | 'subagents'
  | 'workflows'
  | 'background'
  | 'notifications'
  | 'runs'
  | 'raw';

export interface RuntimeSectionItemModel {
  id: string;
  label: string;
  status: string;
  state: string;
  runtimeKind: string;
  summary: string;
  childSessionId: string | null;
  enterable: boolean;
  members: RuntimeSectionItemModel[];
  steps: RuntimeSectionItemModel[];
  leafCalls: RuntimeSectionItemModel[];
  raw: Record<string, unknown>;
}

export interface RuntimeSectionsModel {
  agentTeams: RuntimeSectionItemModel[];
  subagents: RuntimeSectionItemModel[];
  workflows: RuntimeSectionItemModel[];
  background: RuntimeSectionItemModel[];
  notifications: RuntimeSectionItemModel[];
  runs: RuntimeSectionItemModel[];
  raw: RuntimeSectionItemModel[];
  summary: {
    total: number;
    agentTeams: number;
    subagents: number;
    workflows: number;
    background: number;
    notifications: number;
    runs: number;
    raw: number;
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function readCount(source: Record<string, unknown> | null, key: string): number {
  const value = source?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function normalizeCompletionState(value: unknown, status: unknown): CompletionWakeState {
  const raw = String(value || status || '').trim().toLowerCase();
  if (raw === 'pending' || raw === 'queued' || raw === 'scheduled') return 'pending';
  if (raw === 'completed' || raw === 'done' || raw === 'success' || raw === 'succeeded') return 'completed';
  if (raw === 'failed' || raw === 'error' || raw === 'killed' || raw === 'cancelled' || raw === 'canceled') return 'failed';
  return 'running';
}

export function buildCompletionWakeModel(sessionWorkbench?: Record<string, unknown> | null): CompletionWakeModel {
  const summary = asRecord(sessionWorkbench?.completion_wake_summary);
  const rawItems = Array.isArray(sessionWorkbench?.completion_wakes) ? sessionWorkbench.completion_wakes : [];
  const items = rawItems
    .map((item, index): CompletionWakeItemModel | null => {
      const wake = asRecord(item);
      if (!wake) return null;
      const state = normalizeCompletionState(wake.state, wake.status);
      return {
        id: String(wake.id || wake.runtime_task_id || `completion-wake-${index}`),
        kind: String(wake.kind || 'background_task'),
        label: String(wake.label || wake.kind || wake.runtime_task_id || 'background task'),
        status: String(wake.status || state),
        state,
        summary: String(wake.summary || ''),
        source: String(wake.source || ''),
      };
    })
    .filter((item): item is CompletionWakeItemModel => Boolean(item));

  return {
    summary: {
      total: readCount(summary, 'total') || items.length,
      pending: readCount(summary, 'pending') || items.filter((item) => item.state === 'pending').length,
      running: readCount(summary, 'running') || items.filter((item) => item.state === 'running').length,
      completed: readCount(summary, 'completed') || items.filter((item) => item.state === 'completed').length,
      failed: readCount(summary, 'failed') || items.filter((item) => item.state === 'failed').length,
      terminal: readCount(summary, 'terminal') || items.filter((item) => item.state === 'completed' || item.state === 'failed').length,
      needsParentObservation:
        readCount(summary, 'needs_parent_observation') || items.filter((item) => item.state === 'completed' || item.state === 'failed').length,
    },
    items,
  };
}

function readString(source: Record<string, unknown>, keys: string[], fallback = ''): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return fallback;
}

function readBoolean(source: Record<string, unknown>, key: string): boolean | null {
  const value = source[key];
  return typeof value === 'boolean' ? value : null;
}

function readRuntimeSectionArray(sections: Record<string, unknown> | null, key: RuntimeSectionName): unknown[] {
  const value = sections?.[key];
  return Array.isArray(value) ? value : [];
}

function normalizeRuntimeSectionItem(
  value: unknown,
  index: number,
  fallbackKind: string,
): RuntimeSectionItemModel | null {
  const item = asRecord(value);
  if (!item) return null;
  const runtimeKind = readString(item, ['runtime_kind', 'runtimeKind', 'kind', 'type'], fallbackKind);
  const id = readString(item, ['id', 'chat_session_id', 'child_session_id', 'runtime_task_id', 'workflow_run_id'], `${fallbackKind}-${index}`);
  const label = readString(item, ['label', 'name', 'title', 'role', 'runtime_task_id'], id);
  const status = readString(item, ['status', 'state'], 'unknown');
  const state = readString(item, ['state', 'status'], status);
  const childSessionId = readString(item, ['child_session_id', 'childSessionId', 'chat_session_id', 'chatSessionId'], '') || null;
  const explicitEnterable = readBoolean(item, 'enterable');
  const members = Array.isArray(item.members)
    ? item.members
      .map((member, memberIndex) => normalizeRuntimeSectionItem(member, memberIndex, 'team_member'))
      .filter((member): member is RuntimeSectionItemModel => Boolean(member))
    : [];
  const steps = Array.isArray(item.steps)
    ? item.steps
      .map((step, stepIndex) => normalizeRuntimeSectionItem(step, stepIndex, 'workflow_step'))
      .filter((step): step is RuntimeSectionItemModel => Boolean(step))
    : [];
  const leafCallsSource = Array.isArray(item.leaf_calls)
    ? item.leaf_calls
    : (Array.isArray(item.leafCalls) ? item.leafCalls : []);
  const leafCalls = leafCallsSource
    .map((leafCall, leafIndex) => normalizeRuntimeSectionItem(leafCall, leafIndex, 'workflow_leaf'))
    .filter((leafCall): leafCall is RuntimeSectionItemModel => Boolean(leafCall));

  return {
    id,
    label,
    status,
    state,
    runtimeKind,
    summary: readString(item, ['summary', 'description', 'content'], ''),
    childSessionId,
    enterable: explicitEnterable ?? Boolean(childSessionId),
    members,
    steps,
    leafCalls,
    raw: item,
  };
}

function normalizeRuntimeSectionItems(
  values: unknown[],
  fallbackKind: string,
): RuntimeSectionItemModel[] {
  return values
    .map((value, index) => normalizeRuntimeSectionItem(value, index, fallbackKind))
    .filter((item): item is RuntimeSectionItemModel => Boolean(item));
}

function legacyRuntimeSections(sessionWorkbench?: Record<string, unknown> | null): RuntimeSectionsModel {
  const rawTasks = Array.isArray(sessionWorkbench?.runtime_tasks) ? sessionWorkbench.runtime_tasks : [];
  const teams = normalizeRuntimeSectionItems(Array.isArray(sessionWorkbench?.teams) ? sessionWorkbench.teams : [], 'agent_team');
  const tasks = normalizeRuntimeSectionItems(rawTasks, 'runtime_task');
  const workflows = tasks.filter((item) => item.runtimeKind.includes('workflow') || String(item.raw.task_type || '').includes('workflow'));
  const subagents = tasks.filter((item) => item.runtimeKind.includes('subagent') || String(item.raw.task_type || '').includes('subagent'));
  const runs = tasks.filter((item) => !workflows.includes(item) && !subagents.includes(item));
  const background = normalizeRuntimeSectionItems(Array.isArray(sessionWorkbench?.completion_wakes) ? sessionWorkbench.completion_wakes : [], 'background_agent');

  return buildRuntimeSectionsSummary({
    agentTeams: teams,
    subagents,
    workflows,
    background,
    notifications: [],
    runs,
    raw: [],
  });
}

function buildRuntimeSectionsSummary(sections: Omit<RuntimeSectionsModel, 'summary'>): RuntimeSectionsModel {
  const summary = {
    agentTeams: sections.agentTeams.length,
    subagents: sections.subagents.length,
    workflows: sections.workflows.length,
    background: sections.background.length,
    notifications: sections.notifications.length,
    runs: sections.runs.length,
    raw: sections.raw.length,
  };
  return {
    ...sections,
    summary: {
      ...summary,
      total: summary.agentTeams + summary.subagents + summary.workflows + summary.background + summary.notifications + summary.runs + summary.raw,
    },
  };
}

export function buildRuntimeSectionsModel(sessionWorkbench?: Record<string, unknown> | null): RuntimeSectionsModel {
  const runtimeSections = asRecord(sessionWorkbench?.runtime_sections);
  if (!runtimeSections) return legacyRuntimeSections(sessionWorkbench);

  return buildRuntimeSectionsSummary({
    agentTeams: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'agent_teams'), 'agent_team'),
    subagents: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'subagents'), 'subagent'),
    workflows: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'workflows'), 'workflow'),
    background: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'background'), 'background_agent'),
    notifications: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'notifications'), 'notification'),
    runs: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'runs'), 'runtime_task'),
    raw: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'raw'), 'raw_event'),
  });
}

function messageId(message: AgentChatMessage, fallback: string): string {
  return message.id || fallback;
}

function getSessionTitle(activeSession?: Record<string, unknown> | null): string {
  const raw = activeSession?.title;
  return typeof raw === 'string' && raw.trim() ? raw : 'Untitled session';
}

function getSessionId(activeSession?: Record<string, unknown> | null): string | null {
  const raw = activeSession?.id;
  return raw == null ? null : String(raw);
}

function getResumeHealth(sessionIndex?: SessionIndex | null): string {
  const health = sessionIndex?.resume_health;
  if (!health || typeof health !== 'object') return 'unknown';
  const status = health.status ?? health.state ?? health.kind;
  return typeof status === 'string' && status.trim() ? status : 'unknown';
}

function getLatestCheckpointLabel(sessionIndex?: SessionIndex | null): string | null {
  const latest = sessionIndex?.checkpoints?.[sessionIndex.checkpoints.length - 1];
  if (!latest) return null;
  const raw = latest.checkpoint_kind ?? latest.kind ?? latest.type ?? latest.id;
  return raw == null ? null : String(raw);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function compactTokenCount(value: number | null): string | null {
  if (value === null) return null;
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function getContextWindowProjection(sessionWorkbench?: SessionWorkbench | null): {
  label: string | null;
  title: string | null;
} {
  const contextWindow = sessionWorkbench?.context_window;
  if (!contextWindow) return { label: null, title: null };
  const latestStatus = contextWindow.latest_status || null;
  const latestSkipped = contextWindow.latest_skipped || null;
  const latestBudget = contextWindow.latest_tool_result_budget || null;
  const reason = String(latestSkipped?.reason || latestBudget?.reason || '').trim();
  const tokensUntil = finiteNumber(latestStatus?.tokens_until_compaction);
  const activeTokens = finiteNumber(latestStatus?.active_context_tokens ?? latestSkipped?.active_context_tokens);
  const scopeTokens = finiteNumber(latestStatus?.auto_compact_scope_tokens);
  const scopeLimit = finiteNumber(latestStatus?.auto_compact_scope_limit);
  const cumulative = finiteNumber(latestStatus?.cumulative_run_tokens ?? latestSkipped?.cumulative_run_tokens);

  let label: string | null = null;
  if (reason) {
    label = `skipped: ${reason}`;
  } else if (tokensUntil !== null) {
    label = `${compactTokenCount(tokensUntil)} left`;
  } else if (contextWindow.decision_count) {
    label = `${contextWindow.decision_count} decisions`;
  }

  const titleParts = [
    activeTokens !== null ? `active ${compactTokenCount(activeTokens)} tokens` : null,
    scopeTokens !== null ? `scope ${compactTokenCount(scopeTokens)} tokens` : null,
    scopeLimit !== null ? `limit ${compactTokenCount(scopeLimit)} tokens` : null,
    tokensUntil !== null ? `${compactTokenCount(tokensUntil)} tokens until compaction` : null,
    cumulative !== null ? `run total ${compactTokenCount(cumulative)} tokens` : null,
    reason ? `latest decision: ${reason}` : null,
  ].filter(Boolean);

  return {
    label,
    title: titleParts.length ? titleParts.join(' · ') : null,
  };
}

function getBranchDepth(activeSession?: Record<string, unknown> | null, branchLineage?: Array<{ id: unknown; parent_session_id?: unknown }> | null): number {
  if (!activeSession?.id || !Array.isArray(branchLineage) || branchLineage.length <= 1) return 0;
  const activeId = String(activeSession.id);
  const byId = new Map(branchLineage.map((item) => [String(item.id), item]));
  let depth = 0;
  let cursor = byId.get(activeId);
  const seen = new Set<string>();
  while (cursor?.parent_session_id && !seen.has(String(cursor.id))) {
    seen.add(String(cursor.id));
    const parent = byId.get(String(cursor.parent_session_id));
    if (!parent) break;
    depth += 1;
    cursor = parent;
  }
  return depth;
}

function getHeaderStatus(
  cells: ThreadTimelineCell[],
  isWaiting: boolean,
  isStreaming: boolean,
  activeRunStatus?: string | null,
): SessionWorkbenchStatus {
  if (isWaiting) return 'waiting';
  if (isStreaming) return 'streaming';
  if (activeRunStatus) return activeRunStatus === 'failed' ? 'failed' : 'running';
  const lastRun = [...cells].reverse().find((cell): cell is Extract<ThreadTimelineCell, { kind: 'active_run' }> => cell.kind === 'active_run');
  if (lastRun?.timeline.status === 'failed') return 'failed';
  if (lastRun?.timeline.status === 'running') return 'running';
  if (lastRun?.timeline.status === 'blocked') return 'waiting';
  if (cells.length > 0) return 'complete';
  return 'idle';
}

function isRenderableAssistantAnswer(message: AgentChatMessage): boolean {
  return message.role === 'assistant' && Boolean(message.content?.trim());
}

function assistantReasoningStepMessage(message: AgentChatMessage): AgentChatMessage {
  return {
    ...message,
    id: message.id ? `${message.id}:reasoning` : message.id,
    content: '',
  };
}

function assistantAnswerMessage(message: AgentChatMessage): AgentChatMessage {
  return {
    ...message,
    thinking: '',
  };
}

function buildRunCell(
  runIndex: number,
  sourceMessages: Array<{ message: AgentChatMessage; index: number }>,
  answer?: { message: AgentChatMessage; index: number },
): Extract<ThreadTimelineCell, { kind: 'active_run' }> {
  const timelineMessages = answer ? [...sourceMessages.map((entry) => entry.message), answer.message] : sourceMessages.map((entry) => entry.message);
  return {
    kind: 'active_run',
    id: `run-${runIndex}-${sourceMessages[0]?.index ?? answer?.index ?? 0}`,
    timeline: buildRunTimelineFromMessages(timelineMessages),
    sourceMessages,
    answer: answer?.message,
    answerIndex: answer?.index,
  };
}

function hasOpenRunCell(cells: ThreadTimelineCell[]): boolean {
  return cells.some((cell) => (
    cell.kind === 'active_run' &&
    (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
  ));
}

function buildPendingRunCell(input: BuildThreadTimelineInput): Extract<ThreadTimelineCell, { kind: 'active_run' }> | null {
  if (!input.isWaiting && !input.isStreaming && !input.activeRunStatus) return null;
  const status: RunTimelineSnapshot['status'] = input.activeRunStatus === 'failed' ? 'failed' : 'running';
  const title = input.isStreaming ? 'Streaming response' : 'Waiting for model';
  const summary = input.activeRunStatus
    ? `Active run: ${input.activeRunStatus}`
    : input.isStreaming
      ? 'The assistant is streaming this turn.'
      : 'The assistant is continuing this turn.';
  return {
    kind: 'active_run',
    id: 'active-run-pending',
    sourceMessages: [],
    timeline: {
      id: 'active-run-pending',
      status,
      steps: [
        {
          id: 'active-run-pending-step',
          kind: 'reasoning',
          title,
          status,
          summary,
          visibility: 'visible',
        },
      ],
    },
  };
}

function buildCells(messages: AgentChatMessage[]): ThreadTimelineCell[] {
  const cells: ThreadTimelineCell[] = [];
  const pendingRun: Array<{ message: AgentChatMessage; index: number }> = [];
  let runIndex = 0;

  const flushRun = () => {
    if (pendingRun.length === 0) return;
    cells.push(buildRunCell(runIndex, [...pendingRun]));
    runIndex += 1;
    pendingRun.length = 0;
  };

  messages.forEach((message, index) => {
    if (isRenderableAssistantAnswer(message)) {
      if (message.thinking?.trim()) {
        const sourceMessages = [
          ...pendingRun,
          { message: assistantReasoningStepMessage(message), index },
        ];
        cells.push(buildRunCell(runIndex, sourceMessages, { message: assistantAnswerMessage(message), index }));
        runIndex += 1;
        pendingRun.length = 0;
        return;
      }
      if (pendingRun.length > 0) {
        cells.push(buildRunCell(runIndex, [...pendingRun], { message, index }));
        runIndex += 1;
        pendingRun.length = 0;
        return;
      }
      cells.push({
        kind: 'assistant_final',
        id: messageId(message, `assistant-${index}`),
        message,
        index,
      });
      return;
    }

    if (isDisclosureStepMessage(message)) {
      pendingRun.push({ message, index });
      return;
    }

    flushRun();
    if (message.role === 'user') {
      cells.push({
        kind: 'user_turn',
        id: messageId(message, `user-${index}`),
        message,
        index,
      });
      return;
    }

    if (message.role === 'event') {
      cells.push({
        kind: 'boundary',
        id: messageId(message, `boundary-${index}`),
        title: message.eventTitle || message.eventType || 'Session event',
        summary: message.content || message.eventReason || message.eventNextStep,
        index,
      });
    }
  });

  flushRun();
  return cells;
}

export function buildThreadTimeline(input: BuildThreadTimelineInput): ThreadTimelineModel {
  const cells = buildCells(input.messages);
  const pendingRunCell = buildPendingRunCell(input);
  if (pendingRunCell && !hasOpenRunCell(cells)) {
    cells.push(pendingRunCell);
  }
  const sessionIndex = input.sessionIndex && !Array.isArray(input.sessionIndex) ? input.sessionIndex : null;
  const sessionWorkbench = input.sessionWorkbench && !Array.isArray(input.sessionWorkbench) ? input.sessionWorkbench : null;
  const contextWindow = getContextWindowProjection(sessionWorkbench);
  const runtimeSummary = input.runtimeSummary || null;
  const headerStatus = getHeaderStatus(cells, input.isWaiting, input.isStreaming, input.activeRunStatus);

  return {
    cells,
    header: {
      sessionId: getSessionId(input.activeSession),
      title: getSessionTitle(input.activeSession),
      status: headerStatus,
      modelLabel: runtimeSummary?.model?.label || runtimeSummary?.model?.name || null,
      providerLabel: runtimeSummary?.model?.provider || null,
      resumeHealth: getResumeHealth(sessionIndex),
      checkpointCount: sessionIndex?.checkpoints?.length ?? 0,
      branchDepth: getBranchDepth(input.activeSession, input.branchLineage),
      compactionCount: runtimeSummary?.compaction_count ?? 0,
      contextWindowStatusLabel: contextWindow.label,
      contextWindowTitle: contextWindow.title,
      activeRunStatus: input.activeRunStatus || null,
    },
    inspector: {
      sessionEventCount: typeof sessionIndex?.event_count === 'number' ? sessionIndex.event_count : null,
      t0SegmentCount: sessionIndex?.t0_segments?.length ?? 0,
      latestCheckpointLabel: getLatestCheckpointLabel(sessionIndex),
      usedToolCount: runtimeSummary?.used_tools?.length ?? 0,
      blockedCapabilityCount: runtimeSummary?.blocked_capabilities?.length ?? 0,
      activatedToolGroupCount: runtimeSummary?.activated_tool_groups?.length ?? 0,
    },
  };
}

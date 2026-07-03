import type { SessionIndex } from '../../api/domains/chat';
import type { SessionWorkbench } from '../../api/domains/ccParity';
import {
  buildRunTimelineFromMessages,
  isDisclosureStepMessage,
  type RunTimelineSnapshot,
} from '../agent-detail/chatDisclosureReducer';
import type { AgentChatMessage, ChatArtifactPart, ChatRuntimeSummary } from '../agent-detail/chatRuntime';

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
  permissionMode: string | null;
  governanceLabel: string | null;
  activeProjection: string | null;
  checkpointCount: number;
  branchDepth: number;
  compactionCount: number;
  contextWindowStatusLabel: string | null;
  contextWindowTitle: string | null;
  activeRunStatus: string | null;
}


function getHeaderPermissionMode(
  activeSession: Record<string, unknown> | null | undefined,
  workbench: unknown,
): string | null {
  const record = (workbench && typeof workbench === 'object' ? workbench : {}) as Record<string, unknown>;
  const profile = record.permission_profile as Record<string, unknown> | null | undefined;
  const mode =
    (profile && typeof profile.mode === 'string' && profile.mode) ||
    (typeof record.permission_mode === 'string' && record.permission_mode) ||
    (activeSession && typeof activeSession.permission_mode === 'string' && activeSession.permission_mode) ||
    null;
  return mode ? String(mode) : null;
}

function getHeaderGovernanceLabel(workbench: unknown): string | null {
  const record = (workbench && typeof workbench === 'object' ? workbench : {}) as Record<string, unknown>;
  const profile = record.permission_profile as Record<string, unknown> | null | undefined;
  if (!profile) return null;
  const allowed = Array.isArray(profile.allowed_tools) ? profile.allowed_tools.length : null;
  const roots = Array.isArray(profile.writable_roots) ? profile.writable_roots.length : null;
  if (allowed == null && roots == null) return null;
  const parts: string[] = [];
  if (allowed != null) parts.push(`${allowed} tools`);
  if (roots != null) parts.push(`${roots} roots`);
  return parts.join(' · ') || null;
}

function projectionLabel(value: unknown): string | null {
  if (!value) return null;
  if (typeof value === 'string') return value.trim() || null;
  if (typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const reason = record.projection_reason ?? record.command ?? record.status ?? record.kind ?? record.type;
  return typeof reason === 'string' && reason.trim() ? reason.trim() : null;
}

function getHeaderActiveProjection(
  runtimeSummary: ChatRuntimeSummary | null | undefined,
  sessionIndex?: SessionIndex | null,
): string | null {
  const summary = runtimeSummary as unknown as Record<string, unknown> | null | undefined;
  const compaction = summary?.compaction as Record<string, unknown> | null | undefined;
  const projection =
    projectionLabel(sessionIndex?.active_projection) ||
    projectionLabel(compaction?.active_projection) ||
    projectionLabel(summary?.active_projection) ||
    null;
  return projection;
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

export type SessionWindowKind = 'main' | 'team_member' | 'subagent' | 'workflow' | 'background';
export type SessionWindowStatus = 'running' | 'waiting' | 'blocked' | 'completed' | 'failed' | 'cancelled' | 'idle' | 'unknown';
export type SessionWindowTone = 'neutral' | 'running' | 'waiting' | 'blocked' | 'completed' | 'failed';

export interface RuntimeMetricModel {
  elapsedSeconds: number | null;
  elapsedLabel: string | null;
  tokenCount: number | null;
  tokenLabel: string | null;
  toolUseCount: number | null;
  toolUseLabel: string | null;
  lastActivityLabel: string | null;
}

export interface SessionWindowModel {
  id: string;
  kind: SessionWindowKind;
  label: string;
  status: SessionWindowStatus;
  selected?: boolean;
  activeTabLabel?: string;
  tabTone?: SessionWindowTone;
  accentColor?: string;
  preset?: string;
  teamId?: string;
  memberId?: string;
  sessionId?: string;
  runtimeTaskId?: string;
  elapsedSeconds?: number;
  tokenCount?: number;
  toolUseCount?: number;
  lastActivityLabel?: string;
  metrics: RuntimeMetricModel;
}

export interface CheckpointTimelineNode {
  id: string;
  sequence: number;
  label: string;
  state: 'past' | 'current_head' | 'rewound_tail' | 'new_tail' | 'branch_anchor' | 'compacted_scope';
  checkpointEventId: string;
  branchSessionIds: string[];
  compacted?: boolean;
}

export interface RuntimeSectionItemModel {
  id: string;
  label: string;
  status: string;
  state: string;
  runtimeKind: string;
  summary: string;
  childSessionId: string | null;
  enterable: boolean;
  metrics: RuntimeMetricModel;
  members: RuntimeSectionItemModel[];
  steps: RuntimeSectionItemModel[];
  leafCalls: RuntimeSectionItemModel[];
  raw: Record<string, unknown>;
}

export interface RuntimeSectionModel {
  key: RuntimeSectionName;
  title: string;
  items: RuntimeSectionItemModel[];
  count: number;
  runningCount: number;
}

export type RuntimeConsoleSegmentKey = 'team' | 'workers' | 'workflow' | 'activity';
export type RuntimeConsoleState = 'idle' | 'running' | 'waiting' | 'blocked';

export interface RuntimeConsoleGroupModel {
  items: RuntimeSectionItemModel[];
  count: number;
  runningCount: number;
  waitingCount: number;
  blockedCount: number;
}

export interface RuntimeConsoleActivityModel extends RuntimeConsoleGroupModel {
  background: RuntimeSectionItemModel[];
  notifications: RuntimeSectionItemModel[];
  runs: RuntimeSectionItemModel[];
  raw: RuntimeSectionItemModel[];
}

export interface RuntimeConsoleSegmentModel {
  key: RuntimeConsoleSegmentKey;
  label: string;
  count: number;
  runningCount: number;
  waitingCount: number;
  blockedCount: number;
}

export interface RuntimeConsoleSummaryModel {
  state: RuntimeConsoleState;
  totalCount: number;
  runningCount: number;
  waitingCount: number;
  blockedCount: number;
  elapsedLabel: string | null;
  tokenLabel: string | null;
  toolUseLabel: string | null;
}

export interface RuntimeConsoleModel {
  defaultSegment: RuntimeConsoleSegmentKey;
  summary: RuntimeConsoleSummaryModel;
  segments: RuntimeConsoleSegmentModel[];
  team: RuntimeConsoleGroupModel;
  workers: RuntimeConsoleGroupModel;
  workflow: RuntimeConsoleGroupModel;
  activity: RuntimeConsoleActivityModel;
}

export type WorkspaceDocumentGroupKey = 'currentSession' | 'historical' | 'unattributed';

export interface WorkspaceDocumentModel {
  key: string;
  name: string;
  path: string;
  previewKind?: string;
  size?: number;
  source?: string;
  group: WorkspaceDocumentGroupKey;
  status: 'current' | 'snapshot' | 'historical' | 'unattributed';
  artifact: ChatArtifactPart;
  /** How many artifact deliveries collapsed into this row (path-keyed). */
  revisions: number;
}

export interface WorkspaceDocumentGroupModel {
  key: WorkspaceDocumentGroupKey;
  title: string;
  collapsedByDefault: boolean;
  items: WorkspaceDocumentModel[];
}

export interface WorkspaceDocumentsModel {
  total: number;
  currentSession: WorkspaceDocumentGroupModel;
  historical: WorkspaceDocumentGroupModel;
  unattributed: WorkspaceDocumentGroupModel;
}

export interface SessionRightPanelModel {
  workspaceDocuments: WorkspaceDocumentsModel;
  runtimeSections: RuntimeSectionsModel;
  runtimeTables: RuntimeSectionModel[];
  runtimeConsole: RuntimeConsoleModel;
  runtimeMetrics: RuntimeMetricModel & {
    runningCount: number;
    totalCount: number;
  };
  context: Record<string, unknown> | null;
  governance: Record<string, unknown> | null;
  commands: Record<string, unknown> | null;
  runs: RuntimeSectionModel;
  team: RuntimeSectionModel;
  subagents: RuntimeSectionModel;
  workflow: RuntimeSectionModel;
  artifacts: WorkspaceDocumentsModel;
  raw: RuntimeSectionModel;
  sessionWindow: SessionWindowModel | null;
}

export interface WorkflowRunWindowModel {
  id: string;
  label: string;
  status: string;
  breadcrumb: string;
  meta: string;
  childSessionId: string | null;
  metrics: RuntimeMetricModel;
  controls: WorkflowRunControlsModel;
  steps: RuntimeSectionItemModel[];
  leafCalls: RuntimeSectionItemModel[];
  raw: RuntimeSectionItemModel;
}

export interface WorkflowRunActionModel {
  action: 'resume' | 'repair' | 'cancel' | 'promote';
  enabled: boolean;
  runId: string;
  previewId: string | null;
  proposalId: string | null;
  candidateId: string | null;
  reason: string;
}

export interface WorkflowRunControlsModel {
  gateStatus: string;
  waitStatus: string;
  repairable: boolean;
  promotionEligible: boolean;
  actions: WorkflowRunActionModel[];
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
    running: number;
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

function readNumber(source: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function formatElapsedLabel(seconds: number | null): string | null {
  if (seconds === null || seconds < 0) return null;
  const rounded = Math.round(seconds);
  const minutes = Math.floor(rounded / 60);
  const remainingSeconds = rounded % 60;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours > 0) return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
  if (minutes > 0) return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  return `${remainingSeconds}s`;
}

function readRuntimeMetrics(item: Record<string, unknown>): RuntimeMetricModel {
  const existing = asRecord(item.metrics);
  if (existing) {
    const elapsedSeconds = readNumber(existing, ['elapsedSeconds', 'elapsed_seconds']);
    const tokenCount = readNumber(existing, ['tokenCount', 'token_count']);
    const toolUseCount = readNumber(existing, ['toolUseCount', 'tool_use_count']);
    return {
      elapsedSeconds,
      elapsedLabel: readString(existing, ['elapsedLabel', 'elapsed_label'], '') || formatElapsedLabel(elapsedSeconds),
      tokenCount,
      tokenLabel: readString(existing, ['tokenLabel', 'token_label'], '') || compactTokenCount(tokenCount),
      toolUseCount,
      toolUseLabel: readString(existing, ['toolUseLabel', 'tool_use_label'], '') || (toolUseCount === null ? null : String(toolUseCount)),
      lastActivityLabel: readString(existing, ['lastActivityLabel', 'last_activity_label'], '') || null,
    };
  }
  const usage = asRecord(item.usage);
  const elapsedSeconds = readNumber(item, ['elapsed_seconds', 'elapsedSeconds', 'duration_seconds', 'durationSeconds']);
  const tokenCount =
    readNumber(item, ['token_count', 'tokenCount', 'total_tokens', 'totalTokens', 'tokens'])
    ?? (usage ? readNumber(usage, ['total_tokens', 'totalTokens', 'tokens']) : null);
  const explicitToolCount = readNumber(item, ['tool_use_count', 'toolUseCount', 'tool_count', 'toolCount', 'tools_count', 'toolsCount']);
  const toolUseCount = explicitToolCount ?? (Array.isArray(item.used_tools) ? item.used_tools.length : null);
  const lastActivityLabel = readString(item, ['last_activity_label', 'lastActivityLabel', 'last_activity_at', 'updated_at'], '') || null;
  return {
    elapsedSeconds,
    elapsedLabel: formatElapsedLabel(elapsedSeconds),
    tokenCount,
    tokenLabel: compactTokenCount(tokenCount),
    toolUseCount,
    toolUseLabel: toolUseCount === null ? null : String(toolUseCount),
    lastActivityLabel,
  };
}

function normalizeWorkflowControls(item: Record<string, unknown>): WorkflowRunControlsModel {
  const controls = asRecord(item.workflow_controls) ?? asRecord(item.workflowControls) ?? {};
  const actionRows = Array.isArray(controls.actions) ? controls.actions : [];
  const actions = actionRows
    .map((action): WorkflowRunActionModel | null => {
      const record = asRecord(action);
      if (!record) return null;
      const actionName = readString(record, ['action'], '') as WorkflowRunActionModel['action'];
      if (!['resume', 'repair', 'cancel', 'promote'].includes(actionName)) return null;
      return {
        action: actionName,
        enabled: Boolean(record.enabled),
        runId: readString(record, ['run_id', 'runId'], ''),
        previewId: readString(record, ['preview_id', 'previewId'], '') || null,
        proposalId: readString(record, ['proposal_id', 'proposalId'], '') || null,
        candidateId: readString(record, ['candidate_id', 'candidateId'], '') || null,
        reason: readString(record, ['reason'], ''),
      };
    })
    .filter((action): action is WorkflowRunActionModel => Boolean(action));
  return {
    gateStatus: readString(controls, ['gate_status', 'gateStatus'], 'clear'),
    waitStatus: readString(controls, ['wait_status', 'waitStatus'], 'not_waiting'),
    repairable: Boolean(controls.repairable),
    promotionEligible: Boolean(controls.promotion_eligible ?? controls.promotionEligible),
    actions,
    raw: controls,
  };
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
  const id = readString(item, ['id', 'chat_session_id', 'child_session_id', 'child_session', 'runtime_task_id', 'workflow_run_id'], `${fallbackKind}-${index}`);
  const label = readString(item, ['label', 'name', 'title', 'role', 'runtime_task_id'], id);
  const status = readString(item, ['status', 'state'], 'unknown');
  const state = readString(item, ['state', 'status'], status);
  const childSessionId = readString(item, ['child_session_id', 'child_session', 'childSessionId', 'chat_session_id', 'chatSessionId'], '') || null;
  const explicitEnterable = readBoolean(item, 'enterable');
  const isSubagent = runtimeKind.toLowerCase() === 'subagent';
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
    enterable: isSubagent ? false : (explicitEnterable ?? Boolean(childSessionId)),
    metrics: readRuntimeMetrics(item),
    members,
    steps,
    leafCalls,
    raw: item,
  };
}

function flattenRuntimeItems(items: RuntimeSectionItemModel[]): RuntimeSectionItemModel[] {
  return items.flatMap((item) => [item, ...flattenRuntimeItems(item.members), ...flattenRuntimeItems(item.steps), ...flattenRuntimeItems(item.leafCalls)]);
}

function runtimeMetricItems(sections: Omit<RuntimeSectionsModel, 'summary'>): RuntimeSectionItemModel[] {
  return [
    ...sections.agentTeams.flatMap((team) => [team, ...team.members]),
    ...sections.subagents,
    ...sections.workflows,
    ...sections.background,
    ...sections.notifications,
    ...sections.runs,
    ...sections.raw,
  ];
}

function isRunningRuntimeItem(item: RuntimeSectionItemModel): boolean {
  return String(item.status || item.state || '').toLowerCase() === 'running';
}

function runtimeStatusText(item: RuntimeSectionItemModel): string {
  return String(item.status || item.state || '').trim().toLowerCase();
}

function isWaitingRuntimeItem(item: RuntimeSectionItemModel): boolean {
  const status = runtimeStatusText(item);
  return status === 'waiting' || status === 'queued' || status === 'scheduled' || status === 'gate_waiting';
}

function isBlockedRuntimeItem(item: RuntimeSectionItemModel): boolean {
  const status = runtimeStatusText(item);
  return status === 'blocked' || status === 'stalled' || status === 'needs_reconciliation' || status === 'suspended';
}

function hasActiveRuntimeItem(items: RuntimeSectionItemModel[]): boolean {
  return flattenRuntimeItems(items).some((item) => (
    isRunningRuntimeItem(item) || isWaitingRuntimeItem(item) || isBlockedRuntimeItem(item)
  ));
}

function runtimeConsoleGroup(items: RuntimeSectionItemModel[]): RuntimeConsoleGroupModel {
  const flattenedItems = flattenRuntimeItems(items);
  return {
    items,
    count: items.length,
    runningCount: flattenedItems.filter(isRunningRuntimeItem).length,
    waitingCount: flattenedItems.filter(isWaitingRuntimeItem).length,
    blockedCount: flattenedItems.filter(isBlockedRuntimeItem).length,
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
  const metricItems = runtimeMetricItems(sections);
  const summary = {
    agentTeams: sections.agentTeams.length,
    subagents: sections.subagents.length,
    workflows: sections.workflows.length,
    background: sections.background.length,
    notifications: sections.notifications.length,
    runs: sections.runs.length,
    raw: sections.raw.length,
    running: metricItems.filter(isRunningRuntimeItem).length,
  };
  return {
    ...sections,
    summary: {
      ...summary,
      total: summary.agentTeams + summary.subagents + summary.workflows + summary.background + summary.notifications + summary.runs + summary.raw,
    },
  };
}

const RUNTIME_SECTION_TITLES: Record<RuntimeSectionName, string> = {
  agent_teams: 'Agent Teams',
  subagents: 'Sub-agents',
  workflows: 'Dynamic Workflow',
  background: 'Background agents',
  notifications: 'Notifications',
  runs: 'Runs',
  raw: 'Raw',
};

function runtimeSectionModel(key: RuntimeSectionName, items: RuntimeSectionItemModel[]): RuntimeSectionModel {
  return {
    key,
    title: RUNTIME_SECTION_TITLES[key],
    items,
    count: items.length,
    runningCount: flattenRuntimeItems(items).filter(isRunningRuntimeItem).length,
  };
}

function buildRuntimeSectionModels(runtimeSections: RuntimeSectionsModel): RuntimeSectionModel[] {
  return [
    runtimeSectionModel('agent_teams', runtimeSections.agentTeams),
    runtimeSectionModel('subagents', runtimeSections.subagents),
    runtimeSectionModel('workflows', runtimeSections.workflows),
    runtimeSectionModel('background', runtimeSections.background),
    runtimeSectionModel('notifications', runtimeSections.notifications),
    runtimeSectionModel('runs', runtimeSections.runs),
    runtimeSectionModel('raw', runtimeSections.raw),
  ];
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

function workspaceDocumentGroup(
  artifact: ChatArtifactPart,
  sessionRunTaskIds?: Set<string>,
): WorkspaceDocumentGroupKey {
  const source = String(artifact.source || '').toLowerCase();
  if (source.includes('historical')) return 'historical';
  // Session deliverables are what THIS session's runs delivered. An artifact
  // stamped with a runtime task that does not belong to this session (another
  // workflow/task touching the shared workspace) is history, not a deliverable.
  if (
    artifact.runtimeTaskId
    && sessionRunTaskIds
    && sessionRunTaskIds.size > 0
    && !sessionRunTaskIds.has(String(artifact.runtimeTaskId))
  ) {
    return 'historical';
  }
  if (
    artifact.runtimeTaskId
    || artifact.snapshotHash
    || artifact.snapshotStoragePath
    || source.includes('terminal')
    || source.includes('artifact_delivery')
  ) {
    return 'currentSession';
  }
  return 'unattributed';
}

function workspaceDocumentStatus(artifact: ChatArtifactPart, group: WorkspaceDocumentGroupKey): WorkspaceDocumentModel['status'] {
  if (group === 'historical') return 'historical';
  if (group === 'unattributed') return 'unattributed';
  if (artifact.snapshotHash || artifact.snapshotStoragePath) return 'snapshot';
  return 'current';
}

export function buildWorkspaceDocumentsModel(
  messages: AgentChatMessage[],
  sessionRunTaskIds?: Set<string>,
): WorkspaceDocumentsModel {
  // Path-keyed (Codex Workspace Documents semantics): one file = one row.
  // Repeated deliveries of the same path collapse into the latest artifact
  // with a revision count, instead of listing the file once per delivery.
  const docs = new Map<string, WorkspaceDocumentModel>();
  messages.forEach((message) => {
    (message.artifacts || []).forEach((artifact) => {
      if (!artifact.path) return;
      const group = workspaceDocumentGroup(artifact, sessionRunTaskIds);
      const key = `${group}:${artifact.path}`;
      const existing = docs.get(key);
      if (existing) {
        docs.set(key, {
          ...existing,
          name: artifact.name || existing.name,
          previewKind: artifact.previewKind ?? existing.previewKind,
          size: artifact.size ?? existing.size,
          source: artifact.source ?? existing.source,
          status: workspaceDocumentStatus(artifact, group),
          artifact,
          revisions: existing.revisions + 1,
        });
        return;
      }
      docs.set(key, {
        key,
        name: artifact.name || artifact.path.split('/').pop() || artifact.path,
        path: artifact.path,
        previewKind: artifact.previewKind,
        size: artifact.size,
        source: artifact.source,
        group,
        status: workspaceDocumentStatus(artifact, group),
        artifact,
        revisions: 1,
      });
    });
  });
  const values = Array.from(docs.values()).slice(0, 18);
  const currentSession = values.filter((doc) => doc.group === 'currentSession');
  const historical = values.filter((doc) => doc.group === 'historical');
  const unattributed = values.filter((doc) => doc.group === 'unattributed');
  return {
    total: values.length,
    currentSession: { key: 'currentSession', title: 'Current session', collapsedByDefault: false, items: currentSession },
    historical: { key: 'historical', title: 'Historical', collapsedByDefault: true, items: historical },
    unattributed: { key: 'unattributed', title: 'Unattributed', collapsedByDefault: true, items: unattributed },
  };
}

function aggregateRuntimeMetrics(runtimeSections: RuntimeSectionsModel): RuntimeMetricModel & { runningCount: number; totalCount: number } {
  const allItems = runtimeMetricItems(runtimeSections);
  const elapsedValues = allItems.map((item) => item.metrics.elapsedSeconds).filter((value): value is number => value !== null);
  const tokenValues = allItems.map((item) => item.metrics.tokenCount).filter((value): value is number => value !== null);
  const toolValues = allItems.map((item) => item.metrics.toolUseCount).filter((value): value is number => value !== null);
  const elapsedSeconds = elapsedValues.length ? Math.max(...elapsedValues) : null;
  const tokenCount = tokenValues.length ? tokenValues.reduce((sum, value) => sum + value, 0) : null;
  const toolUseCount = toolValues.length ? toolValues.reduce((sum, value) => sum + value, 0) : null;
  const lastActivityLabel = allItems.find((item) => item.metrics.lastActivityLabel)?.metrics.lastActivityLabel ?? null;
  return {
    elapsedSeconds,
    elapsedLabel: formatElapsedLabel(elapsedSeconds),
    tokenCount,
    tokenLabel: compactTokenCount(tokenCount),
    toolUseCount,
    toolUseLabel: toolUseCount === null ? null : String(toolUseCount),
    lastActivityLabel,
    runningCount: allItems.filter(isRunningRuntimeItem).length,
    totalCount: allItems.length,
  };
}

function runtimeConsoleState({
  runningCount,
  waitingCount,
  blockedCount,
}: {
  runningCount: number;
  waitingCount: number;
  blockedCount: number;
}): RuntimeConsoleState {
  if (blockedCount > 0) return 'blocked';
  if (waitingCount > 0) return 'waiting';
  if (runningCount > 0) return 'running';
  return 'idle';
}

function buildRuntimeConsoleModel(
  runtimeSections: RuntimeSectionsModel,
  metrics: RuntimeMetricModel & { runningCount: number; totalCount: number },
): RuntimeConsoleModel {
  const team = runtimeConsoleGroup(runtimeSections.agentTeams);
  const workers = runtimeConsoleGroup(runtimeSections.subagents);
  const workflow = runtimeConsoleGroup(runtimeSections.workflows);
  const activityItems = [
    ...runtimeSections.background,
    ...runtimeSections.notifications,
    ...runtimeSections.runs,
    ...runtimeSections.raw,
  ];
  const activityBase = runtimeConsoleGroup(activityItems);
  const activity: RuntimeConsoleActivityModel = {
    ...activityBase,
    background: runtimeSections.background,
    notifications: runtimeSections.notifications,
    runs: runtimeSections.runs,
    raw: runtimeSections.raw,
  };

  const operationalItems = [
    ...runtimeSections.agentTeams.flatMap((item) => [item, ...item.members]),
    ...runtimeSections.subagents,
    ...runtimeSections.workflows,
    ...runtimeSections.background,
    ...runtimeSections.runs,
  ];
  const runningCount = operationalItems.filter(isRunningRuntimeItem).length;
  const waitingCount = operationalItems.filter(isWaitingRuntimeItem).length;
  const blockedCount = operationalItems.filter(isBlockedRuntimeItem).length;
  const defaultSegment: RuntimeConsoleSegmentKey = hasActiveRuntimeItem(runtimeSections.workflows)
    ? 'workflow'
    : hasActiveRuntimeItem(runtimeSections.agentTeams)
      ? 'team'
      : hasActiveRuntimeItem(runtimeSections.subagents)
        ? 'workers'
        : hasActiveRuntimeItem([...runtimeSections.background, ...runtimeSections.runs])
          ? 'activity'
          : team.count > 0
            ? 'team'
            : workers.count > 0
              ? 'workers'
              : workflow.count > 0
                ? 'workflow'
                : 'activity';

  const segmentModel = (
    key: RuntimeConsoleSegmentKey,
    label: string,
    group: RuntimeConsoleGroupModel,
  ): RuntimeConsoleSegmentModel => ({
    key,
    label,
    count: group.count,
    runningCount: group.runningCount,
    waitingCount: group.waitingCount,
    blockedCount: group.blockedCount,
  });
  const segments: RuntimeConsoleSegmentModel[] = [
    segmentModel('team', 'Team', team),
    segmentModel('workers', 'Workers', workers),
    segmentModel('workflow', 'Workflow', workflow),
    segmentModel('activity', 'Activity', activityBase),
  ];

  return {
    defaultSegment,
    summary: {
      state: runtimeConsoleState({ runningCount, waitingCount, blockedCount }),
      totalCount: team.count + workers.count + workflow.count + activity.count,
      runningCount,
      waitingCount,
      blockedCount,
      elapsedLabel: metrics.elapsedLabel,
      tokenLabel: metrics.tokenLabel,
      toolUseLabel: metrics.toolUseLabel,
    },
    segments,
    team,
    workers,
    workflow,
    activity,
  };
}

function normalizeSessionWindowStatus(status: unknown): SessionWindowStatus {
  const value = String(status || '').toLowerCase();
  if (value === 'running' || value === 'waiting' || value === 'blocked' || value === 'completed' || value === 'failed' || value === 'cancelled') {
    return value;
  }
  if (value === 'canceled') return 'cancelled';
  if (value === 'idle' || value === '') return 'idle';
  return 'unknown';
}

function sessionWindowTone(status: SessionWindowStatus): SessionWindowTone {
  if (status === 'running') return 'running';
  if (status === 'waiting') return 'waiting';
  if (status === 'blocked') return 'blocked';
  if (status === 'completed') return 'completed';
  if (status === 'failed' || status === 'cancelled') return 'failed';
  return 'neutral';
}

export function buildSessionWindowModel(
  session?: Record<string, unknown> | null,
  activeRunStatus?: string | null,
): SessionWindowModel | null {
  if (!session) return null;
  const metadata = asRecord(session.transcript_metadata_json) ?? asRecord(session.metadata_json) ?? asRecord(session.metadata) ?? {};
  const sourceChannel = readString(session, ['source_channel', 'runtime_source', 'session_kind'], '').toLowerCase();
  const kind: SessionWindowKind = sourceChannel.includes('team_member') || (metadata.team_id && metadata.member_name)
    ? 'team_member'
    : sourceChannel.includes('subagent')
      ? 'subagent'
      : sourceChannel.includes('workflow')
        ? 'workflow'
        : sourceChannel.includes('background')
          ? 'background'
          : 'main';
  const id = readString(session, ['id', 'chat_session_id', 'session_id'], 'session');
  const memberName = readString(metadata, ['member_name', 'name'], '') || readString(session, ['member_name'], '');
  const label = memberName || readString(session, ['title', 'name'], id).split('/').pop()?.trim() || id;
  const status = normalizeSessionWindowStatus(activeRunStatus || session.status);
  const metrics = readRuntimeMetrics({ ...session, ...metadata });
  return {
    id,
    kind,
    label,
    status,
    selected: true,
    activeTabLabel: kind === 'team_member' ? `Agent: ${label}` : label,
    tabTone: sessionWindowTone(status),
    teamId: readString(metadata, ['team_id'], '') || undefined,
    memberId: readString(metadata, ['member_id'], '') || readString(session, ['member_id'], '') || undefined,
    sessionId: id,
    runtimeTaskId: readString(metadata, ['runtime_task_id'], '') || readString(session, ['runtime_task_id'], '') || undefined,
    elapsedSeconds: metrics.elapsedSeconds ?? undefined,
    tokenCount: metrics.tokenCount ?? undefined,
    toolUseCount: metrics.toolUseCount ?? undefined,
    lastActivityLabel: metrics.lastActivityLabel ?? undefined,
    metrics,
  };
}

export function buildCheckpointTimelineNodes(
  sessionIndex?: SessionIndex | null,
  branchLineage?: Array<{ id: unknown; parent_session_id?: unknown }> | null,
): CheckpointTimelineNode[] {
  const checkpoints = Array.isArray(sessionIndex?.checkpoints) ? sessionIndex.checkpoints : [];
  let currentHeadIndex = -1;
  checkpoints.forEach((checkpoint, index) => {
    const record = checkpoint as Record<string, unknown>;
    if (!record.compacted && !record.compacted_scope) currentHeadIndex = index;
  });
  const branchIds = (branchLineage || [])
    .filter((row) => row.parent_session_id)
    .map((row) => String(row.id));
  return checkpoints.map((checkpoint, index) => {
    const record = checkpoint as Record<string, unknown>;
    const checkpointEventId = readString(record, ['checkpoint_event_id', 'event_id', 'id'], `checkpoint-${index + 1}`);
    const compacted = Boolean(record.compacted || record.compacted_scope);
    return {
      id: checkpointEventId,
      sequence: readNumber(record, ['sequence', 'turn_index']) ?? index + 1,
      label: readString(record, ['label', 'checkpoint_kind', 'kind', 'role', 'id'], checkpointEventId),
      state: compacted ? 'compacted_scope' : (index === currentHeadIndex ? 'current_head' : 'past'),
      checkpointEventId,
      branchSessionIds: index === 0 ? branchIds : [],
      compacted: compacted || undefined,
    };
  });
}

export function buildWorkflowRunWindowModel(workflow: RuntimeSectionItemModel): WorkflowRunWindowModel {
  const workflowControlSource = { ...workflow.raw, ...(workflow as unknown as Record<string, unknown>) };
  return {
    id: workflow.id,
    label: workflow.label || workflow.id,
    status: workflow.status || 'unknown',
    breadcrumb: 'Main > Dynamic Workflow',
    meta: [workflow.id, workflow.runtimeKind, workflow.summary, workflow.childSessionId ? `session:${workflow.childSessionId}` : '']
      .filter(Boolean)
      .join(' · '),
    childSessionId: workflow.childSessionId,
    metrics: workflow.metrics,
    controls: normalizeWorkflowControls(workflowControlSource),
    steps: workflow.steps,
    leafCalls: workflow.leafCalls,
    raw: workflow,
  };
}

export function buildSessionRightPanelModel({
  messages,
  sessionWorkbench,
  activeSession,
  activeRunStatus,
}: {
  messages: AgentChatMessage[];
  sessionWorkbench?: SessionWorkbench | Record<string, unknown> | null;
  activeSession?: Record<string, unknown> | null;
  activeRunStatus?: string | null;
}): SessionRightPanelModel {
  const workbenchRecord = asRecord(sessionWorkbench) ?? null;
  const runtimeSections = buildRuntimeSectionsModel(workbenchRecord);
  const runtimeTables = buildRuntimeSectionModels(runtimeSections);
  const sectionByKey = new Map(runtimeTables.map((section) => [section.key, section]));
  const fallbackSection = (key: RuntimeSectionName) => runtimeSectionModel(key, []);
  const sessionRunTaskIds = new Set<string>(
    (runtimeSections.runs || [])
      .map((item) => String((item.raw as Record<string, unknown> | undefined)?.runtime_task_id ?? item.id ?? ''))
      .filter((value): value is string => Boolean(value)),
  );
  const workspaceDocuments = buildWorkspaceDocumentsModel(messages, sessionRunTaskIds);
  const runtimeMetrics = aggregateRuntimeMetrics(runtimeSections);
  return {
    workspaceDocuments,
    runtimeSections,
    runtimeTables,
    runtimeConsole: buildRuntimeConsoleModel(runtimeSections, runtimeMetrics),
    runtimeMetrics,
    context: asRecord(workbenchRecord?.context_policy),
    governance: asRecord(workbenchRecord?.permission_profile),
    commands: asRecord(workbenchRecord?.controls),
    runs: sectionByKey.get('runs') ?? fallbackSection('runs'),
    team: sectionByKey.get('agent_teams') ?? fallbackSection('agent_teams'),
    subagents: sectionByKey.get('subagents') ?? fallbackSection('subagents'),
    workflow: sectionByKey.get('workflows') ?? fallbackSection('workflows'),
    artifacts: workspaceDocuments,
    raw: sectionByKey.get('raw') ?? fallbackSection('raw'),
    sessionWindow: buildSessionWindowModel(activeSession ?? asRecord(workbenchRecord?.session), activeRunStatus),
  };
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
  if (typeof status === 'string' && status.trim()) return status.trim();
  const truthSurface = typeof health.truth_surface === 'string' && health.truth_surface.trim()
    ? health.truth_surface.trim()
    : null;
  const hasT0Truth = health.has_t0_truth === true;
  const hasCheckpoints = health.has_checkpoints === true;
  if (hasT0Truth && hasCheckpoints) return `${truthSurface || 'T0 truth'} + checkpoints`;
  if (hasT0Truth) return truthSurface || 'T0 truth';
  if (hasCheckpoints) return 'checkpoints only';
  return truthSurface ? `${truthSurface} unavailable` : 'unknown';
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
      permissionMode: getHeaderPermissionMode(input.activeSession, input.sessionWorkbench),
      governanceLabel: getHeaderGovernanceLabel(input.sessionWorkbench),
      activeProjection: getHeaderActiveProjection(input.runtimeSummary, sessionIndex),
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

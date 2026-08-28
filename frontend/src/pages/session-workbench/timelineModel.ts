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
      /** Live RuntimePhase for the open run (§3 seam 3); settled runs carry none. */
      phase?: string;
    }
  | {
      kind: 'boundary';
      id: string;
      title: string;
      summary?: string;
      message: AgentChatMessage;
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

export interface ThreadTimelineCache {
  previousInputSignature: string | null;
  previousMessages: AgentChatMessage[] | null;
  previousModel: ThreadTimelineModel | null;
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
  /** First-class RuntimePhase for the active turn (backend contract, §3.3). */
  runtimePhase?: string;
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
  | 'peer_a2a'
  | 'subagents'
  | 'workflows'
  | 'background'
  | 'notifications'
  | 'runs'
  | 'raw';

export type SessionWindowKind = 'main' | 'team_member' | 'peer_a2a' | 'subagent' | 'workflow' | 'background';
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
  userBlocker?: RuntimeUserBlockerModel | null;
  raw: Record<string, unknown>;
}

export interface RuntimeUserBlockerModel {
  kind: string;
  status: string;
  title: string;
  reason: string;
  nextAction: string;
  owner: string;
  canContinueOtherWork: boolean;
  autoResume: boolean;
}

export interface RuntimeSectionModel {
  key: RuntimeSectionName;
  title: string;
  items: RuntimeSectionItemModel[];
  count: number;
  runningCount: number;
}

export type RuntimeConsoleSegmentKey = 'team' | 'a2a' | 'workers' | 'workflow' | 'activity';
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

export interface RuntimeConsoleWaiterModel {
  id: string;
  label: string;
  status: string;
  segment: RuntimeConsoleSegmentKey;
  runtimeKind: string;
  summary: string;
  childSessionId: string | null;
  enterable: boolean;
  userBlocker: RuntimeUserBlockerModel | null;
}

export interface RuntimeConsoleModel {
  defaultSegment: RuntimeConsoleSegmentKey;
  summary: RuntimeConsoleSummaryModel;
  segments: RuntimeConsoleSegmentModel[];
  waiters: RuntimeConsoleWaiterModel[];
  team: RuntimeConsoleGroupModel;
  peerA2A: RuntimeConsoleGroupModel;
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
  peerA2A: RuntimeSectionModel;
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
  action: 'approve_gate' | 'reject_gate' | 'repair' | 'cancel' | 'promote';
  enabled: boolean;
  runId: string;
  stepId: string | null;
  previewId: string | null;
  proposalId: string | null;
  candidateId: string | null;
  reason: string;
}

export interface WorkflowRunControlsModel {
  gateStatus: string;
  waitStatus: string;
  repairable: boolean;
  actions: WorkflowRunActionModel[];
  raw: Record<string, unknown>;
}

export interface RuntimeSectionsModel {
  agentTeams: RuntimeSectionItemModel[];
  peerA2A: RuntimeSectionItemModel[];
  subagents: RuntimeSectionItemModel[];
  workflows: RuntimeSectionItemModel[];
  background: RuntimeSectionItemModel[];
  notifications: RuntimeSectionItemModel[];
  runs: RuntimeSectionItemModel[];
  raw: RuntimeSectionItemModel[];
  summary: {
    total: number;
    agentTeams: number;
    peerA2A: number;
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
      if (!['approve_gate', 'reject_gate', 'repair', 'cancel', 'promote'].includes(actionName)) return null;
      return {
        action: actionName,
        enabled: Boolean(record.enabled),
        runId: readString(record, ['run_id', 'runId'], ''),
        stepId: readString(record, ['step_id', 'stepId'], '') || null,
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
  if (Array.isArray(value)) return value;
  const envelope = asRecord(value);
  return Array.isArray(envelope?.items) ? envelope.items : [];
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
  const blocker = asRecord(item.user_blocker ?? item.userBlocker);
  const userBlocker: RuntimeUserBlockerModel | null = blocker
    ? {
      kind: readString(blocker, ['kind'], ''),
      status: readString(blocker, ['status'], ''),
      title: readString(blocker, ['title'], ''),
      reason: readString(blocker, ['reason'], ''),
      nextAction: readString(blocker, ['next_action', 'nextAction'], ''),
      owner: readString(blocker, ['owner'], ''),
      canContinueOtherWork: Boolean(blocker.can_continue_other_work ?? blocker.canContinueOtherWork),
      autoResume: Boolean(blocker.auto_resume ?? blocker.autoResume),
    }
    : null;

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
    userBlocker,
    raw: item,
  };
}

function flattenRuntimeItems(items: RuntimeSectionItemModel[]): RuntimeSectionItemModel[] {
  return items.flatMap((item) => [item, ...flattenRuntimeItems(item.members), ...flattenRuntimeItems(item.steps), ...flattenRuntimeItems(item.leafCalls)]);
}

function runtimeMetricItems(sections: Omit<RuntimeSectionsModel, 'summary'>): RuntimeSectionItemModel[] {
  return [
    ...sections.agentTeams.flatMap((team) => [team, ...team.members]),
    ...sections.peerA2A,
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

function isWaitingStatusText(status: string): boolean {
  return (
    status === 'waiting' ||
    status === 'queued' ||
    status === 'scheduled' ||
    status === 'gate_waiting' ||
    status === 'approval_required' ||
    status === 'pending_approval' ||
    status === 'requires_input' ||
    status === 'needs_user_input' ||
    status.startsWith('awaiting_')
  );
}

function isWaitingRuntimeItem(item: RuntimeSectionItemModel): boolean {
  return item.userBlocker?.status === 'waiting' || isWaitingStatusText(runtimeStatusText(item));
}

function isBlockedStatusText(status: string): boolean {
  return status === 'blocked' || status === 'stalled' || status === 'needs_reconciliation' || status === 'suspended';
}

function isBlockedRuntimeItem(item: RuntimeSectionItemModel): boolean {
  return isBlockedStatusText(runtimeStatusText(item));
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
  const peerA2A = tasks.filter((item) => ['delegation', 'a2a_delegation'].includes(String(item.raw.task_type || '').toLowerCase()));
  const subagents = tasks.filter((item) => item.runtimeKind.includes('subagent') || String(item.raw.task_type || '').toLowerCase() === 'subagent');
  const runs = tasks.filter((item) => !workflows.includes(item) && !peerA2A.includes(item) && !subagents.includes(item));
  const background = normalizeRuntimeSectionItems(Array.isArray(sessionWorkbench?.completion_wakes) ? sessionWorkbench.completion_wakes : [], 'background_agent');

  return buildRuntimeSectionsSummary({
    agentTeams: teams,
    peerA2A,
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
    peerA2A: sections.peerA2A.length,
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
      total: summary.agentTeams + summary.peerA2A + summary.subagents + summary.workflows + summary.background + summary.notifications + summary.runs + summary.raw,
    },
  };
}

function runtimeSectionFallbackFromMessage(
  message: AgentChatMessage,
  index: number,
): { section: RuntimeSectionName; record: Record<string, unknown>; fallbackKind: string } | null {
  if (message.role !== 'event') return null;
  const eventType = String(message.eventType || '').toLowerCase();
  const source = String(message.eventNotificationSource || '').toLowerCase();
  const itemType = message.threadItem?.item_type;

  let section: RuntimeSectionName | null = null;
  let runtimeKind = 'runtime_event';
  let fallbackKind = 'runtime_event';

  if (itemType === 'workflow_activity' || ['dynamic_workflow', 'workflow_run', 'workflow_step', 'workflow_started', 'workflow_completed', 'workflow_failed'].includes(eventType)) {
    section = 'workflows';
    runtimeKind = 'workflow';
    fallbackKind = 'workflow';
  } else if (itemType === 'agent_team_activity' || ['team_member', 'member_spawned', 'member_idle', 'member_run_started', 'member_message_queued', 'member_message_rejected'].includes(eventType)) {
    section = 'agent_teams';
    runtimeKind = 'team_member';
    fallbackKind = runtimeKind;
  } else if (itemType === 'peer_a2a_activity' || eventType === 'delegation_run' || source === 'a2a' || source === 'peer_a2a') {
    section = 'peer_a2a';
    runtimeKind = 'peer_a2a';
    fallbackKind = 'peer_a2a';
  } else if (eventType === 'agent_task_notification') {
    // Exact event-type routing BEFORE the generated
    // threadItem.item_type='subagent_activity' mapping can route the
    // completion notification to Workers: an agent_task_notification is a
    // completion notification (Notifications/Activity), never a worker row.
    section = 'notifications';
    runtimeKind = 'notification';
    fallbackKind = 'notification';
  } else if (itemType === 'subagent_activity' || ['subagent', 'child_session'].includes(eventType) || source === 'subagent_wake') {
    section = 'subagents';
    runtimeKind = 'subagent';
    fallbackKind = 'subagent';
  } else if (['background_agent', 'completion_wake', 'completion_wake_created', 'completion_wake_delivered'].includes(eventType) || source === 'completion_wake') {
    section = 'background';
    runtimeKind = 'background_agent';
    fallbackKind = 'background_agent';
  } else if (['runtime_action_started', 'runtime_action_progress', 'runtime_action_completed', 'runtime_action_blocked', 'runtime_action_failed'].includes(eventType) || (itemType === 'event' && message.eventRuntimeTaskId)) {
    section = 'runs';
    runtimeKind = 'runtime_task';
    fallbackKind = 'runtime_task';
  } else {
    return null;
  }

  const record = message as unknown as Record<string, unknown>;
  const identityKeys = section === 'workflows'
    ? ['eventWorkflowRunId', 'eventRuntimeTaskId', 'eventWorkflowStepId', 'id']
    : section === 'subagents' || section === 'peer_a2a'
      ? ['eventRuntimeTaskId', 'eventChildSessionId', 'id']
      : section === 'agent_teams'
        ? ['eventRuntimeTaskId', 'eventChildSessionId', 'id']
        : ['eventRuntimeTaskId', 'eventWorkflowRunId', 'eventChildSessionId', 'id'];
  const id = readString(record, identityKeys) || `session-runtime-${index}`;
  const label =
    readString(record, ['eventTitle', 'eventNotificationSource', 'eventType']) ||
    readString(record, ['content'], id);
  const status = readString(record, ['eventStatus'], 'unknown');
  const summary = readString(record, ['content', 'eventReason', 'eventNextStep'], '');
  const childSessionId = readString(record, ['eventChildSessionId'], '');

  return {
    section,
    fallbackKind,
    record: {
      id,
      runtime_kind: runtimeKind,
      label,
      status,
      state: status,
      summary,
      child_session_id: childSessionId || undefined,
      // Structured durable aliases for exact merge-key matching against
      // canonical runtime_sections rows (never content/label heuristics).
      runtime_task_id: readString(record, ['eventRuntimeTaskId'], '') || undefined,
      workflow_run_id: readString(record, ['eventWorkflowRunId'], '') || undefined,
      source_event_type: eventType,
      source_notification: source,
    },
  };
}

function runtimeSectionsFromMessages(messages: AgentChatMessage[]): RuntimeSectionsModel {
  const buckets: Record<RuntimeSectionName, Map<string, Record<string, unknown>>> = {
    agent_teams: new Map(),
    peer_a2a: new Map(),
    subagents: new Map(),
    workflows: new Map(),
    background: new Map(),
    notifications: new Map(),
    runs: new Map(),
    raw: new Map(),
  };

  messages.forEach((message, index) => {
    const fallback = runtimeSectionFallbackFromMessage(message, index);
    if (!fallback) return;
    const key = `${fallback.record.runtime_kind || fallback.fallbackKind}:${fallback.record.id}`;
    buckets[fallback.section].set(key, fallback.record);
  });

  return buildRuntimeSectionsSummary({
    agentTeams: normalizeRuntimeSectionItems(Array.from(buckets.agent_teams.values()), 'agent_team'),
    peerA2A: normalizeRuntimeSectionItems(Array.from(buckets.peer_a2a.values()), 'peer_a2a'),
    subagents: normalizeRuntimeSectionItems(Array.from(buckets.subagents.values()), 'subagent'),
    workflows: normalizeRuntimeSectionItems(Array.from(buckets.workflows.values()), 'workflow'),
    background: normalizeRuntimeSectionItems(Array.from(buckets.background.values()), 'background_agent'),
    notifications: normalizeRuntimeSectionItems(Array.from(buckets.notifications.values()), 'notification'),
    runs: normalizeRuntimeSectionItems(Array.from(buckets.runs.values()), 'runtime_task'),
    raw: normalizeRuntimeSectionItems(Array.from(buckets.raw.values()), 'raw_event'),
  });
}

function runtimeItemIdentity(item: RuntimeSectionItemModel): string {
  return `${item.runtimeKind}:${item.id}`;
}

function runtimeItemMergeKeys(item: RuntimeSectionItemModel): string[] {
  // Structured durable identities win when canonical and fallback each carry
  // one: canonical rows key by child_session_id before runtime_task_id while
  // live fallbacks key by eventRuntimeTaskId, so display/navigation ids
  // alone cannot suppress a stale row for the same task.  Two items are the
  // same row only when they share at least one structured key
  // (runtime_task_id / workflow_run_id are globally unique; the kind:id
  // display identity stays the final candidate and is never reordered).
  const keys: string[] = [];
  const raw = item.raw as Record<string, unknown> | undefined;
  const taskId = readString(raw ?? {}, ['runtime_task_id', 'runtimeTaskId'], '');
  if (taskId) keys.push(`runtime_task:${taskId}`);
  const workflowRunId = readString(raw ?? {}, ['workflow_run_id', 'workflowRunId'], '');
  if (workflowRunId) keys.push(`workflow_run:${workflowRunId}`);
  keys.push(runtimeItemIdentity(item));
  return keys;
}

function mergeRuntimeItemLists(
  primary: RuntimeSectionItemModel[],
  fallback: RuntimeSectionItemModel[],
): RuntimeSectionItemModel[] {
  const seen = new Set(primary.flatMap(runtimeItemMergeKeys));
  const merged = [...primary];
  fallback.forEach((item) => {
    const keys = runtimeItemMergeKeys(item);
    if (keys.some((key) => seen.has(key))) return;
    keys.forEach((key) => seen.add(key));
    merged.push(item);
  });
  return merged;
}

function mergeRuntimeSections(primary: RuntimeSectionsModel, fallback: RuntimeSectionsModel): RuntimeSectionsModel {
  return buildRuntimeSectionsSummary({
    agentTeams: mergeRuntimeItemLists(primary.agentTeams, fallback.agentTeams),
    peerA2A: mergeRuntimeItemLists(primary.peerA2A, fallback.peerA2A),
    subagents: mergeRuntimeItemLists(primary.subagents, fallback.subagents),
    workflows: mergeRuntimeItemLists(primary.workflows, fallback.workflows),
    background: mergeRuntimeItemLists(primary.background, fallback.background),
    notifications: mergeRuntimeItemLists(primary.notifications, fallback.notifications),
    runs: mergeRuntimeItemLists(primary.runs, fallback.runs),
    raw: mergeRuntimeItemLists(primary.raw, fallback.raw),
  });
}

const RUNTIME_SECTION_TITLES: Record<RuntimeSectionName, string> = {
  agent_teams: 'Agent Teams',
  peer_a2a: 'Peer A2A',
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
    runtimeSectionModel('peer_a2a', runtimeSections.peerA2A),
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
    peerA2A: normalizeRuntimeSectionItems(readRuntimeSectionArray(runtimeSections, 'peer_a2a'), 'peer_a2a'),
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
  messageRole?: AgentChatMessage['role'],
): WorkspaceDocumentGroupKey {
  const source = String(artifact.source || '').toLowerCase();
  if (source.includes('historical')) return 'historical';
  if (messageRole === 'tool_call' && source === 'workspace_write') {
    return 'unattributed';
  }
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
      const group = workspaceDocumentGroup(artifact, sessionRunTaskIds, message.role);
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

function runtimeWaiterFromItem(
  item: RuntimeSectionItemModel,
  segment: RuntimeConsoleSegmentKey,
): RuntimeConsoleWaiterModel | null {
  if (!isWaitingRuntimeItem(item) && !isBlockedRuntimeItem(item)) return null;
  return {
    id: item.id,
    label: item.label || item.id,
    status: item.userBlocker?.status || item.status || item.state || 'waiting',
    segment,
    runtimeKind: item.runtimeKind,
    summary: item.summary,
    childSessionId: item.childSessionId,
    enterable: item.enterable,
    userBlocker: item.userBlocker ?? null,
  };
}

function runtimeWaitersFromItem(
  item: RuntimeSectionItemModel,
  segment: RuntimeConsoleSegmentKey,
): RuntimeConsoleWaiterModel[] {
  const nestedWaiters = [
    ...item.members,
    ...item.steps,
    ...item.leafCalls,
  ].flatMap((child) => runtimeWaitersFromItem(child, segment));
  if (nestedWaiters.length > 0) return nestedWaiters;
  const waiter = runtimeWaiterFromItem(item, segment);
  return waiter ? [waiter] : [];
}

function buildRuntimeConsoleWaiters(runtimeSections: RuntimeSectionsModel): RuntimeConsoleWaiterModel[] {
  return [
    ...runtimeSections.agentTeams.flatMap((item) => runtimeWaitersFromItem(item, 'team')),
    ...runtimeSections.peerA2A.flatMap((item) => runtimeWaitersFromItem(item, 'a2a')),
    ...runtimeSections.subagents.flatMap((item) => runtimeWaitersFromItem(item, 'workers')),
    ...runtimeSections.workflows.flatMap((item) => runtimeWaitersFromItem(item, 'workflow')),
    ...[
      ...runtimeSections.background,
      ...runtimeSections.runs,
    ].flatMap((item) => runtimeWaitersFromItem(item, 'activity')),
  ];
}

function buildRuntimeConsoleModel(
  runtimeSections: RuntimeSectionsModel,
  metrics: RuntimeMetricModel & { runningCount: number; totalCount: number },
): RuntimeConsoleModel {
  const team = runtimeConsoleGroup(runtimeSections.agentTeams);
  const peerA2A = runtimeConsoleGroup(runtimeSections.peerA2A);
  const workers = runtimeConsoleGroup(runtimeSections.subagents);
  const workflow = runtimeConsoleGroup(runtimeSections.workflows);
  const activityItems = [
    ...runtimeSections.background,
    ...runtimeSections.notifications,
    ...runtimeSections.runs,
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
    ...runtimeSections.agentTeams.flatMap((item) => flattenRuntimeItems([item])),
    ...runtimeSections.peerA2A.flatMap((item) => flattenRuntimeItems([item])),
    ...runtimeSections.subagents.flatMap((item) => flattenRuntimeItems([item])),
    ...runtimeSections.workflows.flatMap((item) => flattenRuntimeItems([item])),
    ...runtimeSections.background.flatMap((item) => flattenRuntimeItems([item])),
    ...runtimeSections.runs.flatMap((item) => flattenRuntimeItems([item])),
  ];
  const runningCount = operationalItems.filter(isRunningRuntimeItem).length;
  const waiters = buildRuntimeConsoleWaiters(runtimeSections);
  const waitingCount = waiters.filter((waiter) => isWaitingStatusText(String(waiter.status || '').toLowerCase())).length;
  const blockedCount = waiters.filter((waiter) => isBlockedStatusText(String(waiter.status || '').toLowerCase())).length;
  const defaultSegment: RuntimeConsoleSegmentKey = hasActiveRuntimeItem(runtimeSections.peerA2A)
    ? 'a2a'
    : hasActiveRuntimeItem(runtimeSections.workflows)
      ? 'workflow'
      : hasActiveRuntimeItem(runtimeSections.agentTeams)
        ? 'team'
        : hasActiveRuntimeItem(runtimeSections.subagents)
          ? 'workers'
          : hasActiveRuntimeItem([...runtimeSections.background, ...runtimeSections.runs])
            ? 'activity'
            : peerA2A.count > 0
              ? 'a2a'
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
    segmentModel('a2a', 'A2A', peerA2A),
    segmentModel('workers', 'Workers', workers),
    segmentModel('workflow', 'Workflow', workflow),
    segmentModel('activity', 'Activity', activityBase),
  ];

  return {
    defaultSegment,
    summary: {
      state: runtimeConsoleState({ runningCount, waitingCount, blockedCount }),
      totalCount: team.count + peerA2A.count + workers.count + workflow.count + activity.count,
      runningCount,
      waitingCount,
      blockedCount,
      elapsedLabel: metrics.elapsedLabel,
      tokenLabel: metrics.tokenLabel,
      toolUseLabel: metrics.toolUseLabel,
    },
    segments,
    waiters,
    team,
    peerA2A,
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
  const sessionRuntimeIdentity = [session.source_channel, session.runtime_source, session.session_kind]
    .map((value) => String(value || '').toLowerCase())
    .join(' ');
  const kind: SessionWindowKind = sessionRuntimeIdentity.includes('team_member') || (metadata.team_id && metadata.member_name)
    ? 'team_member'
    : sessionRuntimeIdentity.includes('delegation_run') || sessionRuntimeIdentity.includes('a2a') || sessionRuntimeIdentity.includes('delegation')
      ? 'peer_a2a'
      : sessionRuntimeIdentity.includes('subagent')
        ? 'subagent'
        : sessionRuntimeIdentity.includes('workflow')
          ? 'workflow'
          : sessionRuntimeIdentity.includes('background')
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
    activeTabLabel: kind === 'team_member' ? `Agent: ${label}` : kind === 'peer_a2a' ? `A2A: ${label}` : label,
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
    meta: [workflow.summary, workflow.metrics.elapsedLabel]
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
  presentationStatus,
}: {
  messages: AgentChatMessage[];
  sessionWorkbench?: SessionWorkbench | Record<string, unknown> | null;
  activeSession?: Record<string, unknown> | null;
  activeRunStatus?: string | null;
  presentationStatus?: SessionWorkbenchStatus | null;
}): SessionRightPanelModel {
  const workbenchRecord = asRecord(sessionWorkbench) ?? null;
  const evidenceRuntimeSections = mergeRuntimeSections(
    buildRuntimeSectionsModel(workbenchRecord),
    runtimeSectionsFromMessages(messages),
  );
  const presentationRunStatus = presentationStatus === 'waiting'
    ? 'waiting'
    : presentationStatus === 'running' || presentationStatus === 'streaming'
      ? 'running'
      : null;
  const selectedSession = activeSession ?? asRecord(workbenchRecord?.session);
  const selectedSessionId = readString(selectedSession ?? {}, ['id', 'chat_session_id', 'session_id'], 'session');
  const durableSessionWindow = buildSessionWindowModel(selectedSession, activeRunStatus);
  // The final answer and empty active-run registry are the presentation commit.
  // Retire a lagging main-run row only in this disposable read model; `raw`
  // keeps the durable status and independently-running child sections stay live.
  const durableRuntimeSections = presentationStatus === 'complete'
    && durableSessionWindow?.kind === 'main'
    && normalizedActiveRunStatus(activeRunStatus) === null
    ? buildRuntimeSectionsSummary({
        agentTeams: evidenceRuntimeSections.agentTeams,
        peerA2A: evidenceRuntimeSections.peerA2A,
        subagents: evidenceRuntimeSections.subagents,
        workflows: evidenceRuntimeSections.workflows,
        background: evidenceRuntimeSections.background,
        notifications: evidenceRuntimeSections.notifications,
        runs: evidenceRuntimeSections.runs.map((run) => (
          isRunningRuntimeItem(run)
            ? { ...run, status: 'completed', state: 'completed' }
            : run
        )),
        raw: evidenceRuntimeSections.raw,
      })
    : evidenceRuntimeSections;
  const presentationRun: RuntimeSectionItemModel | null = presentationRunStatus
    && selectedSession
    && durableSessionWindow?.kind === 'main'
    && !hasActiveRuntimeItem(durableRuntimeSections.runs)
    ? {
        id: `${selectedSessionId}:presentation-run`,
        label: readString(selectedSession, ['title', 'name'], selectedSessionId),
        status: presentationRunStatus,
        state: presentationRunStatus,
        runtimeKind: 'session_run',
        summary: '',
        childSessionId: null,
        enterable: false,
        metrics: {
          elapsedSeconds: null,
          elapsedLabel: null,
          tokenCount: null,
          tokenLabel: null,
          toolUseCount: null,
          toolUseLabel: null,
          lastActivityLabel: null,
        },
        members: [],
        steps: [],
        leafCalls: [],
        raw: { presentation_only: true, session_id: selectedSessionId },
      }
    : null;
  const runtimeSections = presentationRun
    ? buildRuntimeSectionsSummary({
        agentTeams: durableRuntimeSections.agentTeams,
        peerA2A: durableRuntimeSections.peerA2A,
        subagents: durableRuntimeSections.subagents,
        workflows: durableRuntimeSections.workflows,
        background: durableRuntimeSections.background,
        notifications: durableRuntimeSections.notifications,
        runs: [...durableRuntimeSections.runs, presentationRun],
        raw: durableRuntimeSections.raw,
      })
    : durableRuntimeSections;
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
    peerA2A: sectionByKey.get('peer_a2a') ?? fallbackSection('peer_a2a'),
    subagents: sectionByKey.get('subagents') ?? fallbackSection('subagents'),
    workflow: sectionByKey.get('workflows') ?? fallbackSection('workflows'),
    artifacts: workspaceDocuments,
    raw: sectionByKey.get('raw') ?? fallbackSection('raw'),
    sessionWindow: durableSessionWindow?.kind === 'main' && presentationRunStatus
      ? buildSessionWindowModel(selectedSession, presentationRunStatus)
      : durableSessionWindow,
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
  if (!contextWindow) return getContextUsageProjection(sessionWorkbench);
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

function getContextUsageProjection(sessionWorkbench?: SessionWorkbench | null): {
  label: string | null;
  title: string | null;
} {
  const usage = sessionWorkbench?.context_usage;
  if (!usage) return { label: null, title: null };
  const usedTokens = finiteNumber(usage.used_tokens);
  const freeTokens = finiteNumber(usage.free_space_tokens);
  const windowTokens = finiteNumber(usage.model_window_tokens);
  const counts = usage.counts && typeof usage.counts === 'object' ? usage.counts : {};
  const contextCandidates = finiteNumber(counts.context_candidates);
  const selectedContexts = finiteNumber(counts.selected_contexts);
  const deferredTools = finiteNumber(counts.deferred_tools);
  const skills = finiteNumber(counts.skills);

  const label = usedTokens !== null ? `${compactTokenCount(usedTokens)} used` : null;
  const titleParts = [
    windowTokens !== null ? `window ${compactTokenCount(windowTokens)} tokens` : null,
    freeTokens !== null ? `free ${compactTokenCount(freeTokens)} tokens` : null,
    contextCandidates !== null ? `${contextCandidates} candidates` : null,
    selectedContexts !== null ? `${selectedContexts} selected` : null,
    deferredTools !== null ? `${deferredTools} deferred tools` : null,
    skills !== null ? `${skills} skills` : null,
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
  // An interrupted trailing run is not fabricated into complete: the header
  // shows the idle session state and the run block carries the typed detail.
  if (lastRun?.timeline.status === 'interrupted') return 'idle';
  if (cells.length > 0) return 'complete';
  return 'idle';
}

function normalizedActiveRunStatus(status?: string | null): string | null {
  const value = String(status || '').trim().toLowerCase();
  if (!value) return null;
  if (value === 'idle') return null;
  if (value === 'completed' || value === 'complete' || value === 'succeeded' || value === 'success') return null;
  if (value === 'cancelled' || value === 'canceled' || value === 'killed') return null;
  return value;
}

function latestUserMessageIndex(messages: AgentChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'user') return index;
  }
  return -1;
}

function hasAssistantAnswerAfterLatestUser(messages: AgentChatMessage[]): boolean {
  const latestUserIndex = latestUserMessageIndex(messages);
  return messages.some((message, index) => index > latestUserIndex && isRenderableAssistantAnswer(message));
}

function mainTurnActiveRunStatus(input: BuildThreadTimelineInput, cells: ThreadTimelineCell[]): string | null {
  const activeRunStatus = normalizedActiveRunStatus(input.activeRunStatus);
  if (!activeRunStatus) return null;
  if (activeRunStatus === 'failed') return activeRunStatus;
  if (input.isWaiting || input.isStreaming) return activeRunStatus;
  if (hasOpenRunCell(cells)) return activeRunStatus;
  return hasAssistantAnswerAfterLatestUser(input.messages) ? null : activeRunStatus;
}

function isRenderableAssistantAnswer(message: AgentChatMessage): boolean {
  if (message.sessionItem) {
    return message.sessionItem.kind === 'assistant_final'
      && message.sessionItem.terminal
      && Boolean(message.content?.trim());
  }
  if (message.role !== 'assistant' || !message.content?.trim()) return false;
  if (message.eventType === 'assistant_message' || message.eventType === 'assistant_final') return true;
  if (message.eventType?.startsWith('assistant_')) return false;
  return true;
}

function isCanonicalSessionProcessItem(message: AgentChatMessage): boolean {
  const kind = message.sessionItem?.kind;
  if (!kind) return false;
  return kind.startsWith('assistant_')
    || kind.startsWith('tool_')
    || kind.startsWith('file_')
    || kind.startsWith('memory_')
    || kind.startsWith('skill_')
    || kind.startsWith('workflow_')
    || kind.startsWith('a2a_')
    || kind === 'mcp_call'
    || kind === 'web_search'
    || kind === 'image_view'
    || kind === 'code_execution'
    || kind === 'context_source'
    || kind === 'context_compaction'
    || kind === 'subagent'
    || kind === 'hook'
    || kind === 'approval'
    || kind === 'user_question'
    || kind === 'result_commit';
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
  activeRun = false,
  turnStartedAt?: string,
): Extract<ThreadTimelineCell, { kind: 'active_run' }> {
  const timeline = buildRunTimelineFromMessages(
    sourceMessages.map((entry) => entry.message),
    { answer: answer?.message, activeRun, turnStartedAt },
  );
  return {
    kind: 'active_run',
    id: `run-${runIndex}-${sourceMessages[0]?.index ?? answer?.index ?? 0}`,
    timeline: answer
      ? { ...timeline, answerMessageId: messageId(answer.message, `assistant-${answer.index}`) }
      : timeline,
    sourceMessages,
  };
}

function hasOpenRunCell(cells: ThreadTimelineCell[]): boolean {
  return cells.some((cell) => (
    cell.kind === 'active_run' &&
    (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
  ));
}

const LIVE_RUN_PHASES: ReadonlySet<string> = new Set([
  'queued',
  'resuming',
  'starting',
  'thinking',
  'responding',
  'tool_running',
  'hook_evaluating',
  'compacting',
  'awaiting_approval',
  'awaiting_budget',
  'summarizing',
  'continuation_gap',
]);

const WAITING_HEADER_PHASES: ReadonlySet<string> = new Set([
  'awaiting_approval',
  'awaiting_budget',
]);

function livePhaseHeaderStatus(phase: string): SessionWorkbenchStatus {
  if (WAITING_HEADER_PHASES.has(phase)) return 'waiting';
  if (phase === 'responding') return 'streaming';
  return 'running';
}

function liveRunPhase(input: BuildThreadTimelineInput): string | null {
  const phase = String(input.runtimePhase || '').trim();
  return LIVE_RUN_PHASES.has(phase) ? phase : null;
}

function buildPendingRunCell(
  input: BuildThreadTimelineInput,
  activeRunStatus: string | null,
): Extract<ThreadTimelineCell, { kind: 'active_run' }> | null {
  const phase = liveRunPhase(input);
  if (!input.isWaiting && !input.isStreaming && !activeRunStatus && !phase) return null;
  const status: RunTimelineSnapshot['status'] = activeRunStatus === 'failed' ? 'failed' : 'running';
  const title = phase === 'responding' || input.isStreaming ? 'Working' : 'Thinking';
  const summary = activeRunStatus
    ? `Active run: ${activeRunStatus}`
    : input.isStreaming
      ? 'The assistant is streaming this turn.'
      : 'The assistant is continuing this turn.';
  const latestInputIndex = latestUserMessageIndex(input.messages);
  const startedAt = latestInputIndex >= 0 ? input.messages[latestInputIndex]?.timestamp : undefined;
  return {
    kind: 'active_run',
    id: 'active-run-pending',
    sourceMessages: [],
    ...(phase ? { phase } : {}),
    timeline: {
      id: 'active-run-pending',
      status,
      startedAt,
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

function keepLatestProcessRunActive(
  cells: ThreadTimelineCell[],
  input: BuildThreadTimelineInput,
  activeRunStatus: string | null,
  terminalDeliveryPending: boolean,
): boolean {
  const phase = liveRunPhase(input);
  if (!input.isWaiting && !input.isStreaming && !activeRunStatus && !phase && !terminalDeliveryPending) return false;
  const index = cells.length - 1;
  const cell = cells[index];
  if (!cell || cell.kind !== 'active_run' || cell.timeline.answerMessageId) return false;

  // Rebuild the trailing run with live-run authority so genuinely non-terminal
  // steps stay running; without that authority the honest interrupted state
  // (frozen duration, no live stopwatch) is preserved.
  const rebuilt = buildRunTimelineFromMessages(
    cell.sourceMessages.map((entry) => entry.message),
    { activeRun: true, turnStartedAt: cell.timeline.startedAt },
  );
  const status: RunTimelineSnapshot['status'] = activeRunStatus === 'failed'
    ? 'failed'
    : rebuilt.status === 'blocked'
      ? 'blocked'
      : 'running';
  cells[index] = {
    ...cell,
    ...(phase ? { phase } : {}),
    timeline: {
      ...cell.timeline,
      ...rebuilt,
      status,
      completedAt: status === 'failed' ? rebuilt.completedAt ?? cell.timeline.completedAt : undefined,
    },
  };
  return true;
}

function buildCells(messages: AgentChatMessage[]): ThreadTimelineCell[] {
  const cells: ThreadTimelineCell[] = [];
  const pendingRun: Array<{ message: AgentChatMessage; index: number }> = [];
  let runIndex = 0;
  let turnStartedAt: string | undefined;

  const flushRun = () => {
    if (pendingRun.length === 0) return;
    cells.push(buildRunCell(runIndex, [...pendingRun], undefined, false, turnStartedAt));
    runIndex += 1;
    pendingRun.length = 0;
  };

  messages.forEach((message, index) => {
    if (isRenderableAssistantAnswer(message)) {
      if (message.thinking?.trim()) {
        const answerMessage = assistantAnswerMessage(message);
        const sourceMessages = [
          ...pendingRun,
          { message: assistantReasoningStepMessage(message), index },
        ];
        cells.push(buildRunCell(runIndex, sourceMessages, { message: answerMessage, index }, false, turnStartedAt));
        cells.push({
          kind: 'assistant_final',
          id: messageId(answerMessage, `assistant-${index}`),
          message: answerMessage,
          index,
        });
        runIndex += 1;
        pendingRun.length = 0;
        return;
      }
      if (pendingRun.length > 0) {
        cells.push(buildRunCell(runIndex, [...pendingRun], { message, index }, false, turnStartedAt));
        cells.push({
          kind: 'assistant_final',
          id: messageId(message, `assistant-${index}`),
          message,
          index,
        });
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

    if (isCanonicalSessionProcessItem(message)) {
      pendingRun.push({ message, index });
      return;
    }

    if (isDisclosureStepMessage(message)) {
      pendingRun.push({ message, index });
      return;
    }

    flushRun();
    if (message.role === 'user') {
      turnStartedAt = message.timestamp;
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
        title: message.threadItem?.item_type || message.eventTitle || message.eventType || 'Session event',
        summary: message.content || message.eventReason || message.eventNextStep,
        message,
        index,
      });
    }
  });

  flushRun();
  return cells;
}

function stableStringify(value: unknown): string {
  if (value == null) return '';
  const seen = new WeakSet<object>();
  try {
    return JSON.stringify(value, (_key, nested) => {
      if (!nested || typeof nested !== 'object') return nested;
      if (seen.has(nested)) return '[Circular]';
      seen.add(nested);
      if (Array.isArray(nested)) return nested;
      return Object.keys(nested as Record<string, unknown>)
        .sort()
        .reduce<Record<string, unknown>>((acc, key) => {
          acc[key] = (nested as Record<string, unknown>)[key];
          return acc;
        }, {});
    });
  } catch {
    return String(value);
  }
}

function threadTimelineInputSignature(input: BuildThreadTimelineInput): string {
  return stableStringify({
    activeSession: input.activeSession,
    runtimeSummary: input.runtimeSummary,
    sessionIndex: input.sessionIndex,
    sessionWorkbench: input.sessionWorkbench,
    branchLineage: input.branchLineage,
    isWaiting: input.isWaiting,
    isStreaming: input.isStreaming,
    activeRunStatus: input.activeRunStatus ?? null,
    runtimePhase: input.runtimePhase ?? null,
  });
}

function sameCellIdentity(previous: ThreadTimelineCell, next: ThreadTimelineCell): boolean {
  if (previous.kind !== next.kind || previous.id !== next.id) return false;
  if (previous.kind === 'user_turn' && next.kind === 'user_turn') {
    return previous.index === next.index && previous.message === next.message;
  }
  if (previous.kind === 'assistant_final' && next.kind === 'assistant_final') {
    return previous.index === next.index && previous.message === next.message;
  }
  if (previous.kind === 'boundary' && next.kind === 'boundary') {
    return previous.index === next.index && previous.title === next.title && previous.summary === next.summary;
  }
  if (previous.kind === 'active_run' && next.kind === 'active_run') {
    if (previous.phase !== next.phase) return false;
    if (previous.timeline.status !== next.timeline.status) return false;
    if (previous.timeline.answerMessageId !== next.timeline.answerMessageId) return false;
    if (previous.timeline.steps.length !== next.timeline.steps.length) return false;
    if (previous.timeline.steps.some((step, index) => (
      step.id !== next.timeline.steps[index]?.id || step.status !== next.timeline.steps[index]?.status
    ))) return false;
    if (previous.sourceMessages.length !== next.sourceMessages.length) return false;
    return previous.sourceMessages.every((entry, index) => (
      entry.index === next.sourceMessages[index]?.index && entry.message === next.sourceMessages[index]?.message
    ));
  }
  return false;
}

function reuseStableTimelineCells(
  previousModel: ThreadTimelineModel | null,
  nextModel: ThreadTimelineModel,
): ThreadTimelineModel {
  if (!previousModel || previousModel.cells.length === 0 || nextModel.cells.length === 0) return nextModel;
  let reusedAny = false;
  const nextCells = nextModel.cells.map((cell, index) => {
    const previous = previousModel.cells[index];
    if (!previous || !sameCellIdentity(previous, cell)) return cell;
    reusedAny = true;
    return previous;
  });
  return reusedAny ? { ...nextModel, cells: nextCells } : nextModel;
}

export function createThreadTimelineCache(): ThreadTimelineCache {
  return {
    previousInputSignature: null,
    previousMessages: null,
    previousModel: null,
  };
}

export function buildThreadTimeline(input: BuildThreadTimelineInput): ThreadTimelineModel {
  const cells = buildCells(input.messages);
  const activeRunStatus = mainTurnActiveRunStatus(input, cells);
  const runtimePhaseValue = String(input.runtimePhase || '').trim();
  const terminalDeliveryPending = runtimePhaseValue === 'done'
    && latestUserMessageIndex(input.messages) >= 0
    && !hasAssistantAnswerAfterLatestUser(input.messages);
  const retainedProcessRun = keepLatestProcessRunActive(cells, input, activeRunStatus, terminalDeliveryPending);
  const unresolvedUserTurn = cells.at(-1)?.kind === 'user_turn'
    && !input.isWaiting
    && !input.isStreaming
    && !activeRunStatus
    && (!runtimePhaseValue || runtimePhaseValue === 'idle');
  const pendingRunCell = buildPendingRunCell(input, activeRunStatus);
  if (pendingRunCell && !retainedProcessRun && !hasOpenRunCell(cells)) {
    cells.push(pendingRunCell);
  }
  const livePhase = liveRunPhase(input);
  if (livePhase) {
    for (let index = cells.length - 1; index >= 0; index -= 1) {
      const cell = cells[index];
      if (cell.kind !== 'active_run') continue;
      if (cell.timeline.status === 'running' || cell.timeline.status === 'blocked') {
        cells[index] = { ...cell, phase: livePhase };
      }
      break;
    }
  }
  const sessionIndex = input.sessionIndex && !Array.isArray(input.sessionIndex) ? input.sessionIndex : null;
  const sessionWorkbench = input.sessionWorkbench && !Array.isArray(input.sessionWorkbench) ? input.sessionWorkbench : null;
  const contextWindow = getContextWindowProjection(sessionWorkbench);
  const runtimeSummary = input.runtimeSummary || null;
  const headerStatus = livePhase
    ? livePhaseHeaderStatus(livePhase)
    : terminalDeliveryPending
      ? 'running'
      : unresolvedUserTurn
        ? 'running'
        : getHeaderStatus(cells, input.isWaiting, input.isStreaming, activeRunStatus);

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
      activeRunStatus,
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

export function buildThreadTimelineCached(
  input: BuildThreadTimelineInput,
  cache: ThreadTimelineCache,
): ThreadTimelineModel {
  const messagesUnchanged = cache.previousMessages === input.messages;
  const signature = !cache.previousModel || messagesUnchanged ? threadTimelineInputSignature(input) : null;
  if (
    cache.previousModel
    && messagesUnchanged
    && cache.previousInputSignature === signature
  ) {
    return cache.previousModel;
  }

  const nextModel = reuseStableTimelineCells(cache.previousModel, buildThreadTimeline(input));
  cache.previousMessages = input.messages;
  cache.previousInputSignature = signature;
  cache.previousModel = nextModel;
  return nextModel;
}

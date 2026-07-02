import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconCalendarTime,
  IconChevronLeft,
  IconChevronRight,
  IconChecklist,
  IconCircleDashedCheck,
  IconDownload,
  IconExternalLink,
  IconFileText,
  IconGitBranch,
  IconHistory,
  IconLoader2,
  IconPaperclip,
  IconPlus,
  IconSend2,
  IconShieldCheck,
  IconTargetArrow,
  IconThumbDown,
  IconThumbUp,
  IconTrash,
  IconX,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';

import MarkdownRenderer from '../../components/MarkdownRenderer';
import type { AgentPermissions } from '../../api/domains/agents';
import AskUserQuestionCard from './AskUserQuestionCard';
import PlanModeRequestCard from './PlanModeRequestCard';
import CopyMessageButton from './CopyMessageButton';
import PlanCard from './PlanCard';
import RunDisclosureBlock from './RunDisclosureBlock';
import SlashCommandMenu from './SlashCommandMenu';
import ChatWorkLedgerDock from './ChatWorkLedgerDock';
import { SessionWorkbenchHeader } from '../session-workbench/SessionWorkbenchChrome';
import {
  buildSessionRightPanelModel,
  buildThreadTimeline,
  buildWorkflowRunWindowModel,
  type RuntimeSectionItemModel,
  type WorkspaceDocumentGroupModel,
  type WorkspaceDocumentModel,
  type ThreadTimelineModel,
} from '../session-workbench/timelineModel';
import { chatApi, type RecordSessionFeedbackInput } from '../../api/domains/chat';
import { ccParityApi, type SessionWorkbench } from '../../api/domains/ccParity';
import { fileApi } from '../../api/domains/files';
import { planApi } from '../../api/domains/plans';
import { showAppToast } from '../../components/AppDialogs';
import { composerShortcutText } from './sessionComposerShortcuts';
import type { ToolCallMeta } from './toolResultEnvelope';
import {
  computeComposerHeight,
  getCompactionDisplayContent,
  isA2ASession,
  isDraftHumanChatSession,
  shouldUseWritableSessionSurface,
  type AgentChatMessage,
  type ChatArtifactPart,
  type ChatRuntimeSummary,
  type SessionPermissionRequest,
} from './chatRuntime';

type AttachedFile = {
  name: string;
  text: string;
  path?: string;
  imageUrl?: string;
};

type ComposerActionKey = 'upload' | 'plan' | 'goal' | 'schedule';
export type SessionPermissionMode = 'auto' | 'default' | 'bypassPermissions';

const SESSION_PERMISSION_MODE_OPTIONS: Array<{
  value: SessionPermissionMode;
  label: string;
  description: string;
}> = [
  {
    value: 'bypassPermissions',
    label: 'Full access',
    description: 'Bypass session prompts, still obey enterprise rules',
  },
  {
    value: 'auto',
    label: 'Approve for me',
    description: 'Approve low-risk actions and ask for risky ones',
  },
  {
    value: 'default',
    label: 'Ask first',
    description: 'Ask before sensitive session actions',
  },
];

function formatCompactTokenCount(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '-';
  if (value >= 1_000_000) return `${Math.round(value / 100_000) / 10}m`;
  if (value >= 1_000) return `${Math.round(value / 100) / 10}k`;
  return String(Math.round(value));
}

function getRuntimeUsageLabel(runtimeSummary: ChatRuntimeSummary | null): string {
  const used = runtimeSummary?.runtime?.estimated_input_tokens;
  const remaining = runtimeSummary?.runtime?.remaining_tokens_estimate;
  const contextWindow = runtimeSummary?.model?.context_window_tokens;
  const total =
    typeof contextWindow === 'number' && contextWindow > 0
      ? contextWindow
      : typeof used === 'number' && typeof remaining === 'number'
        ? used + remaining
        : null;
  if (typeof used !== 'number' || !total || total <= 0) return '';
  return `${Math.max(0, Math.min(100, Math.round((used / total) * 100)))}% used`;
}

function getRuntimeUsageTitle(runtimeSummary: ChatRuntimeSummary | null, usageLabel: string): string {
  const used = runtimeSummary?.runtime?.estimated_input_tokens;
  const contextWindow = runtimeSummary?.model?.context_window_tokens;
  const remaining = runtimeSummary?.runtime?.remaining_tokens_estimate;
  const parts = [
    usageLabel,
    typeof used === 'number' ? `used ${formatCompactTokenCount(used)} tokens` : null,
    typeof contextWindow === 'number' ? `window ${formatCompactTokenCount(contextWindow)} tokens` : null,
    typeof remaining === 'number' ? `remaining ${formatCompactTokenCount(remaining)} tokens` : null,
  ].filter(Boolean);
  return parts.join(' · ');
}

function isUuidLike(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function getSessionPermissionModeLabel(
  mode: SessionPermissionMode,
  t: (key: string, fallback: string) => string,
): string {
  if (mode === 'default') return t('agent.chat.composer.permissionMode.default', 'Ask first');
  if (mode === 'bypassPermissions') return t('agent.chat.composer.permissionMode.bypassPermissions', 'Full access');
  return t('agent.chat.composer.permissionMode.auto', 'Approve for me');
}

function getComposerIntentLabel(
  planModeRequested: boolean,
  t: (key: string, fallback: string) => string,
): string | null {
  if (planModeRequested) return t('agent.chat.composer.planModeActive', 'Plan Mode');
  return null;
}

function SessionPermissionActions({
  permissionRequest,
  onResolveSessionPermission,
  t,
}: {
  permissionRequest: SessionPermissionRequest;
  onResolveSessionPermission?: (request: SessionPermissionRequest, action: 'allow_once' | 'allow_session' | 'deny') => void | Promise<unknown>;
  t: (key: string, fallback: string) => string;
}) {
  const allowSession =
    permissionRequest.allow_session_allowed !== false
    && permissionRequest.risk_class !== 'destructive_delete'
    && permissionRequest.confirmation_kind !== 'destructive_once';
  return (
    <>
      <div style={{ marginTop: '10px', display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => onResolveSessionPermission?.(permissionRequest, 'allow_once')}
          style={{ fontSize: '12px', padding: '6px 10px' }}
        >
          {t('agent.chat.permission.allowOnce', 'Allow once')}
        </button>
        {allowSession && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => onResolveSessionPermission?.(permissionRequest, 'allow_session')}
            style={{ fontSize: '12px', padding: '6px 10px' }}
          >
            {t('agent.chat.permission.allowSession', 'Allow for this session')}
          </button>
        )}
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => onResolveSessionPermission?.(permissionRequest, 'deny')}
          style={{ fontSize: '12px', padding: '6px 10px' }}
        >
          {t('agent.chat.permission.deny', 'Deny')}
        </button>
      </div>
      {!allowSession && (
        <div style={{ marginTop: '7px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
          {t('agent.chat.permission.deleteOnceOnly', 'Delete actions can only be allowed once.')}
        </div>
      )}
    </>
  );
}

type ConversationBranchMode =
  | 'fork'
  | 'branch'
  | 'edit'
  | 'insert_before'
  | 'insert_after'
  | 'reply'
  | 'regenerate'
  | 'rewind'
  | 'side_question';

export interface BranchLineageItem {
  id: string;
  parent_session_id?: string | null;
  root_session_id?: string | null;
  title?: string | null;
  branch?: Record<string, unknown> | null;
  created_at?: string | null;
}

export interface BranchLineageRow extends BranchLineageItem {
  depth: number;
}

export interface SessionCommandCheckpoint {
  checkpoint_event_id?: string | null;
  event_id?: string | null;
  sequence?: number | string | null;
  turn_index?: number | string | null;
  role?: string | null;
  content?: string | null;
  created_at?: string | null;
}

export type SessionCommandControlType =
  | 'checkpoint_selector'
  | 'projection_status'
  | 'context_panel'
  | 'usage_panel'
  | 'export_panel'
  | 'side_question'
  | 'permissions_panel'
  | 'resume_picker';

export interface SessionCommandControlState {
  type: SessionCommandControlType;
  title: string;
  message?: string;
  command?: string;
  level?: 'success' | 'error' | 'info';
  checkpoints?: SessionCommandCheckpoint[];
  payload?: Record<string, unknown> | null;
}

interface AgentChatSectionProps {
  agentId?: string | null;
  agent: any;
  currentUser: any;
  isAdmin: boolean;
  chatScope: 'mine' | 'all';
  onSetChatScope: (scope: 'mine' | 'all') => void;
  onLoadAllSessions: () => void;
  onCreateNewSession: () => void;
  sessionsLoading: boolean;
  sessions: any[];
  activeSession: any | null;
  branchLineage?: BranchLineageItem[];
  branchLineageLoading?: boolean;
  onSelectBranchSession?: (sessionId: string) => void | Promise<unknown>;
  wsConnected: boolean;
  allSessions: any[];
  allSessionsLoading: boolean;
  allUserFilter: string;
  onSetAllUserFilter: (value: string) => void;
  onSelectSession: (session: any) => void;
  onDeleteSession: (sessionId: string) => void;
  historyContainerRef: React.RefObject<HTMLDivElement | null>;
  onHistoryScroll: () => void;
  historyMsgs: AgentChatMessage[];
  historyMessagesSessionId: string | null;
  showHistoryScrollBtn: boolean;
  onScrollHistoryToBottom: () => void;
  chatContainerRef: React.RefObject<HTMLDivElement | null>;
  onChatScroll: () => void;
  chatMessages: AgentChatMessage[];
  chatMessagesSessionId: string | null;
  runtimeSummary: ChatRuntimeSummary | null;
  agentPermissions?: AgentPermissions | null;
  transportNotice: string | null;
  isWaiting: boolean;
  activeRunStatus?: string | null;

  chatEndRef: React.RefObject<HTMLDivElement | null>;
  showScrollBtn: boolean;
  onScrollToBottom: () => void;
  agentExpired: boolean;
  attachedFiles: AttachedFile[];
  onRemoveAttachedFile: (index: number) => void;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onHandleChatFile: (e: React.ChangeEvent<HTMLInputElement>) => void;
  uploading: boolean;
  uploadProgress: number;
  uploadAbortRef: React.RefObject<(() => void) | null>;
  chatInputRef: React.RefObject<HTMLTextAreaElement | null>;
  chatInput: string;
  onSetChatInput: (value: string) => void;
  onHandlePaste: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
  onSendChatMsg: () => void;
  onBranchMessage?: (message: AgentChatMessage, mode: ConversationBranchMode, content?: string) => void | Promise<unknown>;
  /** Sends an explicit message (not from the composer) — used by inline cards. */
  onSendMessage?: (text: string) => void | Promise<unknown>;
  /**
   * Approves a Plan Mode entry request: sends the given reason as a message
   * carrying `plan_mode_requested=true` so the existing entry path activates
   * Plan Mode. Used by the inline plan-mode-request approval card.
   */
  onEnterPlanMode?: (reason: string) => void | Promise<unknown>;
  planModeRequested?: boolean;
  onTogglePlanMode?: () => void;
  sessionPermissionMode?: SessionPermissionMode;
  onSetSessionPermissionMode?: (mode: SessionPermissionMode) => void | Promise<unknown>;
  sessionCommandControl?: SessionCommandControlState | null;
  onDismissSessionCommandControl?: () => void;
  onRunSessionCommand?: (command: string, args?: Record<string, unknown>) => void | Promise<unknown>;
  onResolveSessionPermission?: (
    request: SessionPermissionRequest,
    action: 'allow_once' | 'allow_session' | 'deny',
  ) => void | Promise<unknown>;
  isStreaming: boolean;
  onAbortGeneration: () => void;
  sessionOnly?: boolean;
}

interface StructuredToolResultBodyProps {
  toolName?: string;
  toolMeta?: ToolCallMeta | null;
  toolResult?: string;
  toolRawResult?: string;
  agentId?: string;
  /** The agent's display name — shown on the plan-mode-request approval card. */
  agentName?: string | null;
  /** True when a persisted clarification card already has a later user answer. */
  submitted?: boolean;
  /**
   * Sends a new user chat message. Threaded from the component that owns the
   * send handler (AgentDetail → AgentChatSection). Used by the clarification
   * card to post the user's answer so the agent's turn resumes.
   */
  onSendMessage?: (text: string) => void | Promise<unknown>;
  /**
   * Approves a Plan Mode entry request — sends a message with
   * `plan_mode_requested=true` so the existing entry path activates Plan Mode.
   * Used by the inline plan-mode-request approval card.
   */
  onEnterPlanMode?: (reason: string) => void | Promise<unknown>;
}

type ArtifactPreviewState = {
  artifact: ChatArtifactPart;
  content?: string;
  url?: string;
  loading?: boolean;
  error?: string;
  usingSnapshot?: boolean;
  workspaceChanged?: boolean;
  legacyCurrentFileFallback?: boolean;
};

type Translate = ReturnType<typeof useTranslation>['t'];

const PLAN_ID_RE = /\bplan_id\s*[=:]\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i;

export function extractPlanIdFromPlanModeMessage(content: string | null | undefined): string | null {
  if (!content) return null;
  return content.match(PLAN_ID_RE)?.[1] ?? null;
}

export function isClarificationCardAnsweredByLaterUserMessage(messages: AgentChatMessage[], index: number): boolean {
  const message = messages[index];
  if (message?.role !== 'tool_call' || message.toolMeta?.kind !== 'user_clarification') return false;
  if (message.toolMeta.answered) return true;
  return messages
    .slice(index + 1)
    .some((candidate) => candidate.role === 'user' && String(candidate.content || '').trim().length > 0);
}

function branchModeLabel(item: BranchLineageItem): string {
  const branch = item.branch || {};
  const mode = String(branch.branch_mode || branch.mode || '').trim();
  if (!mode) return 'root';
  return mode.replace(/_/g, ' ');
}

export function buildBranchLineageRows(lineage: BranchLineageItem[]): BranchLineageRow[] {
  const byId = new Map(lineage.map((item) => [String(item.id), item]));
  const children = new Map<string, BranchLineageItem[]>();
  const roots: BranchLineageItem[] = [];
  lineage.forEach((item) => {
    const parentId = item.parent_session_id ? String(item.parent_session_id) : null;
    if (!parentId || !byId.has(parentId)) {
      roots.push(item);
      return;
    }
    const list = children.get(parentId) || [];
    list.push(item);
    children.set(parentId, list);
  });

  const rows: BranchLineageRow[] = [];
  const seen = new Set<string>();
  const visit = (item: BranchLineageItem, depth: number) => {
    const id = String(item.id);
    if (seen.has(id)) return;
    seen.add(id);
    rows.push({ ...item, depth });
    (children.get(id) || []).forEach((child) => visit(child, depth + 1));
  };
  roots.forEach((root) => visit(root, 0));
  lineage.forEach((item) => {
    if (!seen.has(String(item.id))) visit(item, 0);
  });
  return rows;
}

export function BranchLineagePanel({
  activeSessionId,
  lineage,
  loading = false,
  onSelectSession,
}: {
  activeSessionId?: string | null;
  lineage: BranchLineageItem[];
  loading?: boolean;
  onSelectSession: (sessionId: string) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  if (loading) {
    return (
      <div data-testid="branch-lineage-panel" style={{ padding: '8px 12px', borderBottom: '1px solid var(--border-subtle)', fontSize: '11px', color: 'var(--text-tertiary)' }}>
        {t('common.loading', 'Loading')}
      </div>
    );
  }
  if (lineage.length <= 1) return null;
  const rows = buildBranchLineageRows(lineage);
  return (
    <div data-testid="branch-lineage-panel" style={{ padding: '8px 12px', borderBottom: '1px solid var(--border-subtle)' }}>
      <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: 0, marginBottom: '6px' }}>
        {t('agent.chat.branch.branches', 'Branches')}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
        {rows.map((row) => {
          const isActive = String(row.id) === String(activeSessionId || '');
          return (
            <button
              key={row.id}
              type="button"
              data-testid="branch-lineage-row"
              onClick={() => onSelectSession(String(row.id))}
              style={{
                border: 'none',
                background: isActive ? 'var(--bg-secondary)' : 'transparent',
                color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
                borderRadius: '4px',
                cursor: 'pointer',
                padding: '4px 6px',
                paddingLeft: `${6 + row.depth * 12}px`,
                textAlign: 'left',
                fontSize: '11px',
                lineHeight: 1.3,
              }}
            >
              <span style={{ color: 'var(--text-tertiary)', marginRight: '5px' }}>{branchModeLabel(row)}</span>
              <span>{row.title || row.id}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function sessionCheckpointId(checkpoint: Record<string, unknown>): string {
  return String(
    checkpoint.checkpoint_event_id
      || checkpoint.event_id
      || checkpoint.message_id
      || checkpoint.id
      || '',
  );
}

function sessionCheckpointSequence(checkpoint: Record<string, unknown>): string {
  const metadata = checkpoint.metadata && typeof checkpoint.metadata === 'object' && !Array.isArray(checkpoint.metadata)
    ? checkpoint.metadata as Record<string, unknown>
    : {};
  return String(
    checkpoint.sequence
      ?? checkpoint.turn_index
      ?? metadata.sequence
      ?? metadata.turn_index
      ?? '',
  );
}

function branchAnchorSequence(item: BranchLineageItem): string {
  const branch = item.branch || {};
  return String(
    branch.anchor_sequence
      ?? branch.checkpoint_sequence
      ?? branch.sequence
      ?? branch.turn_index
      ?? '',
  );
}

function resolveBranchAnchorCheckpointId(
  item: BranchLineageItem,
  checkpoints: Array<Record<string, unknown>>,
  checkpointIds: Set<string>,
): string {
  const directAnchorId = branchAnchorId(item);
  if (directAnchorId && checkpointIds.has(directAnchorId)) return directAnchorId;

  const anchorSequence = branchAnchorSequence(item);
  if (!anchorSequence) return directAnchorId;

  const matchedCheckpoint = checkpoints.find((checkpoint) => (
    sessionCheckpointSequence(checkpoint) === anchorSequence
  ));
  return matchedCheckpoint ? sessionCheckpointId(matchedCheckpoint) : directAnchorId;
}

export type SessionScrollCheckpointAnchor = {
  id: string;
  top: number;
};

export function pickFocusedCheckpointIdForScroll(
  anchors: SessionScrollCheckpointAnchor[],
  viewportCenterY: number,
): string | null {
  const sorted = anchors
    .filter((anchor) => anchor.id && Number.isFinite(anchor.top))
    .sort((a, b) => a.top - b.top);
  if (sorted.length === 0) return null;

  let focused = sorted[0];
  for (const anchor of sorted) {
    if (anchor.top > viewportCenterY) break;
    focused = anchor;
  }
  return focused.id;
}

export type SessionGitLineDensity = 'empty' | 'sparse' | 'regular' | 'scrollable';

export function getSessionGitLineDensity(itemCount: number): SessionGitLineDensity {
  if (itemCount <= 0) return 'empty';
  if (itemCount <= 6) return 'sparse';
  if (itemCount >= 32) return 'scrollable';
  return 'regular';
}

function checkpointTextValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const part = item as Record<string, unknown>;
          return checkpointTextValue(part.text ?? part.content ?? part.value);
        }
        return '';
      })
      .filter(Boolean)
      .join(' ');
  }
  return '';
}

function normalizeCheckpointPreviewText(value: unknown): string {
  return checkpointTextValue(value).replace(/\s+/g, ' ').trim();
}

function isOpaqueCheckpointPreview(text: string, checkpoint: Record<string, unknown>): boolean {
  if (!text) return true;
  const id = sessionCheckpointId(checkpoint);
  if (id && text === id) return true;
  if (/^[a-z0-9_.:-]{16,}$/i.test(text) && !/\s/.test(text)) return true;
  return false;
}

function checkpointPromptIntent(checkpoint: Record<string, unknown>): string {
  const metadata = checkpoint.metadata && typeof checkpoint.metadata === 'object'
    ? checkpoint.metadata as Record<string, unknown>
    : {};
  const candidates = [
    checkpoint.prompt,
    checkpoint.user_prompt,
    checkpoint.original_prompt,
    checkpoint.input,
    checkpoint.query,
    checkpoint.request,
    checkpoint.content_preview,
    checkpoint.display_content,
    checkpoint.content,
    metadata.prompt,
    metadata.user_prompt,
    metadata.original_prompt,
    metadata.display_content,
    metadata.content_preview,
    metadata.llm_content,
    metadata.user_message,
    checkpoint.title,
    checkpoint.summary,
  ];
  for (const candidate of candidates) {
    const text = normalizeCheckpointPreviewText(candidate);
    if (!isOpaqueCheckpointPreview(text, checkpoint)) return text;
  }
  return '';
}

export function sessionCheckpointPreview(checkpoint: Record<string, unknown>, index: number): string {
  const intent = checkpointPromptIntent(checkpoint) || `Checkpoint ${index + 1}`;
  return intent.length > 96 ? `${intent.slice(0, 93).trimEnd()}...` : intent;
}

function branchAnchorId(item: BranchLineageItem): string {
  const branch = item.branch || {};
  return String(
    branch.anchor_event_id
      || branch.checkpoint_event_id
      || branch.event_id
      || branch.message_id
      || '',
  );
}

function SessionGitLine({
  activeSessionId,
  axisSessionId,
  checkpoints,
  focusedCheckpointId,
  lineage,
  loading,
  onNavigateCheckpoint,
  onNavigateBranch,
}: {
  activeSessionId?: string | null;
  axisSessionId?: string | null;
  checkpoints: Array<Record<string, unknown>>;
  focusedCheckpointId?: string | null;
  lineage: BranchLineageItem[];
  loading?: boolean;
  onNavigateCheckpoint: (checkpoint: Record<string, unknown>, index: number) => void;
  onNavigateBranch?: (sessionId: string) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const navigationLabel = t('sessionWorkbench.gitLine.navigation', 'Session navigation');
  const lineageRows = lineage.length > 1 ? buildBranchLineageRows(lineage) : [];
  const rootRow = lineageRows.find((row) => row.depth === 0) || null;
  const branchRows = lineageRows.filter((row) => row.depth > 0);
  const checkpointIds = new Set(checkpoints.map((checkpoint) => sessionCheckpointId(checkpoint)).filter(Boolean));
  const branchesByAnchorId = new Map<string, BranchLineageRow[]>();
  const orphanBranchRows: BranchLineageRow[] = [];
  branchRows.forEach((row) => {
    const anchorId = resolveBranchAnchorCheckpointId(row, checkpoints, checkpointIds);
    if (!anchorId || !checkpointIds.has(anchorId)) {
      orphanBranchRows.push(row);
      return;
    }
    const rows = branchesByAnchorId.get(anchorId) || [];
    rows.push(row);
    branchesByAnchorId.set(anchorId, rows);
  });
  const density = getSessionGitLineDensity(checkpoints.length + branchRows.length);
  const densityClass = `is-${density}`;
  const [expandedBranchAnchorId, setExpandedBranchAnchorId] = React.useState<string | null>(null);
  const [hoverPreview, setHoverPreview] = React.useState<{
    label: string;
    top: number;
    left: number;
  } | null>(null);
  const openPreview = React.useCallback((
    event: React.MouseEvent<HTMLButtonElement> | React.FocusEvent<HTMLButtonElement>,
    label: string,
  ) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setHoverPreview({
      label,
      top: rect.top + rect.height / 2,
      left: rect.right + 8,
    });
  }, []);
  const closePreview = React.useCallback(() => {
    setHoverPreview(null);
  }, []);
  const renderBranchButton = (row: BranchLineageRow, compact = false, resolvedAnchorId?: string) => {
    const isActive = String(row.id) === String(activeSessionId || '');
    const title = row.title || row.id;
    const anchorId = resolvedAnchorId || resolveBranchAnchorCheckpointId(row, checkpoints, checkpointIds);
    const previewLabel = `${branchModeLabel(row)} · ${title}`;
    return (
      <button
        key={row.id}
        type="button"
        data-testid="session-gitline-branch"
        data-session-action="navigate-branch"
        data-branch-session-id={row.id}
        data-checkpoint-id={anchorId || undefined}
        className={`session-gitline-branch-node ${compact ? 'is-compact' : ''} ${isActive ? 'is-active' : ''}`}
        title={previewLabel}
        aria-label={previewLabel}
        onMouseEnter={(event) => openPreview(event, previewLabel)}
        onFocus={(event) => openPreview(event, previewLabel)}
        onMouseLeave={closePreview}
        onBlur={closePreview}
        onClick={() => onNavigateBranch?.(String(row.id))}
      >
        <span className="session-gitline-branch-stem" aria-hidden="true" />
        <span className="session-gitline-branch-dot" aria-hidden="true" />
      </button>
    );
  };
  const activeBranchRow = branchRows.find((row) => String(row.id) === String(activeSessionId || '')) || null;
  const activeBranchAnchorId = activeBranchRow
    ? resolveBranchAnchorCheckpointId(activeBranchRow, checkpoints, checkpointIds)
    : '';
  const canReturnToRoot = Boolean(rootRow && activeSessionId && String(rootRow.id) !== String(activeSessionId));
  const renderRootReturnButton = (anchorId?: string) => {
    if (!rootRow || !canReturnToRoot) return null;
    const title = rootRow.title || rootRow.id;
    const previewLabel = `${t('agent.chat.branch.mainSession', 'Main session')} · ${title}`;
    return (
      <button
        key={`root-${rootRow.id}`}
        type="button"
        data-testid="session-gitline-root"
        data-session-action="navigate-root-session"
        data-branch-session-id={rootRow.id}
        data-checkpoint-id={anchorId || undefined}
        className="session-gitline-branch-node session-gitline-root-node"
        title={previewLabel}
        aria-label={previewLabel}
        onMouseEnter={(event) => openPreview(event, previewLabel)}
        onFocus={(event) => openPreview(event, previewLabel)}
        onMouseLeave={closePreview}
        onBlur={closePreview}
        onClick={() => onNavigateBranch?.(String(rootRow.id))}
      >
        <span className="session-gitline-branch-stem" aria-hidden="true" />
        <span className="session-gitline-branch-dot" aria-hidden="true" />
      </button>
    );
  };
  if (loading && checkpoints.length === 0 && branchRows.length === 0) {
    return (
      <aside
        data-testid="session-gitline"
        data-axis-session-id={axisSessionId || undefined}
        data-active-session-id={activeSessionId || undefined}
        data-density={density}
        className={`session-gitline is-loading ${densityClass}`}
        aria-label={navigationLabel}
      >
        <span className="session-gitline-loading-dot" />
      </aside>
    );
  }
  if (checkpoints.length === 0 && branchRows.length === 0) {
    return (
      <aside
        data-testid="session-gitline"
        data-axis-session-id={axisSessionId || undefined}
        data-active-session-id={activeSessionId || undefined}
        data-density={density}
        className={`session-gitline is-empty ${densityClass}`}
        aria-label={navigationLabel}
      />
    );
  }
  return (
    <aside
      data-testid="session-gitline"
      data-axis-session-id={axisSessionId || undefined}
      data-active-session-id={activeSessionId || undefined}
      data-density={density}
      className={`session-gitline ${densityClass}`}
      aria-label={navigationLabel}
    >
      <div className="session-gitline-track" data-testid="session-gitline-checkpoints">
        {checkpoints.map((checkpoint, index) => {
          const id = sessionCheckpointId(checkpoint);
          const isFocused = Boolean(id && id === focusedCheckpointId);
          const previewLabel = sessionCheckpointPreview(checkpoint, index);
          const anchoredBranches = id ? branchesByAnchorId.get(id) || [] : [];
          const hasActiveBranch = anchoredBranches.some((row) => String(row.id) === String(activeSessionId || ''));
          const isExpandedBranchAnchor = Boolean(id && expandedBranchAnchorId === id);
          const rootReturnNode = id && canReturnToRoot && activeBranchAnchorId === id
            ? renderRootReturnButton(id)
            : null;
          const showCluster = anchoredBranches.length > 2 && !isExpandedBranchAnchor;
          const branchClusterLabel = anchoredBranches
            .map((row) => row.title || row.id)
            .slice(0, 4)
            .join(' · ');
          return (
            <div
              key={id || index}
              className={`session-gitline-node-wrap ${anchoredBranches.length ? 'has-branches' : ''} ${hasActiveBranch ? 'has-active-branch' : ''}`}
            >
              <button
                type="button"
                data-testid="session-gitline-checkpoint"
                data-session-action="navigate-checkpoint"
                data-checkpoint-id={id || undefined}
                className={`session-gitline-node ${isFocused ? 'is-focused' : ''}`}
                title={previewLabel}
                aria-label={previewLabel}
                onMouseEnter={(event) => openPreview(event, previewLabel)}
                onFocus={(event) => openPreview(event, previewLabel)}
                onMouseLeave={closePreview}
                onBlur={closePreview}
                onClick={() => onNavigateCheckpoint(checkpoint, index)}
              />
              {(anchoredBranches.length > 0 || rootReturnNode) && (
                <div className={`session-gitline-branch-group ${isExpandedBranchAnchor ? 'is-expanded' : ''}`}>
                  {rootReturnNode}
                  {showCluster ? (
                    <button
                      type="button"
                      data-testid="session-gitline-branch-cluster"
                      data-session-action="expand-branches"
                      data-checkpoint-id={id || undefined}
                      className={`session-gitline-branch-cluster ${hasActiveBranch ? 'is-active' : ''}`}
                      title={branchClusterLabel}
                      aria-label={branchClusterLabel || t('agent.chat.branch.branches', 'Branches')}
                      onMouseEnter={(event) => openPreview(event, branchClusterLabel || t('agent.chat.branch.branches', 'Branches'))}
                      onFocus={(event) => openPreview(event, branchClusterLabel || t('agent.chat.branch.branches', 'Branches'))}
                      onMouseLeave={closePreview}
                      onBlur={closePreview}
                      onClick={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        setExpandedBranchAnchorId(id || null);
                      }}
                    >
                      +{anchoredBranches.length}
                    </button>
                  ) : (
                    anchoredBranches.map((row) => renderBranchButton(row, isExpandedBranchAnchor, id || undefined))
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {orphanBranchRows.length > 0 && (
        <div className="session-gitline-branches" data-testid="session-gitline-branches">
          {activeBranchRow && orphanBranchRows.some((row) => String(row.id) === String(activeSessionId || ''))
            ? renderRootReturnButton(activeBranchAnchorId || undefined)
            : null}
          {orphanBranchRows.map((row) => renderBranchButton(row))}
        </div>
      )}
      {hoverPreview ? (
        <div
          className="session-gitline-preview"
          style={{ top: `${hoverPreview.top}px`, left: `${hoverPreview.left}px` }}
          role="tooltip"
        >
          {hoverPreview.label}
        </div>
      ) : null}
    </aside>
  );
}

function commandPanelTypeLabel(type: SessionCommandControlType): string {
  switch (type) {
    case 'checkpoint_selector':
      return 'Rewind';
    case 'projection_status':
      return 'Session context';
    case 'context_panel':
      return 'Context';
    case 'usage_panel':
      return 'Usage';
    case 'export_panel':
      return 'Export';
    case 'side_question':
      return 'Side question';
    case 'permissions_panel':
      return 'Permissions';
    case 'resume_picker':
      return 'Resume';
    default:
      return 'Session';
  }
}

function checkpointId(checkpoint: SessionCommandCheckpoint): string {
  return String(checkpoint.checkpoint_event_id || checkpoint.event_id || '');
}

function checkpointLabel(checkpoint: SessionCommandCheckpoint, index: number): string {
  const sequence = checkpoint.sequence ?? checkpoint.turn_index ?? index + 1;
  const role = checkpoint.role ? `${checkpoint.role}: ` : '';
  const content = String(checkpoint.content || '').trim();
  return `${sequence}. ${role}${content || checkpointId(checkpoint) || 'checkpoint'}`;
}

function payloadSummary(payload?: Record<string, unknown> | null): Array<[string, string]> {
  if (!payload) return [];
  return Object.entries(payload)
    .filter(([key, value]) => key !== 'ui_action' && value != null && typeof value !== 'object')
    .slice(0, 4)
    .map(([key, value]) => [key, String(value)]);
}

export function SessionCommandControlPanel({
  control,
  onDismiss,
  onRunCommand,
}: {
  control?: SessionCommandControlState | null;
  onDismiss: () => void;
  onRunCommand: (command: string, args?: Record<string, unknown>) => void | Promise<unknown>;
}) {
  const checkpoints = control?.checkpoints || [];
  const checkpointIds = checkpoints.map(checkpointId);
  const availableCheckpointIds = checkpointIds.filter(Boolean);
  const defaultFocusedCheckpointId = availableCheckpointIds.length
    ? availableCheckpointIds[availableCheckpointIds.length - 1]
    : null;
  const [focusedCheckpointId, setFocusedCheckpointId] = React.useState<string | null>(defaultFocusedCheckpointId);
  React.useEffect(() => {
    setFocusedCheckpointId((current) => {
      if (current && checkpointIds.includes(current)) return current;
      return defaultFocusedCheckpointId;
    });
  }, [defaultFocusedCheckpointId, checkpointIds.join('|')]);
  if (!control) return null;
  const details = payloadSummary(control.payload);
  return (
    <section data-testid="session-command-control-panel" className="session-tui-command-panel">
      <div className={`session-tui-command-panel-header ${checkpoints.length > 0 || details.length > 0 ? 'has-body' : ''}`}>
        <div style={{ minWidth: 0 }}>
          <div className="session-tui-kicker">
            {commandPanelTypeLabel(control.type)}
          </div>
          <div className="session-tui-row-title">
            {control.title}
          </div>
          {control.message ? (
            <div className="session-tui-row-meta">
              {control.message}
            </div>
          ) : null}
        </div>
        <button
          type="button"
          aria-label="Close"
          onClick={onDismiss}
          className="session-tui-icon-button"
        >
          ×
        </button>
      </div>
      {checkpoints.length > 0 ? (
        <div className="session-tui-command-panel-body">
          <div data-testid="session-checkpoint-rail" className="session-tui-checkpoint-rail">
            {checkpoints.map((checkpoint, index) => {
              const id = checkpointId(checkpoint);
              const isFocused = Boolean(id && id === focusedCheckpointId);
              return (
                <button
                  key={id || index}
                  type="button"
                  data-session-action="focus-checkpoint"
                  data-checkpoint-id={id || undefined}
                  aria-pressed={isFocused}
                  title={checkpointLabel(checkpoint, index)}
                  disabled={!id}
                  onClick={() => id && setFocusedCheckpointId(id)}
                  className={`session-tui-checkpoint-node ${isFocused ? 'is-focused' : ''}`}
                />
              );
            })}
          </div>
          <div className="session-tui-checkpoint-list">
            {checkpoints.map((checkpoint, index) => {
              const id = checkpointId(checkpoint);
              const isFocused = Boolean(id && id === focusedCheckpointId);
              return (
                <div key={id || index} className={`session-tui-checkpoint-row-shell ${isFocused ? 'is-focused' : ''}`}>
                  <button
                    type="button"
                    data-testid="session-checkpoint-row"
                    data-session-action="focus-checkpoint"
                    data-checkpoint-id={id || undefined}
                    aria-pressed={isFocused}
                    disabled={!id}
                    onClick={() => id && setFocusedCheckpointId(id)}
                    className="session-tui-checkpoint-row"
                  >
                    {checkpointLabel(checkpoint, index)}
                  </button>
                  {isFocused && id ? (
                    <div className="session-tui-checkpoint-actions">
                      <button
                        type="button"
                        data-testid="session-checkpoint-rewind-action"
                        data-session-command="rewind"
                        onClick={() => onRunCommand('rewind', { checkpoint_event_id: id })}
                      >
                        Rewind here
                      </button>
                      <button
                        type="button"
                        data-testid="session-checkpoint-branch-action"
                        data-session-command="branch"
                        onClick={() => onRunCommand('branch', { anchor_event_id: id })}
                      >
                        Branch here
                      </button>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
      {details.length > 0 ? (
        <dl className="session-tui-command-details">
          {details.map(([key, value]) => (
            <React.Fragment key={key}>
              <dt>{key}</dt>
              <dd>{value}</dd>
            </React.Fragment>
          ))}
        </dl>
      ) : null}
    </section>
  );
}

function InlinePlanCard({ agentId, planId }: { agentId: string; planId: string }) {
  const { t } = useTranslation();
  const { data: plan, isLoading, error, refetch } = useQuery({
    queryKey: ['agent-plan-inline', agentId, planId],
    queryFn: () => planApi.get(agentId, planId),
    enabled: !!agentId && !!planId,
    refetchInterval: 10000,
  });

  if (isLoading) {
    return (
      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
        {t('agent.plan.inlineLoading', 'Loading plan card...')}
      </div>
    );
  }

  if (error || !plan) {
    return (
      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
        {t('agent.plan.inlineLoadFailed', 'Plan card could not load. Open Aware > Plan Queue if it is still pending.')}
      </div>
    );
  }

  return <PlanCard agentId={agentId} plan={plan} onChanged={() => refetch()} dense />;
}

function formatArtifactSize(size: number | undefined): string | null {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) return null;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export type ArtifactOpenMode = 'inspector_preview' | 'download';

function getEffectiveArtifactPreviewKind(artifact: Pick<ChatArtifactPart, 'name' | 'path' | 'previewKind'>): string {
  const explicit = artifact.previewKind?.toLowerCase();
  if (explicit) return explicit;
  const suffix = (artifact.name || artifact.path).split('.').pop()?.toLowerCase() || '';
  if (['md', 'markdown'].includes(suffix)) return 'markdown';
  if (['txt', 'csv', 'json', 'jsonl', 'log', 'xml', 'yaml', 'yml'].includes(suffix)) return 'text';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(suffix)) return 'image';
  if (suffix === 'pdf') return 'pdf';
  return 'download';
}

export function getArtifactOpenMode(artifact: Pick<ChatArtifactPart, 'name' | 'path' | 'previewKind'>): ArtifactOpenMode {
  const previewKind = getEffectiveArtifactPreviewKind(artifact);
  if (previewKind && ['markdown', 'text', 'image', 'pdf'].includes(previewKind)) {
    return 'inspector_preview';
  }
  return 'download';
}

export function isPendingEmptyArtifactPreview(
  artifact: Partial<ChatArtifactPart>,
  content?: string,
  loading?: boolean,
  error?: string,
): boolean {
  if (loading || error) return false;
  return artifact.size === 0 && !String(content ?? '').trim();
}

function artifactWorkspaceAgentId(
  artifact: Pick<ChatArtifactPart, 'downloadAgentId' | 'ownerAgentId' | 'sourceAgentId'>,
  fallbackAgentId?: string | null,
): string | null {
  return artifact.downloadAgentId || artifact.ownerAgentId || artifact.sourceAgentId || fallbackAgentId || null;
}

function ArtifactPreviewPanel({
  preview,
  onClose,
  t,
}: {
  preview: ArtifactPreviewState;
  onClose: () => void;
  t: Translate;
}) {
  const previewKind = getEffectiveArtifactPreviewKind(preview.artifact);
  const showPendingEmptyPreview = isPendingEmptyArtifactPreview(
    preview.artifact,
    preview.content,
    preview.loading,
    preview.error,
  );
  const pendingEmptyPreview = (
    <div
      style={{
        border: '1px dashed var(--border-subtle)',
        borderRadius: '7px',
        padding: '14px',
        color: 'var(--text-tertiary)',
        fontSize: '12px',
        lineHeight: 1.6,
        background: 'var(--bg-primary)',
      }}
    >
      {t(
        'agent.chat.artifacts.emptyPending',
        'This file is empty for now. Its content will appear here after the session permission is approved or the write finishes.',
      )}
    </div>
  );
  return (
    <div
      data-testid="session-artifact-preview-inspector"
      role="complementary"
      aria-label={preview.artifact.name}
      style={{
        position: 'fixed',
        zIndex: 80,
        top: '76px',
        right: '16px',
        bottom: '112px',
        width: 'min(680px, calc(100vw - 32px))',
        display: 'flex',
        pointerEvents: 'none',
      }}
    >
      <section
        data-testid="session-artifact-inspector"
        style={{
          width: '100%',
          minHeight: 0,
          border: '1px solid var(--border-subtle)',
          borderRadius: '10px',
          background: 'var(--bg-secondary)',
          boxShadow: '0 18px 64px rgba(15, 23, 42, 0.24)',
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          pointerEvents: 'auto',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '9px 10px', borderBottom: '1px solid var(--border-subtle)' }}>
          <IconFileText size={15} color="var(--text-tertiary)" />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {preview.artifact.name}
            </div>
            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
              {preview.usingSnapshot
                ? t('agent.chat.artifacts.snapshotPreview', 'Previewing saved session snapshot')
                : preview.legacyCurrentFileFallback
                  ? t('agent.chat.artifacts.legacyCurrentFilePreview', 'Previewing current workspace file; no delivery snapshot exists')
                : t('agent.chat.artifacts.preview', 'Preview')}
              {preview.workspaceChanged ? ` · ${t('agent.chat.artifacts.workspaceChanged', 'Workspace file changed after delivery')}` : ''}
            </div>
          </div>
          {preview.url && (
            <a
              href={preview.url}
              target="_blank"
              rel="noreferrer"
              style={{ color: 'var(--text-tertiary)', display: 'inline-flex', alignItems: 'center' }}
              title={t('agent.chat.artifacts.openInNewTab', 'Open in new tab')}
            >
              <IconExternalLink size={14} />
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label={t('common.close', 'Close')}
            title={t('common.close', 'Close')}
            style={{
              border: '1px solid var(--border-subtle)',
              background: 'transparent',
              borderRadius: '6px',
              padding: '3px',
              color: 'var(--text-secondary)',
              cursor: 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <IconX size={13} />
          </button>
        </div>
        <div style={{ padding: '12px', overflow: 'auto', flex: 1, minHeight: 0 }}>
          {preview.loading ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('common.loading', 'Loading')}
            </div>
          ) : preview.error ? (
            <div style={{ fontSize: '12px', color: 'var(--danger-text)' }}>
              {preview.error}
            </div>
          ) : previewKind === 'image' && preview.url ? (
            <img
              src={preview.url}
              alt={preview.artifact.name}
              style={{ maxWidth: '100%', maxHeight: '62vh', borderRadius: '6px', border: '1px solid var(--border-subtle)' }}
            />
          ) : previewKind === 'pdf' && preview.url ? (
            <iframe
              title={preview.artifact.name}
              src={preview.url}
              style={{ width: '100%', height: '62vh', border: '1px solid var(--border-subtle)', borderRadius: '6px' }}
            />
          ) : previewKind === 'markdown' ? (
            <div style={{ fontSize: '12px', lineHeight: 1.6 }}>
              {showPendingEmptyPreview ? pendingEmptyPreview : <MarkdownRenderer content={preview.content || ''} />}
            </div>
          ) : (
            <pre
              style={{
                margin: 0,
                fontSize: '11px',
                lineHeight: 1.55,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                color: 'var(--text-secondary)',
              }}
            >
              {showPendingEmptyPreview ? t(
                'agent.chat.artifacts.emptyPending',
                'This file is empty for now. Its content will appear here after the session permission is approved or the write finishes.',
              ) : preview.content || ''}
            </pre>
          )}
        </div>
      </section>
    </div>
  );
}

function ArtifactCards({
  agentId,
  artifacts,
  onOpenArtifact,
}: {
  agentId?: string | null;
  artifacts?: ChatArtifactPart[];
  onOpenArtifact?: (artifact: ChatArtifactPart) => void;
}) {
  const { t } = useTranslation();
  const visibleArtifacts = (artifacts || []).filter((artifact) => artifact.path);
  if (!agentId || visibleArtifacts.length === 0) return null;

  return (
    <div style={{ display: 'grid', gap: '6px', marginTop: '8px' }}>
      {visibleArtifacts.map((artifact) => {
        const downloadAgentId = artifactWorkspaceAgentId(artifact, agentId);
        const href = downloadAgentId
          ? (artifact.id ? fileApi.artifactDownloadUrl(downloadAgentId, artifact.id) : fileApi.downloadUrl(downloadAgentId, artifact.path))
          : '#';
        const size = formatArtifactSize(artifact.size);
        const openArtifact = () => onOpenArtifact?.(artifact);
        return (
          <div
            key={`${artifact.id || artifact.path}`}
            role="button"
            tabIndex={0}
            data-testid="chat-artifact-row-open"
            aria-label={t('agent.chat.artifacts.openNamed', 'Open {{name}}', { name: artifact.name })}
            onClick={openArtifact}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openArtifact();
              }
            }}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '8px',
              borderRadius: '8px',
              border: '1px solid var(--border-subtle)',
              background: 'var(--bg-elevated)',
              minWidth: 0,
              cursor: onOpenArtifact ? 'pointer' : 'default',
            }}
          >
            <IconFileText size={16} color="var(--text-tertiary)" style={{ flexShrink: 0 }} />
            <div style={{ minWidth: 0, flex: 1 }}>
              <div
                style={{
                  fontSize: '12px',
                  fontWeight: 600,
                  color: 'var(--text-primary)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {artifact.name}
              </div>
              <div
                style={{
                  fontSize: '10px',
                  color: 'var(--text-tertiary)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {[artifact.previewKind, size].filter(Boolean).join(' · ') || artifact.path}
              </div>
            </div>
            <button
              type="button"
              data-testid="chat-artifact-open"
              onClick={(event) => {
                event.stopPropagation();
                openArtifact();
              }}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                padding: 0,
                border: 'none',
                background: 'transparent',
                fontSize: '11px',
                color: 'var(--accent-primary)',
                cursor: 'pointer',
                flexShrink: 0,
              }}
            >
              <IconExternalLink size={13} />
              {t('agent.chat.artifacts.open', 'Open')}
            </button>
            <a
              href={href}
              download={artifact.name}
              onClick={(event) => event.stopPropagation()}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                fontSize: '11px',
                color: 'var(--text-secondary)',
                textDecoration: 'none',
                flexShrink: 0,
              }}
            >
              <IconDownload size={13} />
              {t('agent.chat.artifacts.download', 'Download')}
            </a>
          </div>
        );
      })}
    </div>
  );
}

type RuntimeRecord = Record<string, unknown>;

function isRuntimeRecord(value: unknown): value is RuntimeRecord {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function sessionMetadata(session: any): Record<string, unknown> {
  const metadata = session?.transcript_metadata_json ?? session?.metadata_json ?? session?.metadata;
  return isRuntimeRecord(metadata) ? metadata : {};
}

function isTeamMemberSession(session: any): boolean {
  if (!session) return false;
  const metadata = sessionMetadata(session);
  return (
    stringValue(session.session_kind).toLowerCase() === 'team_member'
    || stringValue(session.runtime_source).toLowerCase() === 'team_member'
    || stringValue(session.source_channel).toLowerCase() === 'agent_team'
    || Boolean(metadata.team_id && metadata.member_name)
  );
}

function teamMemberSessionLabel(session: any): string {
  const metadata = sessionMetadata(session);
  return (
    stringValue(metadata.member_name)
    || stringValue(session.member_name)
    || stringValue(session.title).split('/').pop()?.trim()
    || stringValue(session.id)
  );
}

function teamMemberRoleLabel(session: any): string {
  const metadata = sessionMetadata(session);
  return stringValue(metadata.member_role || session.member_role);
}

function stringValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function SessionRuntimePanel({
  messages,
  sessionWorkbench,
  activeSession,
  activeRunStatus,
  collapsed = false,
  onToggleCollapsed,
  onOpenDocument,
  onSelectSession,
  onSelectWorkflowRun,
}: {
  messages: AgentChatMessage[];
  sessionWorkbench: SessionWorkbench | null;
  activeSession?: Record<string, unknown> | null;
  activeRunStatus?: string | null;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onOpenDocument?: (artifact: ChatArtifactPart) => void | Promise<unknown>;
  onSelectSession?: (sessionId: string) => void | Promise<unknown>;
  onSelectWorkflowRun?: (workflow: RuntimeSectionItemModel) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const rightPanel = buildSessionRightPanelModel({
    messages,
    sessionWorkbench: sessionWorkbench as Record<string, unknown> | null,
    activeSession,
    activeRunStatus,
  });
  const docs = rightPanel.workspaceDocuments;
  const runtimeSections = rightPanel.runtimeSections;
  const collaborationCount = rightPanel.runtimeMetrics.runningCount > 0
    ? `Running ${rightPanel.runtimeMetrics.runningCount}`
    : `${rightPanel.runtimeMetrics.totalCount} total`;
  const metricSummary = (item: RuntimeSectionItemModel): string => [
    item.metrics.elapsedLabel,
    item.metrics.tokenLabel ? `${item.metrics.tokenLabel} tokens` : null,
    item.metrics.toolUseLabel ? `${item.metrics.toolUseLabel} tools` : null,
  ].filter(Boolean).join(' · ');

  const renderRuntimeItem = (item: RuntimeSectionItemModel, fallback: string) => {
    const sessionId = item.childSessionId;
    const clickable = Boolean(item.enterable && sessionId && onSelectSession);
    const meta = [item.id, item.runtimeKind, item.summary, sessionId ? `session:${sessionId}` : '', metricSummary(item)].filter(Boolean).join(' · ');
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{item.label || fallback}</span>
          <span className="session-runtime-row-meta">{meta || item.id}</span>
        </span>
        <span className="session-runtime-status">{item.status || 'unknown'}</span>
      </>
    );
    return clickable ? (
      <button
        key={item.id}
        type="button"
        className="session-runtime-row session-runtime-row-button"
        onClick={() => sessionId && onSelectSession?.(sessionId)}
      >
        {content}
      </button>
    ) : (
      <div key={item.id} className="session-runtime-row">
        {content}
      </div>
    );
  };

  const renderWorkflowRoot = (workflow: RuntimeSectionItemModel, fallback: string) => {
    const meta = [workflow.id, workflow.runtimeKind, workflow.summary, metricSummary(workflow)].filter(Boolean).join(' · ');
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{workflow.label || fallback}</span>
          <span className="session-runtime-row-meta">{meta || workflow.id}</span>
        </span>
        <span className="session-runtime-status">{workflow.status || 'unknown'}</span>
      </>
    );
    return onSelectWorkflowRun ? (
      <button
        key={`${workflow.id}:root`}
        type="button"
        data-testid="session-runtime-workflow-open"
        data-runtime-action="open-workflow-run"
        className="session-runtime-row session-runtime-row-button"
        onClick={() => onSelectWorkflowRun(workflow)}
      >
        {content}
      </button>
    ) : (
      <div key={`${workflow.id}:root`} className="session-runtime-row">
        {content}
      </div>
    );
  };

  const renderTeamMemberItem = (member: RuntimeSectionItemModel, fallback: string) => {
    const sessionId = member.childSessionId;
    const canEnter = Boolean(member.enterable && sessionId && onSelectSession);
    const actionButton = (
      action: 'enter' | 'send' | 'resume' | 'close',
      label: string,
      disabled: boolean,
      title: string,
      onClick?: () => void,
    ) => (
      <button
        key={action}
        type="button"
        data-runtime-action={`agent-team-member-${action}`}
        className="session-runtime-action-button"
        disabled={disabled}
        title={title}
        onClick={onClick}
        style={{
          minHeight: '24px',
          padding: '2px 7px',
          borderRadius: '6px',
          border: '1px solid var(--border-subtle)',
          background: disabled ? 'var(--bg-secondary)' : 'var(--bg-primary)',
          color: disabled ? 'var(--text-tertiary)' : 'var(--text-secondary)',
          fontSize: '11px',
          cursor: disabled ? 'not-allowed' : 'pointer',
        }}
      >
        {label}
      </button>
    );
    const meta = [member.id, member.runtimeKind, member.summary, sessionId ? `session:${sessionId}` : '', metricSummary(member)].filter(Boolean).join(' · ');
    return (
      <div key={member.id || fallback} className="session-runtime-row" data-testid="session-agent-team-member-row">
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{member.label || fallback}</span>
          <span className="session-runtime-row-meta">{meta || member.id}</span>
        </span>
        <span className="session-runtime-status">{member.status || 'unknown'}</span>
        <span
          data-testid="session-agent-team-member-actions"
          style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', flexWrap: 'wrap', justifyContent: 'flex-end' }}
        >
          {actionButton(
            'enter',
            t('sessionWorkbench.rightPanel.memberEnter', 'Enter'),
            !canEnter,
            canEnter
              ? t('sessionWorkbench.rightPanel.memberEnterTitle', 'Open member session')
              : t('sessionWorkbench.rightPanel.memberEnterDisabled', 'No member session is available'),
            canEnter && sessionId ? () => onSelectSession?.(sessionId) : undefined,
          )}
          {actionButton(
            'send',
            t('sessionWorkbench.rightPanel.memberSend', 'Send'),
            true,
            t('sessionWorkbench.rightPanel.memberSendDisabled', 'Enter the member session to send a follow-up from its composer'),
          )}
          {actionButton(
            'resume',
            t('sessionWorkbench.rightPanel.memberResume', 'Resume'),
            true,
            t('sessionWorkbench.rightPanel.memberResumeDisabled', 'Resume is available from the member session active-run controls'),
          )}
          {actionButton(
            'close',
            t('sessionWorkbench.rightPanel.memberClose', 'Close'),
            true,
            t('sessionWorkbench.rightPanel.memberCloseDisabled', 'Team close is available from the Agent Team control surface'),
          )}
        </span>
      </div>
    );
  };

  const renderRuntimeSection = (
    testId: string,
    title: string,
    items: RuntimeSectionItemModel[],
    empty: string,
    renderItem?: (item: RuntimeSectionItemModel, index: number) => React.ReactNode,
  ) => (
    <div className="session-runtime-card" data-testid={testId}>
      <div className="session-runtime-card-title">{title}</div>
      {items.length === 0 ? (
        <div className="session-runtime-empty">{empty}</div>
      ) : (
        items.map((item, index) => renderItem?.(item, index) ?? renderRuntimeItem(item, `${title}-${index + 1}`))
      )}
    </div>
  );

  const renderDocumentRow = (doc: WorkspaceDocumentModel) => (
    <button
      key={doc.key}
      type="button"
      className="session-runtime-doc-row"
      onClick={() => onOpenDocument?.(doc.artifact)}
    >
      <IconFileText size={15} />
      <span>
        <strong>{doc.name}</strong>
        <small>{[doc.previewKind, doc.status, formatArtifactSize(doc.size), doc.path].filter(Boolean).join(' · ')}</small>
      </span>
    </button>
  );

  const renderDocumentGroup = (group: WorkspaceDocumentGroupModel, testId: string) => {
    if (group.items.length === 0) return null;
    const title = t(`sessionWorkbench.rightPanel.documentGroups.${group.key}`, group.title);
    const body = <div className="session-runtime-list">{group.items.map(renderDocumentRow)}</div>;
    if (group.collapsedByDefault) {
      return (
        <details key={group.key} data-testid={testId} className="session-runtime-doc-group">
          <summary>
            <span>{title}</span>
            <strong>{group.items.length}</strong>
          </summary>
          {body}
        </details>
      );
    }
    return (
      <div key={group.key} data-testid={testId} className="session-runtime-doc-group is-open">
        <div className="session-runtime-doc-group-title">
          <span>{title}</span>
          <strong>{group.items.length}</strong>
        </div>
        {body}
      </div>
    );
  };

  const sessionWindow = rightPanel.sessionWindow;

  if (collapsed) {
    return (
      <aside data-testid="session-runtime-panel" className="session-runtime-panel is-collapsed">
        <button
          type="button"
          data-testid="session-runtime-collapse-toggle"
          className="session-runtime-collapse-toggle"
          aria-label={t('sessionWorkbench.rightPanel.expandRuntime', 'Expand runtime panel')}
          title={t('sessionWorkbench.rightPanel.expandRuntime', 'Expand runtime panel')}
          onClick={onToggleCollapsed}
        >
          <IconChevronLeft size={15} stroke={1.8} />
        </button>
        <div className="session-runtime-collapsed-label">
          {t('sessionWorkbench.rightPanel.workspace', 'Workspace')}
        </div>
      </aside>
    );
  }

  return (
    <aside data-testid="session-runtime-panel" className="session-runtime-panel">
      <button
        type="button"
        data-testid="session-runtime-collapse-toggle"
        className="session-runtime-collapse-toggle"
        aria-label={t('sessionWorkbench.rightPanel.collapseRuntime', 'Collapse runtime panel')}
        title={t('sessionWorkbench.rightPanel.collapseRuntime', 'Collapse runtime panel')}
        onClick={onToggleCollapsed}
      >
        <IconChevronRight size={15} stroke={1.8} />
      </button>
      <section
        className="session-runtime-section session-runtime-documents"
        aria-label={t('sessionWorkbench.rightPanel.sessionArtifacts', 'Session artifacts')}
      >
        <div className="session-runtime-section-header">
          <div>
            <div className="session-tui-kicker">{t('sessionWorkbench.rightPanel.session', 'Session')}</div>
            <h3>{t('sessionWorkbench.rightPanel.sessionArtifacts', 'Session artifacts')}</h3>
          </div>
          <span>{docs.total}</span>
        </div>
        {docs.total === 0 ? (
          <div className="session-runtime-empty">
            {t('sessionWorkbench.rightPanel.noSessionArtifacts', 'No delivered artifacts in this session yet.')}
          </div>
        ) : (
          <div className="session-runtime-doc-groups">
            {renderDocumentGroup(docs.currentSession, 'session-workspace-documents-current')}
            {renderDocumentGroup(docs.historical, 'session-workspace-documents-historical')}
            {renderDocumentGroup(docs.unattributed, 'session-workspace-documents-unattributed')}
          </div>
        )}
      </section>

      <div data-testid="session-runtime-divider" className="session-runtime-divider" aria-hidden="true" />

      <section
        data-testid="session-runtime-collaboration"
        className="session-runtime-section session-runtime-lower"
        aria-label={t('sessionWorkbench.rightPanel.collaboration', 'Collaboration')}
      >
        <div className="session-runtime-section-header">
          <div>
            <div className="session-tui-kicker">{t('sessionWorkbench.rightPanel.session', 'Session')}</div>
            <h3>{t('sessionWorkbench.rightPanel.collaboration', 'Collaboration')}</h3>
          </div>
          <span>{collaborationCount}</span>
        </div>

        {sessionWindow && (
          <div data-testid="session-runtime-main-row" className="session-runtime-card">
            <div className="session-runtime-card-title">{t('sessionWorkbench.rightPanel.mainSession', 'Main session')}</div>
            <div className="session-runtime-row">
              <span className="session-runtime-row-main">
                <span className="session-runtime-row-title">{sessionWindow.label}</span>
                <span className="session-runtime-row-meta">
                  {[sessionWindow.kind, sessionWindow.sessionId ? `session:${sessionWindow.sessionId}` : '', sessionWindow.metrics.lastActivityLabel]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              <span className="session-runtime-status">{sessionWindow.status}</span>
            </div>
          </div>
        )}

        <div data-testid="session-runtime-metrics" className="session-runtime-card">
          <div className="session-runtime-card-title">{t('sessionWorkbench.rightPanel.runtimeMetrics', 'Runtime metrics')}</div>
          <div className="session-runtime-metric-row">
            <span>{t('sessionWorkbench.rightPanel.runningSummary', 'Running')}</span>
            <strong>{rightPanel.runtimeMetrics.runningCount}</strong>
          </div>
          <div className="session-runtime-metric-row">
            <span>{t('sessionWorkbench.rightPanel.elapsed', 'Elapsed')}</span>
            <strong>{rightPanel.runtimeMetrics.elapsedLabel || '-'}</strong>
          </div>
          <div className="session-runtime-metric-row">
            <span>{t('sessionWorkbench.rightPanel.tokens', 'Tokens')}</span>
            <strong>{rightPanel.runtimeMetrics.tokenLabel || '-'}</strong>
          </div>
          <div className="session-runtime-metric-row">
            <span>{t('sessionWorkbench.rightPanel.tools', 'Tools')}</span>
            <strong>{rightPanel.runtimeMetrics.toolUseLabel || '-'}</strong>
          </div>
        </div>

        {renderRuntimeSection(
          'session-runtime-agent-teams',
          t('sessionWorkbench.rightPanel.agentTeams', 'Agent Teams'),
          runtimeSections.agentTeams,
          t('sessionWorkbench.rightPanel.noAgentTeams', 'No Agent Team containers in this session.'),
          (team) => (
            <div key={team.id} className="session-runtime-team">
              <div className="session-runtime-team-header">
                <span>{team.label || team.id}</span>
                <small>{team.status || 'unknown'}</small>
              </div>
              {team.members.length === 0 ? (
                <div className="session-runtime-empty">
                  {t('sessionWorkbench.rightPanel.noTeamMembers', 'No team members have started yet.')}
                </div>
              ) : (
                team.members.map((member, index) => renderTeamMemberItem(member, `team-member-${index + 1}`))
              )}
            </div>
          ),
        )}

        {renderRuntimeSection(
          'session-runtime-subagents',
          t('sessionWorkbench.rightPanel.subagents', 'Sub-agents'),
          runtimeSections.subagents,
          t('sessionWorkbench.rightPanel.noSubagents', 'No one-shot Sub-agent workers in this session.'),
        )}

        {renderRuntimeSection(
          'session-runtime-workflows',
          t('sessionWorkbench.rightPanel.dynamicWorkflows', 'Dynamic Workflow'),
          runtimeSections.workflows,
          t('sessionWorkbench.rightPanel.noWorkflows', 'No active workflows.'),
          (workflow, index) => (
            <div key={workflow.id} className="session-runtime-team">
              {renderWorkflowRoot(workflow, `workflow-${index + 1}`)}
              {workflow.steps.map((step, stepIndex) => renderRuntimeItem(step, `workflow-step-${stepIndex + 1}`))}
              {workflow.leafCalls.map((leafCall, leafIndex) => renderRuntimeItem(leafCall, `workflow-leaf-${leafIndex + 1}`))}
            </div>
          ),
        )}

        {renderRuntimeSection(
          'session-runtime-background',
          t('sessionWorkbench.rightPanel.backgroundAgents', 'Background agents'),
          runtimeSections.background,
          t('sessionWorkbench.rightPanel.noBackgroundAgents', 'No background agents running.'),
        )}

        {renderRuntimeSection(
          'session-runtime-notifications',
          t('sessionWorkbench.rightPanel.notifications', 'Notifications'),
          runtimeSections.notifications,
          t('sessionWorkbench.rightPanel.noCompletionWakes', 'No completion notifications.'),
        )}

        {renderRuntimeSection(
          'session-runtime-runs',
          t('sessionWorkbench.rightPanel.runs', 'Runs'),
          runtimeSections.runs,
          t('sessionWorkbench.rightPanel.noRuns', 'No runtime runs recorded.'),
        )}

        {renderRuntimeSection(
          'session-runtime-raw',
          t('sessionWorkbench.rightPanel.rawEvents', 'Raw'),
          runtimeSections.raw,
          t('sessionWorkbench.rightPanel.noRawEvents', 'No raw runtime events.'),
        )}
      </section>
    </aside>
  );
}

export function WorkflowRunFocusPanel({
  workflow,
  onClose,
  onSelectSession,
}: {
  workflow: RuntimeSectionItemModel;
  onClose: () => void;
  onSelectSession?: (sessionId: string) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const windowModel = buildWorkflowRunWindowModel(workflow);
  const leafCalls = windowModel.leafCalls || [];
  const steps = windowModel.steps || [];
  const meta = [
    windowModel.meta,
    windowModel.metrics.elapsedLabel,
    windowModel.metrics.tokenLabel ? `${windowModel.metrics.tokenLabel} tokens` : '',
    windowModel.metrics.toolUseLabel ? `${windowModel.metrics.toolUseLabel} tools` : '',
  ].filter(Boolean).join(' · ');
  const renderStep = (step: RuntimeSectionItemModel, index: number) => (
    <div
      key={step.id || `step-${index}`}
      data-testid="session-workflow-step-row"
      className="session-runtime-row"
    >
      <span className="session-runtime-row-main">
        <span className="session-runtime-row-title">
          {step.label || t('sessionWorkbench.workflowRunWindow.stepFallback', 'Step {{index}}', { index: index + 1 })}
        </span>
        <span className="session-runtime-row-meta">
          {[step.id, step.runtimeKind, step.summary].filter(Boolean).join(' · ') || step.id}
        </span>
      </span>
      <span className="session-runtime-status">{step.status || 'unknown'}</span>
    </div>
  );
  const renderLeaf = (leaf: RuntimeSectionItemModel, index: number) => {
    const canEnter = Boolean(leaf.enterable && leaf.childSessionId && onSelectSession);
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">
            {leaf.label || t('sessionWorkbench.workflowRunWindow.leafFallback', 'Leaf {{index}}', { index: index + 1 })}
          </span>
          <span className="session-runtime-row-meta">
            {[leaf.id, leaf.runtimeKind, leaf.summary, leaf.childSessionId ? `session:${leaf.childSessionId}` : '']
              .filter(Boolean)
              .join(' · ') || leaf.id}
          </span>
        </span>
        <span className="session-runtime-status">{leaf.status || 'unknown'}</span>
      </>
    );
    return canEnter ? (
      <button
        key={leaf.id || `leaf-${index}`}
        type="button"
        data-testid="session-workflow-leaf-enter"
        className="session-runtime-row session-runtime-row-button"
        onClick={() => leaf.childSessionId && onSelectSession?.(leaf.childSessionId)}
      >
        {content}
      </button>
    ) : (
      <div
        key={leaf.id || `leaf-${index}`}
        data-testid="session-workflow-leaf-detail"
        className="session-runtime-row"
      >
        {content}
      </div>
    );
  };

  return (
    <section
      data-testid="session-workflow-run-window"
      className="session-runtime-section session-runtime-lower"
      aria-label={t('sessionWorkbench.workflowRunWindow.title', 'Workflow Run Window')}
    >
      <div className="session-runtime-section-header">
        <div>
          <div className="session-tui-kicker">
            {t('sessionWorkbench.workflowRunWindow.breadcrumb', windowModel.breadcrumb)}
          </div>
          <h3>{windowModel.label}</h3>
          {meta ? <small>{meta}</small> : null}
        </div>
        <button
          type="button"
          className="session-runtime-collapse-toggle"
          aria-label={t('sessionWorkbench.workflowRunWindow.close', 'Close workflow run')}
          title={t('sessionWorkbench.workflowRunWindow.close', 'Close workflow run')}
          onClick={onClose}
        >
          <IconX size={15} stroke={1.8} />
        </button>
      </div>

      <div className="session-runtime-card">
        <div className="session-runtime-card-title">
          {t('sessionWorkbench.workflowRunWindow.steps', 'Steps')}
        </div>
        {steps.length === 0 ? (
          <div className="session-runtime-empty">
            {t('sessionWorkbench.workflowRunWindow.noSteps', 'No workflow steps recorded.')}
          </div>
        ) : (
          steps.map(renderStep)
        )}
      </div>

      <div className="session-runtime-card">
        <div className="session-runtime-card-title">
          {t('sessionWorkbench.workflowRunWindow.leafCalls', 'Leaf calls')}
        </div>
        {leafCalls.length === 0 ? (
          <div className="session-runtime-empty">
            {t('sessionWorkbench.workflowRunWindow.noLeafCalls', 'No leaf calls recorded.')}
          </div>
        ) : (
          leafCalls.map(renderLeaf)
        )}
      </div>
    </section>
  );
}

function MessageBranchActions({
  message,
  checkpointMessage,
  isLeft,
  onBranchMessage,
  onFeedbackMessage,
  onRewindMessage,
}: {
  message: AgentChatMessage;
  checkpointMessage?: AgentChatMessage | null;
  isLeft: boolean;
  onBranchMessage?: (message: AgentChatMessage) => void | Promise<unknown>;
  onFeedbackMessage?: (message: AgentChatMessage, label: RecordSessionFeedbackInput['label']) => void | Promise<unknown>;
  onRewindMessage?: (message: AgentChatMessage) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  if (!message.id) return null;
  if (message.role !== 'assistant') return null;
  const checkpointReady = Boolean(checkpointMessage?.transcriptEventId);

  const actions: Array<{
    key: string;
    label: string;
    testId: string;
    icon: React.ReactNode;
    onClick: () => void;
    disabled?: boolean;
  }> = [
    {
      key: 'like',
      label: t('agent.chat.actions.like', 'Like'),
      testId: 'message-action-like',
      icon: <IconThumbUp size={12} />,
      disabled: !onFeedbackMessage,
      onClick: () => void onFeedbackMessage?.(message, 'useful'),
    },
    {
      key: 'dislike',
      label: t('agent.chat.actions.dislike', 'Dislike'),
      testId: 'message-action-dislike',
      icon: <IconThumbDown size={12} />,
      disabled: !onFeedbackMessage,
      onClick: () => void onFeedbackMessage?.(message, 'misleading'),
    },
    {
      key: 'branch',
      label: t('agent.chat.actions.branch', 'Branch'),
      testId: 'message-action-branch',
      icon: <IconGitBranch size={12} />,
      disabled: !onBranchMessage || !checkpointReady,
      onClick: () => {
        if (!checkpointMessage) return;
        void onBranchMessage?.(checkpointMessage);
      },
    },
    {
      key: 'rewind',
      label: t('agent.chat.actions.rewind', 'Rewind'),
      testId: 'message-action-rewind',
      icon: <IconHistory size={12} />,
      disabled: !onRewindMessage || !checkpointReady,
      onClick: () => {
        if (!checkpointMessage) return;
        void onRewindMessage?.(checkpointMessage);
      },
    },
  ];

  return (
    <div
      style={{
        display: 'inline-flex',
        gap: '4px',
        marginLeft: isLeft ? '6px' : 0,
        marginRight: isLeft ? 0 : '6px',
        flexWrap: 'wrap',
        justifyContent: isLeft ? 'flex-start' : 'flex-end',
      }}
    >
      {actions.map((action) => (
        <button
          key={action.key}
          type="button"
          data-testid={action.testId}
          aria-label={action.label}
          title={action.label}
          disabled={action.disabled}
          onClick={action.onClick}
          style={{
            border: '1px solid var(--border-subtle)',
            background: 'transparent',
            color: action.disabled ? 'var(--text-disabled)' : 'var(--text-tertiary)',
            borderRadius: '4px',
            fontSize: '10px',
            lineHeight: 1,
            padding: '3px',
            width: '20px',
            height: '20px',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: action.disabled ? 'not-allowed' : 'pointer',
          }}
        >
          {action.icon}
        </button>
      ))}
    </div>
  );
}

function previousUserCheckpointForMessage(messages: AgentChatMessage[], index: number): AgentChatMessage | null {
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const candidate = messages[cursor];
    if (candidate?.role === 'user' && candidate.transcriptEventId) return candidate;
  }
  return null;
}

function RawToolResultBlock({ text }: { text: string }) {
  return (
    <div
      style={{
        color: 'var(--text-secondary)',
        fontSize: '11px',
        fontFamily: 'var(--font-mono)',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        maxHeight: '240px',
        overflow: 'auto',
        background: 'rgba(0,0,0,0.15)',
        borderRadius: '4px',
        padding: '4px 6px',
      }}
    >
      {text}
    </div>
  );
}

function StructuredToolSection({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <div style={{ fontSize: '10px', fontWeight: 700, color: 'var(--text-tertiary)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {label}
      </div>
      <ul style={{ margin: 0, paddingLeft: '16px', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
        {items.map((item) => (
          <li key={`${label}-${item}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export function StructuredToolResultBody({
  toolName,
  toolMeta,
  toolResult,
  toolRawResult,
  agentId,
  agentName,
  submitted = false,
  onSendMessage,
  onEnterPlanMode,
}: StructuredToolResultBodyProps) {
  const { t } = useTranslation();
  const rawText = typeof toolRawResult === 'string' && toolRawResult.trim() ? toolRawResult : '';

  if (!toolMeta) {
    return toolResult ? <RawToolResultBlock text={toolResult} /> : null;
  }

  if (toolMeta.kind === 'user_clarification') {
    if (!onSendMessage) {
      // No send path available (e.g. read-only history view) — fall back to a
      // static rendering with the questions but no interactive submit.
      return (
        <AskUserQuestionCard
          questions={toolMeta.questions}
          blocking={toolMeta.blocking}
          nextAction={toolMeta.nextAction}
          onSubmit={() => undefined}
          submitted
          dense
        />
      );
    }
    return (
      <AskUserQuestionCard
        questions={toolMeta.questions}
        blocking={toolMeta.blocking}
        nextAction={toolMeta.nextAction}
        onSubmit={(answerText) => onSendMessage(answerText)}
        submitted={submitted}
        dense
      />
    );
  }

  if (toolMeta.kind === 'plan_mode_request') {
    // CC EnterPlanMode parity: the agent requested Plan Mode; the user is the gate.
    // Approve → onEnterPlanMode sends the reason with plan_mode_requested=true so
    // the existing entry path activates Plan Mode. Decline → a normal message so
    // the agent continues without Plan Mode. With no send path (read-only history
    // view) render a static, already-decided card.
    if (!onEnterPlanMode || !onSendMessage) {
      return (
        <PlanModeRequestCard
          agentName={agentName}
          reason={toolMeta.reason}
          onApprove={() => undefined}
          onDecline={() => undefined}
          submitted
          dense
        />
      );
    }
    return (
      <PlanModeRequestCard
        agentName={agentName}
        reason={toolMeta.reason}
        onApprove={() => onEnterPlanMode(toolMeta.reason)}
        onDecline={() => onSendMessage(t('agent.plan.request.declineMessage', 'Continue without entering Plan Mode.'))}
        dense
      />
    );
  }

  if (toolMeta.kind === 'plan_proposal') {
    // CC-align §4.5: render the REAL plan by id (InlinePlanCard fetches it and
    // refetches), NOT a synthetic card hardcoded to awaiting_confirmation — the
    // synthetic one left a stale confirm button after the user confirmed, so a
    // re-click hit the backend's 409 "cannot confirm a confirmed plan".
    return (
      <div style={{ display: 'grid', gap: '8px' }}>
        {agentId && toolMeta.planId ? (
          <InlinePlanCard agentId={agentId} planId={toolMeta.planId} />
        ) : (
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            {toolMeta.summary || t('agent.plan.needsConfirmation', 'A plan needs your confirmation.')}
          </div>
        )}
      </div>
    );
  }

  if (toolMeta.kind === 'dynamic_workflow_proposal') {
    return (
      <div style={{ display: 'grid', gap: '8px' }}>
        <div style={{ display: 'grid', gap: '4px' }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--accent-text)' }}>
            {t('agent.chat.toolResults.dynamicWorkflowProposalTitle', 'Dynamic Workflow Proposal')}
          </div>
          {toolMeta.goal && (
            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>{toolMeta.goal}</div>
          )}
          {toolMeta.whyWorkflow && (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{toolMeta.whyWorkflow}</div>
          )}
        </div>
        <StructuredToolSection
          label={t('agent.chat.toolResults.successCriteria', 'Success Criteria')}
          items={toolMeta.successCriteria}
        />
        <div style={{ display: 'grid', gap: '6px' }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)' }}>
            {t('agent.chat.toolResults.candidates', 'Candidates')}
          </div>
          {toolMeta.candidates.map((candidate) => {
            const recommended = candidate.candidateId === toolMeta.recommendedCandidateId;
            const facts = [
              candidate.patternMix.length ? candidate.patternMix.join(', ') : '',
              candidate.riskLevel ? `${t('agent.chat.toolResults.risk', 'Risk')}: ${candidate.riskLevel}` : '',
              candidate.plannedLeafCalls != null
                ? `${t('agent.chat.toolResults.leafCalls', 'Leaf calls')}: ${candidate.plannedLeafCalls}`
                : '',
              candidate.budgetTokens != null
                ? `${t('agent.chat.toolResults.budgetTokens', 'Budget tokens')}: ${candidate.budgetTokens}`
                : '',
              candidate.confirmationRequired
                ? t('agent.chat.toolResults.confirmationRequired', 'Confirmation required')
                : '',
            ].filter(Boolean);
            return (
              <div
                key={candidate.candidateId}
                style={{
                  display: 'grid',
                  gap: '3px',
                  padding: '8px',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: '6px',
                }}
              >
                <div style={{ display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' }}>
                  <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                    {candidate.name || candidate.candidateId}
                  </span>
                  {recommended && (
                    <span style={{ fontSize: '11px', color: 'var(--accent-text)' }}>
                      {t('agent.chat.toolResults.recommended', 'Recommended')}
                    </span>
                  )}
                </div>
                {facts.length > 0 && (
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{facts.join(' · ')}</div>
                )}
              </div>
            );
          })}
        </div>
        {toolMeta.nextAction && (
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{toolMeta.nextAction}</div>
        )}
      </div>
    );
  }

  if (toolMeta.kind === 'hr_preview') {
    const showRawOutput = rawText.length > 0 && rawText !== toolResult;
    return (
      <div style={{ display: 'grid', gap: '8px' }}>
        <div style={{ display: 'grid', gap: '4px' }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--accent-text)' }}>
            {t('agent.chat.toolResults.blueprintPreviewTitle', 'Agent Blueprint Preview')}
          </div>
          {toolMeta.name && <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>{toolMeta.name}</div>}
          {toolMeta.mission && (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              <strong>{t('agent.chat.toolResults.mission', 'Mission')}:</strong> {toolMeta.mission}
            </div>
          )}
          {toolMeta.firstMission && (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              <strong>{t('agent.chat.toolResults.firstMission', 'First Mission')}:</strong> {toolMeta.firstMission}
            </div>
          )}
        </div>
        <StructuredToolSection label={t('agent.chat.toolResults.readyNow', 'Ready Now')} items={toolMeta.readyNow} />
        <StructuredToolSection label={t('agent.chat.toolResults.willInstall', 'Will Install')} items={toolMeta.willInstall} />
        <StructuredToolSection
          label={t('agent.chat.toolResults.deferredCapabilities', 'Deferred Capabilities')}
          items={toolMeta.deferredCapabilities}
        />
        <StructuredToolSection label={t('agent.chat.toolResults.warnings', 'Warnings')} items={toolMeta.warnings} />
        <StructuredToolSection label={t('agent.chat.toolResults.manualSteps', 'Manual Steps')} items={toolMeta.manualSteps} />
        {showRawOutput && (
          <details>
            <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)' }}>
              {t('agent.chat.toolResults.rawOutput', 'Raw output')}
            </summary>
            <div style={{ marginTop: '6px' }}>
              <RawToolResultBlock text={rawText} />
            </div>
          </details>
        )}
      </div>
    );
  }

  if (toolMeta.kind === 'runtime_step') {
    return toolResult ? <RawToolResultBlock text={toolResult} /> : null;
  }

  const showRawOutput = rawText.length > 0 && rawText !== toolMeta.message;
  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <div style={{ display: 'grid', gap: '4px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--accent-text)' }}>
          {t('agent.chat.toolResults.createdTitle', 'Digital Employee Created')}
        </div>
        <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{toolMeta.message}</div>
        {toolMeta.agentName && (
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
            <strong>{t('agent.chat.toolResults.agentName', 'Agent')}:</strong> {toolMeta.agentName}
          </div>
        )}
      </div>
      <StructuredToolSection label={t('agent.chat.toolResults.warnings', 'Warnings')} items={toolMeta.warnings} />
      <StructuredToolSection label={t('agent.chat.toolResults.manualSteps', 'Manual Steps')} items={toolMeta.manualSteps} />
      {showRawOutput && (
        <details>
          <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)' }}>
            {t('agent.chat.toolResults.rawOutput', 'Raw output')}
          </summary>
          <div style={{ marginTop: '6px' }}>
            <RawToolResultBlock text={rawText} />
          </div>
        </details>
      )}
      {!showRawOutput && toolName === 'create_digital_employee' && toolResult && toolResult !== toolMeta.message && (
        <RawToolResultBlock text={toolResult} />
      )}
    </div>
  );
}

function SessionHydratingState({ label }: { label: string }) {
  return (
    <div
      data-testid="session-loading-state"
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '8px',
        minHeight: '160px',
        color: 'var(--text-tertiary)',
        fontSize: '12px',
      }}
    >
      <IconLoader2 size={15} stroke={1.7} style={{ animation: 'spin 0.8s linear infinite' }} />
      <span>{label}</span>
    </div>
  );
}

export default function AgentChatSection({
  agentId,
  agent,
  currentUser,
  isAdmin,
  chatScope,
  onSetChatScope,
  onLoadAllSessions,
  onCreateNewSession,
  sessionsLoading,
  sessions,
  activeSession,
  branchLineage = [],
  branchLineageLoading = false,
  onSelectBranchSession,
  wsConnected,
  allSessions,
  allSessionsLoading,
  allUserFilter,
  onSetAllUserFilter,
  onSelectSession,
  onDeleteSession,
  historyContainerRef,
  onHistoryScroll,
  historyMsgs,
  historyMessagesSessionId,
  showHistoryScrollBtn,
  onScrollHistoryToBottom,
  chatContainerRef,
  onChatScroll,
  chatMessages,
  chatMessagesSessionId,
  runtimeSummary,
  agentPermissions,
  transportNotice,
  isWaiting,
  activeRunStatus,

  chatEndRef,
  showScrollBtn,
  onScrollToBottom,
  agentExpired,
  attachedFiles,
  onRemoveAttachedFile,
  fileInputRef,
  onHandleChatFile,
  uploading,
  uploadProgress,
  uploadAbortRef,
  chatInputRef,
  chatInput,
  onSetChatInput,
  onHandlePaste,
  onSendChatMsg,
  onBranchMessage,
  onSendMessage,
  onEnterPlanMode,
  planModeRequested = false,
  onTogglePlanMode,
  sessionPermissionMode = 'auto',
  onSetSessionPermissionMode,
  sessionCommandControl = null,
  onDismissSessionCommandControl,
  onRunSessionCommand,
  onResolveSessionPermission,
  isStreaming,
  onAbortGeneration,
  sessionOnly = false,
}: AgentChatSectionProps) {
  const { t } = useTranslation();
  const effectiveAgentId = agentId ? String(agentId) : (agent?.id ? String(agent.id) : null);

  const isReadOnlySession =
    !!activeSession &&
    !shouldUseWritableSessionSurface(activeSession as any, currentUser?.id);
  const isDraftSession = isDraftHumanChatSession(activeSession as any);
  const canUseComposer = Boolean(activeSession) && !isReadOnlySession && !agentExpired;

  const [artifactPreview, setArtifactPreview] = React.useState<ArtifactPreviewState | null>(null);
  const [composerMenuOpen, setComposerMenuOpen] = React.useState(false);
  const [permissionMenuOpen, setPermissionMenuOpen] = React.useState(false);
  const [runtimePanelCollapsed, setRuntimePanelCollapsed] = React.useState(false);
  const [focusedWorkflow, setFocusedWorkflow] = React.useState<RuntimeSectionItemModel | null>(null);
  const [focusedGitCheckpointId, setFocusedGitCheckpointId] = React.useState<string | null>(null);
  const gitScrollFrameRef = React.useRef<number | null>(null);

  const runtimeUsageLabel = getRuntimeUsageLabel(runtimeSummary);
  const runtimeUsageTitle = getRuntimeUsageTitle(runtimeSummary, runtimeUsageLabel);
  const permissionModeLabel = getSessionPermissionModeLabel(sessionPermissionMode, t);
  const composerIntentLabel = getComposerIntentLabel(planModeRequested, t);
  const modelBadgeLabel =
    runtimeSummary?.model?.label ||
    runtimeSummary?.model?.name ||
    agent?.primary_model_id ||
    t('agent.chat.composer.modelUnknown', 'Unknown model');
  const modelBadgeTitle = [
    runtimeSummary?.model?.provider,
    runtimeSummary?.model?.name,
    runtimeUsageTitle,
  ].filter(Boolean).join(' · ');

  const focusChatInput = React.useCallback(() => {
    setTimeout(() => chatInputRef.current?.focus(), 0);
  }, [chatInputRef]);

  const setComposerAction = React.useCallback(
    (action: ComposerActionKey) => {
      setComposerMenuOpen(false);
      if (action === 'upload') {
        fileInputRef.current?.click();
        return;
      }
      if (action === 'plan') {
        onTogglePlanMode?.();
        focusChatInput();
        return;
      }
      if (action === 'goal' || action === 'schedule') {
        onSetChatInput(composerShortcutText(action));
        focusChatInput();
        return;
      }
    },
    [fileInputRef, focusChatInput, onSetChatInput, onTogglePlanMode],
  );

  const sendFromComposer = React.useCallback(() => {
    setComposerMenuOpen(false);
    onSendChatMsg();
  }, [onSendChatMsg]);

  const composerPlaceholder =
    attachedFiles.length > 0
        ? t('agent.chat.askAboutFile', { name: attachedFiles.length === 1 ? attachedFiles[0].name : `${attachedFiles.length} files` })
        : t('chat.placeholder');

	  const startBranchAction = React.useCallback(
	    async (message: AgentChatMessage) => {
	      if (!onBranchMessage) return;
	      await onBranchMessage(message, 'branch');
	    },
	    [onBranchMessage],
	  );

  const submitMessageFeedback = React.useCallback(
    async (message: AgentChatMessage, label: RecordSessionFeedbackInput['label']) => {
      const sessionId = activeSession?.id ? String(activeSession.id) : null;
      if (!effectiveAgentId || !sessionId || !message.id) return;
      const messageId = String(message.id);
      const input: RecordSessionFeedbackInput = {
        label,
        reason: 'message action bar',
      };
      if (isUuidLike(messageId)) {
        input.message_id = messageId;
      } else {
        input.decision_id = `message:${messageId}`;
      }

      try {
        await chatApi.recordSessionFeedback(effectiveAgentId, sessionId, input);
        showAppToast(t('agent.chat.feedback.recorded', 'Feedback recorded.'), 'success');
      } catch (error: any) {
        showAppToast(
          t('agent.chat.feedback.failed', 'Failed to record feedback: {{message}}', {
            message: error?.message || String(error),
          }),
          'error',
        );
      }
    },
    [activeSession?.id, effectiveAgentId, t],
  );

	  const rewindFromMessage = React.useCallback(
	    async (message: AgentChatMessage) => {
	      if (!message.transcriptEventId || !onRunSessionCommand) return;
	      await onRunSessionCommand('rewind', { checkpoint_event_id: String(message.transcriptEventId) });
	    },
	    [onRunSessionCommand],
	  );

  const openArtifact = React.useCallback(async (artifact: ChatArtifactPart) => {
    const artifactAgentId = artifactWorkspaceAgentId(artifact, effectiveAgentId);
    if (!artifactAgentId) return;
    const href = artifact.id
      ? fileApi.artifactDownloadUrl(artifactAgentId, artifact.id)
      : fileApi.downloadUrl(artifactAgentId, artifact.path);
    if (getArtifactOpenMode(artifact) === 'download') {
      window.open(href, '_blank', 'noopener,noreferrer');
      return;
    }

    const previewKind = getEffectiveArtifactPreviewKind(artifact);
    if (previewKind === 'markdown' || previewKind === 'text' || !previewKind) {
      setArtifactPreview({ artifact, loading: true });
      try {
        const response = artifact.id
          ? await fileApi.readArtifact(artifactAgentId, artifact.id)
          : await fileApi.read(artifactAgentId, artifact.path);
        setArtifactPreview({
          artifact,
          content: response.content || '',
          url: href,
          usingSnapshot: Boolean(response.uses_snapshot || artifact.snapshotHash),
          workspaceChanged: Boolean(response.workspace_changed),
          legacyCurrentFileFallback: Boolean(response.legacy_current_file_fallback),
        });
      } catch (error) {
        if (typeof artifact.previewSnapshotContent === 'string') {
          setArtifactPreview({
            artifact,
            content: artifact.previewSnapshotContent,
            url: href,
            usingSnapshot: true,
          });
          return;
        }
        setArtifactPreview({
          artifact,
          url: href,
          error: error instanceof Error && !String(error.message || '').includes('File not found')
            ? error.message
            : t('agent.chat.artifacts.missingNoSnapshot', 'This file is no longer available in the workspace.'),
        });
      }
      return;
    }

    setArtifactPreview({ artifact, url: href });
  }, [effectiveAgentId, t]);

  React.useEffect(() => {
    const input = chatInputRef.current;
    if (!input) return;
    input.style.height = '0px';
    const nextHeight = computeComposerHeight(input.scrollHeight);
    input.style.height = `${nextHeight}px`;
    input.style.overflowY = nextHeight >= 160 ? 'auto' : 'hidden';
  }, [chatInput, chatInputRef]);

  const renderEventMessage = React.useCallback(
    (msg: AgentChatMessage, index: number) => {
      const permissionRequest = msg.sessionPermissionRequest;
      const isSessionPermissionRequest = Boolean(permissionRequest && msg.eventStatus === 'session_permission_required');
      const permissionToolLabel =
        permissionRequest?.tool_display_name || permissionRequest?.tool_name || msg.eventToolName || 'this tool';
      const statusColor =
        msg.eventStatus === 'blocked' || msg.eventStatus === 'capability_denied'
          ? 'var(--error)'
          : msg.eventStatus === 'approval_required' || isSessionPermissionRequest
            ? 'var(--warning)'
            : 'var(--accent-primary)';
      const metaParts: string[] = [];
      if (typeof msg.originalMessageCount === 'number' && typeof msg.keptMessageCount === 'number') {
        metaParts.push(
          t('agent.chat.runtime.compactionMeta', {
            original: msg.originalMessageCount,
            kept: msg.keptMessageCount,
            defaultValue: `Kept ${msg.keptMessageCount} of ${msg.originalMessageCount}`,
          }),
        );
      }
      if (msg.activatedToolGroupCount) {
        // Runtime status only — internal tool-group names are never surfaced (§8.4).
        metaParts.push(
          t('agent.chat.runtime.toolGroupsActivated', {
            count: msg.activatedToolGroupCount,
            defaultValue: '{{count}} runtime tool groups activated',
          }),
        );
      }
      if (msg.eventToolName) metaParts.push(msg.eventToolName);
      if (!isSessionPermissionRequest && msg.eventCapability) metaParts.push(msg.eventCapability);
      if (!isSessionPermissionRequest && msg.eventSecurityZone) metaParts.push(`zone:${msg.eventSecurityZone}`);
      if (!isSessionPermissionRequest && msg.eventApprovalId) metaParts.push(`approval:${msg.eventApprovalId}`);
      const isCompactionEvent = msg.eventType === 'session_compact';
      const compactionDisplay = isCompactionEvent ? getCompactionDisplayContent(msg.content) : null;
      const compactionDetails = isCompactionEvent
        ? compactionDisplay?.details || (msg.content?.trim() ? msg.content : null)
        : null;
      const compactionInProgress = msg.eventStatus === 'running' || msg.eventStatus === 'in_progress';
      const permissionActions = permissionRequest && msg.eventStatus === 'session_permission_required' ? (
        <SessionPermissionActions
          permissionRequest={permissionRequest}
          onResolveSessionPermission={onResolveSessionPermission}
          t={t}
        />
      ) : null;

      return (
        <div key={`event-${index}`} style={{ paddingLeft: '36px', marginBottom: '8px' }}>
          <div
            style={{
              borderRadius: '10px',
              border: `1px solid color-mix(in srgb, ${statusColor} 30%, transparent)`,
              background: `color-mix(in srgb, ${statusColor} 10%, var(--bg-secondary))`,
              padding: '10px 12px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
              <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: statusColor, flexShrink: 0 }} />
              <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                {isCompactionEvent
                  ? (compactionInProgress
                      ? t('agent.chat.runtime.compactingTitle', 'Automatic compression in progress')
                      : t('agent.chat.runtime.compactionCompleteTitle', 'Automatic compression complete'))
                  : msg.eventTitle || t('agent.chat.runtime.eventTitle', 'Runtime Event')}
              </span>
            </div>
            {isCompactionEvent ? (
              <div style={{ display: 'grid', gap: '6px' }}>
                <div style={{ fontSize: '12px', lineHeight: 1.6, color: 'var(--text-secondary)' }}>
                  {compactionInProgress
                    ? t('agent.chat.runtime.compactingNotice', 'Automatically compressing context...')
                    : t('agent.chat.runtime.compactedNotice', 'Context was compressed. The active working state was preserved.')}
                </div>
                {compactionDetails && (
                  <details style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    <summary style={{ cursor: 'pointer', userSelect: 'none' }}>
                      {t('agent.chat.runtime.compactionDetails', 'Show compression details')}
                    </summary>
                    <div style={{ marginTop: '6px' }}>
                      <RawToolResultBlock text={compactionDetails} />
                    </div>
                  </details>
                )}
              </div>
            ) : isSessionPermissionRequest ? (
              <div style={{ fontSize: '12px', lineHeight: 1.6, color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>
                {t('agent.chat.runtime.permissionNeeded', 'The agent needs permission to use {{tool}}.', {
                  tool: permissionToolLabel,
                })}
              </div>
            ) : (
              <div style={{ fontSize: '12px', lineHeight: 1.6, color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{msg.content}</div>
            )}
            {(msg.eventReason || msg.eventNextStep) && (
              <div style={{ marginTop: '8px', fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
                {msg.eventReason && <div>{msg.eventReason}</div>}
                {msg.eventNextStep && <div>{msg.eventNextStep}</div>}
              </div>
            )}
            {permissionActions}
            {metaParts.length > 0 && (
              <div style={{ marginTop: '6px', fontSize: '11px', color: 'var(--text-tertiary)' }}>{metaParts.join(' · ')}</div>
            )}
          </div>
        </div>
      );
    },
    [onResolveSessionPermission, t],
  );

	  const ChatMessageItem = React.useMemo(
	    () =>
	      React.memo(({ msg, i, isLeft, checkpointMessage }: { msg: AgentChatMessage; i: number; isLeft: boolean; checkpointMessage?: AgentChatMessage | null }) => {
        const extension = msg.fileName?.split('.').pop()?.toLowerCase() ?? '';
        const fileIcon =
          extension === 'pdf'
            ? '📄'
            : extension === 'csv' || extension === 'xlsx' || extension === 'xls'
              ? '📊'
              : extension === 'docx' || extension === 'doc'
                ? '📝'
                : '📎';
        const isImage = !!msg.imageUrl && ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(extension);
        const inlinePlanId = isLeft && msg.role === 'assistant' ? extractPlanIdFromPlanModeMessage(msg.content) : null;

        const timestampHtml = (() => {
          let timeStr = '';
          if (msg.timestamp) {
            const date = new Date(msg.timestamp);
            const now = new Date();
            const diffMs = now.getTime() - date.getTime();
            const isToday = date.toDateString() === now.toDateString();
            if (isToday) timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            else if (diffMs < 7 * 86400000)
              timeStr = `${date.toLocaleDateString([], { weekday: 'short' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
            else
              timeStr = `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
          }
          return (
            <div
              className={`session-tui-message-meta ${isLeft ? 'align-left' : 'align-right'}`}
            >
              {timeStr}
              {msg.content && <CopyMessageButton text={msg.content} />}
	              <MessageBranchActions
	                message={msg}
	                checkpointMessage={checkpointMessage}
	                isLeft={isLeft}
	                onBranchMessage={startBranchAction}
                onFeedbackMessage={submitMessageFeedback}
                onRewindMessage={rewindFromMessage}
              />
            </div>
          );
        })();

        return (
          <div
            key={i}
	            data-session-message-id={msg.transcriptEventId || msg.id || undefined}
            data-session-message-index={i}
            className={`session-tui-message-row ${isLeft ? 'session-tui-message-row-assistant' : 'session-tui-message-row-user'}`}
          >
            <div className="session-tui-message-avatar">
              {isLeft ? (msg.sender_name ? msg.sender_name[0] : 'A') : 'U'}
            </div>
            <div className="session-tui-message-bubble">
              {isLeft && msg.sender_name && (
                <div className="session-tui-message-sender">
                  {msg.sender_name}
                </div>
              )}
              {isImage ? (
                <div style={{ marginBottom: '4px' }}>
                  <img
                    src={msg.imageUrl}
                    alt={msg.fileName}
                    style={{ maxWidth: '200px', maxHeight: '150px', borderRadius: '8px', border: '1px solid var(--border-subtle)' }}
                    loading="lazy"
                  />
                </div>
              ) : (
                msg.fileName && (
                  <div
                    className="session-tui-file-chip"
                    style={{ marginBottom: msg.content ? '4px' : '0' }}
                  >
                    <span>{fileIcon}</span>
                    <span
                      style={{
                        fontWeight: 500,
                        color: 'var(--text-primary)',
                        maxWidth: '200px',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {msg.fileName}
                    </span>
                  </div>
                )
              )}
              {msg.thinking && (
                <details
                  className="session-tui-thinking"
                >
                  <summary
                    className="session-tui-thinking-summary"
                  >
                    Thinking
                  </summary>
                  <div
                    className="session-tui-thinking-body"
                  >
                    {msg.thinking}
                  </div>
                </details>
              )}
              {msg.role === 'assistant' ? (
                <MarkdownRenderer content={msg.content} />
              ) : (
                <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
              )}
              <ArtifactCards agentId={effectiveAgentId} artifacts={msg.artifacts} onOpenArtifact={openArtifact} />
              {inlinePlanId && effectiveAgentId && (
                <div style={{ marginTop: '10px', minWidth: 'min(520px, 100%)' }} data-testid="chat-inline-plan-card">
                  <InlinePlanCard agentId={effectiveAgentId} planId={inlinePlanId} />
                </div>
              )}
              {timestampHtml}
            </div>
          </div>
        );
      }),
    [effectiveAgentId, openArtifact, rewindFromMessage, startBranchAction, submitMessageFeedback, t],
  );

  const renderToolCall = (msg: AgentChatMessage, index: number, running = false) => {
    const permissionActions = msg.sessionPermissionRequest ? (
      <SessionPermissionActions
        permissionRequest={msg.sessionPermissionRequest}
        onResolveSessionPermission={onResolveSessionPermission}
        t={t}
      />
    ) : null;

    return (
      <div key={index} style={{ display: 'flex', gap: '8px', marginBottom: '6px', paddingLeft: '36px', minWidth: 0 }}>
        <details
          style={{
            flex: 1,
            minWidth: 0,
            borderRadius: '8px',
            background: 'var(--accent-subtle)',
            border: '1px solid var(--accent-subtle)',
            fontSize: '12px',
            overflow: 'hidden',
          }}
        >
          <summary
            style={{
              padding: '6px 10px',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              userSelect: 'none',
              listStyle: 'none',
              overflow: 'hidden',
            }}
          >
            <span style={{ fontSize: '13px' }}>{running ? '⏳' : '⚡'}</span>
            <span style={{ fontWeight: 600, color: 'var(--accent-text)' }}>{msg.toolName}</span>
            {msg.toolArgs && Object.keys(msg.toolArgs).length > 0 && (
              <span
                style={{
                  color: 'var(--text-tertiary)',
                  fontSize: '11px',
                  fontFamily: 'var(--font-mono)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  flex: 1,
                }}
              >
                {`(${Object.entries(msg.toolArgs)
                  .map(([k, v]) => `${k}: ${typeof v === 'string' ? v.slice(0, 30) : JSON.stringify(v)}`)
                  .join(', ')})`}
              </span>
            )}
            {running && <span style={{ color: 'var(--text-tertiary)', fontSize: '11px', marginLeft: 'auto' }}>{t('common.loading')}</span>}
          </summary>
          {(msg.toolResult || msg.toolMeta) && (
            <div style={{ padding: '4px 10px 8px' }}>
              <StructuredToolResultBody
                toolName={msg.toolName}
                toolMeta={msg.toolMeta}
                toolResult={msg.toolResult}
                toolRawResult={msg.toolRawResult}
                agentId={effectiveAgentId ?? undefined}
                agentName={agent?.name}
                submitted={isClarificationCardAnsweredByLaterUserMessage(visibleTimeline, index)}
                onSendMessage={onSendMessage}
                onEnterPlanMode={onEnterPlanMode}
              />
              {permissionActions}
            </div>
          )}
          {!(msg.toolResult || msg.toolMeta) && permissionActions}
          <ArtifactCards agentId={effectiveAgentId} artifacts={msg.artifacts} onOpenArtifact={openArtifact} />
        </details>
      </div>
    );
  };

  const renderInlinePlanToolCall = (msg: AgentChatMessage, index: number) => (
    <div key={index} style={{ paddingLeft: '36px', marginBottom: '8px', maxWidth: '75%' }} data-testid="chat-inline-plan-tool-call">
      <StructuredToolResultBody
        toolName={msg.toolName}
        toolMeta={msg.toolMeta}
        toolResult={msg.toolResult}
        toolRawResult={msg.toolRawResult}
        agentId={effectiveAgentId ?? undefined}
        agentName={agent?.name}
        submitted={isClarificationCardAnsweredByLaterUserMessage(visibleTimeline, index)}
        onSendMessage={onSendMessage}
        onEnterPlanMode={onEnterPlanMode}
      />
      {msg.sessionPermissionRequest && (
        <SessionPermissionActions
          permissionRequest={msg.sessionPermissionRequest}
          onResolveSessionPermission={onResolveSessionPermission}
          t={t}
        />
      )}
      <ArtifactCards agentId={effectiveAgentId} artifacts={msg.artifacts} onOpenArtifact={openArtifact} />
    </div>
  );

  const renderThinkingCard = (thinking: string, key: string | number) => (
    <div key={key} style={{ paddingLeft: '36px', marginBottom: '6px' }}>
      <details
        style={{
          fontSize: '12px',
          background: 'rgba(147, 130, 220, 0.08)',
          borderRadius: '6px',
          border: '1px solid rgba(147, 130, 220, 0.15)',
        }}
      >
        <summary
          style={{
            padding: '6px 10px',
            cursor: 'pointer',
            color: 'rgba(147, 130, 220, 0.9)',
            fontWeight: 500,
            userSelect: 'none',
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
          }}
        >
          Thinking
        </summary>
        <div
          style={{
            padding: '4px 10px 8px',
            fontSize: '12px',
            lineHeight: '1.6',
            color: 'var(--text-secondary)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            maxHeight: '300px',
            overflow: 'auto',
          }}
        >
          {thinking}
        </div>
      </details>
    </div>
  );

  const isInlineToolCardMessage = (message: AgentChatMessage) => (
    message.role === 'tool_call' && (
      Boolean(message.artifacts?.length) ||
      message.toolMeta?.kind === 'plan_proposal' ||
      message.toolMeta?.kind === 'dynamic_workflow_proposal' ||
      message.toolMeta?.kind === 'user_clarification' ||
      message.toolMeta?.kind === 'plan_mode_request' ||
      message.toolMeta?.kind === 'create_employee_success' ||
      message.toolMeta?.kind === 'hr_preview'
    )
  );

	  const renderConversationMessage = (message: AgentChatMessage, index: number, isLeft: boolean, timelineMessages: AgentChatMessage[]) => {
	    if (message.role === 'event') {
	      return renderEventMessage(message, index);
    }
    if (message.role === 'tool_call') {
      if (isInlineToolCardMessage(message)) return renderInlinePlanToolCall(message, index);
      return null;
    }
	    if (message.role === 'assistant' && !message.content?.trim()) {
	      return null;
	    }
	    return (
	      <ChatMessageItem
	        key={index}
	        msg={message}
	        i={index}
	        isLeft={isLeft}
	        checkpointMessage={message.role === 'assistant' ? previousUserCheckpointForMessage(timelineMessages, index) : null}
	      />
	    );
	  };

  const renderConversationMessages = (
    messages: AgentChatMessage[],
    resolveIsLeft: (message: AgentChatMessage, index: number) => boolean,
    timelineModel?: ThreadTimelineModel,
  ) => {
    const nodes: React.ReactNode[] = [];
    const model = timelineModel ?? buildThreadTimeline({
      messages,
      activeSession,
      runtimeSummary,
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    model.cells.forEach((cell) => {
      if (cell.kind === 'user_turn') {
	        nodes.push(renderConversationMessage(cell.message, cell.index, resolveIsLeft(cell.message, cell.index), messages));
	        return;
	      }
	      if (cell.kind === 'assistant_final') {
	        nodes.push(renderConversationMessage(cell.message, cell.index, resolveIsLeft(cell.message, cell.index), messages));
        return;
      }
      if (cell.kind === 'active_run') {
        nodes.push(
          <div key={cell.id} data-testid="active-run-cell" style={{ marginBottom: '8px' }}>
            <RunDisclosureBlock timeline={cell.timeline} />
            {cell.sourceMessages.map((entry) => (
              entry.message.role === 'event' && entry.message.sessionPermissionRequest
                ? renderEventMessage(entry.message, entry.index)
                : isInlineToolCardMessage(entry.message)
                  ? renderInlinePlanToolCall(entry.message, entry.index)
                  : null
            ))}
	            {cell.answer ? renderConversationMessage(cell.answer, cell.answerIndex ?? 0, resolveIsLeft(cell.answer, cell.answerIndex ?? 0), messages) : null}
          </div>,
        );
        return;
      }
      nodes.push(
        <div
          key={cell.id}
          data-testid="session-boundary-cell"
          style={{
            margin: '8px auto',
            width: 'fit-content',
            maxWidth: '80%',
            border: '1px solid var(--border-subtle)',
            borderRadius: '999px',
            background: 'var(--bg-secondary)',
            color: 'var(--text-tertiary)',
            fontSize: '11px',
            padding: '4px 9px',
          }}
        >
          {cell.title}
          {cell.summary ? ` · ${cell.summary}` : ''}
        </div>,
      );
    });

    return nodes;
  };

  const activeSessionId = activeSession?.id ? String(activeSession.id) : null;
  React.useEffect(() => {
    setFocusedWorkflow(null);
  }, [activeSessionId]);
  const visibleHistoryMsgs = historyMessagesSessionId === activeSessionId ? historyMsgs : [];
  const visibleChatMessages = chatMessagesSessionId === activeSessionId ? chatMessages : [];
  const activeSessionHydrating = Boolean(activeSessionId) && (
    isReadOnlySession ? historyMessagesSessionId !== activeSessionId : chatMessagesSessionId !== activeSessionId
  );
  const visibleTimeline = isReadOnlySession ? visibleHistoryMsgs : visibleChatMessages;
  const branchLineageRowsForGitLine = React.useMemo(
    () => (branchLineage.length > 1 ? buildBranchLineageRows(branchLineage) : []),
    [branchLineage],
  );
  const gitLineAxisSessionId = React.useMemo(() => {
    const activeLineageItem = branchLineage.find((item) => String(item.id) === String(activeSessionId || ''));
    const activeLineageBranch = activeLineageItem?.branch || {};
    const activeLineageRootId = activeLineageItem?.root_session_id || stringValue(activeLineageBranch.root_session_id);
    if (activeLineageRootId) return String(activeLineageRootId);
    if (activeSession?.root_session_id) return String(activeSession.root_session_id);
    const rootRow = branchLineageRowsForGitLine.find((row) => row.depth === 0);
    return rootRow?.id ? String(rootRow.id) : activeSessionId;
  }, [activeSession?.root_session_id, activeSessionId, branchLineage, branchLineageRowsForGitLine]);
  const shouldUseGitLineAxisSession = Boolean(
    branchLineage.length > 1
      && gitLineAxisSessionId
      && activeSessionId
      && String(gitLineAxisSessionId) !== String(activeSessionId),
  );
  const { data: sessionIndexData } = useQuery({
    queryKey: ['chat-session-index', effectiveAgentId, activeSessionId],
    queryFn: () => chatApi.getSessionIndex(effectiveAgentId!, activeSessionId!),
    enabled: Boolean(effectiveAgentId && activeSessionId),
    staleTime: 10_000,
  });
  const { data: sessionWorkbenchData } = useQuery({
    queryKey: ['chat-session-workbench', effectiveAgentId, activeSessionId],
    queryFn: () => ccParityApi.getSessionWorkbench(effectiveAgentId!, activeSessionId!),
    enabled: Boolean(effectiveAgentId && activeSessionId),
    staleTime: 10_000,
  });
  const { data: gitLineAxisSessionIndexData, isLoading: gitLineAxisSessionIndexLoading } = useQuery({
    queryKey: ['chat-session-index', effectiveAgentId, gitLineAxisSessionId, 'gitline-axis'],
    queryFn: () => chatApi.getSessionIndex(effectiveAgentId!, gitLineAxisSessionId!),
    enabled: Boolean(effectiveAgentId && gitLineAxisSessionId && shouldUseGitLineAxisSession),
    staleTime: 10_000,
  });
  const { data: gitLineAxisSessionWorkbenchData, isLoading: gitLineAxisSessionWorkbenchLoading } = useQuery({
    queryKey: ['chat-session-workbench', effectiveAgentId, gitLineAxisSessionId, 'gitline-axis'],
    queryFn: () => ccParityApi.getSessionWorkbench(effectiveAgentId!, gitLineAxisSessionId!),
    enabled: Boolean(effectiveAgentId && gitLineAxisSessionId && shouldUseGitLineAxisSession),
    staleTime: 10_000,
  });
  const sessionIndex = sessionIndexData && !Array.isArray(sessionIndexData) ? sessionIndexData : null;
  const sessionWorkbench = sessionWorkbenchData && !Array.isArray(sessionWorkbenchData) ? sessionWorkbenchData : null;
  const gitLineAxisSessionIndex = gitLineAxisSessionIndexData && !Array.isArray(gitLineAxisSessionIndexData)
    ? gitLineAxisSessionIndexData
    : null;
  const gitLineAxisSessionWorkbench = gitLineAxisSessionWorkbenchData && !Array.isArray(gitLineAxisSessionWorkbenchData)
    ? gitLineAxisSessionWorkbenchData
    : null;
  const getCheckpointsFromSessionSurfaces = React.useCallback((
    indexData: typeof sessionIndex,
    workbenchData: SessionWorkbench | null,
  ): Array<Record<string, unknown>> => {
    const indexCheckpoints = Array.isArray(indexData?.checkpoints) ? indexData.checkpoints : [];
    if (indexCheckpoints.length > 0) return indexCheckpoints;
    const workbenchCheckpoints = Array.isArray(workbenchData?.turn?.checkpoints)
      ? workbenchData.turn.checkpoints
      : [];
    return workbenchCheckpoints;
  }, []);
  const activeSessionGitCheckpoints = React.useMemo<Array<Record<string, unknown>>>(() => (
    getCheckpointsFromSessionSurfaces(sessionIndex, sessionWorkbench)
  ), [getCheckpointsFromSessionSurfaces, sessionIndex, sessionWorkbench]);
  const gitLineAxisSessionCheckpoints = React.useMemo<Array<Record<string, unknown>>>(() => (
    getCheckpointsFromSessionSurfaces(gitLineAxisSessionIndex, gitLineAxisSessionWorkbench)
  ), [getCheckpointsFromSessionSurfaces, gitLineAxisSessionIndex, gitLineAxisSessionWorkbench]);
  const sessionGitCheckpoints = React.useMemo<Array<Record<string, unknown>>>(() => {
    if (shouldUseGitLineAxisSession && gitLineAxisSessionCheckpoints.length > 0) {
      return gitLineAxisSessionCheckpoints;
    }
    return activeSessionGitCheckpoints;
  }, [activeSessionGitCheckpoints, gitLineAxisSessionCheckpoints, shouldUseGitLineAxisSession]);
  const sessionGitLineLoading = branchLineageLoading || Boolean(
    shouldUseGitLineAxisSession
      && gitLineAxisSessionCheckpoints.length === 0
      && (gitLineAxisSessionIndexLoading || gitLineAxisSessionWorkbenchLoading),
  );
  const checkpointIdSignature = React.useMemo(
    () => sessionGitCheckpoints.map(sessionCheckpointId).filter(Boolean).join('|'),
    [sessionGitCheckpoints],
  );
  const activeRunRecord = sessionWorkbench?.active_run && isRuntimeRecord(sessionWorkbench.active_run)
    ? sessionWorkbench.active_run
    : null;
  const runtimeRecord = runtimeSummary?.runtime as Record<string, unknown> | undefined;
  const activeRuntimeTaskId = stringValue(
    activeRunRecord?.runtime_task_id
      || activeRunRecord?.id
      || runtimeRecord?.runtime_task_id
      || runtimeRecord?.task_id,
  );
  const teamMemberWindow = isTeamMemberSession(activeSession)
    ? {
        memberName: teamMemberSessionLabel(activeSession),
        memberRole: teamMemberRoleLabel(activeSession),
        status: activeRunStatus || stringValue(activeRunRecord?.status) || 'idle',
        sessionId: activeSessionId || '',
      }
    : null;
  const liveRunStatus = String(activeRunRecord?.status || activeRunStatus || '').toLowerCase();
  const sessionWorkLedgerLive = isWaiting || isStreaming || ['running', 'pending', 'queued'].includes(liveRunStatus);
  const escapeAttrValue = React.useCallback((value: string) => {
    if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(value);
    return value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }, []);
  const findCheckpointMessageElement = React.useCallback(
    (scrollRoot: HTMLElement, checkpoint: Record<string, unknown>, index: number): HTMLElement | null => {
      const id = sessionCheckpointId(checkpoint);
      const target = id
        ? scrollRoot.querySelector<HTMLElement>(`[data-session-message-id="${escapeAttrValue(id)}"]`)
        : null;
      if (target) return target;

      const fallbackNodes = scrollRoot.querySelectorAll<HTMLElement>('[data-session-message-index]');
      return fallbackNodes[Math.max(0, Math.min(index, fallbackNodes.length - 1))] || null;
    },
    [escapeAttrValue],
  );
  const syncFocusedGitCheckpointFromScroll = React.useCallback(() => {
    const scrollRoot = isReadOnlySession ? historyContainerRef.current : chatContainerRef.current;
    if (!scrollRoot || sessionGitCheckpoints.length === 0) return;

    const rootRect = scrollRoot.getBoundingClientRect();
    const viewportCenterY = rootRect.top + rootRect.height / 2;
    const anchors = sessionGitCheckpoints
      .map((checkpoint, index) => {
        const id = sessionCheckpointId(checkpoint);
        const element = id ? findCheckpointMessageElement(scrollRoot, checkpoint, index) : null;
        if (!id || !element) return null;
        return {
          id,
          top: element.getBoundingClientRect().top,
        };
      })
      .filter((anchor): anchor is SessionScrollCheckpointAnchor => Boolean(anchor));
    const nextFocusedId = pickFocusedCheckpointIdForScroll(anchors, viewportCenterY);
    if (!nextFocusedId) return;
    setFocusedGitCheckpointId((current) => (current === nextFocusedId ? current : nextFocusedId));
  }, [
    chatContainerRef,
    findCheckpointMessageElement,
    historyContainerRef,
    isReadOnlySession,
    sessionGitCheckpoints,
  ]);
  const trackGitCheckpointFromScroll = React.useCallback(() => {
    if (gitScrollFrameRef.current !== null) return;
    if (typeof window === 'undefined') return;
    const scheduleFrame = typeof window.requestAnimationFrame === 'function'
      ? window.requestAnimationFrame.bind(window)
      : (callback: FrameRequestCallback) => window.setTimeout(() => callback(Date.now()), 0);
    gitScrollFrameRef.current = scheduleFrame(() => {
      gitScrollFrameRef.current = null;
      syncFocusedGitCheckpointFromScroll();
    });
  }, [syncFocusedGitCheckpointFromScroll]);
  React.useEffect(() => () => {
    if (gitScrollFrameRef.current === null || typeof window === 'undefined') return;
    if (typeof window.cancelAnimationFrame === 'function') {
      window.cancelAnimationFrame(gitScrollFrameRef.current);
    } else {
      window.clearTimeout(gitScrollFrameRef.current);
    }
    gitScrollFrameRef.current = null;
  }, []);
  React.useEffect(() => {
    if (!checkpointIdSignature) {
      setFocusedGitCheckpointId(null);
      return;
    }
    trackGitCheckpointFromScroll();
  }, [activeSessionId, checkpointIdSignature, trackGitCheckpointFromScroll, visibleTimeline.length]);
  const navigateGitCheckpoint = React.useCallback((checkpoint: Record<string, unknown>, index: number) => {
    const id = sessionCheckpointId(checkpoint);
    if (id) setFocusedGitCheckpointId(id);
    const scrollRoot = (isReadOnlySession ? historyContainerRef.current : chatContainerRef.current);
    if (!scrollRoot) return;
    findCheckpointMessageElement(scrollRoot, checkpoint, index)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [chatContainerRef, findCheckpointMessageElement, historyContainerRef, isReadOnlySession]);
  const handleHistoryScroll = React.useCallback(() => {
    onHistoryScroll();
    trackGitCheckpointFromScroll();
  }, [onHistoryScroll, trackGitCheckpointFromScroll]);
  const handleChatScroll = React.useCallback(() => {
    onChatScroll();
    trackGitCheckpointFromScroll();
  }, [onChatScroll, trackGitCheckpointFromScroll]);
  const threadTimelineModel = buildThreadTimeline({
    messages: visibleTimeline,
    activeSession,
    runtimeSummary,
    sessionIndex,
    sessionWorkbench,
    branchLineage,
    isWaiting,
    isStreaming,
    activeRunStatus,
  });
  const showDetailAuditBrowser = !sessionOnly && isAdmin;
  const detailSessionRows = allSessions;
  const detailSessionsLoading = allSessionsLoading;
  const renderHistoryFrame = (children: React.ReactNode) => (
    <div className="session-tui-history-frame">
      <SessionGitLine
        activeSessionId={activeSessionId}
        axisSessionId={gitLineAxisSessionId || activeSessionId}
        checkpoints={sessionGitCheckpoints}
        focusedCheckpointId={focusedGitCheckpointId}
        lineage={branchLineage}
        loading={sessionGitLineLoading}
        onNavigateCheckpoint={navigateGitCheckpoint}
        onNavigateBranch={onSelectBranchSession}
      />
      <div className="session-tui-history-content">
        {children}
      </div>
    </div>
  );
  const renderTeamMemberWindowHeader = () => {
    if (!teamMemberWindow) return null;
    const roleSuffix = teamMemberWindow.memberRole ? ` · ${teamMemberWindow.memberRole}` : '';
    return (
      <div
        data-testid="session-team-member-window"
        style={{
          display: 'grid',
          gap: '7px',
          marginBottom: '12px',
          padding: '10px 12px',
          border: '1px solid var(--border-subtle)',
          borderRadius: '8px',
          background: 'var(--bg-secondary)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div className="session-tui-kicker">
              {t('sessionWorkbench.teamMemberWindow.breadcrumb', 'Main > Agent: {{name}}', { name: teamMemberWindow.memberName })}
            </div>
            <strong style={{ display: 'block', marginTop: '3px', fontSize: '13px', color: 'var(--text-primary)' }}>
              {teamMemberWindow.memberName}{roleSuffix}
            </strong>
          </div>
          <span
            data-testid="session-active-session-tab"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
              minHeight: '24px',
              padding: '3px 8px',
              borderRadius: '999px',
              border: '1px solid var(--border-subtle)',
              background: 'var(--bg-primary)',
              color: 'var(--text-secondary)',
              fontSize: '11px',
              whiteSpace: 'nowrap',
            }}
            title={teamMemberWindow.sessionId}
          >
            <span
              aria-hidden="true"
              style={{
                display: 'inline-block',
                width: '6px',
                height: '6px',
                borderRadius: '50%',
                background: teamMemberWindow.status === 'failed' ? 'rgb(220,38,38)' : 'var(--accent-primary)',
              }}
            />
            {teamMemberWindow.status}
          </span>
        </div>
      </div>
    );
  };
  const formatDetailSessionTime = (session: any) => {
    const raw = session.last_message_at || session.updated_at || session.created_at;
    if (!raw) return '';
    try {
      return new Date(raw).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    } catch {
      return '';
    }
  };
  return (
    <div
      data-testid="session-workbench"
      className={`session-chat-workbench session-tui-shell ${sessionOnly ? 'session-only session-tui-shell-session-only' : 'session-tui-shell-managed'}`}
    >
      {showDetailAuditBrowser && (
        <aside className="detail-session-browser" data-testid="detail-session-browser">
          <div className="detail-session-browser-header">
            <div>
              <div className="detail-session-browser-title">{t('agent.chat.allUsers', 'All Users')}</div>
              <div className="detail-session-browser-subtitle">{agent?.name || t('agent.chat.session', 'Session')}</div>
            </div>
          </div>
          <input
            className="detail-session-filter"
            value={allUserFilter}
            onChange={(event) => onSetAllUserFilter(event.target.value)}
            placeholder={t('agent.chat.filterUsers', 'Filter users or sources...')}
          />
          <div className="detail-session-list">
            {detailSessionsLoading ? (
              <div className="detail-session-empty">{t('common.loading', 'Loading')}</div>
            ) : detailSessionRows.length === 0 ? (
              <div className="detail-session-empty">{t('agent.chat.noSessionsYet', 'No conversations yet.')}</div>
            ) : (
              detailSessionRows
                .filter((session: any) => {
                  if (!allUserFilter.trim()) return true;
                  const q = allUserFilter.trim().toLowerCase();
                  return [session.title, session.username, session.source_channel, session.peer_agent_name]
                    .filter(Boolean)
                    .some((value) => String(value).toLowerCase().includes(q));
                })
                .map((session: any) => {
                  const isActive = activeSession?.id && String(activeSession.id) === String(session.id);
                  const sourceLabel = session.source_channel && session.source_channel !== 'web' ? String(session.source_channel).toUpperCase() : '';
                  return (
                    <div key={session.id} className={`detail-session-row ${isActive ? 'active' : ''}`}>
                      <button
                        type="button"
                        className="detail-session-row-main"
                        onClick={() => onSelectSession(session)}
                      >
                        <span className="detail-session-row-title">{session.title || t('agent.chat.session', 'Session')}</span>
                        <span className="detail-session-row-meta">
                          {(session.username || sourceLabel) ? `${session.username || sourceLabel} · ` : ''}
                          {formatDetailSessionTime(session)}
                          {session.message_count ? ` · ${session.message_count}` : ''}
                        </span>
                      </button>
                      <button
                        type="button"
                        className="detail-session-row-action"
                        aria-label={`Delete session ${session.title || t('agent.chat.session', 'Session')}`}
                        title={t('common.delete', 'Delete')}
                        onClick={() => onDeleteSession(String(session.id))}
                      >
                        <IconTrash size={13} stroke={1.8} />
                      </button>
                    </div>
                  );
                })
            )}
          </div>
        </aside>
      )}
      <div className="session-tui-center">
        <SessionWorkbenchHeader model={threadTimelineModel.header} />
        {!activeSession ? (
          <div
            style={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexDirection: 'column',
              gap: '12px',
              padding: '32px',
            }}
          >
            <div style={{ fontSize: '28px', opacity: 0.6 }}>💬</div>
            <div style={{ fontSize: 'var(--text-sm)', color: 'var(--text-secondary)', textAlign: 'center', lineHeight: 1.5 }}>
              {t('agent.chat.startConversation', { name: agent?.name || '' })}
            </div>
            <button className="btn btn-primary" onClick={onCreateNewSession} style={{ fontSize: '13px' }}>
              {t('agent.chat.newSession')}
            </button>
            <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-tertiary)' }}>
              {t('agent.chat.fileSupport')}
            </div>
          </div>
        ) : isReadOnlySession ? (
          <>
            <div ref={historyContainerRef} onScroll={handleHistoryScroll} className="session-tui-history">
              {renderHistoryFrame(
                <>
                  {renderTeamMemberWindowHeader()}
                  <div
                    style={{
                      fontSize: '11px',
                      color: 'var(--text-tertiary)',
                      marginBottom: '12px',
                      padding: '4px 8px',
                      background: 'var(--bg-secondary)',
                      borderRadius: '4px',
                      display: 'inline-block',
                    }}
                  >
                    {isA2ASession(activeSession as any) ? `🤖 Agent Conversation · ${activeSession.username || 'Agents'}` : `Read-only · ${activeSession.username || 'User'}`}
                  </div>
                  {focusedWorkflow ? (
                    <WorkflowRunFocusPanel
                      workflow={focusedWorkflow}
                      onClose={() => setFocusedWorkflow(null)}
                      onSelectSession={onSelectBranchSession}
                    />
                  ) : activeSessionHydrating ? (
                    <SessionHydratingState label={t('common.loading', 'Loading')} />
                  ) : (() => {
                      const isA2A = isA2ASession(activeSession as any);
                      const thisAgentName = agent?.name;
                      const thisAgentPid = isA2A && thisAgentName ? visibleHistoryMsgs.find((message) => message.sender_name === thisAgentName)?.participant_id : null;
                      return renderConversationMessages(
                        visibleHistoryMsgs,
                        (message) => (isA2A && thisAgentPid ? message.participant_id !== thisAgentPid : message.role === 'assistant'),
                      );
                    })()}
                </>,
              )}
            </div>
            {showHistoryScrollBtn && (
              <button
                onClick={onScrollHistoryToBottom}
                style={{
                  position: 'absolute',
                  bottom: '20px',
                  right: '20px',
                  width: '32px',
                  height: '32px',
                  borderRadius: '50%',
                  background: 'var(--bg-elevated)',
                  border: '1px solid var(--border-default)',
                  color: 'var(--text-secondary)',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: '16px',
                  boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
                  zIndex: 10,
                }}
                title="Scroll to bottom"
              >
                ↓
              </button>
            )}
          </>
        ) : (
          <>
            <div ref={chatContainerRef} onScroll={handleChatScroll} className="session-tui-history">
              {renderHistoryFrame(
                <>
                  {renderTeamMemberWindowHeader()}
                  {focusedWorkflow ? (
                    <WorkflowRunFocusPanel
                      workflow={focusedWorkflow}
                      onClose={() => setFocusedWorkflow(null)}
                      onSelectSession={onSelectBranchSession}
                    />
                  ) : activeSessionHydrating ? (
                    <SessionHydratingState label={t('common.loading', 'Loading')} />
                  ) : visibleChatMessages.length === 0 && (
                    <div style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-tertiary)' }}>
                      <div style={{ fontSize: '13px', marginBottom: '4px' }}>{activeSession?.title || t('agent.chat.startChat')}</div>
                      <div style={{ fontSize: '12px' }}>{t('agent.chat.startConversation', { name: agent.name })}</div>
                      <div style={{ fontSize: '11px', marginTop: '4px', opacity: 0.7 }}>{t('agent.chat.fileSupport')}</div>
                    </div>
                  )}
                  {!focusedWorkflow && renderConversationMessages(visibleChatMessages, (message) => message.role === 'assistant', threadTimelineModel)}
                  <div ref={chatEndRef} />
                </>,
              )}
            </div>
            {showScrollBtn && (
              <button
                onClick={onScrollToBottom}
                style={{
                  position: 'absolute',
                  bottom: '70px',
                  right: '20px',
                  width: '32px',
                  height: '32px',
                  borderRadius: '50%',
                  background: 'var(--bg-elevated)',
                  border: '1px solid var(--border-default)',
                  color: 'var(--text-secondary)',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: '16px',
                  boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
                  zIndex: 10,
                }}
                title="Scroll to bottom"
              >
                ↓
              </button>
            )}
            {agentExpired ? (
              <div
                style={{
                  padding: '7px 16px',
                  borderTop: '1px solid rgba(245,158,11,0.3)',
                  background: 'rgba(245,158,11,0.08)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  fontSize: '12px',
                  color: 'rgb(180,100,0)',
                }}
              >
                <span>u23f8</span>
                <span>
                  This Agent has <strong>expired</strong> and is off duty. Contact your admin to extend its service.
                </span>
              </div>
            ) : transportNotice ? (
              <div
                style={{
                  padding: '7px 16px',
                  borderTop: '1px solid rgba(245,158,11,0.25)',
                  background: 'rgba(245,158,11,0.08)',
                  fontSize: '12px',
                  color: 'rgb(180,100,0)',
                }}
              >
                {transportNotice}
              </div>
            ) : !wsConnected && !isDraftSession ? (
              <div style={{ padding: '3px 16px', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                <span
                  style={{
                    display: 'inline-block',
                    width: '5px',
                    height: '5px',
                    borderRadius: '50%',
                    background: 'var(--accent-primary)',
                    opacity: 0.8,
                    animation: 'pulse 1.2s ease-in-out infinite',
                  }}
                />
                {t('agent.chat.transport.reconnecting', 'Live updates reconnecting...')}
              </div>
            ) : null}
            <div
              data-testid="session-composer"
              className="session-tui-composer"
            >
              {teamMemberWindow && (
                <div
                  data-testid="session-composer-target"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    padding: '0 2px 7px',
                    fontSize: '11px',
                    color: 'var(--text-tertiary)',
                  }}
                >
                  <span style={{ color: 'var(--text-secondary)', fontWeight: 650 }}>
                    {t('sessionWorkbench.teamMemberWindow.composerTarget', 'Composer target')}
                  </span>
                  <span>
                    {t('sessionWorkbench.teamMemberWindow.composerTargetValue', 'Agent Team member')} · {teamMemberWindow.memberName}
                  </span>
                </div>
              )}
              {effectiveAgentId && activeSession?.id && !isDraftSession && (
                <ChatWorkLedgerDock
                  agentId={effectiveAgentId}
                  sessionId={String(activeSession.id)}
                  runtimeTaskId={activeRuntimeTaskId || undefined}
                  live={sessionWorkLedgerLive}
                />
              )}
              {artifactPreview && (
                <ArtifactPreviewPanel
                  preview={artifactPreview}
                  onClose={() => setArtifactPreview(null)}
                  t={t}
                />
              )}
              {effectiveAgentId && activeSession?.id && !isDraftSession && (
                <SlashCommandMenu
                  agentId={effectiveAgentId}
                  sessionId={String(activeSession.id)}
                  inputValue={chatInput}
                  disabled={!canUseComposer}
                  onPickCommand={(_command, template) => {
                    onSetChatInput(template);
                    setTimeout(() => chatInputRef.current?.focus(), 0);
                  }}
                />
              )}
              <SessionCommandControlPanel
                control={sessionCommandControl}
                onDismiss={onDismissSessionCommandControl || (() => undefined)}
                onRunCommand={onRunSessionCommand || (() => undefined)}
              />
              {attachedFiles.length > 0 && (
                <div
                  data-testid="session-composer-attachments"
                  style={{
                    padding: '0 0 7px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    flexWrap: 'wrap',
                  }}
                >
                  {attachedFiles.map((file, index) => (
                    <div
                      key={index}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                        fontSize: '11px',
                        background: 'var(--bg-secondary)',
                        padding: '4px 6px',
                        borderRadius: '6px',
                        border: '1px solid var(--border-subtle)',
                        maxWidth: '220px',
                      }}
                    >
                      {file.imageUrl ? (
                        <img src={file.imageUrl} alt={file.name} style={{ width: '20px', height: '20px', borderRadius: '4px', objectFit: 'cover' }} />
                      ) : (
                        <IconFileText size={14} color="var(--text-tertiary)" />
                      )}
                      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.name}</span>
                      <button
                        type="button"
                        onClick={() => onRemoveAttachedFile(index)}
                        aria-label={t('agent.chat.removeAttachment', 'Remove attachment')}
                        style={{
                          background: 'none',
                          border: 'none',
                          color: 'var(--text-tertiary)',
                          cursor: 'pointer',
                          padding: '1px',
                          display: 'inline-flex',
                          alignItems: 'center',
                        }}
                        title={t('agent.chat.removeAttachment', 'Remove attachment')}
                      >
                        <IconX size={13} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div
                data-testid="session-composer-shell"
                style={{
                  position: 'relative',
                  border: '1px solid var(--border-default)',
                  borderRadius: '18px',
                  background: 'var(--bg-primary)',
                  boxShadow: '0 16px 36px rgba(15, 23, 42, 0.08)',
                  overflow: 'visible',
                }}
              >
                <div
                  data-testid="session-composer-plus-menu"
                  hidden={!composerMenuOpen}
                  style={{
                    position: 'absolute',
                    left: '12px',
                    bottom: '44px',
                    width: '248px',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '12px',
                    background: 'var(--bg-primary)',
                    boxShadow: '0 18px 48px rgba(15, 23, 42, 0.18)',
                    padding: '6px',
                    zIndex: 16,
                    display: composerMenuOpen ? 'grid' : 'none',
                    gap: '2px',
                  }}
                >
                  {([
                    {
                      key: 'upload' as const,
                      label: t('agent.chat.composer.uploadFile', 'Upload file'),
                      description: t('agent.chat.composer.uploadFileDesc', 'Attach files or screenshots to this turn'),
                      icon: uploading ? <IconLoader2 size={16} /> : <IconPaperclip size={16} />,
                      disabled: !canUseComposer || uploading || attachedFiles.length >= 10,
                    },
                    {
                      key: 'plan' as const,
                      label: t('agent.chat.composer.planMode', 'Plan Mode'),
                      description: planModeRequested
                        ? t('agent.chat.composer.planModeOnDesc', 'Next message will request a plan first')
                        : t('agent.chat.composer.planModeDesc', 'Ask the agent to plan before execution'),
                      icon: <IconChecklist size={16} />,
                      checked: planModeRequested,
                      disabled: !canUseComposer || isWaiting || isStreaming,
                    },
                    {
                      key: 'goal' as const,
                      label: t('agent.chat.composer.goalMode', 'Goal mode'),
                      description: t('agent.chat.composer.goalModeDesc', 'Start a session goal through the command surface'),
                      icon: <IconTargetArrow size={16} />,
                      disabled: !canUseComposer || isWaiting || isStreaming,
                    },
                    {
                      key: 'schedule' as const,
                      label: t('agent.chat.composer.scheduledTask', 'Scheduled task'),
                      description: t('agent.chat.composer.scheduledTaskDesc', 'Draft a scheduled task request for this agent'),
                      icon: <IconCalendarTime size={16} />,
                      disabled: !canUseComposer || isWaiting || isStreaming,
                    },
                  ] satisfies Array<{
                    key: ComposerActionKey;
                    label: string;
                    description: string;
                    icon: React.ReactNode;
                    checked?: boolean;
                    disabled: boolean;
                  }>).map((action) => (
                    <button
                      key={action.key}
                      type="button"
                      onClick={() => setComposerAction(action.key)}
                      disabled={action.disabled}
                      style={{
                        width: '100%',
                        display: 'grid',
                        gridTemplateColumns: action.checked === undefined ? '22px minmax(0, 1fr)' : '22px minmax(0, 1fr) 34px',
                        gap: '9px',
                        alignItems: 'center',
                        padding: '9px 10px',
                        border: 0,
                        borderRadius: '8px',
                        background: 'transparent',
                        color: action.disabled ? 'var(--text-tertiary)' : 'var(--text-primary)',
                        cursor: action.disabled ? 'not-allowed' : 'pointer',
                        textAlign: 'left',
                      }}
                    >
                      <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-secondary)' }}>
                        {action.icon}
                      </span>
                      <span style={{ display: 'grid', gap: '2px', minWidth: 0 }}>
                        <strong style={{ fontSize: '12px', fontWeight: 650 }}>{action.label}</strong>
                        <span style={{ fontSize: '11px', lineHeight: 1.35, color: 'var(--text-tertiary)' }}>{action.description}</span>
                      </span>
                      {action.checked !== undefined && (
                        <span
                          data-testid={`session-composer-action-${action.key}-switch`}
                          role="switch"
                          aria-checked={action.checked}
                          aria-label={action.label}
                          style={{
                            position: 'relative',
                            width: '30px',
                            height: '18px',
                            borderRadius: '999px',
                            background: action.checked ? 'var(--text-primary)' : 'var(--bg-tertiary)',
                            border: '1px solid var(--border-subtle)',
                            transition: 'background 0.15s ease',
                            justifySelf: 'end',
                          }}
                        >
                          <span
                            style={{
                              position: 'absolute',
                              top: '2px',
                              left: action.checked ? '14px' : '2px',
                              width: '12px',
                              height: '12px',
                              borderRadius: '50%',
                              background: 'var(--bg-primary)',
                              boxShadow: '0 1px 2px rgba(15, 23, 42, 0.25)',
                              transition: 'left 0.15s ease',
                            }}
                          />
                        </span>
                      )}
                    </button>
                  ))}
                </div>
                <input type="file" multiple ref={fileInputRef} onChange={onHandleChatFile} style={{ display: 'none' }} />
                <textarea
                  data-testid="session-composer-input"
                  ref={chatInputRef}
                  className="chat-input"
                  value={chatInput}
                  onChange={(e) => onSetChatInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                      e.preventDefault();
                      sendFromComposer();
                    }
                  }}
                  onPaste={onHandlePaste}
                  placeholder={composerPlaceholder}
                  disabled={!canUseComposer}
                  rows={1}
                  style={{
                    width: '100%',
                    minHeight: '58px',
                    maxHeight: '180px',
                    resize: 'none',
                    padding: '16px 18px 8px',
                    lineHeight: 1.5,
                    border: 0,
                    borderRadius: '18px 18px 0 0',
                    background: 'transparent',
                    boxShadow: 'none',
                    boxSizing: 'border-box',
                  }}
                  autoFocus
                />
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    padding: '8px 10px 10px 12px',
                    minHeight: '46px',
                  }}
                >
                  <button
                    type="button"
                    onClick={() => setComposerMenuOpen((open) => !open)}
                    aria-label={t('agent.chat.composer.openMenu', 'Open composer actions')}
                    aria-expanded={composerMenuOpen}
                    title={t('agent.chat.composer.openMenu', 'Open composer actions')}
                    disabled={!canUseComposer}
                    style={{
                      width: '32px',
                      height: '32px',
                      padding: 0,
                      border: '1px solid transparent',
                      borderRadius: '8px',
                      background: composerMenuOpen ? 'var(--bg-secondary)' : 'transparent',
                      color: canUseComposer ? 'var(--text-secondary)' : 'var(--text-tertiary)',
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      cursor: canUseComposer ? 'pointer' : 'not-allowed',
                      flexShrink: 0,
                    }}
                  >
                    <IconPlus size={20} stroke={1.7} />
                  </button>
                  <button
                    type="button"
                    data-testid="session-composer-permission-badge"
                    aria-label={t('agent.chat.composer.permissionTitle', 'Session permission mode')}
                    aria-expanded={permissionMenuOpen}
                    title={t('agent.chat.composer.permissionTitle', 'Session permission mode')}
                    onClick={() => {
                      setPermissionMenuOpen((open) => !open);
                      setComposerMenuOpen(false);
                    }}
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '6px',
                      height: '30px',
                      padding: '0 9px',
                      border: '1px solid transparent',
                      borderRadius: '8px',
                      background: permissionMenuOpen ? 'var(--bg-secondary)' : 'transparent',
                      color: 'rgb(194, 86, 0)',
                      fontSize: '12px',
                      fontWeight: 650,
                      whiteSpace: 'nowrap',
                      cursor: 'pointer',
                    }}
                  >
                    <IconShieldCheck size={15} stroke={1.8} />
                    {permissionModeLabel}
                  </button>
                  <div
                    data-testid="session-composer-permission-menu"
                    hidden={!permissionMenuOpen}
                    style={{
                      position: 'absolute',
                      left: '52px',
                      bottom: '44px',
                      width: '238px',
                      border: '1px solid var(--border-subtle)',
                      borderRadius: '12px',
                      background: 'var(--bg-primary)',
                      boxShadow: '0 18px 48px rgba(15, 23, 42, 0.18)',
                      padding: '6px',
                      zIndex: 17,
                      display: permissionMenuOpen ? 'grid' : 'none',
                      gap: '2px',
                    }}
                  >
                    {SESSION_PERMISSION_MODE_OPTIONS.map((option) => {
                      const selected = option.value === sessionPermissionMode;
                      return (
                        <button
                          key={option.value}
                          type="button"
                          data-testid={`session-composer-permission-mode-${option.value}`}
                          aria-pressed={selected}
                          onClick={() => {
                            onSetSessionPermissionMode?.(option.value);
                            setPermissionMenuOpen(false);
                          }}
                          style={{
                            width: '100%',
                            display: 'grid',
                            gridTemplateColumns: '20px minmax(0, 1fr)',
                            gap: '9px',
                            alignItems: 'center',
                            padding: '9px 10px',
                            border: 0,
                            borderRadius: '8px',
                            background: selected ? 'var(--bg-secondary)' : 'transparent',
                            color: 'var(--text-primary)',
                            cursor: 'pointer',
                            textAlign: 'left',
                          }}
                        >
                          <IconShieldCheck size={15} stroke={selected ? 2.2 : 1.7} />
                          <span style={{ display: 'grid', gap: '2px', minWidth: 0 }}>
                            <strong style={{ fontSize: '12px', fontWeight: 650 }}>
                              {t(`agent.chat.composer.permissionMode.${option.value}`, option.label)}
                            </strong>
                            <span style={{ fontSize: '11px', lineHeight: 1.35, color: 'var(--text-tertiary)' }}>
                              {t(`agent.chat.composer.permissionModeDesc.${option.value}`, option.description)}
                            </span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  {composerIntentLabel && (
                    <span
                      data-testid="session-composer-intent-badge"
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        height: '28px',
                        padding: '0 8px',
                        borderRadius: '8px',
                        background: 'var(--bg-secondary)',
                        color: 'var(--text-secondary)',
                        fontSize: '11px',
                        fontWeight: 650,
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {composerIntentLabel}
                    </span>
                  )}
                  {uploading && uploadProgress >= 0 && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flex: '0 0 140px' }}>
                      {uploadProgress <= 100 ? (
                        <>
                          <div style={{ flex: 1, height: '4px', borderRadius: '2px', background: 'var(--bg-tertiary)', overflow: 'hidden' }}>
                            <div
                              style={{
                                height: '100%',
                                width: '100%',
                                borderRadius: '2px',
                                background: 'var(--accent-primary)',
                                transform: `scaleX(${Math.max(0, Math.min(100, uploadProgress)) / 100})`,
                                transformOrigin: 'left center',
                                transition: 'transform 0.15s ease',
                              }}
                            />
                          </div>
                          <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>{uploadProgress}%</span>
                        </>
                      ) : (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                          <span
                            style={{
                              display: 'inline-block',
                              width: '5px',
                              height: '5px',
                              borderRadius: '50%',
                              background: 'var(--accent-primary)',
                              animation: 'pulse 1.2s ease-in-out infinite',
                            }}
                          />
                          <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>Processing...</span>
                        </div>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          uploadAbortRef.current?.();
                        }}
                        aria-label={t('common.cancel', 'Cancel')}
                        style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', cursor: 'pointer', padding: '0 2px', lineHeight: 1, display: 'inline-flex' }}
                        title="Cancel upload"
                      >
                        <IconX size={13} />
                      </button>
                    </div>
                  )}
                  <span style={{ flex: 1, minWidth: '12px' }} />
                  <span
                    data-testid="session-composer-model-badge"
                    title={modelBadgeTitle || t('agent.chat.composer.modelTitle', 'Model information')}
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '7px',
                      minWidth: 0,
                      maxWidth: '260px',
                      color: 'var(--text-secondary)',
                      fontSize: '12px',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    <IconCircleDashedCheck size={17} stroke={1.9} color="var(--text-tertiary)" />
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{modelBadgeLabel}</span>
                    {runtimeUsageLabel && <span style={{ color: 'var(--text-tertiary)' }}>{runtimeUsageLabel}</span>}
                  </span>
                  {(isStreaming || isWaiting) && (
                    <button className="btn btn-stop-generation" onClick={onAbortGeneration} style={{ padding: '6px 12px' }} title={t('chat.stop', 'Stop')}>
                      <span className="stop-icon" />
                    </button>
                  )}
                  <button
                    data-testid="session-composer-send"
                    className="btn btn-primary"
                    onClick={sendFromComposer}
                    disabled={!canUseComposer || (!chatInput.trim() && attachedFiles.length === 0)}
                    aria-label={t('chat.send')}
                    title={t('chat.send')}
                    style={{
                      width: '38px',
                      height: '38px',
                      padding: 0,
                      borderRadius: '10px',
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <IconSend2 size={18} />
                  </button>
                </div>
              </div>
            </div>
          </>
        )}
      </div>
      {activeSession ? (
        <SessionRuntimePanel
          messages={visibleTimeline}
          sessionWorkbench={sessionWorkbench}
          activeSession={activeSession as Record<string, unknown> | null}
          activeRunStatus={activeRunStatus}
          collapsed={runtimePanelCollapsed}
          onToggleCollapsed={() => setRuntimePanelCollapsed((value) => !value)}
          onOpenDocument={openArtifact}
          onSelectSession={onSelectBranchSession}
          onSelectWorkflowRun={setFocusedWorkflow}
        />
      ) : null}
    </div>
  );
}

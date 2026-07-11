import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconChevronLeft,
  IconChevronRight,
  IconDownload,
  IconExternalLink,
  IconFileText,
  IconGitBranch,
  IconHistory,
  IconLoader2,
  IconThumbDown,
  IconThumbUp,
  IconTrash,
  IconX,
} from '@tabler/icons-react';
import { useMutation, useQuery } from '@tanstack/react-query';

import MarkdownRenderer from '../../components/MarkdownRenderer';
import StreamingMarkdown from '../../components/StreamingMarkdown';
import ThinkingDisclosure from './ThinkingDisclosure';
import type { AgentPermissions } from '../../api/domains/agents';
import AskUserQuestionCard from './AskUserQuestionCard';
import PlanModeRequestCard from './PlanModeRequestCard';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import CopyMessageButton from './CopyMessageButton';
import PlanCard from './PlanCard';
import RunDisclosureBlock from './RunDisclosureBlock';
import SlashCommandMenu from './SlashCommandMenu';
import ChatWorkLedgerDock from './ChatWorkLedgerDock';
import { SessionTransportStatus } from './SessionTransportStatus';
import type { ChatTransportPhase } from './chatTransportRecovery';
import { SessionWorkbenchHeader } from '../session-workbench/SessionWorkbenchChrome';
import { ThreadItemInspector } from '../session-workbench/ThreadItemInspector';
import { shouldRenderThreadItemInConversation, ThreadItemRenderer } from '../session-workbench/ThreadItemRenderer';
import { normalizeThreadItemPayload } from '../session-workbench/threadItemReducer';
import { SessionComposer } from '../session-workbench/SessionComposer';
import { SessionGoalPanel } from '../session-workbench/SessionGoalPanel';
import {
  SessionAgentTeamCloseControl,
  SessionAgentTeamMemberControls,
} from '../session-workbench/SessionAgentTeamControls';
import {
  useResponsiveRuntimePanel,
  useThreadItemRuntimeController,
} from '../session-workbench/threadItemRuntimeController';
import type { ThreadItem } from '../../api/domains/threadItems.generated';
import {
  buildSessionRightPanelModel,
  buildThreadTimeline,
  buildThreadTimelineCached,
  createThreadTimelineCache,
  buildWorkflowRunWindowModel,
  type RuntimeConsoleSegmentKey,
  type RuntimeConsoleWaiterModel,
  type RuntimeSectionItemModel,
  type WorkflowRunActionModel,
  type WorkspaceDocumentGroupModel,
  type WorkspaceDocumentModel,
  type ThreadTimelineModel,
} from '../session-workbench/timelineModel';
import { chatApi, type RecordSessionFeedbackInput } from '../../api/domains/chat';
import { ccParityApi, type SessionContextUsage, type SessionWorkbench } from '../../api/domains/ccParity';
import { fileApi } from '../../api/domains/files';
import { planApi } from '../../api/domains/plans';
import {
  cancelWorkflowRun,
  decideWorkflowGate,
  getWorkflowPreview,
  previewWorkflowCandidate,
  repairWorkflowRun,
  startWorkflow,
  submitWorkflowPromotionProposal,
} from '../../api/domains/workflows';
import { showAppToast } from '../../components/AppDialogs';
import { composerShortcutText } from './sessionComposerShortcuts';
import type { ToolCallMeta, WorkflowPreviewToolMeta } from './toolResultEnvelope';
import {
  computeComposerHeight,
  isA2ASession,
  isDraftHumanChatSession,
  shouldUseWritableSessionSurface,
  type AgentChatMessage,
  type ChatArtifactPart,
  type ChatRuntimeSummary,
  type RuntimePhase,
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
const EMPTY_CHAT_MESSAGES: AgentChatMessage[] = [];

const SESSION_PERMISSION_MODE_OPTIONS: Array<{
  value: SessionPermissionMode;
  label: string;
  description: string;
}> = [
  {
    value: 'default',
    label: 'Ask first',
    description: 'Ask before sensitive session actions',
  },
  {
    value: 'auto',
    label: 'Approve for me',
    description: 'Approve low-risk actions and ask for risky ones',
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
  goalModeRequested: boolean,
  t: (key: string, fallback: string) => string,
): string | null {
  if (goalModeRequested) return t('agent.chat.composer.goalModeActive', 'Goal mode');
  if (planModeRequested) return t('agent.chat.composer.planModeActive', 'Plan Mode');
  return null;
}

export function permissionOnceOnlyMessageKey(request: SessionPermissionRequest): string {
  const destructive = request.destructive === true
    || request.risk_class === 'destructive_delete'
    || request.confirmation_kind === 'destructive_once';
  return destructive ? 'agent.chat.permission.deleteOnceOnly' : 'agent.chat.permission.onceOnly';
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
          {permissionOnceOnlyMessageKey(permissionRequest) === 'agent.chat.permission.deleteOnceOnly'
            ? t('agent.chat.permission.deleteOnceOnly', 'Delete actions can only be allowed once.')
            : t('agent.chat.permission.onceOnly', 'This action can only be allowed once.')}
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

export type SessionRewindMode = 'conversation' | 'workspace' | 'both';

export const SESSION_REWIND_MODE_OPTIONS: Array<{
  value: SessionRewindMode;
  label: string;
  description: string;
}> = [
  {
    value: 'conversation',
    label: 'Conversation',
    description: 'Rewind only the chat projection',
  },
  {
    value: 'workspace',
    label: 'Workspace',
    description: 'Restore only workspace files',
  },
  {
    value: 'both',
    label: 'Both',
    description: 'Rewind chat and restore files',
  },
];

export function normalizeSessionRewindMode(value: unknown): SessionRewindMode {
  return value === 'workspace' || value === 'both' ? value : 'conversation';
}

export function buildSessionRewindCommandArgs(
  checkpointEventId: string,
  mode: SessionRewindMode = 'conversation',
  confirmWorkspaceRestore = false,
  expectedLastSequence?: number | null,
): Record<string, unknown> {
  return {
    checkpoint_event_id: checkpointEventId,
    mode,
    ...(confirmWorkspaceRestore ? { confirm_workspace_restore: true } : {}),
    ...(typeof expectedLastSequence === 'number' ? { expected_last_sequence: expectedLastSequence } : {}),
  };
}

export type SessionCommandControlType =
  | 'checkpoint_selector'
  | 'projection_status'
  | 'context_panel'
  | 'usage_panel'
  | 'export_panel'
  | 'side_question'
  | 'permissions_panel'
  | 'workspace_restore_confirmation'
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
  transportPhase?: ChatTransportPhase;
  transportReconnectAttempt?: number;
  onReconnectTransport?: () => void;
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
  onLoadOlderMessages: () => void;
  olderMessagesLoading: boolean;
  hasOlderMessages: boolean;
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
  /** Turn lifecycle phase for the active session (§3 seam 1). Rendering seams 2-4 consume this. */
  runtimePhase?: RuntimePhase;
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
  goalModeRequested?: boolean;
  onToggleGoalMode?: () => void;
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

export function findRetryAnchorMessage(
  messages: AgentChatMessage[],
  errorIndex: number,
): AgentChatMessage | null {
  for (let index = Math.min(errorIndex, messages.length) - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'user') return messages[index];
  }
  return null;
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
const SESSION_CHECKPOINT_PREVIEW_MAX_CHARS = 22;
const SESSION_CHECKPOINT_PREVIEW_ELLIPSIS = '...';

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
  if (intent.length <= SESSION_CHECKPOINT_PREVIEW_MAX_CHARS) return intent;
  return `${intent.slice(0, SESSION_CHECKPOINT_PREVIEW_MAX_CHARS - SESSION_CHECKPOINT_PREVIEW_ELLIPSIS.length).trimEnd()}${SESSION_CHECKPOINT_PREVIEW_ELLIPSIS}`;
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
  rewindAnchorCheckpointId,
  onNavigateCheckpoint,
  onNavigateBranch,
}: {
  activeSessionId?: string | null;
  axisSessionId?: string | null;
  checkpoints: Array<Record<string, unknown>>;
  focusedCheckpointId?: string | null;
  lineage: BranchLineageItem[];
  loading?: boolean;
  rewindAnchorCheckpointId?: string | null;
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
  // Rewound tail (soft projection): checkpoints after the rewind anchor stay
  // navigable but render dimmed — the transcript keeps them, the head moved.
  const rewindAnchorIndex = rewindAnchorCheckpointId
    ? checkpoints.findIndex((checkpoint) => sessionCheckpointId(checkpoint) === rewindAnchorCheckpointId)
    : -1;
  const density = getSessionGitLineDensity(checkpoints.length + branchRows.length);
  const densityClass = `is-${density}`;
  const [expandedBranchAnchorId, setExpandedBranchAnchorId] = React.useState<string | null>(null);
  const [hoverPreview, setHoverPreview] = React.useState<{
    label: string;
    meta: string[];
    top: number;
    left: number;
  } | null>(null);
  const openPreview = React.useCallback((
    event: React.MouseEvent<HTMLButtonElement> | React.FocusEvent<HTMLButtonElement>,
    label: string,
    meta: string[] = [],
  ) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setHoverPreview({
      label,
      meta,
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
          const isRewoundTail = rewindAnchorIndex >= 0 && index > rewindAnchorIndex;
          const nodeState = isRewoundTail
            ? 'rewound_tail'
            : rewindAnchorIndex >= 0 && index === rewindAnchorIndex
              ? 'current_head'
              : 'past';
          const previewMeta = [
            String((checkpoint as Record<string, unknown>).created_at || '').slice(0, 16),
            isRewoundTail ? t('agent.chat.gitline.rewoundTail', 'rewound — head moved before this point') : '',
          ].filter(Boolean) as string[];
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
                className={`session-gitline-node ${isFocused ? 'is-focused' : ''} ${isRewoundTail ? 'is-rewound-tail' : ''}`}
                data-state={nodeState}
                title={previewLabel}
                aria-label={previewLabel}
                onMouseEnter={(event) => openPreview(event, previewLabel, previewMeta)}
                onFocus={(event) => openPreview(event, previewLabel, previewMeta)}
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
                      onMouseEnter={(event) => openPreview(
                        event,
                        t('agent.chat.branch.branches', 'Branches'),
                        anchoredBranches.map((row) => `${branchModeLabel(row)} · ${row.title || row.id}`),
                      )}
                      onFocus={(event) => openPreview(
                        event,
                        t('agent.chat.branch.branches', 'Branches'),
                        anchoredBranches.map((row) => `${branchModeLabel(row)} · ${row.title || row.id}`),
                      )}
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
          className="session-gitline-preview session-gitline-hovercard"
          style={{ top: `${hoverPreview.top}px`, left: `${hoverPreview.left}px` }}
          role="tooltip"
          data-testid="session-gitline-hovercard"
        >
          <strong>{hoverPreview.label}</strong>
          {hoverPreview.meta.map((line, index) => (
            <div key={index}>{line}</div>
          ))}
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
    case 'workspace_restore_confirmation':
      return 'Workspace restore';
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

function recordValue(value: unknown): Record<string, unknown> {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function workspaceRestoreConfirmationArgs(control: SessionCommandControlState): {
  checkpointEventId: string;
  mode: SessionRewindMode;
  expectedLastSequence: number | null;
} {
  const payload = recordValue(control.payload);
  const uiAction = recordValue(payload.ui_action);
  const debugPayload = recordValue(payload.debug_payload);
  const checkpoint = recordValue(payload.checkpoint);
  const checkpointEventId = String(
    uiAction.checkpoint_event_id
      || debugPayload.checkpoint_event_id
      || checkpoint.checkpoint_event_id
      || '',
  ).trim();
  const mode = normalizeSessionRewindMode(
    uiAction.requested_mode
      || debugPayload.requested_mode
      || payload.requested_mode,
  );
  const rewindGuard = recordValue(payload.rewind_guard);
  const rawSequence = rewindGuard.last_sequence;
  const expectedLastSequence = typeof rawSequence === 'number'
    ? rawSequence
    : (typeof rawSequence === 'string' && rawSequence.trim() ? Number(rawSequence) : null);
  return {
    checkpointEventId,
    mode,
    expectedLastSequence: Number.isFinite(expectedLastSequence) ? expectedLastSequence : null,
  };
}

export function SessionCommandControlPanel({
  control,
  onDismiss,
  onRunCommand,
  rewindUnavailableReason,
}: {
  control?: SessionCommandControlState | null;
  onDismiss: () => void;
  onRunCommand: (command: string, args?: Record<string, unknown>) => void | Promise<unknown>;
  rewindUnavailableReason?: string | null;
}) {
  const checkpoints = control?.checkpoints || [];
  const checkpointIds = checkpoints.map(checkpointId);
  const availableCheckpointIds = checkpointIds.filter(Boolean);
  const defaultFocusedCheckpointId = availableCheckpointIds.length
    ? availableCheckpointIds[availableCheckpointIds.length - 1]
    : null;
  const [focusedCheckpointId, setFocusedCheckpointId] = React.useState<string | null>(defaultFocusedCheckpointId);
  const [rewindMode, setRewindMode] = React.useState<SessionRewindMode>('conversation');
  React.useEffect(() => {
    setFocusedCheckpointId((current) => {
      if (current && checkpointIds.includes(current)) return current;
      return defaultFocusedCheckpointId;
    });
  }, [defaultFocusedCheckpointId, checkpointIds.join('|')]);
  if (!control) return null;
  const details = payloadSummary(control.payload);
  const confirmationArgs = control.type === 'workspace_restore_confirmation'
    ? workspaceRestoreConfirmationArgs(control)
    : null;
  const rewindGuard = recordValue(control.payload?.rewind_guard);
  const rawExpectedSequence = rewindGuard.last_sequence;
  const expectedLastSequence = typeof rawExpectedSequence === 'number'
    ? rawExpectedSequence
    : (typeof rawExpectedSequence === 'string' && rawExpectedSequence.trim() ? Number(rawExpectedSequence) : null);
  const hasPanelBody = checkpoints.length > 0 || details.length > 0 || Boolean(confirmationArgs);
  return (
    <section data-testid="session-command-control-panel" className="session-tui-command-panel">
      <div className={`session-tui-command-panel-header ${hasPanelBody ? 'has-body' : ''}`}>
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
          <div className="session-tui-rewind-mode-group" role="group" aria-label="Rewind mode">
            {SESSION_REWIND_MODE_OPTIONS.map(option => (
              <button
                key={option.value}
                type="button"
                data-testid={`session-rewind-mode-${option.value}`}
                data-rewind-mode={option.value}
                aria-pressed={rewindMode === option.value}
                title={option.description}
                onClick={() => setRewindMode(option.value)}
                className={`session-tui-rewind-mode-button ${rewindMode === option.value ? 'is-active' : ''}`}
              >
                {option.label}
              </button>
            ))}
          </div>
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
                        data-rewind-mode={rewindMode}
                        disabled={Boolean(rewindUnavailableReason)}
                        title={rewindUnavailableReason || undefined}
                        onClick={() => onRunCommand(
                          'rewind',
                          buildSessionRewindCommandArgs(
                            id,
                            rewindMode,
                            false,
                            Number.isFinite(expectedLastSequence) ? expectedLastSequence : null,
                          ),
                        )}
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
          {rewindUnavailableReason ? (
            <div className="session-tui-row-meta" role="status">{rewindUnavailableReason}</div>
          ) : null}
        </div>
      ) : null}
      {confirmationArgs ? (
        <div className="session-tui-command-panel-body">
          <div className="session-tui-confirm-row">
            <button
              type="button"
              data-testid="session-workspace-restore-confirm-action"
              data-session-command="rewind"
              data-rewind-mode={confirmationArgs.mode}
              disabled={!confirmationArgs.checkpointEventId || Boolean(rewindUnavailableReason)}
              title={rewindUnavailableReason || undefined}
              onClick={() => onRunCommand(
                'rewind',
                buildSessionRewindCommandArgs(
                  confirmationArgs.checkpointEventId,
                  confirmationArgs.mode,
                  true,
                  confirmationArgs.expectedLastSequence,
                ),
              )}
            >
              Confirm restore
            </button>
            <button
              type="button"
              data-testid="session-workspace-restore-cancel-action"
              onClick={onDismiss}
            >
              Cancel
            </button>
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

export type ArtifactDeliveryContext = 'assistant' | 'tool';

export function isUserFacingDeliveryArtifact(
  artifact: Partial<ChatArtifactPart>,
  context: ArtifactDeliveryContext = 'assistant',
): boolean {
  if (context === 'assistant') return true;
  const source = String(artifact.source || '').toLowerCase();
  return (
    source.includes('artifact_delivery')
    || source.includes('a2a_delivery_ref')
    || source.includes('terminal')
    || source.includes('final')
  );
}

function artifactWorkspaceAgentId(
  artifact: Pick<ChatArtifactPart, 'downloadAgentId' | 'ownerAgentId' | 'sourceAgentId'>,
  fallbackAgentId?: string | null,
): string | null {
  return artifact.downloadAgentId || artifact.ownerAgentId || artifact.sourceAgentId || fallbackAgentId || null;
}

function shortArtifactAgentId(value: unknown): string {
  const text = stringValue(value).trim();
  if (!text) return '';
  return text.length > 12 ? text.slice(0, 8) : text;
}

function artifactContributorLabel(artifact: ChatArtifactPart, fallbackAgent?: Record<string, unknown> | null): string {
  const explicitName = stringValue(
    artifact.sourceAgentName
    || artifact.ownerAgentName
    || artifact.downloadAgentName
    || artifact.deliveryAgentName,
  ).trim();
  if (explicitName) return explicitName;

  const contributorId = stringValue(
    artifact.sourceAgentId
    || artifact.ownerAgentId
    || artifact.downloadAgentId
    || artifact.deliveryAgentId,
  ).trim();
  if (!contributorId) return stringValue(fallbackAgent?.name).trim();
  if (contributorId === stringValue(fallbackAgent?.id).trim()) {
    return stringValue(fallbackAgent?.name).trim() || shortArtifactAgentId(contributorId);
  }
  return shortArtifactAgentId(contributorId);
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
  context = 'assistant',
  operatorView = false,
}: {
  agentId?: string | null;
  artifacts?: ChatArtifactPart[];
  onOpenArtifact?: (artifact: ChatArtifactPart) => void;
  context?: ArtifactDeliveryContext;
  operatorView?: boolean;
}) {
  const { t } = useTranslation();
  const visibleArtifacts = (artifacts || []).filter((artifact) => (
    artifact.path && isUserFacingDeliveryArtifact(artifact, context)
  ));
  if (!agentId || visibleArtifacts.length === 0) return null;

  // Codex 式聚合卡：一个容器承载全部交付物，每文件一行，动作 hover 浮现。
  return (
    <div className="chat-artifacts" data-testid="chat-artifacts-card">
      <div className="chat-artifacts-title">
        <IconFileText size={13} />
        {t('agent.chat.artifacts.count', '{{count}} files', { count: visibleArtifacts.length })}
      </div>
      {visibleArtifacts.map((artifact) => {
        const downloadAgentId = artifactWorkspaceAgentId(artifact, agentId);
        const authority = operatorView
          ? { operatorView: true, reason: 'Agent session administration' }
          : undefined;
        const href = downloadAgentId
          ? (artifact.id
              ? fileApi.artifactDownloadUrl(downloadAgentId, artifact.id, authority)
              : fileApi.downloadUrl(downloadAgentId, artifact.path, authority))
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
            className="chat-artifact-row"
          >
            <IconFileText size={14} className="chat-artifact-icon" />
            <span className="chat-artifact-name">{artifact.name}</span>
            <span className="chat-artifact-meta">
              {[artifact.previewKind, size].filter(Boolean).join(' · ') || artifact.path}
            </span>
            <span className="chat-artifact-actions">
              <button
                type="button"
                data-testid="chat-artifact-open"
                className="chat-artifact-action"
                onClick={(event) => {
                  event.stopPropagation();
                  openArtifact();
                }}
              >
                <IconExternalLink size={12} />
                {t('agent.chat.artifacts.open', 'Open')}
              </button>
              <a
                href={href}
                download={artifact.name}
                className="chat-artifact-action"
                onClick={(event) => event.stopPropagation()}
              >
                <IconDownload size={12} />
                {t('agent.chat.artifacts.download', 'Download')}
              </a>
            </span>
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

const RUNTIME_PHASE_LABEL_KEYS: Record<string, string> = {
  queued: 'agent.chat.phase.queued',
  resuming: 'agent.chat.phase.resuming',
  starting: 'agent.chat.phase.starting',
  thinking: 'agent.chat.phase.thinking',
  responding: 'agent.chat.phase.responding',
  tool_running: 'agent.chat.phase.tool_running',
  hook_evaluating: 'agent.chat.phase.hook_evaluating',
  compacting: 'agent.chat.phase.compacting',
  awaiting_approval: 'agent.chat.phase.awaiting_approval',
  awaiting_budget: 'agent.chat.phase.awaiting_budget',
  summarizing: 'agent.chat.phase.summarizing',
  continuation_gap: 'agent.chat.phase.continuation_gap',
};

function formatElapsedSeconds(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${String(seconds).padStart(2, '0')}s` : `${seconds}s`;
}

/** §3 seam 2: the activeTail working-state line — phase label + live stopwatch. */
export function ActiveTailStatusLine({
  phase,
  detail,
  startedAt,
}: {
  phase: string;
  detail?: string | null;
  startedAt?: string | null;
}) {
  const { t } = useTranslation();
  const anchorRef = React.useRef<number | null>(null);
  if (anchorRef.current === null) {
    const parsed = startedAt ? Date.parse(startedAt) : NaN;
    anchorRef.current = Number.isFinite(parsed) ? parsed : Date.now();
  }
  const [nowTick, setNowTick] = React.useState<number>(() => Date.now());
  React.useEffect(() => {
    const timer = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const labelKey = RUNTIME_PHASE_LABEL_KEYS[phase];
  if (!labelKey) return null;
  const elapsedSeconds = Math.max(0, Math.floor((nowTick - (anchorRef.current ?? nowTick)) / 1000));
  return (
    <div className="session-tui-active-tail" data-testid="active-tail-status" data-phase={phase}>
      <span className="session-tui-active-tail-dot" />
      <span className="session-tui-shimmer">{t(labelKey)}</span>
      {detail ? <span className="session-tui-active-tail-detail">{detail}</span> : null}
      <span className="session-tui-active-tail-elapsed">{formatElapsedSeconds(elapsedSeconds)}</span>
    </div>
  );
}

const LINKED_HIGHLIGHT_CLASS = 'is-linked-highlight';

/** §3 seam 4: middle-stream <-> right-panel linking is pure DOM decoration —
 * zero React re-renders on hover (the remount-storm lesson). */
function setRuntimeLinkHighlight(runtimeId: string | null | undefined, active: boolean): void {
  if (!runtimeId || typeof document === 'undefined') return;
  document
    .querySelectorAll(`[data-runtime-link-id="${CSS.escape(String(runtimeId))}"]`)
    .forEach((element) => element.classList.toggle(LINKED_HIGHLIGHT_CLASS, active));
}

function scrollToRuntimeLinkMarker(runtimeId: string | null | undefined): void {
  if (!runtimeId || typeof document === 'undefined') return;
  const marker = document.querySelector(
    `.session-tui-render-cell[data-runtime-link-id="${CSS.escape(String(runtimeId))}"]`,
  );
  marker?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function runtimeMetricSummary(item: RuntimeSectionItemModel): string {
  return [
    item.metrics.elapsedLabel,
    item.metrics.tokenLabel ? `${item.metrics.tokenLabel} tokens` : null,
    item.metrics.toolUseLabel ? `${item.metrics.toolUseLabel} tools` : null,
  ].filter(Boolean).join(' · ');
}

export function runtimeItemDisplayMeta(item: RuntimeSectionItemModel): string {
  const metadata = isRuntimeRecord(item.raw.metadata) ? item.raw.metadata : {};
  const role = stringValue(
    item.raw.member_role
      || item.raw.memberRole
      || item.raw.role
      || metadata.member_role
      || metadata.memberRole,
  ).trim();
  return [role, item.summary, runtimeMetricSummary(item)].filter(Boolean).join(' · ');
}

export function runtimeItemDisplayStatus(item: RuntimeSectionItemModel): string {
  const metadata = isRuntimeRecord(item.raw.metadata) ? item.raw.metadata : {};
  const status = stringValue(item.status || item.state || 'unknown').trim() || 'unknown';
  const closeStatus = item.runtimeKind === 'agent_team'
    ? stringValue(item.raw.close_status || item.raw.closeStatus).trim().toLowerCase()
    : '';
  const outcome = closeStatus === 'failed' ? 'close failed' : stringValue(
    item.raw.last_turn_status
      || item.raw.lastTurnStatus
      || metadata.last_turn_status
      || metadata.lastTurnStatus,
  ).trim();
  const safeStatus = userFacingRuntimeStatus(status);
  const safeOutcome = outcome ? userFacingRuntimeStatus(outcome) : '';
  return safeOutcome && safeOutcome !== safeStatus ? `${safeStatus} · ${safeOutcome}` : safeStatus;
}

export function subagentWorkerRecoveryModel(worker: RuntimeSectionItemModel): {
  canRequestNewWorker: boolean;
  requiresPlatformAdmin: boolean;
} {
  const status = String(worker.status || worker.state || '').trim().toLowerCase();
  const decision = isRuntimeRecord(worker.raw.subagent_decision_entry)
    ? worker.raw.subagent_decision_entry
    : {};
  const requiredUserAction = stringValue(decision.required_user_action).trim().toLowerCase();
  const requiresPlatformAdmin = status === 'needs_reconciliation'
    || worker.userBlocker?.kind === 'runtime_reconciliation'
    || requiredUserAction === 'approve_reconciliation_retry';
  return {
    canRequestNewWorker: !requiresPlatformAdmin && status === 'failed',
    requiresPlatformAdmin,
  };
}

function runtimeItemDisplayLabel(item: RuntimeSectionItemModel, fallback: string): string {
  const label = stringValue(item.label).trim();
  if (!label || label === item.id || isUuidLike(label)) return fallback;
  return label;
}

export function userFacingRuntimeStatus(status: unknown): string {
  const normalized = String(status || '').trim().toLowerCase();
  const labels: Record<string, string> = {
    idle: 'Ready',
    queued: 'Queued',
    pending: 'Queued',
    running: 'Working',
    streaming: 'Working',
    resuming: 'Resuming',
    waiting: 'Waiting',
    waiting_user: 'Waiting for you',
    awaiting_approval: 'Waiting for approval',
    waiting_budget_approval: 'Waiting for approval',
    completed: 'Completed',
    succeeded: 'Completed',
    failed: 'Needs attention',
    killed: 'Stopped',
    cancelled: 'Cancelled',
    exhausted: 'Paused',
    hard_stopped: 'Stopped',
    needs_reconciliation: 'Needs admin review',
  };
  const exact = labels[normalized];
  if (exact) return exact;
  if (normalized.includes('approval') || normalized.includes('confirm') || normalized.includes('wait')) return 'Waiting';
  if (normalized.includes('fail') || normalized.includes('error') || normalized.includes('reconcil')) return 'Needs attention';
  if (normalized.includes('complete') || normalized.includes('success') || normalized.includes('done')) return 'Completed';
  if (normalized.includes('cancel') || normalized.includes('stop') || normalized.includes('kill')) return 'Stopped';
  if (normalized.includes('queue') || normalized.includes('pending')) return 'Queued';
  return 'Working';
}

function SessionRuntimePanel({
  messages,
  sessionWorkbench,
  activeSession,
  agent,
  activeRunStatus,
  collapsed = false,
  onToggleCollapsed,
  onOpenDocument,
  onSelectSession,
  onSelectWorkflowRun,
  selectedThreadItem,
  onClearSelectedThreadItem,
  agentId,
  sessionId,
  onGoalChanged,
  onTeamChanged,
  onRetrySubagent,
}: {
  messages: AgentChatMessage[];
  sessionWorkbench: SessionWorkbench | null;
  activeSession?: Record<string, unknown> | null;
  agent?: Record<string, unknown> | null;
  activeRunStatus?: string | null;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onOpenDocument?: (artifact: ChatArtifactPart) => void | Promise<unknown>;
  onSelectSession?: (sessionId: string) => void | Promise<unknown>;
  onSelectWorkflowRun?: (workflow: RuntimeSectionItemModel) => void | Promise<unknown>;
  selectedThreadItem?: ThreadItem | null;
  onClearSelectedThreadItem?: () => void;
  agentId?: string;
  sessionId?: string;
  onGoalChanged?: () => void | Promise<unknown>;
  onTeamChanged?: () => void | Promise<unknown>;
  onRetrySubagent?: (worker: RuntimeSectionItemModel) => void | Promise<unknown>;
}) {
  const RUNTIME_PANEL_WIDTH_KEY = 'hive.sessionRuntimePanel.width';
  const [panelWidth, setPanelWidth] = React.useState<number | null>(() => {
    try {
      const stored = window.localStorage.getItem(RUNTIME_PANEL_WIDTH_KEY);
      const parsed = stored ? Number.parseInt(stored, 10) : NaN;
      return Number.isFinite(parsed) ? Math.min(560, Math.max(360, parsed)) : null;
    } catch {
      return null;
    }
  });
  const [resizeDragging, setResizeDragging] = React.useState(false);
  const startPanelResize = React.useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = (event.currentTarget.parentElement as HTMLElement | null)?.getBoundingClientRect().width ?? 360;
    setResizeDragging(true);
    const onMove = (move: MouseEvent) => {
      const next = Math.min(560, Math.max(360, Math.round(startWidth + (startX - move.clientX))));
      setPanelWidth(next);
    };
    const onUp = (up: MouseEvent) => {
      const finalWidth = Math.min(560, Math.max(360, Math.round(startWidth + (startX - up.clientX))));
      setPanelWidth(finalWidth);
      try {
        window.localStorage.setItem(RUNTIME_PANEL_WIDTH_KEY, String(finalWidth));
      } catch {
        // persistence is best-effort
      }
      setResizeDragging(false);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  const { t } = useTranslation();
  const rightPanel = buildSessionRightPanelModel({
    messages,
    sessionWorkbench: sessionWorkbench as Record<string, unknown> | null,
    activeSession,
    activeRunStatus,
  });
  const docs = rightPanel.workspaceDocuments;
  const currentSessionDocumentCount = docs.currentSession.items.length;
  const runtimeConsole = rightPanel.runtimeConsole;
  const [showAllCurrentDocuments, setShowAllCurrentDocuments] = React.useState(false);
  const [runtimeSegmentOverride, setRuntimeSegmentOverride] = React.useState<RuntimeConsoleSegmentKey | null>(null);
  const selectedRuntimeSegment: RuntimeConsoleSegmentKey = runtimeSegmentOverride && runtimeConsole.segments.some((segment) => segment.key === runtimeSegmentOverride)
    ? runtimeSegmentOverride
    : runtimeConsole.defaultSegment;
  const runStatusCount = runtimeConsole.summary.runningCount > 0
    ? t('sessionWorkbench.rightPanel.runningCount', '{{count}} running', { count: runtimeConsole.summary.runningCount })
    : t('sessionWorkbench.rightPanel.totalCount', '{{count}} total', { count: runtimeConsole.summary.totalCount });
  const runtimeWaiterTestId = (waiter: RuntimeConsoleWaiterModel): string => (
    `session-runtime-waiter-${waiter.id.replace(/[^a-zA-Z0-9_-]/g, '-')}`
  );

  const renderRuntimeItem = (item: RuntimeSectionItemModel, fallback: string) => {
    const sessionId = item.childSessionId;
    const clickable = Boolean(item.enterable && sessionId && onSelectSession);
    const meta = runtimeItemDisplayMeta(item);
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{runtimeItemDisplayLabel(item, fallback)}</span>
          <span className="session-runtime-row-meta">{meta || t('sessionWorkbench.rightPanel.noAdditionalDetails', 'No additional details')}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(item)}</span>
      </>
    );
    const linkId = item.childSessionId || item.id;
    const linkProps = {
      'data-runtime-link-id': linkId,
      onMouseEnter: () => setRuntimeLinkHighlight(linkId, true),
      onMouseLeave: () => setRuntimeLinkHighlight(linkId, false),
    } as const;
    return clickable ? (
      <button
        key={item.id}
        type="button"
        className="session-runtime-row session-runtime-row-button"
        onClick={() => sessionId && onSelectSession?.(sessionId)}
        {...linkProps}
      >
        {content}
      </button>
    ) : (
      <div
        key={item.id}
        className="session-runtime-row"
        {...linkProps}
        onClick={() => scrollToRuntimeLinkMarker(linkId)}
      >
        {content}
      </div>
    );
  };

  const renderRuntimeWaiter = (waiter: RuntimeConsoleWaiterModel) => {
    const sessionId = waiter.childSessionId;
    const clickable = Boolean(waiter.enterable && sessionId && onSelectSession);
    const segmentLabel = t(`sessionWorkbench.rightPanel.runtimeSegments.${waiter.segment}`, waiter.segment);
    const blocker = waiter.userBlocker;
    const meta = blocker
      ? [blocker.reason, blocker.nextAction].filter(Boolean).join(' ')
      : [waiter.summary, segmentLabel].filter(Boolean).join(' · ');
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{blocker?.title || waiter.label}</span>
          <span className="session-runtime-row-meta">{meta || waiter.summary || waiter.label}</span>
        </span>
        <span className="session-runtime-status">{userFacingRuntimeStatus(waiter.status || 'waiting')}</span>
      </>
    );
    return clickable ? (
      <button
        key={`${waiter.segment}:${waiter.id}`}
        type="button"
        data-testid={runtimeWaiterTestId(waiter)}
        data-runtime-waiter-segment={waiter.segment}
        className="session-runtime-row session-runtime-row-button"
        title={t('sessionWorkbench.rightPanel.waiterOpenTitle', 'Open waiting session')}
        onClick={() => sessionId && onSelectSession?.(sessionId)}
      >
        {content}
      </button>
    ) : (
      <div
        key={`${waiter.segment}:${waiter.id}`}
        data-testid={runtimeWaiterTestId(waiter)}
        data-runtime-waiter-segment={waiter.segment}
        className="session-runtime-row"
      >
        {content}
      </div>
    );
  };

  const renderWorkflowRoot = (workflow: RuntimeSectionItemModel, fallback: string) => {
    const meta = runtimeItemDisplayMeta(workflow);
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{runtimeItemDisplayLabel(workflow, fallback)}</span>
          <span className="session-runtime-row-meta">{meta}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(workflow)}</span>
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

  const renderTeamMemberItem = (
    team: RuntimeSectionItemModel,
    member: RuntimeSectionItemModel,
    fallback: string,
  ) => {
    const sessionId = member.childSessionId;
    const meta = runtimeItemDisplayMeta(member);
    return (
      <div key={member.id || fallback} className="session-runtime-row" data-testid="session-agent-team-member-row">
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{member.label || fallback}</span>
          <span className="session-runtime-row-meta">{meta}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(member)}</span>
        {agentId ? (
          <SessionAgentTeamMemberControls
            agentId={agentId}
            teamId={team.id}
            teamStatus={team.status}
            member={member}
            onEnter={sessionId && onSelectSession ? () => onSelectSession(sessionId) : undefined}
            onChanged={onTeamChanged}
          />
        ) : null}
      </div>
    );
  };

  const renderSubagentWorkerItem = (worker: RuntimeSectionItemModel, fallback: string) => {
    const sessionId = worker.childSessionId;
    const canInspect = Boolean(sessionId && onSelectSession);
    const meta = runtimeItemDisplayMeta(worker);
    const recovery = subagentWorkerRecoveryModel(worker);
    const workerAction = (
      action: 'inspect' | 'retry',
      label: string,
      disabled: boolean,
      title: string,
      onClick?: () => void,
    ) => (
      <button
        key={action}
        type="button"
        data-runtime-action={`subagent-worker-${action}`}
        className="session-runtime-action-button"
        disabled={disabled}
        title={title}
        onClick={onClick}
      >
        {label}
      </button>
    );
    return (
      <div key={worker.id || fallback} className="session-runtime-row" data-testid="session-subagent-worker-row">
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{worker.label || fallback}</span>
          <span className="session-runtime-row-meta">{meta}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(worker)}</span>
        <span className="session-runtime-actions" data-testid="session-subagent-worker-actions">
          {workerAction(
            'inspect',
            t('sessionWorkbench.rightPanel.workerInspect', 'Inspect'),
            !canInspect,
            canInspect
              ? t('sessionWorkbench.rightPanel.workerInspectTitle', 'Inspect worker session')
              : t('sessionWorkbench.rightPanel.workerInspectDisabled', 'No worker session is available'),
            canInspect && sessionId ? () => onSelectSession?.(sessionId) : undefined,
          )}
          {recovery.canRequestNewWorker
            ? workerAction(
                'retry',
                t('sessionWorkbench.rightPanel.workerRetry', 'Retry with new worker'),
                !onRetrySubagent,
                t(
                  'sessionWorkbench.rightPanel.workerRetryTitle',
                  'Ask the main Agent to inspect the failure and create a new one-shot worker if safe',
                ),
                onRetrySubagent ? () => void onRetrySubagent(worker) : undefined,
              )
            : null}
        </span>
      </div>
    );
  };

  const renderWorkflowSegment = () => (
    <div data-testid="session-runtime-segment-body-workflow" className="session-runtime-segment-body" role="tabpanel">
      {runtimeConsole.workflow.items.length === 0 ? (
        <div className="session-runtime-empty">{t('sessionWorkbench.rightPanel.noWorkflows', 'No active workflows.')}</div>
      ) : (
        runtimeConsole.workflow.items.map((workflow, index) => (
          <div key={workflow.id} className="session-runtime-team">
            {renderWorkflowRoot(workflow, `workflow-${index + 1}`)}
            {workflow.steps.map((step, stepIndex) => renderRuntimeItem(step, `workflow-step-${stepIndex + 1}`))}
            {workflow.leafCalls.map((leafCall, leafIndex) => renderRuntimeItem(leafCall, `workflow-leaf-${leafIndex + 1}`))}
          </div>
        ))
      )}
    </div>
  );

  const renderTeamSegment = () => (
    <div data-testid="session-runtime-segment-body-team" className="session-runtime-segment-body" role="tabpanel">
      {runtimeConsole.team.items.length === 0 ? (
        <div className="session-runtime-empty">{t('sessionWorkbench.rightPanel.noAgentTeams', 'No Agent Team containers in this session.')}</div>
      ) : (
        runtimeConsole.team.items.map((team) => (
          <div key={team.id} className="session-runtime-team">
            <div className="session-runtime-team-header">
              <span>{runtimeItemDisplayLabel(team, t('sessionWorkbench.rightPanel.agentTeam', 'Agent Team'))}</span>
              <span className="session-agent-team-close-control">
                <small>{runtimeItemDisplayStatus(team)}</small>
                {agentId ? (
                  <SessionAgentTeamCloseControl agentId={agentId} team={team} onChanged={onTeamChanged} />
                ) : null}
              </span>
            </div>
            {team.members.length === 0 ? (
              <div className="session-runtime-empty">
                {t('sessionWorkbench.rightPanel.noTeamMembers', 'No team members have started yet.')}
              </div>
            ) : (
              team.members.map((member, index) => renderTeamMemberItem(team, member, `team-member-${index + 1}`))
            )}
          </div>
        ))
      )}
    </div>
  );

  const renderWorkersSegment = () => (
    <div data-testid="session-runtime-segment-body-workers" className="session-runtime-segment-body" role="tabpanel">
      {runtimeConsole.workers.items.length === 0 ? (
        <div className="session-runtime-empty">{t('sessionWorkbench.rightPanel.noSubagents', 'No one-shot Sub-agent workers in this session.')}</div>
      ) : (
        runtimeConsole.workers.items.map((worker, index) => renderSubagentWorkerItem(worker, `subagent-worker-${index + 1}`))
      )}
    </div>
  );

  const renderActivityBucket = (
    testId: string,
    title: string,
    items: RuntimeSectionItemModel[],
    empty: string,
  ) => {
    return (
      <div data-testid={testId} className="session-runtime-activity-bucket">
        <div className="session-runtime-card-title">{title}</div>
        {items.length === 0 ? (
          <div className="session-runtime-empty">{empty}</div>
        ) : (
          items.map((item, index) => renderRuntimeItem(item, `${title}-${index + 1}`))
        )}
      </div>
    );
  };

  const renderActivitySegment = () => (
    <div data-testid="session-runtime-segment-body-activity" className="session-runtime-segment-body" role="tabpanel">
      {renderActivityBucket(
        'session-runtime-activity-background',
        t('sessionWorkbench.rightPanel.backgroundAgents', 'Background agents'),
        runtimeConsole.activity.background,
        t('sessionWorkbench.rightPanel.noBackgroundAgents', 'No background agents running.'),
      )}
      {renderActivityBucket(
        'session-runtime-activity-notifications',
        t('sessionWorkbench.rightPanel.notifications', 'Notifications'),
        runtimeConsole.activity.notifications,
        t('sessionWorkbench.rightPanel.noCompletionWakes', 'No completion notifications.'),
      )}
      {renderActivityBucket(
        'session-runtime-activity-runs',
        t('sessionWorkbench.rightPanel.runs', 'Runs'),
        runtimeConsole.activity.runs,
        t('sessionWorkbench.rightPanel.noRuns', 'No runtime runs recorded.'),
      )}
    </div>
  );

  const renderRuntimeConsoleBody = () => {
    if (selectedRuntimeSegment === 'team') return renderTeamSegment();
    if (selectedRuntimeSegment === 'workers') return renderWorkersSegment();
    if (selectedRuntimeSegment === 'workflow') return renderWorkflowSegment();
    return renderActivitySegment();
  };

  const renderDocumentRow = (doc: WorkspaceDocumentModel) => {
    const contributorLabel = artifactContributorLabel(doc.artifact, agent);
    return (
      <button
        key={doc.key}
        type="button"
        className="session-runtime-doc-row"
        onClick={() => onOpenDocument?.(doc.artifact)}
      >
        <IconFileText size={15} />
        <span>
          <strong>{doc.name}</strong>
          <small>{[doc.previewKind, doc.status, formatArtifactSize(doc.size)].filter(Boolean).join(' · ')}</small>
          {contributorLabel ? (
            <em className="session-runtime-doc-author">
              {t('sessionWorkbench.rightPanel.documentAuthor', 'By {{name}}', { name: contributorLabel })}
            </em>
          ) : null}
        </span>
      </button>
    );
  };

  const renderDocumentGroup = (group: WorkspaceDocumentGroupModel, testId: string) => {
    if (group.items.length === 0) return null;
    const title = t(`sessionWorkbench.rightPanel.documentGroups.${group.key}`, group.title);
    const currentDocumentVisibleLimit = 5;
    const shouldLimitCurrentGroup = group.key === 'currentSession' && !showAllCurrentDocuments;
    const visibleItems = shouldLimitCurrentGroup ? group.items.slice(0, currentDocumentVisibleLimit) : group.items;
    const hiddenCount = group.items.length - visibleItems.length;
    const body = (
      <div className="session-runtime-list">
        {visibleItems.map(renderDocumentRow)}
        {hiddenCount > 0 ? (
          <button
            type="button"
            className="session-runtime-view-all"
            onClick={() => setShowAllCurrentDocuments(true)}
          >
            {t('sessionWorkbench.rightPanel.viewAllCurrentArtifacts', 'View all {{count}} current artifacts', { count: group.items.length })}
          </button>
        ) : null}
      </div>
    );
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
    <aside
      data-testid="session-runtime-panel"
      className="session-runtime-panel"
      style={panelWidth ? { flexBasis: `${panelWidth}px`, maxWidth: `${panelWidth}px` } : undefined}
    >
      <div
        data-testid="session-runtime-resize-handle"
        className={`session-runtime-resize-handle${resizeDragging ? ' is-dragging' : ''}`}
        onMouseDown={startPanelResize}
        role="separator"
        aria-orientation="vertical"
        aria-label={t('sessionWorkbench.rightPanel.resize', 'Resize runtime panel')}
      />
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
        data-testid="session-runtime-deliverables"
        className={`session-runtime-section session-runtime-documents${currentSessionDocumentCount === 0 ? ' is-empty' : ''}`}
        aria-label={t('sessionWorkbench.rightPanel.sessionArtifacts', 'Deliverables')}
      >
        <div className="session-runtime-section-header">
          <div>
            <div className="session-tui-kicker">{t('sessionWorkbench.rightPanel.session', 'Session')}</div>
            <h3>{t('sessionWorkbench.rightPanel.sessionArtifacts', 'Deliverables')}</h3>
          </div>
          <span>{currentSessionDocumentCount}</span>
        </div>
        {currentSessionDocumentCount === 0 ? (
          <div className="session-runtime-empty">
            {t('sessionWorkbench.rightPanel.noSessionArtifacts', 'No delivered artifacts in this session yet.')}
          </div>
        ) : (
          <div className="session-runtime-doc-groups">
            {renderDocumentGroup(docs.currentSession, 'session-workspace-documents-current')}
          </div>
        )}
      </section>

      <div data-testid="session-runtime-divider" className="session-runtime-divider" aria-hidden="true" />

      <section
        data-testid="session-runtime-run-status"
        className="session-runtime-section session-runtime-lower"
        aria-label={t('sessionWorkbench.rightPanel.runStatus', 'Run status')}
      >
        <div className="session-runtime-section-header">
          <div>
            <div className="session-tui-kicker">{t('sessionWorkbench.rightPanel.session', 'Session')}</div>
            <h3>{t('sessionWorkbench.rightPanel.runStatus', 'Run status')}</h3>
          </div>
          <span>{runStatusCount}</span>
        </div>

        {agentId && sessionId && sessionWorkbench?.goals?.length ? (
          <SessionGoalPanel
            agentId={agentId}
            sessionId={sessionId}
            goals={sessionWorkbench.goals}
            onChanged={onGoalChanged}
          />
        ) : null}

        <div data-testid="session-runtime-console" className="session-runtime-console">
          <div
            data-testid="session-runtime-summary-strip"
            className="session-runtime-summary-strip"
            data-runtime-state={runtimeConsole.summary.state}
          >
            <div className="session-runtime-summary-main">
              <span className="session-runtime-summary-dot" aria-hidden="true" />
              <span>
                <strong>
                  {t(
                    `sessionWorkbench.rightPanel.runtimeStates.${runtimeConsole.summary.state}`,
                    userFacingRuntimeStatus(runtimeConsole.summary.state),
                  )}
                </strong>
                <small>
                  {sessionWindow
                    ? [sessionWindow.label, userFacingRuntimeStatus(sessionWindow.status)].filter(Boolean).join(' · ')
                    : t('sessionWorkbench.rightPanel.noMainSession', 'No selected session')}
                </small>
              </span>
            </div>
            <div className="session-runtime-summary-metrics">
              <span>{t('sessionWorkbench.rightPanel.runningCount', '{{count}} running', { count: runtimeConsole.summary.runningCount })}</span>
              <span>{t('sessionWorkbench.rightPanel.waitingCount', '{{count}} waiting', { count: runtimeConsole.summary.waitingCount })}</span>
              <span>{runtimeConsole.summary.elapsedLabel || '-'}</span>
              <span>{runtimeConsole.summary.tokenLabel || '-'}</span>
              <span>{runtimeConsole.summary.toolUseLabel || '-'}</span>
            </div>
          </div>

          {runtimeConsole.waiters.length > 0 && (
            <div
              data-testid="session-runtime-waiters"
              className="session-runtime-waiters"
              aria-label={t('sessionWorkbench.rightPanel.waiters', 'Waiting items')}
            >
              {runtimeConsole.waiters.map(renderRuntimeWaiter)}
            </div>
          )}

          <div className="session-runtime-segmented" role="tablist" aria-label={t('sessionWorkbench.rightPanel.runtimeConsole', 'Runtime Console')}>
            {runtimeConsole.segments.map((segment) => {
              const selected = selectedRuntimeSegment === segment.key;
              return (
                <button
                  key={segment.key}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  data-testid={`session-runtime-segment-${segment.key}`}
                  className={`session-runtime-segment${selected ? ' is-active' : ''}`}
                  onClick={() => setRuntimeSegmentOverride(segment.key)}
                >
                  <span>{t(`sessionWorkbench.rightPanel.runtimeSegments.${segment.key}`, segment.label)}</span>
                  <strong>{segment.count}</strong>
                </button>
              );
            })}
          </div>

          {renderRuntimeConsoleBody()}
        </div>
      </section>
      {selectedThreadItem?.audience === 'operator' && selectedThreadItem.operator_details ? (
        <div className="session-technical-drawer" role="dialog" aria-modal="false">
          <ThreadItemInspector item={selectedThreadItem} onClose={onClearSelectedThreadItem} />
        </div>
      ) : null}
    </aside>
  );
}

export function WorkflowRunFocusPanel({
  workflow,
  onClose,
  onSelectSession,
  onWorkflowAction,
}: {
  workflow: RuntimeSectionItemModel;
  onClose: () => void;
  onSelectSession?: (sessionId: string) => void | Promise<unknown>;
  onWorkflowAction?: (action: WorkflowRunActionModel, workflow: RuntimeSectionItemModel) => void | Promise<unknown>;
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
  const controls = windowModel.controls;
  const actionLabel = (action: WorkflowRunActionModel['action']) => {
    if (action === 'approve_gate') return t('sessionWorkbench.workflowRunWindow.approveGate', 'Approve');
    if (action === 'reject_gate') return t('sessionWorkbench.workflowRunWindow.rejectGate', 'Reject');
    if (action === 'repair') return t('sessionWorkbench.workflowRunWindow.repair', 'Repair');
    if (action === 'cancel') return t('sessionWorkbench.workflowRunWindow.cancel', 'Cancel');
    return t('sessionWorkbench.workflowRunWindow.promote', 'Submit for approval');
  };
  const renderWorkflowAction = (action: WorkflowRunActionModel) => (
    <button
      key={action.action}
      type="button"
      data-testid={`session-workflow-action-${action.action}`}
      data-workflow-action={action.action}
      disabled={!action.enabled || !onWorkflowAction}
      title={action.reason || actionLabel(action.action)}
      onClick={() => void onWorkflowAction?.(action, workflow)}
      className="session-workflow-action-button"
    >
      {actionLabel(action.action)}
    </button>
  );
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
          {step.summary || t('sessionWorkbench.workflowRunWindow.noStepDetails', 'No additional details')}
        </span>
      </span>
      <span className="session-runtime-status">{userFacingRuntimeStatus(step.status)}</span>
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
            {leaf.summary || t('sessionWorkbench.workflowRunWindow.noLeafDetails', 'No additional details')}
          </span>
        </span>
        <span className="session-runtime-status">{userFacingRuntimeStatus(leaf.status)}</span>
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

      <div className="session-runtime-card" data-testid="session-workflow-controls">
        <div className="session-runtime-card-title">
          {t('sessionWorkbench.workflowRunWindow.controls', 'Controls')}
        </div>
        <div data-testid="session-workflow-gate-status" className="session-runtime-metric-row">
          <span>{t('sessionWorkbench.workflowRunWindow.gateStatus', 'Gate')}</span>
          <strong>{controls.gateStatus}</strong>
        </div>
        <div data-testid="session-workflow-wait-status" className="session-runtime-metric-row">
          <span>{t('sessionWorkbench.workflowRunWindow.waitStatus', 'Wait')}</span>
          <strong>{controls.waitStatus}</strong>
        </div>
        <div className="session-workflow-action-row">
          {controls.actions.length === 0 ? (
            <span className="session-runtime-empty">
              {t('sessionWorkbench.workflowRunWindow.noActions', 'No workflow actions available.')}
            </span>
          ) : (
            controls.actions.map(renderWorkflowAction)
          )}
        </div>
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

function getChatMessageKey(message: AgentChatMessage, index: number, prefix = 'message'): string {
  return String(
    message.transcriptEventId
    || message.messageId
    || message.id
    || `${prefix}-${index}`,
  );
}

function buildPreviousUserCheckpointMap(messages: AgentChatMessage[]): Map<number, AgentChatMessage> {
  const checkpoints = new Map<number, AgentChatMessage>();
  let previousUserCheckpoint: AgentChatMessage | null = null;
  messages.forEach((message, index) => {
    if (message.role === 'assistant' && previousUserCheckpoint) {
      checkpoints.set(index, previousUserCheckpoint);
    }
    if (message.role === 'user' && message.transcriptEventId) {
      previousUserCheckpoint = message;
    }
  });
  return checkpoints;
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

function WorkflowPreviewSummary({
  meta,
  status,
}: {
  meta: WorkflowPreviewToolMeta;
  status: WorkflowPreviewToolMeta['previewStatus'];
}) {
  const { t } = useTranslation();
  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '8px', alignItems: 'center' }}>
        <div style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)' }}>
          {t('agent.chat.workflowPreview.title', 'Workflow ready to run')}
        </div>
        <span className={`badge ${status === 'failed' || status === 'expired' ? 'badge-error' : status === 'started' ? 'badge-success' : 'badge-neutral'}`}>
          {status === 'started'
            ? t('agent.chat.workflowPreview.started', 'Started')
            : status === 'starting'
              ? t('agent.chat.workflowPreview.starting', 'Starting')
              : status === 'failed'
                ? t('agent.chat.workflowPreview.failed', 'Retry available')
                : status === 'expired'
                  ? t('agent.chat.workflowPreview.expired', 'Expired')
                  : t('agent.chat.workflowPreview.ready', 'Ready')}
        </span>
      </div>
      <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
        {meta.plannedLeafCalls != null
          ? t('agent.chat.workflowPreview.workUnits', '{{count}} planned work units', { count: meta.plannedLeafCalls })
          : t('agent.chat.workflowPreview.review', 'Review the workflow before running it.')}
        {meta.budgetTokens != null
          ? ` · ${t('agent.chat.workflowPreview.tokenBudget', 'Token budget {{count}}', { count: meta.budgetTokens })}`
          : ''}
      </div>
      <StructuredToolSection
        label={t('agent.chat.workflowPreview.confirmationReasons', 'Why confirmation is needed')}
        items={meta.confirmationReasons}
      />
    </div>
  );
}

function InteractiveWorkflowPreviewCard({ meta, agentId }: { meta: WorkflowPreviewToolMeta; agentId: string }) {
  const { t } = useTranslation();
  const statusQuery = useQuery({
    queryKey: ['workflow-preview', agentId, meta.previewId],
    queryFn: () => getWorkflowPreview(agentId, meta.previewId),
    refetchInterval: (query) => (query.state.data?.preview_status === 'starting' ? 1500 : false),
  });
  const startMutation = useMutation({
    mutationFn: () => startWorkflow(agentId, { previewId: meta.previewId }),
    onSuccess: () => statusQuery.refetch(),
  });
  const status = statusQuery.data?.preview_status ?? meta.previewStatus;
  const canStart = status === 'ready' || status === 'failed';

  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <WorkflowPreviewSummary meta={meta} status={status} />
      {canStart && (
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => startMutation.mutate()}
          disabled={startMutation.isPending}
        >
          {startMutation.isPending
            ? t('agent.chat.workflowPreview.starting', 'Starting')
            : meta.confirmationRequired
              ? t('agent.chat.workflowPreview.confirmAndRun', 'Confirm and run')
              : t('agent.chat.workflowPreview.run', 'Run workflow')}
        </button>
      )}
      {startMutation.isError && (
        <div className="agent-workflows-error" role="alert">
          {startMutation.error instanceof Error
            ? startMutation.error.message
            : t('agent.chat.workflowPreview.startFailed', 'Workflow could not start. You can retry safely.')}
        </div>
      )}
    </div>
  );
}

function WorkflowPreviewConfirmationCard({ meta, agentId }: { meta: WorkflowPreviewToolMeta; agentId?: string }) {
  if (!agentId) {
    return <WorkflowPreviewSummary meta={meta} status={meta.previewStatus} />;
  }
  return <InteractiveWorkflowPreviewCard meta={meta} agentId={agentId} />;
}

function DynamicWorkflowProposalCard({
  meta,
  agentId,
}: {
  meta: Extract<ToolCallMeta, { kind: 'dynamic_workflow_proposal' }>;
  agentId?: string;
}) {
  const { t } = useTranslation();
  const previewMutation = useMutation({
    mutationFn: (candidateId: string) => (
      agentId
        ? previewWorkflowCandidate(agentId, meta.proposalId, candidateId)
        : Promise.reject(new Error('Agent context is unavailable'))
    ),
  });
  const selectedPreview = previewMutation.data;
  const selectedPreviewMeta: WorkflowPreviewToolMeta | null = selectedPreview
    ? {
        kind: 'workflow_preview',
        previewId: selectedPreview.preview_id,
        sessionId: selectedPreview.session_id || null,
        previewStatus: selectedPreview.preview_status,
        proposalId: selectedPreview.proposal_id || meta.proposalId,
        candidateId: selectedPreview.candidate_id || null,
        confirmationRequired: Boolean(selectedPreview.confirmation_required),
        confirmationReasons: selectedPreview.confirmation_reasons || [],
        plannedLeafCalls: selectedPreview.planned_leaf_calls ?? null,
        budgetTokens: selectedPreview.budget_tokens ?? null,
      }
    : null;

  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <div style={{ display: 'grid', gap: '4px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--accent-text)' }}>
          {t('agent.chat.toolResults.dynamicWorkflowProposalTitle', 'Dynamic Workflow Proposal')}
        </div>
        {meta.goal && <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>{meta.goal}</div>}
        {meta.whyWorkflow && <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{meta.whyWorkflow}</div>}
      </div>
      <StructuredToolSection
        label={t('agent.chat.toolResults.successCriteria', 'Success Criteria')}
        items={meta.successCriteria}
      />
      <div style={{ display: 'grid', gap: '6px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)' }}>
          {t('agent.chat.toolResults.candidates', 'Candidates')}
        </div>
        {meta.candidates.map((candidate, index) => {
          const recommended = candidate.candidateId === meta.recommendedCandidateId;
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
                  {candidate.name || t('agent.chat.toolResults.candidateFallback', 'Candidate {{index}}', { index: index + 1 })}
                </span>
                {recommended && (
                  <span style={{ fontSize: '11px', color: 'var(--accent-text)' }}>
                    {t('agent.chat.toolResults.recommended', 'Recommended')}
                  </span>
                )}
              </div>
              {facts.length > 0 && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{facts.join(' · ')}</div>}
              {agentId && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => previewMutation.mutate(candidate.candidateId)}
                  disabled={previewMutation.isPending}
                >
                  {previewMutation.isPending
                    ? t('agent.chat.workflowPreview.starting', 'Creating preview')
                    : t('agent.chat.toolResults.selectAndPreview', 'Select and preview')}
                </button>
              )}
            </div>
          );
        })}
      </div>
      {previewMutation.isError && (
        <div className="agent-workflows-error" role="alert">
          {previewMutation.error instanceof Error
            ? previewMutation.error.message
            : t('agent.chat.workflowPreview.previewFailed', 'Workflow preview could not be created. You can retry safely.')}
        </div>
      )}
      {selectedPreviewMeta ? <WorkflowPreviewConfirmationCard meta={selectedPreviewMeta} agentId={agentId} /> : null}
      {!selectedPreviewMeta && (
        <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
          {t('agent.chat.toolResults.dynamicWorkflowNotStarted', 'Select a candidate to create a reviewable preview. Nothing has run yet.')}
        </div>
      )}
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

  if (toolMeta.kind === 'workflow_preview') {
    return <WorkflowPreviewConfirmationCard meta={toolMeta} agentId={agentId} />;
  }

  if (toolMeta.kind === 'dynamic_workflow_proposal') {
    return <DynamicWorkflowProposalCard meta={toolMeta} agentId={agentId} />;
  }

  if (toolMeta.kind === 'hr_preview') {
    return (
      <HrBlueprintPreviewCard
        agentId={agentId}
        preview={toolMeta}
        onSendMessage={onSendMessage}
      />
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

interface ChatMessageItemProps {
  msg: AgentChatMessage;
  i: number;
  isLeft: boolean;
  checkpointMessage?: AgentChatMessage | null;
  effectiveAgentId: string | null;
  onOpenArtifact: (artifact: ChatArtifactPart) => void | Promise<unknown>;
  onBranchMessage?: (message: AgentChatMessage) => void | Promise<unknown>;
  onFeedbackMessage?: (message: AgentChatMessage, label: RecordSessionFeedbackInput['label']) => void | Promise<unknown>;
  onRewindMessage?: (message: AgentChatMessage) => void | Promise<unknown>;
  t: Translate;
  operatorView?: boolean;
}

const ChatMessageItem = React.memo(function ChatMessageItem({
  msg,
  i,
  isLeft,
  checkpointMessage,
  effectiveAgentId,
  onOpenArtifact,
  onBranchMessage,
  onFeedbackMessage,
  onRewindMessage,
  t,
  operatorView = false,
}: ChatMessageItemProps) {
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
      else if (diffMs < 7 * 86400000) {
        timeStr = `${date.toLocaleDateString([], { weekday: 'short' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
      } else {
        timeStr = `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
      }
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
          onBranchMessage={onBranchMessage}
          onFeedbackMessage={onFeedbackMessage}
          onRewindMessage={onRewindMessage}
        />
      </div>
    );
  })();

  return (
    <div
      data-session-message-id={msg.transcriptEventId || msg.id || undefined}
      data-session-message-index={i}
      className={`session-tui-render-cell session-tui-message-row ${isLeft ? 'session-tui-message-row-assistant' : 'session-tui-message-row-user'}`}
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
          <ThinkingDisclosure thinking={msg.thinking} streaming={Boolean((msg as any)._streaming)} />
        )}
        {msg.role === 'assistant' ? (
          <StreamingMarkdown content={msg.content} streaming={Boolean((msg as any)._streaming)} />
        ) : (
          <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
        )}
        <ArtifactCards
          agentId={effectiveAgentId}
          artifacts={msg.artifacts}
          onOpenArtifact={onOpenArtifact}
          context="assistant"
          operatorView={operatorView}
        />
        {inlinePlanId && effectiveAgentId && (
          <div style={{ marginTop: '10px', minWidth: 'min(520px, 100%)' }} data-testid="chat-inline-plan-card">
            <InlinePlanCard agentId={effectiveAgentId} planId={inlinePlanId} />
          </div>
        )}
        {timestampHtml}
      </div>
    </div>
  );
});

function AgentChatSection({
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
  transportPhase = wsConnected ? 'connected' : 'reconnecting',
  transportReconnectAttempt = 0,
  onReconnectTransport,
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
  onLoadOlderMessages,
  olderMessagesLoading,
  hasOlderMessages,
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
  runtimePhase = 'idle',
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
  goalModeRequested = false,
  onToggleGoalMode,
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
  const activeSessionId = activeSession?.id ? String(activeSession.id) : null;
  const sessionAuthorityMode = activeSession?.operator_view ? 'operator' : 'owner';
  const sessionOperatorOptions = activeSession?.operator_view
    ? { operatorView: true, operatorReason: 'Agent session administration' }
    : undefined;
  const resourceOperatorOptions = activeSession?.operator_view
    ? { operatorView: true, reason: 'Agent session administration' }
    : undefined;
  const visibleHistoryMsgs = historyMessagesSessionId === activeSessionId ? historyMsgs : EMPTY_CHAT_MESSAGES;
  const visibleChatMessages = chatMessagesSessionId === activeSessionId ? chatMessages : EMPTY_CHAT_MESSAGES;
  const visibleTimeline = isReadOnlySession ? visibleHistoryMsgs : visibleChatMessages;
  const normalizedActiveRunStatus = String(activeRunStatus || '').trim().toLowerCase();
  const rewindUnavailableReason = isStreaming
    || isWaiting
    || ['created', 'pending', 'queued', 'running', 'started', 'in_progress', 'resuming', 'waiting', 'waiting_user'].includes(normalizedActiveRunStatus)
    ? t('sessionWorkbench.rewind.activeRunBlocked', 'Stop the current turn before rewinding. Branch remains available.')
    : null;
  const {
    selectedId: selectedThreadItemId,
    selectedItem: selectedThreadItem,
    selectItem: selectThreadItem,
    clearSelection: clearThreadItemSelection,
  } = useThreadItemRuntimeController(activeSessionId, visibleTimeline);

  const [artifactPreview, setArtifactPreview] = React.useState<ArtifactPreviewState | null>(null);
  const [runtimePanelCollapsed, setRuntimePanelCollapsed] = useResponsiveRuntimePanel();
  const [focusedWorkflow, setFocusedWorkflow] = React.useState<RuntimeSectionItemModel | null>(null);
  const [focusedGitCheckpointId, setFocusedGitCheckpointId] = React.useState<string | null>(null);
  const gitScrollFrameRef = React.useRef<number | null>(null);

  const runtimeUsageLabel = getRuntimeUsageLabel(runtimeSummary);
  const runtimeUsageTitle = getRuntimeUsageTitle(runtimeSummary, runtimeUsageLabel);
  const permissionModeLabel = getSessionPermissionModeLabel(sessionPermissionMode, t);
  const composerIntentLabel = getComposerIntentLabel(planModeRequested, goalModeRequested, t);
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
      if (action === 'upload') {
        fileInputRef.current?.click();
        return;
      }
      if (action === 'plan') {
        onTogglePlanMode?.();
        focusChatInput();
        return;
      }
      if (action === 'goal') {
        onToggleGoalMode?.();
        focusChatInput();
        return;
      }
      if (action === 'schedule') {
        onSetChatInput(composerShortcutText(action));
        focusChatInput();
        return;
      }
    },
    [fileInputRef, focusChatInput, onSetChatInput, onToggleGoalMode, onTogglePlanMode],
  );

  const sendFromComposer = React.useCallback(() => {
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
	      if (!message.transcriptEventId || !onRunSessionCommand || rewindUnavailableReason) return;
	      await onRunSessionCommand('rewind', { checkpoint_event_id: String(message.transcriptEventId) });
	    },
	    [onRunSessionCommand, rewindUnavailableReason],
	  );

  const openArtifact = React.useCallback(async (artifact: ChatArtifactPart) => {
    const artifactAgentId = artifactWorkspaceAgentId(artifact, effectiveAgentId);
    if (!artifactAgentId) return;
    const href = artifact.id
      ? fileApi.artifactDownloadUrl(artifactAgentId, artifact.id, resourceOperatorOptions)
      : fileApi.downloadUrl(artifactAgentId, artifact.path, resourceOperatorOptions);
    if (getArtifactOpenMode(artifact) === 'download') {
      window.open(href, '_blank', 'noopener,noreferrer');
      return;
    }

    const previewKind = getEffectiveArtifactPreviewKind(artifact);
    if (previewKind === 'markdown' || previewKind === 'text' || !previewKind) {
      setArtifactPreview({ artifact, loading: true });
      try {
        const response = artifact.id
          ? await fileApi.readArtifact(artifactAgentId, artifact.id, resourceOperatorOptions)
          : await fileApi.read(artifactAgentId, artifact.path, resourceOperatorOptions);
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
  }, [effectiveAgentId, resourceOperatorOptions, t]);

  const requestSubagentRetry = React.useCallback(
    async (worker: RuntimeSectionItemModel) => {
      if (!onSendMessage || !subagentWorkerRecoveryModel(worker).canRequestNewWorker) return;
      const workerLabel = runtimeItemDisplayLabel(worker, 'Sub-agent worker');
      const evidence = worker.summary ? ` 已有失败证据：${worker.summary}` : '';
      await onSendMessage(
        `请检查一次性 Sub-agent“${workerLabel}”失败的原因。若确认不会重复外部副作用，请保留已有证据，并以同一任务要求创建一个新的 Worker 重试；不要复用或继续原 Worker。${evidence}`,
      );
    },
    [onSendMessage],
  );

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
      const item = msg.threadItem || normalizeThreadItemPayload({
        id: msg.transcriptEventId || msg.id || `legacy-event-${index}`,
        eventType: msg.eventType,
        content: msg.content,
        status: msg.eventStatus,
        title: msg.eventTitle,
        runtime_task_id: msg.eventRuntimeTaskId,
        child_session_id: msg.eventChildSessionId,
        permission_request: permissionRequest,
        created_at: msg.timestamp,
      });
      if (!item) return null;
      if (!shouldRenderThreadItemInConversation(item, Boolean(activeSession?.operator_view))) return null;
      const permissionActions = permissionRequest && msg.eventStatus === 'session_permission_required' ? (
        <SessionPermissionActions
          permissionRequest={permissionRequest}
          onResolveSessionPermission={onResolveSessionPermission}
          t={t}
        />
      ) : null;
      const retryAnchor = item.item_type === 'error' && item.item_data.retryable
        ? findRetryAnchorMessage(visibleTimeline, index)
        : null;
      const recoveryActions = retryAnchor && onBranchMessage ? (
        <button
          type="button"
          className="btn btn-secondary"
          data-testid="thread-item-retry-turn"
          onClick={() => void onBranchMessage(retryAnchor, 'regenerate')}
        >
          {t('sessionWorkbench.threadItem.retryTurn', 'Retry turn')}
        </button>
      ) : null;

      return (
        <div
          key={`event-${getChatMessageKey(msg, index, 'event')}`}
          className="session-tui-render-cell"
          style={{ paddingInlineStart: '36px', marginBottom: '8px' }}
          data-runtime-link-id={msg.eventRuntimeTaskId || msg.eventChildSessionId || undefined}
          onMouseEnter={() => setRuntimeLinkHighlight(msg.eventRuntimeTaskId || msg.eventChildSessionId, true)}
          onMouseLeave={() => setRuntimeLinkHighlight(msg.eventRuntimeTaskId || msg.eventChildSessionId, false)}
        >
          <ThreadItemRenderer
            item={item}
            selected={selectedThreadItemId === item.id}
            onSelect={item.audience === 'operator' && activeSession?.operator_view ? selectThreadItem : undefined}
            approvalActions={permissionActions}
            actions={recoveryActions}
          />
        </div>
      );
    },
    [activeSession?.operator_view, onBranchMessage, onResolveSessionPermission, selectThreadItem, selectedThreadItemId, t, visibleTimeline],
  );

  const renderInlinePlanToolCall = (msg: AgentChatMessage, index: number) => (
    <div
      key={`tool-inline-${getChatMessageKey(msg, index, 'tool')}`}
      className="session-tui-render-cell"
      style={{ paddingLeft: '36px', marginBottom: '8px', maxWidth: '75%' }}
      data-testid="chat-inline-plan-tool-call"
    >
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
      <ArtifactCards
        agentId={effectiveAgentId}
        artifacts={msg.artifacts}
        onOpenArtifact={openArtifact}
        context="tool"
        operatorView={Boolean(activeSession?.operator_view)}
      />
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

	  const renderConversationMessage = (
    message: AgentChatMessage,
    index: number,
    isLeft: boolean,
    checkpointByIndex: Map<number, AgentChatMessage>,
  ) => {
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
	        key={getChatMessageKey(message, index, isLeft ? 'assistant' : 'user')}
	        msg={message}
	        i={index}
	        isLeft={isLeft}
	        checkpointMessage={message.role === 'assistant' ? checkpointByIndex.get(index) || null : null}
          effectiveAgentId={effectiveAgentId}
          onOpenArtifact={openArtifact}
          onBranchMessage={startBranchAction}
          onFeedbackMessage={submitMessageFeedback}
          onRewindMessage={rewindUnavailableReason ? undefined : rewindFromMessage}
          t={t}
          operatorView={Boolean(activeSession?.operator_view)}
	      />
	    );
	  };

  const renderConversationMessages = (
    messages: AgentChatMessage[],
    resolveIsLeft: (message: AgentChatMessage, index: number) => boolean,
    timelineModel?: ThreadTimelineModel,
  ) => {
    const nodes: React.ReactNode[] = [];
    const checkpointByIndex = buildPreviousUserCheckpointMap(messages);
    const model = timelineModel ?? buildThreadTimeline({
      messages,
      activeSession,
      runtimeSummary,
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
      runtimePhase,
    });

    model.cells.forEach((cell) => {
      if (cell.kind === 'user_turn') {
	        nodes.push(renderConversationMessage(cell.message, cell.index, resolveIsLeft(cell.message, cell.index), checkpointByIndex));
	        return;
	      }
	      if (cell.kind === 'assistant_final') {
	        nodes.push(renderConversationMessage(cell.message, cell.index, resolveIsLeft(cell.message, cell.index), checkpointByIndex));
        return;
      }
      if (cell.kind === 'active_run') {
        const runningStep = cell.phase === 'tool_running'
          ? [...cell.timeline.steps].reverse().find((step) => step.status === 'running')
          : null;
        nodes.push(
          <div
            key={cell.id}
            className="session-tui-render-cell session-tui-active-run-cell"
            data-testid="active-run-cell"
            style={{ marginBottom: '8px' }}
          >
            <RunDisclosureBlock timeline={cell.timeline} />
            {cell.phase ? (
              <ActiveTailStatusLine
                phase={cell.phase}
                detail={runningStep?.title || null}
                startedAt={cell.timeline.startedAt || null}
              />
            ) : null}
            {cell.sourceMessages.map((entry) => (
              entry.message.role === 'event'
                ? renderEventMessage(entry.message, entry.index)
                : isInlineToolCardMessage(entry.message)
                  ? renderInlinePlanToolCall(entry.message, entry.index)
                  : null
            ))}
          </div>,
        );
        return;
      }
      nodes.push(renderEventMessage(cell.message, cell.index));
    });

    return nodes;
  };

  React.useEffect(() => {
    setFocusedWorkflow(null);
  }, [activeSessionId]);
  const activeSessionHydrating = Boolean(activeSessionId) && (
    isReadOnlySession ? historyMessagesSessionId !== activeSessionId : chatMessagesSessionId !== activeSessionId
  );
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
  // Workbench/index reads are refreshed by explicit invalidations (session
  // commands, WS run boundaries); a long staleTime plus no focus-refetch keeps
  // tab switching from re-pulling the payloads (plan D3).
  const { data: sessionIndexData } = useQuery({
    queryKey: ['chat-session-index', effectiveAgentId, activeSessionId, sessionAuthorityMode],
    queryFn: () => chatApi.getSessionIndex(effectiveAgentId!, activeSessionId!, sessionOperatorOptions),
    enabled: Boolean(effectiveAgentId && activeSessionId),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const { data: sessionWorkbenchData, refetch: refetchSessionWorkbench } = useQuery({
    queryKey: ['chat-session-workbench', effectiveAgentId, activeSessionId, sessionAuthorityMode],
    queryFn: () => ccParityApi.getSessionWorkbench(effectiveAgentId!, activeSessionId!, sessionOperatorOptions),
    enabled: Boolean(effectiveAgentId && activeSessionId),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const handleWorkflowAction = React.useCallback(
    async (action: WorkflowRunActionModel) => {
      if (!effectiveAgentId || !action.runId) return;
      try {
        if (action.action === 'cancel') {
          await cancelWorkflowRun(effectiveAgentId, action.runId);
        } else if (action.action === 'promote') {
          await submitWorkflowPromotionProposal(effectiveAgentId, action.runId);
        } else if (action.action === 'approve_gate' || action.action === 'reject_gate') {
          if (!action.stepId) throw new Error('Workflow gate step is missing');
          await decideWorkflowGate(
            effectiveAgentId,
            action.runId,
            action.stepId,
            action.action === 'approve_gate' ? 'approve' : 'reject',
          );
        } else {
          await repairWorkflowRun(effectiveAgentId, action.runId);
        }
        await refetchSessionWorkbench();
        showAppToast(t('sessionWorkbench.workflowRunWindow.actionQueued', 'Workflow action queued.'), 'success');
      } catch (error: any) {
        showAppToast(
          t('sessionWorkbench.workflowRunWindow.actionFailed', 'Workflow action failed: {{message}}', {
            message: error?.message || String(error),
          }),
          'error',
        );
      }
    },
    [effectiveAgentId, refetchSessionWorkbench, t],
  );
  const { data: sessionContextUsageData } = useQuery({
    queryKey: ['chat-session-context-usage', effectiveAgentId, activeSessionId, sessionAuthorityMode],
    queryFn: () => ccParityApi.getSessionContextUsage(effectiveAgentId!, activeSessionId!, sessionOperatorOptions),
    enabled: Boolean(effectiveAgentId && activeSessionId),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const { data: gitLineAxisSessionIndexData, isLoading: gitLineAxisSessionIndexLoading } = useQuery({
    queryKey: ['chat-session-index', effectiveAgentId, gitLineAxisSessionId, 'gitline-axis'],
    queryFn: () => chatApi.getSessionIndex(effectiveAgentId!, gitLineAxisSessionId!, sessionOperatorOptions),
    enabled: Boolean(effectiveAgentId && gitLineAxisSessionId && shouldUseGitLineAxisSession),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  // The branch-axis workbench is only a checkpoints fallback — fetch it just
  // when the axis session index came back without checkpoints (plan D3).
  const gitLineAxisIndexMissingCheckpoints = Boolean(
    gitLineAxisSessionIndexData
      && !(
        Array.isArray((gitLineAxisSessionIndexData as { checkpoints?: unknown[] })?.checkpoints)
        && ((gitLineAxisSessionIndexData as { checkpoints?: unknown[] }).checkpoints as unknown[]).length > 0
      ),
  );
  const { data: gitLineAxisSessionWorkbenchData, isLoading: gitLineAxisSessionWorkbenchLoading } = useQuery({
    queryKey: ['chat-session-workbench', effectiveAgentId, gitLineAxisSessionId, 'gitline-axis'],
    queryFn: () => ccParityApi.getSessionWorkbench(
      effectiveAgentId!,
      gitLineAxisSessionId!,
      sessionOperatorOptions,
    ),
    enabled: Boolean(
      effectiveAgentId && gitLineAxisSessionId && shouldUseGitLineAxisSession && gitLineAxisIndexMissingCheckpoints,
    ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const sessionIndex = sessionIndexData && !Array.isArray(sessionIndexData) ? sessionIndexData : null;
  const sessionWorkbenchBase = sessionWorkbenchData && !Array.isArray(sessionWorkbenchData) ? sessionWorkbenchData : null;
  const sessionContextUsage = sessionContextUsageData && !Array.isArray(sessionContextUsageData)
    ? sessionContextUsageData as SessionContextUsage
    : null;
  const sessionWorkbench = React.useMemo<SessionWorkbench | null>(() => {
    if (!sessionWorkbenchBase) return null;
    if (!sessionContextUsage) return sessionWorkbenchBase;
    return {
      ...sessionWorkbenchBase,
      context_usage: sessionContextUsage,
    };
  }, [sessionContextUsage, sessionWorkbenchBase]);
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
  const threadTimelineCacheRef = React.useRef(createThreadTimelineCache());
  const threadTimelineModel = React.useMemo(
    () => buildThreadTimelineCached({
      messages: visibleTimeline,
      activeSession,
      runtimeSummary,
      sessionIndex,
      sessionWorkbench,
      branchLineage,
      isWaiting,
      isStreaming,
      activeRunStatus,
      runtimePhase,
    }, threadTimelineCacheRef.current),
    [
      activeRunStatus,
      activeSession,
      branchLineage,
      isStreaming,
      isWaiting,
      runtimePhase,
      runtimeSummary,
      sessionIndex,
      sessionWorkbench,
      visibleTimeline,
    ],
  );
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
        rewindAnchorCheckpointId={
          ((sessionIndex as Record<string, unknown> | null)?.active_projection as Record<string, unknown> | null)
            ?.projection_reason === 'rewind'
            ? String(
                ((sessionIndex as Record<string, unknown> | null)?.active_projection as Record<string, unknown>)
                  ?.checkpoint_event_id || '',
              ) || null
            : null
        }
        onNavigateCheckpoint={navigateGitCheckpoint}
        onNavigateBranch={onSelectBranchSession}
      />
      <div className="session-tui-history-content">
        {hasOlderMessages && !activeSessionHydrating ? (
          <div style={{ textAlign: 'center', marginBottom: '10px' }}>
            <button
              type="button"
              data-testid="load-older-messages"
              onClick={onLoadOlderMessages}
              disabled={olderMessagesLoading}
              style={{
                fontSize: '12px',
                padding: '4px 12px',
                borderRadius: '12px',
                border: '1px solid var(--border-default)',
                background: 'var(--bg-secondary)',
                color: 'var(--text-secondary)',
                cursor: olderMessagesLoading ? 'wait' : 'pointer',
              }}
            >
              {olderMessagesLoading
                ? t('common.loading', 'Loading')
                : t('agent.chat.loadOlderMessages', 'Load earlier messages')}
            </button>
          </div>
        ) : null}
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
            {userFacingRuntimeStatus(teamMemberWindow.status)}
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
                      {!session.operator_view && String(session.user_id || '') === String(currentUser?.id || '') && (
                        <button
                          type="button"
                          className="detail-session-row-action"
                          aria-label={`Delete session ${session.title || t('agent.chat.session', 'Session')}`}
                          title={t('common.delete', 'Delete')}
                          onClick={() => onDeleteSession(String(session.id))}
                        >
                          <IconTrash size={13} stroke={1.8} />
                        </button>
                      )}
                    </div>
                  );
                })
            )}
          </div>
        </aside>
      )}
      <div className="session-tui-center">
        <SessionWorkbenchHeader model={threadTimelineModel.header} />
        {activeSession?.operator_view && (
          <div className="session-operator-view" data-testid="session-operator-view" role="status">
            <strong>{t('agent.chat.operatorView', 'Operator View')}</strong>
            <span>{t('agent.chat.operatorViewDesc', 'Audited, read-only access to another user’s session.')}</span>
          </div>
        )}
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
                      onWorkflowAction={handleWorkflowAction}
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
                role="alert"
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
                role="status"
                aria-live="polite"
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
            ) : !isDraftSession ? (
              <SessionTransportStatus
                phase={transportPhase}
                attempt={transportReconnectAttempt}
                onReconnect={onReconnectTransport}
              />
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
                  operatorView={Boolean(activeSession.operator_view)}
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
                rewindUnavailableReason={rewindUnavailableReason}
              />
              <SessionComposer
                value={chatInput}
                inputRef={chatInputRef}
                fileInputRef={fileInputRef}
                placeholder={composerPlaceholder}
                disabled={!canUseComposer}
                attachments={attachedFiles}
                permissionMode={sessionPermissionMode}
                permissionModeLabel={permissionModeLabel}
                permissionOptions={SESSION_PERMISSION_MODE_OPTIONS}
                modelLabel={modelBadgeLabel}
                modelTitle={modelBadgeTitle}
                runtimeUsageLabel={runtimeUsageLabel}
                intentLabel={composerIntentLabel}
                planModeRequested={planModeRequested}
                goalModeRequested={goalModeRequested}
                uploading={uploading}
                uploadProgress={uploadProgress}
                running={isStreaming || isWaiting}
                onChange={onSetChatInput}
                onPaste={onHandlePaste}
                onSubmit={sendFromComposer}
                onStop={onAbortGeneration}
                onAction={setComposerAction}
                onPermissionModeChange={(mode) => onSetSessionPermissionMode?.(mode)}
                onFilesSelected={onHandleChatFile}
                onRemoveAttachment={onRemoveAttachedFile}
                onCancelUpload={() => uploadAbortRef.current?.()}
              />
            </div>
          </>
        )}
      </div>
      {activeSession ? (
        <SessionRuntimePanel
          messages={visibleTimeline}
          sessionWorkbench={sessionWorkbench}
          activeSession={activeSession as Record<string, unknown> | null}
          agent={agent as Record<string, unknown> | null}
          activeRunStatus={activeRunStatus}
          collapsed={runtimePanelCollapsed}
          onToggleCollapsed={() => setRuntimePanelCollapsed((value) => !value)}
          onOpenDocument={openArtifact}
          onSelectSession={onSelectBranchSession}
          onSelectWorkflowRun={setFocusedWorkflow}
          selectedThreadItem={selectedThreadItem}
          onClearSelectedThreadItem={clearThreadItemSelection}
          agentId={effectiveAgentId || undefined}
          sessionId={activeSessionId || undefined}
          onGoalChanged={() => void refetchSessionWorkbench()}
          onTeamChanged={() => void refetchSessionWorkbench()}
          onRetrySubagent={requestSubagentRetry}
        />
      ) : null}
    </div>
  );
}

export default React.memo(AgentChatSection);

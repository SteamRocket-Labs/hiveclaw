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
import AuthenticatedImage from '../../components/AuthenticatedImage';
import ThinkingDisclosure from './ThinkingDisclosure';
import type { AgentPermissions } from '../../api/domains/agents';
import AskUserQuestionCard from './AskUserQuestionCard';
import PlanModeRequestCard from './PlanModeRequestCard';
import CopyMessageButton from './CopyMessageButton';
import PlanCard from './PlanCard';
import RunDisclosureBlock from './RunDisclosureBlock';
import SlashCommandMenu from './SlashCommandMenu';
import ChatWorkLedgerDock from './ChatWorkLedgerDock';
import { SessionTransportStatus } from './SessionTransportStatus';
import type { ChatTransportPhase } from './chatTransportRecovery';
import { SessionWorkbenchHeader } from '../session-workbench/SessionWorkbenchChrome';
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
import {
  chatApi,
  type RecordSessionFeedbackInput,
  type SessionDecisionTrace,
} from '../../api/domains/chat';
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
  buildComposerRuntimePresentation,
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
import {
  buildBranchLineageRows,
  pickFocusedCheckpointIdForScroll,
  sessionCheckpointId,
  SessionGitLine,
  type BranchLineageItem,
  type SessionScrollCheckpointAnchor,
} from './SessionLineageSurface';
import {
  ArtifactCards,
  ArtifactPreviewPanel,
  artifactWorkspaceAgentId,
  downloadChatArtifact,
  getArtifactOpenMode,
  getEffectiveArtifactPreviewKind,
  isUserFacingDeliveryArtifact,
  loadOfficeArtifactPreview,
  type ArtifactPreviewState,
} from './ArtifactSurface';
import {
  ActiveTailStatusLine,
  isRuntimeRecord,
  isTeamMemberSession,
  runtimeItemDisplayLabel,
  SessionRuntimePanel,
  setRuntimeLinkHighlight,
  stringValue,
  subagentWorkerRecoveryModel,
  teamMemberRoleLabel,
  teamMemberSessionLabel,
  runtimeStatusLabel,
  WorkflowRunFocusPanel,
} from './SessionRuntimePanel';
import { InlinePlanCard, StructuredToolResultBody } from './StructuredToolResult';
import { isDedicatedToolCardMessage } from './chatDisclosureReducer';
import {
  AssistantMessageBody,
  shouldCollapseAssistantSupplement,
} from './CanonicalCardAssistantSupplement';

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
  {
    value: 'bypassPermissions',
    label: 'Full access',
    description: 'Bypass session prompts, still obey enterprise rules',
  },
];

export const sessionPermissionModeOptions = () => SESSION_PERMISSION_MODE_OPTIONS;

function isUuidLike(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

export function buildMessageFeedbackInput(
  messageId: string,
  label: RecordSessionFeedbackInput['label'],
): RecordSessionFeedbackInput {
  const input: RecordSessionFeedbackInput = { label };
  if (isUuidLike(messageId)) input.message_id = messageId;
  return input;
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
  | 'side_question';

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

export const SESSION_REWIND_MODE_OPTIONS: Array<{ value: SessionRewindMode; label: string; description: string }> = [
  { value: 'conversation', label: 'Conversation', description: 'Rewind only the chat projection' },
  { value: 'workspace', label: 'Workspace', description: 'Restore only workspace files' },
  { value: 'both', label: 'Both', description: 'Rewind chat and restore files' },
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
  sessionTransitionPending?: boolean;
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
  activeRunId?: string | null;
  runtimeBudget?: React.ComponentProps<typeof SessionTransportStatus>['runtimeBudget'];

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
  const hasLaterVisibleAnswer = messages
    .slice(index + 1)
    .some((candidate) => candidate.role === 'user' && String(candidate.content || '').trim().length > 0);
  if (hasLaterVisibleAnswer) return true;

  // Reconciliation may temporarily place an older card after its answer; event time remains authoritative.
  const clarificationTime = Date.parse(String(message.timestamp || ''));
  if (!Number.isFinite(clarificationTime)) return false;
  return messages.some((candidate) => {
    if (candidate.role !== 'user' || !String(candidate.content || '').trim()) return false;
    const candidateTime = Date.parse(String(candidate.timestamp || ''));
    return Number.isFinite(candidateTime) && candidateTime > clarificationTime;
  });
}

export function isInlineToolCardMessage(message: AgentChatMessage): boolean {
  return message.role === 'tool_call' && (
    isDedicatedToolCardMessage(message)
    || Boolean(message.artifacts?.some((artifact) => isUserFacingDeliveryArtifact(artifact, 'tool')))
  );
}

export function findRetryAnchorMessage(
  messages: AgentChatMessage[],
  errorIndex: number,
): AgentChatMessage | null {
  for (let index = Math.min(errorIndex, messages.length) - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === 'user') return message.transcriptEventId ? message : null;
  }
  return null;
}
function commandPanelTypeLabel(type: SessionCommandControlType, t: Translate): string {
  switch (type) {
    case 'checkpoint_selector':
      return t('sessionWorkbench.commandPanel.rewind', 'Rewind');
    case 'projection_status':
      return t('sessionWorkbench.commandPanel.sessionContext', 'Session context');
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
      return t('sessionWorkbench.commandPanel.workspaceRestore', 'Workspace restore');
    case 'resume_picker':
      return t('sessionWorkbench.commandPanel.resume', 'Resume');
    default:
      return 'Session';
  }
}

function checkpointId(checkpoint: SessionCommandCheckpoint): string {
  return String(checkpoint.checkpoint_event_id || checkpoint.event_id || '');
}

function checkpointLabel(checkpoint: SessionCommandCheckpoint, index: number): string {
  const sequence = checkpoint.sequence ?? checkpoint.turn_index ?? index + 1;
  const content = String(checkpoint.content || '').trim();
  return `${sequence}. ${content || checkpointId(checkpoint) || 'checkpoint'}`;
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
  onContinueSession,
  rewindUnavailableReason,
}: {
  control?: SessionCommandControlState | null;
  onDismiss: () => void;
  onRunCommand: (command: string, args?: Record<string, unknown>) => void | Promise<unknown>;
  onContinueSession?: (content: string) => void | Promise<unknown>;
  rewindUnavailableReason?: string | null;
}) {
  const { t } = useTranslation();
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
  const hideInternalDetails = control.type === 'resume_picker'
    || control.type === 'projection_status'
    || control.type === 'workspace_restore_confirmation'
    || control.type === 'checkpoint_selector';
  const details = hideInternalDetails ? [] : payloadSummary(control.payload);
  const resumeQuery = control.type === 'resume_picker'
    && control.payload?.interrupted === true
    && typeof control.payload.next_query === 'string'
    ? control.payload.next_query.trim()
    : '';
  const confirmationArgs = control.type === 'workspace_restore_confirmation'
    ? workspaceRestoreConfirmationArgs(control)
    : null;
  const rewindGuard = recordValue(control.payload?.rewind_guard);
  const rawExpectedSequence = rewindGuard.last_sequence;
  const expectedLastSequence = typeof rawExpectedSequence === 'number'
    ? rawExpectedSequence
    : (typeof rawExpectedSequence === 'string' && rawExpectedSequence.trim() ? Number(rawExpectedSequence) : null);
  const hasPanelBody = checkpoints.length > 0 || details.length > 0 || Boolean(confirmationArgs) || Boolean(resumeQuery);
  return (
    <section data-testid="session-command-control-panel" className="session-tui-command-panel">
      <div className={`session-tui-command-panel-header ${hasPanelBody ? 'has-body' : ''}`}>
        <div style={{ minWidth: 0 }}>
          <div className="session-tui-kicker">
            {commandPanelTypeLabel(control.type, t)}
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
          aria-label={t('common.close', 'Close')}
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
                title={t(`sessionWorkbench.commandPanel.rewindMode.${option.value}Description`, option.description)}
                onClick={() => setRewindMode(option.value)}
                className={`session-tui-rewind-mode-button ${rewindMode === option.value ? 'is-active' : ''}`}
              >
                {t(`sessionWorkbench.commandPanel.rewindMode.${option.value}`, option.label)}
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
                        {t('sessionWorkbench.rewind.sameSessionHere', 'Rewind this session here')}
                      </button>
                      <button
                        type="button"
                        data-testid="session-checkpoint-branch-action"
                        data-session-command="branch"
                        onClick={() => onRunCommand('branch', { anchor_event_id: id })}
                      >
                        {t('sessionWorkbench.rewind.branchNewSession', 'Branch into new session')}
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
              {t('sessionWorkbench.commandPanel.confirmRestore', 'Confirm restore')}
            </button>
            <button
              type="button"
              data-testid="session-workspace-restore-cancel-action"
              onClick={onDismiss}
            >
              {t('common.cancel', 'Cancel')}
            </button>
          </div>
        </div>
      ) : null}
      {resumeQuery && onContinueSession ? (
        <div className="session-tui-command-panel-body">
          <div className="session-tui-confirm-row">
            <button
              type="button"
              data-testid="session-resume-continue-action"
              onClick={() => {
                onDismiss();
                void onContinueSession(t('sessionWorkbench.commandPanel.resumeContinuePrompt', resumeQuery));
              }}
            >
              {t('sessionWorkbench.commandPanel.continue', 'Continue')}
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

function SessionHydratingState({ label }: { label: string }) {
  return (
    <div
      data-testid="session-loading-state"
      role="status"
      aria-live="polite"
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
  collapseAssistantContent?: boolean;
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
  collapseAssistantContent = false,
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
            <AuthenticatedImage
              src={msg.imageUrl}
              alt={msg.fileName}
              style={{ maxWidth: '200px', maxHeight: '150px', borderRadius: '8px', border: '1px solid var(--border-subtle)' }}
              loading="lazy"
              pendingClassName="session-tui-image-pending"
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
          <AssistantMessageBody
            content={msg.content}
            streaming={Boolean((msg as any)._streaming)}
            supplemental={collapseAssistantContent}
            supplementalLabel={t('agent.chat.canonicalCardSupplement', 'Agent supplemental notes')}
          />
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
  activeSession: selectedActiveSession,
  sessionTransitionPending = false,
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
  showHistoryScrollBtn,
  onScrollHistoryToBottom,
  chatContainerRef,
  onChatScroll,
  chatMessages,
  chatMessagesSessionId,
  runtimeSummary: selectedRuntimeSummary,
  agentPermissions,
  transportNotice,
  isWaiting: selectedIsWaiting,
  runtimePhase: selectedRuntimePhase = 'idle',
  activeRunStatus: selectedActiveRunStatus,
  activeRunId: selectedActiveRunId,
  runtimeBudget,

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
  isStreaming: selectedIsStreaming,
  onAbortGeneration,
  sessionOnly = false,
}: AgentChatSectionProps) {
  const { t } = useTranslation();
  const activeSession = sessionTransitionPending ? null : selectedActiveSession;
  const runtimeSummary = sessionTransitionPending ? null : selectedRuntimeSummary;
  const isWaiting = sessionTransitionPending ? false : selectedIsWaiting;
  const runtimePhase = sessionTransitionPending ? 'idle' : selectedRuntimePhase;
  const activeRunStatus = sessionTransitionPending ? null : selectedActiveRunStatus;
  const activeRunId = sessionTransitionPending ? null : selectedActiveRunId;
  const isStreaming = sessionTransitionPending ? false : selectedIsStreaming;
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
    ? t(
      'sessionWorkbench.rewind.activeRunBlocked',
      'Stop the current turn before rewinding. Creating a new branch session remains available.',
    )
    : null;
  const {
    selectedId: selectedThreadItemId,
    selectedItem: selectedThreadItem,
    selectItem: selectThreadItem,
    clearSelection: clearThreadItemSelection,
  } = useThreadItemRuntimeController(activeSessionId, visibleTimeline);

  const [artifactPreview, setArtifactPreview] = React.useState<ArtifactPreviewState | null>(null);
  React.useEffect(() => {
    const url = artifactPreview?.url;
    return () => {
      if (url?.startsWith('blob:')) URL.revokeObjectURL(url);
    };
  }, [artifactPreview?.url]);
  const [runtimePanelCollapsed, setRuntimePanelCollapsed] = useResponsiveRuntimePanel();
  const [focusedWorkflow, setFocusedWorkflow] = React.useState<RuntimeSectionItemModel | null>(null);
  const [focusedGitCheckpointId, setFocusedGitCheckpointId] = React.useState<string | null>(null);
  const gitScrollFrameRef = React.useRef<number | null>(null);

  const composerRuntime = buildComposerRuntimePresentation(runtimeSummary);
  const runtimeUsageLabel = composerRuntime.contextUsedPercent !== null && composerRuntime.contextUsedPercent >= 75
    ? t('agent.chat.composer.contextUsage', 'Context {{percent}}%', { percent: composerRuntime.contextUsedPercent })
    : '';
  const runtimeUsageTitle = composerRuntime.contextUsedPercent !== null && composerRuntime.contextUsedPercent >= 75
    ? t(
      'agent.chat.composer.contextUsageTitle',
      '{{percent}}% of this conversation context is in use.',
      { percent: composerRuntime.contextUsedPercent },
    )
    : '';
  const agentDisplayName = (agent as any)?.agent_class === 'internal_system' || agent?.name === '__system_hr__'
    ? t('agentCreate.hrAgentName', 'HR Agent')
    : agent?.name || '';
  const permissionModeLabel = getSessionPermissionModeLabel(sessionPermissionMode, t);
  const composerIntentLabel = getComposerIntentLabel(planModeRequested, goalModeRequested, t);
  const modelBadgeLabel = composerRuntime.modelLabel || t('agent.chat.composer.currentModel', 'Current model');
  const modelBadgeTitle = [modelBadgeLabel, runtimeUsageTitle].filter(Boolean).join(' · ');

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
      const input = buildMessageFeedbackInput(messageId, label);

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

  const downloadArtifactFile = React.useCallback((artifact: ChatArtifactPart) => {
    const artifactAgentId = artifactWorkspaceAgentId(artifact, effectiveAgentId);
    return artifactAgentId ? downloadChatArtifact(artifact, artifactAgentId, resourceOperatorOptions, t) : Promise.resolve();
  }, [effectiveAgentId, resourceOperatorOptions, t]);
  const openArtifact = React.useCallback(async (artifact: ChatArtifactPart) => {
    const artifactAgentId = artifactWorkspaceAgentId(artifact, effectiveAgentId);
    if (!artifactAgentId) return;
    const fetchArtifactBlob = () => artifact.id
      ? fileApi.downloadArtifact(artifactAgentId, artifact.id, resourceOperatorOptions)
      : fileApi.download(artifactAgentId, artifact.path, resourceOperatorOptions);
    if (getArtifactOpenMode(artifact) === 'download') {
      await downloadArtifactFile(artifact);
      return;
    }

    const previewKind = getEffectiveArtifactPreviewKind(artifact);
    if (previewKind === 'office') {
      setArtifactPreview({ artifact, loading: true });
      try {
        setArtifactPreview(await loadOfficeArtifactPreview(artifact, artifactAgentId, resourceOperatorOptions));
      } catch (error) {
        setArtifactPreview({
          artifact,
          error: error instanceof Error ? error.message : String(error),
        });
      }
      return;
    }
    if (previewKind === 'markdown' || previewKind === 'text' || !previewKind) {
      setArtifactPreview({ artifact, loading: true });
      try {
        const response = artifact.id
          ? await fileApi.readArtifact(artifactAgentId, artifact.id, resourceOperatorOptions)
          : await fileApi.read(artifactAgentId, artifact.path, resourceOperatorOptions);
        setArtifactPreview({
          artifact,
          content: response.content || '',
          usingSnapshot: Boolean(response.uses_snapshot || artifact.snapshotHash),
          workspaceChanged: Boolean(response.workspace_changed),
          legacyCurrentFileFallback: Boolean(response.legacy_current_file_fallback),
        });
      } catch (error) {
        if (typeof artifact.previewSnapshotContent === 'string') {
          setArtifactPreview({
            artifact,
            content: artifact.previewSnapshotContent,
            usingSnapshot: true,
          });
          return;
        }
        setArtifactPreview({
          artifact,
          error: error instanceof Error && !String(error.message || '').includes('File not found')
            ? error.message
            : t('agent.chat.artifacts.missingNoSnapshot', 'This file is no longer available in the workspace.'),
        });
      }
      return;
    }

    try {
      const blob = await fetchArtifactBlob();
      setArtifactPreview({ artifact, url: URL.createObjectURL(blob) });
    } catch (error) {
      setArtifactPreview({
        artifact,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }, [downloadArtifactFile, effectiveAgentId, resourceOperatorOptions, t]);

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
      const retryAnchor = (item.item_type === 'warning' || item.item_type === 'error') && item.item_data.retryable
        ? findRetryAnchorMessage(visibleTimeline, index)
        : null;
      const recoveryActions = retryAnchor && onBranchMessage ? (
        <button
          type="button"
          className="btn btn-secondary"
          data-testid="thread-item-retry-turn"
          onClick={() => void onBranchMessage(retryAnchor, 'edit', retryAnchor.content)}
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
          collapseAssistantContent={shouldCollapseAssistantSupplement(visibleTimeline, index)}
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
            <RunDisclosureBlock
              timeline={cell.timeline}
              presentationKey={activeSessionId && (cell.runId || cell.timeline.startedAt)
                ? `${activeSessionId}:${cell.runId || cell.timeline.startedAt}`
                : null}
            />
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
  const { data: sessionDecisionData, refetch: refetchSessionDecisions } = useQuery({
    queryKey: ['chat-session-decisions', effectiveAgentId, activeSessionId, sessionAuthorityMode],
    queryFn: () => chatApi.listSessionDecisions(effectiveAgentId!, activeSessionId!, sessionOperatorOptions),
    enabled: Boolean(effectiveAgentId && activeSessionId && !isDraftSession),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const sessionDecisions = Array.isArray(sessionDecisionData)
    ? sessionDecisionData as SessionDecisionTrace[]
    : [];
  const submitDecisionFeedback = React.useCallback(
    async (decision: SessionDecisionTrace, label: RecordSessionFeedbackInput['label']) => {
      if (!effectiveAgentId || !activeSessionId) return;
      try {
        await chatApi.recordSessionFeedback(effectiveAgentId, activeSessionId, {
          label,
          decision_id: decision.id,
        });
        await refetchSessionDecisions?.();
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
    [activeSessionId, effectiveAgentId, refetchSessionDecisions, t],
  );
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
      activeRunId,
      runtimePhase,
    }, threadTimelineCacheRef.current),
    [
      activeRunStatus,
      activeRunId,
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
            {runtimeStatusLabel(teamMemberWindow.status, t)}
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
              <div className="detail-session-browser-subtitle">{agentDisplayName || t('agent.chat.session', 'Session')}</div>
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
        {sessionTransitionPending ? (
          <SessionHydratingState label={t('agent.chat.startingNewSession', 'Starting a new conversation…')} />
        ) : !activeSession ? (
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
              {t('agent.chat.startConversation', { name: agentDisplayName })}
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
                    <SessionHydratingState label={t('agent.chat.transport.initializing', 'Loading durable session history...')} />
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
                    <SessionHydratingState label={t('agent.chat.transport.initializing', 'Loading durable session history...')} />
                  ) : visibleChatMessages.length === 0 && (
                    <div style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-tertiary)' }}>
                      <div style={{ fontSize: '13px', marginBottom: '4px' }}>{activeSession?.title || t('agent.chat.startChat')}</div>
                      <div style={{ fontSize: '12px' }}>{t('agent.chat.startConversation', { name: agentDisplayName })}</div>
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
                phase={transportPhase} runtimeBudget={runtimeBudget}
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
                  onRetry={() => void openArtifact(artifactPreview.artifact)}
                  onDownload={() => void downloadArtifactFile(artifactPreview.artifact)}
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
                onContinueSession={onSendMessage}
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
                permissionOptions={sessionPermissionModeOptions()}
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
          presentationStatus={threadTimelineModel.header.status}
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
          sessionDecisions={sessionDecisions}
          onDecisionFeedback={submitDecisionFeedback}
        />
      ) : null}
    </div>
  );
}

export default React.memo(AgentChatSection);

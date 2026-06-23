import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconChecklist,
  IconDownload,
  IconExternalLink,
  IconFileText,
  IconLoader2,
  IconPaperclip,
  IconSend2,
  IconX,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';

import MarkdownRenderer from '../../components/MarkdownRenderer';
import AskUserQuestionCard from './AskUserQuestionCard';
import PlanModeRequestCard from './PlanModeRequestCard';
import ChatWorkLedgerDock from './ChatWorkLedgerDock';
import CopyMessageButton from './CopyMessageButton';
import DeepResearchStreamPanel from './DeepResearchStreamPanel';
import PlanCard from './PlanCard';
import RunDisclosureBlock from './RunDisclosureBlock';
import SlashCommandMenu from './SlashCommandMenu';
import { SessionWorkbenchHeader, SessionWorkbenchInspector } from '../session-workbench/SessionWorkbenchChrome';
import SessionNativeControls from '../session-workbench/SessionNativeControls';
import { buildThreadTimeline, type ThreadTimelineModel } from '../session-workbench/timelineModel';
import { chatApi } from '../../api/domains/chat';
import { fileApi } from '../../api/domains/files';
import { planApi } from '../../api/domains/plans';
import type { ToolCallMeta } from './toolResultEnvelope';
import {
  computeComposerHeight,
  getCompactionDisplayContent,
  type AgentChatMessage,
  type ChatArtifactPart,
  type ChatRuntimeSummary,
} from './chatRuntime';

type AttachedFile = {
  name: string;
  text: string;
  path?: string;
  imageUrl?: string;
};

type ConversationBranchMode = 'fork' | 'edit' | 'insert_before' | 'insert_after' | 'reply' | 'regenerate' | 'rewind';

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

export interface BranchComposeDraft {
  mode: ConversationBranchMode;
  message: AgentChatMessage;
  content: string;
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
  isStreaming: boolean;
  onAbortGeneration: () => void;
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
};

type Translate = ReturnType<typeof useTranslation>['t'];

const _LIVE_DEEP_RESEARCH_STATUSES = new Set(['running', 'pending', 'in_progress']);
const DEEP_RESEARCH_FALLBACK_MAX_AGE_MS = 6 * 60 * 60 * 1000;
const PLAN_ID_RE = /\bplan_id\s*[=:]\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i;

function _isLiveDeepResearchStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return _LIVE_DEEP_RESEARCH_STATUSES.has(status.toLowerCase());
}

function _isRecentDeepResearchFallbackMessage(message: AgentChatMessage): boolean {
  if (!message.timestamp) return true;
  const timestampMs = new Date(message.timestamp).getTime();
  if (!Number.isFinite(timestampMs)) return false;
  return Date.now() - timestampMs <= DEEP_RESEARCH_FALLBACK_MAX_AGE_MS;
}

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

export function BranchComposePanel({
  draft,
  busy,
  onChange,
  onCancel,
  onSubmit,
}: {
  draft: BranchComposeDraft | null;
  busy: boolean;
  onChange: (content: string) => void;
  onCancel: () => void;
  onSubmit: () => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  if (!draft) return null;
  const modeLabel = draft.mode.replace(/_/g, ' ');
  const canSubmit = draft.content.trim().length > 0 && !busy;
  return (
    <div
      data-testid="branch-compose-panel"
      style={{
        borderTop: '1px solid var(--border-subtle)',
        background: 'var(--bg-secondary)',
        padding: '10px 12px',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '10px', alignItems: 'center', marginBottom: '8px' }}>
        <div>
          <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
            {t('agent.chat.branch.composeTitle', 'Create branch')}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
            {modeLabel}
          </div>
        </div>
        <button type="button" onClick={onCancel} disabled={busy} style={{ border: 'none', background: 'transparent', color: 'var(--text-tertiary)', cursor: busy ? 'not-allowed' : 'pointer' }}>
          {t('common.cancel', 'Cancel')}
        </button>
      </div>
      <textarea
        data-testid="branch-compose-input"
        value={draft.content}
        onChange={(event) => onChange(event.target.value)}
        rows={4}
        style={{
          width: '100%',
          boxSizing: 'border-box',
          resize: 'vertical',
          minHeight: '84px',
          border: '1px solid var(--border-subtle)',
          borderRadius: '6px',
          padding: '8px 10px',
          background: 'var(--bg-primary)',
          color: 'var(--text-primary)',
          font: 'inherit',
          fontSize: '12px',
          lineHeight: 1.5,
        }}
      />
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '8px' }}>
        <button type="button" className="btn btn-primary" disabled={!canSubmit} onClick={onSubmit} style={{ fontSize: '12px', padding: '5px 10px' }}>
          {busy ? t('common.saving', 'Saving...') : t('agent.chat.branch.createBranch', 'Create branch')}
        </button>
      </div>
    </div>
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

export type ArtifactOpenMode = 'inline_preview' | 'download';

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
    return 'inline_preview';
  }
  return 'download';
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
  return (
    <section
      data-testid="session-artifact-inspector"
      style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: '8px',
        background: 'var(--bg-secondary)',
        overflow: 'hidden',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '9px 10px', borderBottom: '1px solid var(--border-subtle)' }}>
        <IconFileText size={15} color="var(--text-tertiary)" />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {preview.artifact.name}
          </div>
          <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
            {t('agent.chat.artifacts.preview', 'Preview')}
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
      <div style={{ padding: '10px', maxHeight: '42vh', overflow: 'auto' }}>
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
            style={{ maxWidth: '100%', maxHeight: '34vh', borderRadius: '6px', border: '1px solid var(--border-subtle)' }}
          />
        ) : previewKind === 'pdf' && preview.url ? (
          <iframe
            title={preview.artifact.name}
            src={preview.url}
            style={{ width: '100%', height: '34vh', border: '1px solid var(--border-subtle)', borderRadius: '6px' }}
          />
        ) : previewKind === 'markdown' ? (
          <div style={{ fontSize: '12px', lineHeight: 1.6 }}>
            <MarkdownRenderer content={preview.content || ''} />
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
            {preview.content || ''}
          </pre>
        )}
      </div>
    </section>
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
        const href = fileApi.downloadUrl(String(agentId), artifact.path);
        const size = formatArtifactSize(artifact.size);
        return (
          <div
            key={`${artifact.id || artifact.path}`}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '8px',
              borderRadius: '8px',
              border: '1px solid var(--border-subtle)',
              background: 'var(--bg-elevated)',
              minWidth: 0,
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
              onClick={() => onOpenArtifact?.(artifact)}
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

function MessageBranchActions({
  message,
  isLeft,
  onBranchMessage,
}: {
  message: AgentChatMessage;
  isLeft: boolean;
  onBranchMessage?: (message: AgentChatMessage, mode: ConversationBranchMode) => void | Promise<unknown>;
}) {
  if (!message.id || !onBranchMessage) return null;
  const isUser = message.role === 'user';
  const isAssistant = message.role === 'assistant';
  if (!isUser && !isAssistant) return null;

  const actions: Array<{ mode: ConversationBranchMode; label: string; testId: string }> = [
    { mode: 'fork', label: 'Fork', testId: 'message-action-fork' },
    ...(isUser ? [{ mode: 'edit' as ConversationBranchMode, label: 'Edit', testId: 'message-action-edit' }] : []),
    { mode: 'insert_before', label: 'Insert before', testId: 'message-action-insert-before' },
    { mode: 'insert_after', label: 'Insert after', testId: 'message-action-insert-after' },
    { mode: 'reply', label: 'Reply', testId: 'message-action-reply' },
    ...(isAssistant ? [{ mode: 'regenerate' as ConversationBranchMode, label: 'Regenerate', testId: 'message-action-regenerate' }] : []),
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
          key={action.mode}
          type="button"
          data-testid={action.testId}
          title={action.label}
          onClick={() => onBranchMessage(message, action.mode)}
          style={{
            border: '1px solid var(--border-subtle)',
            background: 'var(--bg-secondary)',
            color: 'var(--text-tertiary)',
            borderRadius: '4px',
            fontSize: '10px',
            lineHeight: 1,
            padding: '3px 5px',
            cursor: 'pointer',
          }}
        >
          {action.label}
        </button>
      ))}
    </div>
  );
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

  if (toolMeta.kind === 'plan_needs_confirmation') {
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

  if (toolMeta.kind === 'deep_research') {
    const gateRows = Object.entries(toolMeta.qualityGates).map(([gate, state]) => `${gate}: ${state}`);
    const statRows = [
      toolMeta.taskId ? `${t('agent.chat.toolResults.taskId', 'Task')}: ${toolMeta.taskId}` : '',
      toolMeta.status ? `${t('agent.chat.toolResults.status', 'Status')}: ${toolMeta.status}` : '',
      typeof toolMeta.sourceCount === 'number' ? `${t('agent.chat.toolResults.sources', 'Sources')}: ${toolMeta.sourceCount}` : '',
      typeof toolMeta.claimCount === 'number' ? `${t('agent.chat.toolResults.claims', 'Claims')}: ${toolMeta.claimCount}` : '',
      toolMeta.reportPath ? `${t('agent.chat.toolResults.report', 'Report')}: ${toolMeta.reportPath}` : '',
    ].filter(Boolean);
    const showRawOutput = rawText.length > 0 && rawText !== toolResult;
    const shouldStreamLive =
      Boolean(agentId) &&
      Boolean(toolMeta.taskId) &&
      _isLiveDeepResearchStatus(toolMeta.status);
    return (
      <div style={{ display: 'grid', gap: '8px' }}>
        <div style={{ display: 'grid', gap: '4px' }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--accent-text)' }}>
            {t('agent.chat.toolResults.deepResearchTitle', 'Deep Research')}
          </div>
          {toolMeta.summary && <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{toolMeta.summary}</div>}
        </div>
        {shouldStreamLive && (
          <DeepResearchStreamPanel agentId={agentId as string} taskId={toolMeta.taskId as string} />
        )}
        <StructuredToolSection label={t('agent.chat.toolResults.runState', 'Run State')} items={statRows} />
        <StructuredToolSection label={t('agent.chat.toolResults.qualityGates', 'Quality Gates')} items={gateRows} />
        <StructuredToolSection label={t('agent.chat.toolResults.gaps', 'Gaps')} items={toolMeta.gaps} />
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
  isStreaming,
  onAbortGeneration,
}: AgentChatSectionProps) {
  const { t, i18n } = useTranslation();
  const effectiveAgentId = agentId ? String(agentId) : (agent?.id ? String(agent.id) : null);
  const planModeToggleLabel = planModeRequested
    ? t('agent.plan.composerToggleOn', 'Plan Mode enabled')
    : t('agent.plan.composerToggleOff', 'Start next message in Plan Mode');

  const currentUserId = currentUser?.id ? String(currentUser.id) : null;
  const isReadOnlySession =
    !!activeSession &&
    (((activeSession.user_id && currentUser && activeSession.user_id !== String(currentUser.id)) as boolean) ||
      activeSession.source_channel === 'agent' ||
      activeSession.participant_type === 'agent');

  const locale = i18n.language === 'zh' ? 'zh-CN' : 'en-US';
  const channelLabel: Record<string, string> = {
    feishu: t('common.channels.feishu'),
    discord: t('common.channels.discord'),
    slack: t('common.channels.slack'),
    dingtalk: t('common.channels.dingtalk'),
    wecom: t('common.channels.wecom'),
    wechat_personal: t('common.channels.wechatPersonal'),
    telegram: t('common.channels.telegram'),
    email: t('common.channels.email'),
    local_bridge: t('common.channels.localBridge', 'Local Bridge'),
  };

  const [artifactPreview, setArtifactPreview] = React.useState<ArtifactPreviewState | null>(null);
  const [branchDraft, setBranchDraft] = React.useState<BranchComposeDraft | null>(null);
  const [branchBusy, setBranchBusy] = React.useState(false);

  const startBranchAction = React.useCallback(
    async (message: AgentChatMessage, mode: ConversationBranchMode) => {
      if (!onBranchMessage) return;
      if (mode === 'fork' || mode === 'regenerate') {
        await onBranchMessage(message, mode);
        return;
      }
      setBranchDraft({
        mode,
        message,
        content: mode === 'edit' ? message.content || '' : '',
      });
    },
    [onBranchMessage],
  );

  const submitBranchDraft = React.useCallback(async () => {
    if (!branchDraft || !onBranchMessage || !branchDraft.content.trim()) return;
    setBranchBusy(true);
    try {
      await onBranchMessage(branchDraft.message, branchDraft.mode, branchDraft.content.trim());
      setBranchDraft(null);
    } finally {
      setBranchBusy(false);
    }
  }, [branchDraft, onBranchMessage]);

  const openArtifact = React.useCallback(async (artifact: ChatArtifactPart) => {
    if (!effectiveAgentId) return;
    const href = fileApi.downloadUrl(effectiveAgentId, artifact.path);
    if (getArtifactOpenMode(artifact) === 'download') {
      window.open(href, '_blank', 'noopener,noreferrer');
      return;
    }

    const previewKind = getEffectiveArtifactPreviewKind(artifact);
    if (previewKind === 'markdown' || previewKind === 'text' || !previewKind) {
      setArtifactPreview({ artifact, loading: true });
      try {
        const response = await fileApi.read(effectiveAgentId, artifact.path);
        setArtifactPreview({ artifact, content: response.content || '', url: href });
      } catch (error) {
        setArtifactPreview({
          artifact,
          url: href,
          error: error instanceof Error ? error.message : t('agent.chat.artifacts.previewFailed', 'Preview failed'),
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
      const statusColor =
        msg.eventStatus === 'blocked' || msg.eventStatus === 'capability_denied'
          ? 'var(--error)'
          : msg.eventStatus === 'approval_required'
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
      if (msg.eventCapability) metaParts.push(msg.eventCapability);
      if (msg.eventSecurityZone) metaParts.push(`zone:${msg.eventSecurityZone}`);
      if (msg.eventApprovalId) metaParts.push(`approval:${msg.eventApprovalId}`);
      const isCompactionEvent = msg.eventType === 'session_compact';
      const compactionDisplay = isCompactionEvent ? getCompactionDisplayContent(msg.content) : null;
      const compactionDetails = isCompactionEvent
        ? compactionDisplay?.details || (msg.content?.trim() ? msg.content : null)
        : null;
      const compactionInProgress = msg.eventStatus === 'running' || msg.eventStatus === 'in_progress';

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
            ) : (
              <div style={{ fontSize: '12px', lineHeight: 1.6, color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{msg.content}</div>
            )}
            {(msg.eventReason || msg.eventNextStep) && (
              <div style={{ marginTop: '8px', fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
                {msg.eventReason && <div>{msg.eventReason}</div>}
                {msg.eventNextStep && <div>{msg.eventNextStep}</div>}
              </div>
            )}
            {metaParts.length > 0 && (
              <div style={{ marginTop: '6px', fontSize: '11px', color: 'var(--text-tertiary)' }}>{metaParts.join(' · ')}</div>
            )}
          </div>
        </div>
      );
    },
    [t],
  );

  const ChatMessageItem = React.useMemo(
    () =>
      React.memo(({ msg, i, isLeft }: { msg: AgentChatMessage; i: number; isLeft: boolean }) => {
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
              style={{
                fontSize: '10px',
                color: 'var(--text-tertiary)',
                marginTop: '4px',
                opacity: 0.6,
                display: 'flex',
                alignItems: 'center',
                justifyContent: isLeft ? 'flex-start' : 'flex-end',
              }}
            >
              {timeStr}
              {msg.content && <CopyMessageButton text={msg.content} />}
              <MessageBranchActions message={msg} isLeft={isLeft} onBranchMessage={startBranchAction} />
            </div>
          );
        })();

        return (
          <div key={i} style={{ display: 'flex', flexDirection: isLeft ? 'row' : 'row-reverse', gap: '8px', marginBottom: '8px' }}>
            <div
              style={{
                width: '28px',
                height: '28px',
                borderRadius: '50%',
                background: isLeft ? 'var(--bg-elevated)' : 'rgba(16,185,129,0.15)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '11px',
                flexShrink: 0,
                color: 'var(--text-secondary)',
                fontWeight: 600,
              }}
            >
              {isLeft ? (msg.sender_name ? msg.sender_name[0] : 'A') : 'U'}
            </div>
            <div
              style={{
                maxWidth: '75%',
                padding: '8px 12px',
                borderRadius: '12px',
                background: isLeft ? 'var(--bg-secondary)' : 'rgba(16,185,129,0.1)',
                fontSize: '13px',
                lineHeight: '1.5',
                wordBreak: 'break-word',
              }}
            >
              {isLeft && msg.sender_name && (
                <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginBottom: '2px', fontWeight: 600 }}>
                  🤖 {msg.sender_name}
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
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '5px',
                      background: isLeft ? 'rgba(0,0,0,0.05)' : 'rgba(0,0,0,0.08)',
                      borderRadius: '6px',
                      padding: '4px 8px',
                      marginBottom: msg.content ? '4px' : '0',
                      fontSize: '11px',
                      border: '1px solid var(--border-subtle)',
                      color: 'var(--text-secondary)',
                    }}
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
                  style={{
                    marginBottom: '8px',
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
                    💭 Thinking
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
		    [effectiveAgentId, startBranchAction, openArtifact, t],
	  );

  const renderToolCall = (msg: AgentChatMessage, index: number, running = false) => (
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
          </div>
        )}
      </details>
    </div>
  );

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
      message.toolMeta?.kind === 'plan_needs_confirmation' ||
      message.toolMeta?.kind === 'user_clarification' ||
      message.toolMeta?.kind === 'plan_mode_request' ||
      message.toolMeta?.kind === 'create_employee_success' ||
      message.toolMeta?.kind === 'hr_preview'
    )
  );

  const renderConversationMessage = (message: AgentChatMessage, index: number, isLeft: boolean) => {
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
    return <ChatMessageItem key={index} msg={message} i={index} isLeft={isLeft} />;
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
        nodes.push(renderConversationMessage(cell.message, cell.index, resolveIsLeft(cell.message, cell.index)));
        return;
      }
      if (cell.kind === 'assistant_final') {
        nodes.push(renderConversationMessage(cell.message, cell.index, resolveIsLeft(cell.message, cell.index)));
        return;
      }
      if (cell.kind === 'active_run') {
        nodes.push(
          <div key={cell.id} data-testid="active-run-cell" style={{ marginBottom: '8px' }}>
            <RunDisclosureBlock timeline={cell.timeline} />
            {cell.sourceMessages.map((entry) => (
              isInlineToolCardMessage(entry.message) ? renderInlinePlanToolCall(entry.message, entry.index) : null
            ))}
            {cell.answer ? renderConversationMessage(cell.answer, cell.answerIndex ?? 0, resolveIsLeft(cell.answer, cell.answerIndex ?? 0)) : null}
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
  const visibleHistoryMsgs = historyMessagesSessionId === activeSessionId ? historyMsgs : [];
  const visibleChatMessages = chatMessagesSessionId === activeSessionId ? chatMessages : [];
  const visibleTimeline = isReadOnlySession ? visibleHistoryMsgs : visibleChatMessages;
  const { data: sessionIndexData } = useQuery({
    queryKey: ['chat-session-index', effectiveAgentId, activeSessionId],
    queryFn: () => chatApi.getSessionIndex(effectiveAgentId!, activeSessionId!),
    enabled: Boolean(effectiveAgentId && activeSessionId),
    staleTime: 10_000,
  });
  const hasActiveChatRun = Boolean(activeRunStatus || isWaiting || isStreaming);
  const fallbackWorkLedger = (() => {
    for (const message of [...visibleTimeline].reverse()) {
      const meta = message.toolMeta;
      if (meta?.kind !== 'deep_research' || !meta.taskId) {
        continue;
      }
      const live =
        message.toolStatus === 'running' ||
        (_isLiveDeepResearchStatus(meta.status) &&
          (hasActiveChatRun || _isRecentDeepResearchFallbackMessage(message)));
      if (!live) {
        continue;
      }
      return {
        runtimeTaskId: meta.taskId,
        title: t('agent.chat.toolResults.deepResearchTitle', 'Deep Research'),
        showDeepResearchStream: true,
        live,
      };
    }
    return null;
  })();
  const workLedgerLive = Boolean(hasActiveChatRun || fallbackWorkLedger?.live);
  const sessionIndex = sessionIndexData && !Array.isArray(sessionIndexData) ? sessionIndexData : null;
  const threadTimelineModel = buildThreadTimeline({
    messages: visibleTimeline,
    activeSession,
    runtimeSummary,
    sessionIndex,
    branchLineage,
    isWaiting,
    isStreaming,
    activeRunStatus,
  });

  return (
    <div data-testid="session-workbench" style={{ display: 'flex', gap: '0', flex: 1, minHeight: 0, height: 'calc(100vh - 206px)', background: 'var(--bg-primary)' }}>
      <div
        data-testid="session-workbench-sidebar"
        style={{
          width: '220px',
          flexShrink: 0,
          borderRight: '1px solid var(--border-subtle)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', padding: '10px 12px 0', gap: '4px', borderBottom: '1px solid var(--border-subtle)' }}>
          <button
            onClick={() => onSetChatScope('mine')}
            style={{
              flex: 1,
              padding: '5px 0',
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: chatScope === 'mine' ? 600 : 400,
              color: chatScope === 'mine' ? 'var(--text-primary)' : 'var(--text-tertiary)',
              borderBottom: chatScope === 'mine' ? '2px solid var(--accent-primary)' : '2px solid transparent',
              paddingBottom: '8px',
            }}
          >
            {t('agent.chat.mySessions')}
          </button>
          {isAdmin && (
            <button
              onClick={() => {
                onSetChatScope('all');
                onLoadAllSessions();
              }}
              style={{
                flex: 1,
                padding: '5px 0',
                background: 'none',
                border: 'none',
                cursor: 'pointer',
                fontSize: '12px',
                fontWeight: chatScope === 'all' ? 600 : 400,
                color: chatScope === 'all' ? 'var(--text-primary)' : 'var(--text-tertiary)',
                borderBottom: chatScope === 'all' ? '2px solid var(--accent-primary)' : '2px solid transparent',
                paddingBottom: '8px',
              }}
            >
              {t('agent.chat.allUsers')}
            </button>
          )}
        </div>

        {chatScope === 'mine' && (
          <div style={{ padding: '8px 12px', borderBottom: '1px solid var(--border-subtle)' }}>
            <button
              onClick={onCreateNewSession}
              style={{
                width: '100%',
                padding: '5px 8px',
                background: 'none',
                border: '1px solid var(--border-subtle)',
                borderRadius: '6px',
                cursor: 'pointer',
                fontSize: '12px',
                color: 'var(--text-secondary)',
                textAlign: 'left',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
              }}
            >
              + {t('agent.chat.newSession')}
            </button>
          </div>
        )}

        {chatScope === 'mine' && (
          <BranchLineagePanel
            activeSessionId={activeSessionId}
            lineage={branchLineage}
            loading={branchLineageLoading}
            onSelectSession={(sessionId) => {
              if (onSelectBranchSession) {
                return onSelectBranchSession(sessionId);
              }
              const target = [...sessions, ...allSessions].find((session: any) => String(session.id) === String(sessionId));
              if (target) return onSelectSession(target);
              return undefined;
            }}
          />
        )}

        <div style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
          {chatScope === 'mine' ? (
            sessionsLoading ? (
              <div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('common.loading')}</div>
            ) : sessions.length === 0 ? (
              <div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--text-tertiary)' }}>
                {t('agent.chat.noSessionsYet')}
                <br />
                {t('agent.chat.clickToStart')}
              </div>
            ) : (
              sessions.filter((s: any) => s.source_channel !== 'heartbeat').map((session) => {
                const isActive = activeSession?.id === session.id;
                const isOwn = session.user_id === currentUserId;
                const sessionChannelLabel = channelLabel[session.source_channel];
                return (
                  <div
                    key={session.id}
                    onClick={() => onSelectSession(session)}
                    className="session-item"
                    style={{
                      padding: '8px 12px',
                      cursor: 'pointer',
                      borderLeft: isActive ? '2px solid var(--accent-primary)' : '2px solid transparent',
                      background: isActive ? 'var(--bg-secondary)' : 'transparent',
                      marginBottom: '1px',
                      position: 'relative',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '2px' }}>
                      <div
                        style={{
                          fontSize: '12px',
                          fontWeight: isActive ? 600 : 400,
                          color: 'var(--text-primary)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          flex: 1,
                        }}
                      >
                        {session.title}
                      </div>
                      {sessionChannelLabel && (
                        <span
                          style={{
                            fontSize: '9px',
                            padding: '1px 4px',
                            borderRadius: '3px',
                            background: 'var(--bg-tertiary)',
                            color: 'var(--text-tertiary)',
                            flexShrink: 0,
                          }}
                        >
                          {sessionChannelLabel}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                      {isOwn && isActive && wsConnected && <span className="status-dot running" style={{ width: '5px', height: '5px', flexShrink: 0 }} />}
                      {session.last_message_at
                        ? new Date(session.last_message_at).toLocaleString(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                        : new Date(session.created_at).toLocaleString(locale, { month: 'short', day: 'numeric' })}
                      {session.message_count > 0 && <span style={{ marginLeft: 'auto' }}>{session.message_count}</span>}
                    </div>
                    <button
                      className="del-btn"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteSession(session.id);
                      }}
                      style={{
                        position: 'absolute',
                        top: '4px',
                        right: '4px',
                        background: 'none',
                        border: 'none',
                        cursor: 'pointer',
                        padding: '2px 4px',
                        opacity: 0,
                        fontSize: '14px',
                        color: 'var(--text-tertiary)',
                        lineHeight: 1,
                        transition: 'opacity 0.15s',
                      }}
                      title={t('chat.deleteSession', 'Delete session')}
                    >
                      ×
                    </button>
                  </div>
                );
              })
            )
          ) : (
            <>
              <div style={{ padding: '8px 10px', borderBottom: '1px solid var(--border-subtle)' }}>
                <select
                  value={allUserFilter}
                  onChange={(e) => onSetAllUserFilter(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '4px 6px',
                    fontSize: '11px',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '5px',
                    color: 'var(--text-primary)',
                    cursor: 'pointer',
                  }}
                >
                  <option value="">All Users</option>
                  {Array.from(new Set(allSessions.map((session) => session.username || session.user_id)))
                    .filter(Boolean)
                    .map((username) => (
                      <option key={String(username)} value={String(username)}>
                        {String(username)}
                      </option>
                    ))}
                </select>
              </div>
              {allSessionsLoading ? (
                <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  {[...Array(6)].map((_, index) => (
                    <div key={index} style={{ padding: '6px 0', animation: 'pulse 1.5s ease-in-out infinite', animationDelay: `${index * 0.1}s` }}>
                      <div style={{ height: '12px', width: `${70 + (index % 3) * 10}%`, background: 'var(--bg-tertiary)', borderRadius: '4px', marginBottom: '6px' }} />
                      <div style={{ height: '10px', width: `${40 + (index % 4) * 8}%`, background: 'var(--bg-tertiary)', borderRadius: '3px', opacity: 0.6 }} />
                    </div>
                  ))}
                </div>
              ) : allSessions.length === 0 ? (
                <div style={{ padding: '20px 12px', fontSize: '12px', color: 'var(--text-tertiary)', textAlign: 'center' }}>{t('agent.chat.noSessionsYet')}</div>
              ) : null}
              {!allSessionsLoading &&
                allSessions
                  .filter((session: any) => session.source_channel !== 'heartbeat')
                  .filter((session) => !allUserFilter || (session.username || session.user_id) === allUserFilter)
                  .map((session) => {
                    const isActive = activeSession?.id === session.id;
                    const sessionChannelLabel = channelLabel[session.source_channel];
                    return (
                      <div
                        key={session.id}
                        onClick={() => onSelectSession(session)}
                        className="session-item"
                        style={{
                          padding: '6px 12px',
                          cursor: 'pointer',
                          borderLeft: isActive ? '2px solid var(--accent-primary)' : '2px solid transparent',
                          background: isActive ? 'var(--bg-secondary)' : 'transparent',
                          position: 'relative',
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '1px' }}>
                          <div
                            style={{
                              fontSize: '12px',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                              color: 'var(--text-primary)',
                              flex: 1,
                            }}
                          >
                            {session.title}
                          </div>
                          {sessionChannelLabel && (
                            <span
                              style={{
                                fontSize: '9px',
                                padding: '1px 4px',
                                borderRadius: '3px',
                                background: 'var(--bg-tertiary)',
                                color: 'var(--text-tertiary)',
                                flexShrink: 0,
                              }}
                            >
                              {sessionChannelLabel}
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', display: 'flex', gap: '4px' }}>
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{session.username || ''}</span>
                          <span style={{ flexShrink: 0 }}>
                            {session.last_message_at ? new Date(session.last_message_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
                            {session.message_count > 0 ? ` · ${session.message_count}` : ''}
                          </span>
                        </div>
                        <button
                          className="del-btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            onDeleteSession(session.id);
                          }}
                          style={{
                            position: 'absolute',
                            top: '4px',
                            right: '4px',
                            background: 'none',
                            border: 'none',
                            cursor: 'pointer',
                            padding: '2px 4px',
                            opacity: 0,
                            fontSize: '14px',
                            color: 'var(--text-tertiary)',
                            lineHeight: 1,
                            transition: 'opacity 0.15s',
                          }}
                          title={t('chat.deleteSession', 'Delete session')}
                        >
                          ×
                        </button>
                      </div>
                    );
                  })}
            </>
          )}
        </div>
      </div>

      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', position: 'relative', minWidth: 0, overflow: 'hidden' }}>
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
            <div ref={historyContainerRef} onScroll={onHistoryScroll} style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
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
                {activeSession.source_channel === 'agent' ? `🤖 Agent Conversation · ${activeSession.username || 'Agents'}` : `Read-only · ${activeSession.username || 'User'}`}
              </div>
              {(() => {
                const isA2A = activeSession.source_channel === 'agent' || activeSession.participant_type === 'agent';
                const thisAgentName = agent?.name;
                const thisAgentPid = isA2A && thisAgentName ? visibleHistoryMsgs.find((message) => message.sender_name === thisAgentName)?.participant_id : null;
                return renderConversationMessages(
                  visibleHistoryMsgs,
                  (message) => (isA2A && thisAgentPid ? message.participant_id !== thisAgentPid : message.role === 'assistant'),
                );
              })()}
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
            <div ref={chatContainerRef} onScroll={onChatScroll} style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
              {visibleChatMessages.length === 0 && (
                <div style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-tertiary)' }}>
                  <div style={{ fontSize: '13px', marginBottom: '4px' }}>{activeSession?.title || t('agent.chat.startChat')}</div>
                  <div style={{ fontSize: '12px' }}>{t('agent.chat.startConversation', { name: agent.name })}</div>
                  <div style={{ fontSize: '11px', marginTop: '4px', opacity: 0.7 }}>{t('agent.chat.fileSupport')}</div>
                </div>
              )}
              {renderConversationMessages(visibleChatMessages, (message) => message.role === 'assistant', threadTimelineModel)}
              <div ref={chatEndRef} />
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
            ) : !wsConnected ? (
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
                Connecting...
              </div>
            ) : null}
            <div
              data-testid="session-composer"
              style={{
                position: 'relative',
                borderTop: '1px solid var(--border-subtle)',
                background: 'var(--bg-primary)',
                padding: '8px 12px',
              }}
            >
              {effectiveAgentId && activeSession?.id && (
                <SlashCommandMenu
                  agentId={effectiveAgentId}
                  sessionId={String(activeSession.id)}
                  inputValue={chatInput}
                  disabled={!wsConnected}
                  onPickCommand={(_command, template) => {
                    onSetChatInput(template);
                    setTimeout(() => chatInputRef.current?.focus(), 0);
                  }}
                />
              )}
              <BranchComposePanel
                draft={branchDraft}
                busy={branchBusy}
                onChange={(content) => setBranchDraft((draft) => (draft ? { ...draft, content } : draft))}
                onCancel={() => setBranchDraft(null)}
                onSubmit={submitBranchDraft}
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
              <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-end' }}>
              <input type="file" multiple ref={fileInputRef} onChange={onHandleChatFile} style={{ display: 'none' }} />
              <button
                className="btn btn-secondary"
                onClick={() => fileInputRef.current?.click()}
                disabled={!wsConnected || uploading || attachedFiles.length >= 10}
                aria-label={t('agent.chat.attachFile', 'Attach file')}
                title={t('agent.chat.attachFile', 'Attach file')}
                style={{
                  width: '36px',
                  height: '36px',
                  padding: 0,
                  minWidth: 'auto',
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  ...((!wsConnected || uploading || attachedFiles.length >= 10) ? { cursor: 'not-allowed', opacity: 0.4 } : {}),
                }}
              >
                {uploading ? <IconLoader2 size={17} /> : <IconPaperclip size={17} />}
              </button>
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
              <button
                type="button"
                onClick={onTogglePlanMode}
                aria-label={planModeToggleLabel}
                aria-pressed={planModeRequested}
                title={planModeToggleLabel}
                disabled={!wsConnected || isWaiting || isStreaming}
                style={{
                  width: '36px',
                  height: '36px',
                  borderRadius: '6px',
                  border: planModeRequested ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                  background: planModeRequested ? 'rgba(16,185,129,0.12)' : 'var(--bg-secondary)',
                  color: planModeRequested ? 'var(--accent-primary)' : 'var(--text-secondary)',
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: !wsConnected || isWaiting || isStreaming ? 'not-allowed' : 'pointer',
                  flexShrink: 0,
                }}
              >
                <IconChecklist size={18} stroke={1.8} />
              </button>
              <textarea
                ref={chatInputRef}
                className="chat-input"
                value={chatInput}
                onChange={(e) => onSetChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    onSendChatMsg();
                  }
	                }}
                onPaste={onHandlePaste}
                placeholder={
                  !wsConnected
                    ? 'Connecting...'
                    : attachedFiles.length > 0
                      ? t('agent.chat.askAboutFile', { name: attachedFiles.length === 1 ? attachedFiles[0].name : `${attachedFiles.length} files` })
                      : t('chat.placeholder')
                }
                disabled={!wsConnected}
                rows={1}
                style={{
                  flex: 1,
                  minHeight: '44px',
                  maxHeight: '160px',
                  resize: 'none',
                  padding: '10px 14px',
                  lineHeight: 1.5,
                }}
                autoFocus
              />
              {(isStreaming || isWaiting) && (
                <button className="btn btn-stop-generation" onClick={onAbortGeneration} style={{ padding: '6px 12px' }} title={t('chat.stop', 'Stop')}>
                  <span className="stop-icon" />
                </button>
              )}
              <button
                className="btn btn-primary"
                onClick={onSendChatMsg}
                disabled={!wsConnected || (!chatInput.trim() && attachedFiles.length === 0)}
                aria-label={t('chat.send')}
                title={t('chat.send')}
                style={{ width: '40px', height: '36px', padding: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
              >
                <IconSend2 size={18} />
              </button>
              </div>
            </div>
          </>
        )}
      </div>
      <SessionWorkbenchInspector model={threadTimelineModel.inspector}>
        {artifactPreview && (
          <ArtifactPreviewPanel
            preview={artifactPreview}
            onClose={() => setArtifactPreview(null)}
            t={t}
          />
        )}
        <SessionNativeControls
          agentId={effectiveAgentId}
          sessionId={activeSessionId}
          sessionIndex={sessionIndex}
          onEnterSession={onSelectBranchSession}
        />
        {effectiveAgentId && activeSession?.id && (
          <ChatWorkLedgerDock
            agentId={effectiveAgentId}
            sessionId={String(activeSession.id)}
            runtimeTaskId={fallbackWorkLedger?.runtimeTaskId}
            live={workLedgerLive}
          />
        )}
      </SessionWorkbenchInspector>
    </div>
  );
}

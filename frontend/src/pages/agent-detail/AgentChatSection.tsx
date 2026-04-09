import React from 'react';
import { useTranslation } from 'react-i18next';

import MarkdownRenderer from '../../components/MarkdownRenderer';
import CopyMessageButton from './CopyMessageButton';
import type { ToolCallMeta } from './toolResultEnvelope';
import {
  computeComposerHeight,
  type AgentChatMessage,
  type ChatRuntimeSummary,
} from './chatRuntime';

type AttachedFile = {
  name: string;
  text: string;
  path?: string;
  imageUrl?: string;
};

interface AgentChatSectionProps {
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
  showHistoryScrollBtn: boolean;
  onScrollHistoryToBottom: () => void;
  chatContainerRef: React.RefObject<HTMLDivElement | null>;
  onChatScroll: () => void;
  chatMessages: AgentChatMessage[];
  runtimeSummary: ChatRuntimeSummary | null;
  transportNotice: string | null;
  isWaiting: boolean;

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
  isStreaming: boolean;
  onAbortGeneration: () => void;
}

interface StructuredToolResultBodyProps {
  toolName?: string;
  toolMeta?: ToolCallMeta | null;
  toolResult?: string;
  toolRawResult?: string;
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
}: StructuredToolResultBodyProps) {
  const { t } = useTranslation();
  const rawText = typeof toolRawResult === 'string' && toolRawResult.trim() ? toolRawResult : '';

  if (!toolMeta) {
    return toolResult ? <RawToolResultBlock text={toolResult} /> : null;
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
  showHistoryScrollBtn,
  onScrollHistoryToBottom,
  chatContainerRef,
  onChatScroll,
  chatMessages,
  runtimeSummary,
  transportNotice,
  isWaiting,

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
  isStreaming,
  onAbortGeneration,
}: AgentChatSectionProps) {
  const { t, i18n } = useTranslation();

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
  };

  const [showInternalTrace, setShowInternalTrace] = React.useState(false);

  React.useEffect(() => {
    const input = chatInputRef.current;
    if (!input) return;
    input.style.height = '0px';
    const nextHeight = computeComposerHeight(input.scrollHeight);
    input.style.height = `${nextHeight}px`;
    input.style.overflowY = nextHeight >= 160 ? 'auto' : 'hidden';
  }, [chatInput, chatInputRef]);

  const formatCompactNumber = React.useCallback((value?: number | null) => {
    if (typeof value !== 'number' || Number.isNaN(value)) return '—';
    if (Math.abs(value) >= 1000) {
      const compact = value / 1000;
      const digits = Math.abs(compact) >= 100 ? 0 : 1;
      return `${compact.toFixed(digits)}K`;
    }
    return `${value}`;
  }, []);

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
      if (msg.activatedPacks?.length) metaParts.push(msg.activatedPacks.join(', '));
      if (msg.eventToolName) metaParts.push(msg.eventToolName);
      if (msg.eventCapability) metaParts.push(msg.eventCapability);
      if (msg.eventSecurityZone) metaParts.push(`zone:${msg.eventSecurityZone}`);
      if (msg.eventApprovalId) metaParts.push(`approval:${msg.eventApprovalId}`);

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
                {msg.eventTitle || t('agent.chat.runtime.eventTitle', 'Runtime Event')}
              </span>
            </div>
            <div style={{ fontSize: '12px', lineHeight: 1.6, color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{msg.content}</div>
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

        const timestampHtml = msg.timestamp
          ? (() => {
              const date = new Date(msg.timestamp);
              const now = new Date();
              const diffMs = now.getTime() - date.getTime();
              const isToday = date.toDateString() === now.toDateString();
              let timeStr = '';
              if (isToday) timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
              else if (diffMs < 7 * 86400000)
                timeStr = `${date.toLocaleDateString([], { weekday: 'short' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
              else
                timeStr = `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;

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
                </div>
              );
            })()
          : null;

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
              {timestampHtml}
            </div>
          </div>
        );
      }),
    [t],
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
            />
          </div>
        )}
      </details>
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

  const renderConversationMessage = (message: AgentChatMessage, index: number, isLeft: boolean) => {
    if (message.role === 'event') {
      return renderEventMessage(message, index);
    }
    if (message.role === 'tool_call') {
      if (!showInternalTrace) return null;
      return renderToolCall(message, index, message.toolStatus === 'running');
    }
    if (message.role === 'assistant' && !message.content?.trim()) {
      if (!message.thinking) return null;
      return renderThinkingCard(message.thinking, index);
    }
    return <ChatMessageItem key={index} msg={message} i={index} isLeft={isLeft} />;
  };

  const runtimeInfoItems = [
    {
      label: t('agent.chat.runtime.model', 'Model'),
      value: runtimeSummary?.model?.label || '—',
    },
    {
      label: t('agent.chat.runtime.remaining', 'Remaining'),
      value: formatCompactNumber(runtimeSummary?.runtime?.remaining_tokens_estimate),
    },
    {
      label: t('agent.chat.runtime.compactions', 'Compactions'),
      value: `${runtimeSummary?.compaction_count ?? 0}`,
    },
    {
      label: t('agent.chat.runtime.permissions', 'Permissions'),
      value: `${runtimeSummary?.permission_event_count ?? 0}`,
    },
    {
      label: t('agent.chat.runtime.teamMemory', 'Team Memory'),
      value: `${runtimeSummary?.team_memory_hit_count ?? 0}`,
    },
    {
      label: t('agent.chat.runtime.retryReason', 'Retry'),
      value: runtimeSummary?.last_retry_reason || '—',
    },
  ];

  const visibleTimeline = isReadOnlySession ? historyMsgs : chatMessages;
  const hasInternalTrace = visibleTimeline.some((message) => message.role === 'tool_call');

  return (
    <div style={{ display: 'flex', gap: '0', flex: 1, minHeight: 0, height: 'calc(100vh - 206px)' }}>
      <div
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
        {activeSession && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: '12px',
              padding: '10px 16px',
              borderBottom: '1px solid var(--border-subtle)',
              background: 'var(--bg-elevated)',
              flexWrap: 'wrap',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              {runtimeInfoItems.map((item) => (
                <div
                  key={item.label}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '6px',
                    padding: '5px 8px',
                    borderRadius: '999px',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    fontSize: '11px',
                  }}
                >
                  <span style={{ color: 'var(--text-tertiary)' }}>{item.label}</span>
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{item.value}</span>
                </div>
              ))}
              {runtimeSummary?.last_compaction?.summary && (
                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{runtimeSummary.last_compaction.summary}</span>
              )}
              {runtimeSummary?.last_tool_budget_event?.reason && (
                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                  {`${runtimeSummary.last_tool_budget_event.reason}${runtimeSummary.last_tool_budget_event.tool_name ? ` · ${runtimeSummary.last_tool_budget_event.tool_name}` : ''}`}
                </span>
              )}
            </div>
            {hasInternalTrace && (
              <button
                onClick={() => setShowInternalTrace((value) => !value)}
                style={{
                  background: 'none',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: '999px',
                  padding: '5px 10px',
                  fontSize: '11px',
                  color: 'var(--text-secondary)',
                  cursor: 'pointer',
                }}
              >
                {showInternalTrace
                  ? t('agent.chat.runtime.hideTrace', 'Hide internal trace')
                  : t('agent.chat.runtime.showTrace', 'Show internal trace')}
              </button>
            )}
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
                const thisAgentPid = isA2A && thisAgentName ? historyMsgs.find((message) => message.sender_name === thisAgentName)?.participant_id : null;
                return historyMsgs.map((message, index) => {
                  const isLeft = isA2A && thisAgentPid ? message.participant_id !== thisAgentPid : message.role === 'assistant';
                  return renderConversationMessage(message, index, isLeft);
                });
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
              {chatMessages.length === 0 && (
                <div style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-tertiary)' }}>
                  <div style={{ fontSize: '13px', marginBottom: '4px' }}>{activeSession?.title || t('agent.chat.startChat')}</div>
                  <div style={{ fontSize: '12px' }}>{t('agent.chat.startConversation', { name: agent.name })}</div>
                  <div style={{ fontSize: '11px', marginTop: '4px', opacity: 0.7 }}>{t('agent.chat.fileSupport')}</div>
                </div>
              )}
              {chatMessages.map((message, index) => {
                return renderConversationMessage(message, index, message.role === 'assistant');
              })}
              {isWaiting && (
                <div style={{ display: 'flex', gap: '8px', marginBottom: '8px', animation: 'fadeIn .2s ease' }}>
                  <div
                    style={{
                      width: '28px',
                      height: '28px',
                      borderRadius: '50%',
                      background: 'var(--bg-elevated)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: '11px',
                      flexShrink: 0,
                      color: 'var(--text-secondary)',
                      fontWeight: 600,
                    }}
                  >
                    A
                  </div>
                  <div style={{ padding: '8px 12px', borderRadius: '12px', background: 'var(--bg-secondary)', fontSize: '13px' }}>
                    <div className="thinking-indicator">
                      <div className="thinking-dots">
                        <span />
                        <span />
                        <span />
                      </div>
                      <span style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('agent.chat.thinking', 'Thinking...')}</span>
                    </div>
                  </div>
                </div>
              )}
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
            {attachedFiles.length > 0 && (
              <div
                style={{
                  padding: '6px 16px',
                  background: 'var(--bg-elevated)',
                  borderTop: '1px solid var(--border-subtle)',
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
                      borderRadius: '4px',
                      border: '1px solid var(--border-subtle)',
                      maxWidth: '200px',
                    }}
                  >
                    {file.imageUrl ? (
                      <img src={file.imageUrl} alt={file.name} style={{ width: '20px', height: '20px', borderRadius: '4px', objectFit: 'cover' }} />
                    ) : (
                      <span>📎</span>
                    )}
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.name}</span>
                    <button
                      onClick={() => onRemoveAttachedFile(index)}
                      style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', cursor: 'pointer', fontSize: '14px', padding: '0 2px' }}
                      title="Remove file"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', gap: '8px', padding: '8px 12px', borderTop: '1px solid var(--border-subtle)', alignItems: 'flex-end' }}>
              <input type="file" multiple ref={fileInputRef} onChange={onHandleChatFile} style={{ display: 'none' }} />
              <button
                className="btn btn-secondary"
                onClick={() => fileInputRef.current?.click()}
                disabled={!wsConnected || uploading || isWaiting || isStreaming || attachedFiles.length >= 10}
                style={{
                  padding: '6px 10px',
                  fontSize: '14px',
                  minWidth: 'auto',
                  ...((!wsConnected || uploading || isWaiting || isStreaming) ? { cursor: 'not-allowed', opacity: 0.4 } : {}),
                }}
              >
                {uploading ? '⏳' : '⦹'}
              </button>
              {uploading && uploadProgress >= 0 && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flex: '0 0 140px' }}>
                  {uploadProgress <= 100 ? (
                    <>
                      <div style={{ flex: 1, height: '4px', borderRadius: '2px', background: 'var(--bg-tertiary)', overflow: 'hidden' }}>
                        <div style={{ height: '100%', borderRadius: '2px', background: 'var(--accent-primary)', width: `${uploadProgress}%`, transition: 'width 0.15s ease' }} />
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
                    style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', cursor: 'pointer', fontSize: '12px', padding: '0 2px', lineHeight: 1 }}
                    title="Cancel upload"
                  >
                    ✕
                  </button>
                </div>
              )}
              <textarea
                ref={chatInputRef}
                className="chat-input"
                value={chatInput}
                onChange={(e) => onSetChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !isWaiting && !isStreaming) {
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
              {isStreaming || isWaiting ? (
                <button className="btn btn-stop-generation" onClick={onAbortGeneration} style={{ padding: '6px 16px' }} title={t('chat.stop', 'Stop')}>
                  <span className="stop-icon" />
                </button>
              ) : (
                <button className="btn btn-primary" onClick={onSendChatMsg} disabled={!wsConnected || (!chatInput.trim() && attachedFiles.length === 0)} style={{ padding: '6px 16px' }}>
                  {t('chat.send')}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

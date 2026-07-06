import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  IconCalendarTime,
  IconChecklist,
  IconCircleDashedCheck,
  IconFileText,
  IconLoader2,
  IconPaperclip,
  IconPlus,
  IconSend2,
  IconShieldCheck,
  IconTargetArrow,
  IconX,
} from '@tabler/icons-react';

import './LocalAgentChatSection.css';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import type { AgentPermissions } from '../../api/domains/agents';
import {
  localBridgeApi,
  type LocalAgentChannelEvent,
  type LocalAgentChannelSession,
  type LocalAgentWorkspaceUpload,
  type LocalBridgeConnection,
} from '../../api/domains/localBridge';
import { browserChannelWsUrl, connectionPresenceStatus, isOnlineConnection, mergeChannelEvents } from '../LocalAgents';
import { SessionWorkbenchHeader } from '../session-workbench/SessionWorkbenchChrome';
import type { AgentChatMessage, ChatArtifactPart } from './chatRuntime';
import type { SessionWorkbenchHeaderModel } from '../session-workbench/timelineModel';
import { composerShortcutText } from './sessionComposerShortcuts';

type AttachedLocalFile = LocalAgentWorkspaceUpload & {
  attachmentPath: string;
};

type LocalAgentChatSectionProps = {
  agentId: string;
  agent: any;
  agentPermissions?: AgentPermissions | null;
};

type LocalComposerActionKey = 'upload' | 'plan' | 'goal' | 'schedule';

function routeSessionIdFromSearch(search: string): string | null {
  const params = new URLSearchParams(search);
  return params.get('session_id') || params.get('session') || null;
}

export function localAgentRuntimeResumeHealth(
  connections: LocalBridgeConnection[] | null | undefined,
  loadingOrUnavailable = false,
): 'online' | 'offline' | 'unknown' {
  if (loadingOrUnavailable || !connections) return 'unknown';
  const activeConnections = connections.filter((connection) => connection.status === 'active');
  if (activeConnections.some(isOnlineConnection)) return 'online';
  if (activeConnections.length === 0) return 'offline';
  if (activeConnections.some((connection) => connectionPresenceStatus(connection) === 'unknown')) return 'unknown';
  return 'offline';
}

function localAgentEventText(event: LocalAgentChannelEvent): string {
  const payload = event.payload || {};
  const candidates = [
    payload.output,
    payload.text,
    payload.content,
    payload.message,
    payload.result,
    payload.summary,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate;
  }
  return '';
}

function artifactFromRecord(record: Record<string, unknown>): ChatArtifactPart | null {
  const path = String(record.path || record.workspace_path || '').trim();
  if (!path) return null;
  const fallbackName = path.split('/').filter(Boolean).pop() || path;
  const size = typeof record.size === 'number' && Number.isFinite(record.size) ? record.size : undefined;
  return {
    id: typeof record.id === 'string' ? record.id : (typeof record.artifact_id === 'string' ? record.artifact_id : undefined),
    name: typeof record.name === 'string' && record.name.trim()
      ? record.name.trim()
      : (typeof record.filename === 'string' && record.filename.trim() ? record.filename.trim() : fallbackName),
    path,
    previewKind: typeof record.previewKind === 'string'
      ? record.previewKind
      : (typeof record.preview_kind === 'string' ? record.preview_kind : undefined),
    mimeType: typeof record.mimeType === 'string'
      ? record.mimeType
      : (typeof record.mime_type === 'string' ? record.mime_type : undefined),
    size,
    source: 'local_agent',
  };
}

function localAgentEventArtifacts(event: LocalAgentChannelEvent): ChatArtifactPart[] {
  const payload = event.payload || {};
  const artifacts: ChatArtifactPart[] = [];
  const seen = new Set<string>();
  const pushArtifact = (candidate: unknown) => {
    if (!candidate || typeof candidate !== 'object') return;
    const artifact = artifactFromRecord(candidate as Record<string, unknown>);
    if (!artifact) return;
    const key = `${artifact.id || ''}:${artifact.path}`;
    if (seen.has(key)) return;
    seen.add(key);
    artifacts.push(artifact);
  };

  if (Array.isArray(payload.artifacts)) {
    payload.artifacts.forEach(pushArtifact);
  }
  if (Array.isArray(payload.files)) {
    payload.files.forEach(pushArtifact);
  }
  if (event.type === 'file') {
    pushArtifact(payload);
  }
  return artifacts;
}

export function localAgentChannelEventsToChatMessages(events: LocalAgentChannelEvent[]): AgentChatMessage[] {
  const ordered = [...events].sort((a, b) => {
    const left = a.created_at ? new Date(a.created_at).getTime() : 0;
    const right = b.created_at ? new Date(b.created_at).getTime() : 0;
    if (left !== right) return left - right;
    return String(a.id).localeCompare(String(b.id));
  });

  const messages: AgentChatMessage[] = [];
  const localRepliesByMessageId = new Map<string, AgentChatMessage>();
  const mergeArtifacts = (message: AgentChatMessage, artifacts: ChatArtifactPart[]) => {
    if (artifacts.length === 0) return;
    const current = message.artifacts || [];
    const seen = new Set(current.map((artifact) => `${artifact.id || ''}:${artifact.path}`));
    const next = [...current];
    for (const artifact of artifacts) {
      const key = `${artifact.id || ''}:${artifact.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      next.push(artifact);
    }
    message.artifacts = next;
  };

  for (const event of ordered) {
    const content = localAgentEventText(event);
    const artifacts = localAgentEventArtifacts(event);
    if (!content.trim() && artifacts.length === 0) continue;
    const role: AgentChatMessage['role'] = event.direction === 'hive_to_local' ? 'user' : 'assistant';
    if (role === 'assistant' && event.message_id) {
      const existing = localRepliesByMessageId.get(event.message_id);
      if (existing) {
        if (event.type === 'result') {
          existing.id = event.id;
          existing.content = content || existing.content;
        } else if (content) {
          existing.content = `${existing.content || ''}${content}`;
        }
        existing.timestamp = event.created_at || existing.timestamp;
        mergeArtifacts(existing, artifacts);
        continue;
      }
      const message: AgentChatMessage = {
        id: event.id,
        role,
        content,
        timestamp: event.created_at || undefined,
        artifacts: artifacts.length > 0 ? artifacts : undefined,
        sender_name: 'Local Agent',
      };
      localRepliesByMessageId.set(event.message_id, message);
      messages.push(message);
      continue;
    }
    messages.push({
      id: event.id,
      role,
      content,
      timestamp: event.created_at || undefined,
      artifacts: artifacts.length > 0 ? artifacts : undefined,
      sender_name: role === 'assistant' ? 'Local Agent' : 'Hive',
    });
  }

  return messages;
}

type LocalArtifactDownloadContext = {
  agentId?: string | null;
  sessionId?: string | null;
};

export function localAgentArtifactDownloadUrl(path: string, context: LocalArtifactDownloadContext = {}): string {
  const params = new URLSearchParams({ path });
  const token = typeof localStorage !== 'undefined' ? localStorage.getItem('token') : '';
  if (token) params.set('token', token);
  if (context.agentId && context.sessionId) {
    return `/api/agents/${encodeURIComponent(context.agentId)}/local-agent/sessions/${encodeURIComponent(context.sessionId)}/workspace/download?${params.toString()}`;
  }
  return `/api/local-agents/workspace/download?${params.toString()}`;
}

function formatTime(value?: string | null): string {
  if (!value) return '';
  try {
    return new Date(value).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

function LocalArtifactCards({
  artifacts,
  downloadContext,
}: {
  artifacts?: ChatArtifactPart[];
  downloadContext?: LocalArtifactDownloadContext;
}) {
  const { t } = useTranslation();
  if (!artifacts?.length) return null;
  return (
    <div className="local-chat-artifacts">
      {artifacts.map((artifact) => (
        <a
          key={`${artifact.id || ''}:${artifact.path}`}
          href={localAgentArtifactDownloadUrl(artifact.path, downloadContext)}
          download={artifact.name}
          className="local-chat-artifact"
        >
          <IconFileText size={16} color="var(--text-tertiary)" className="local-chat-artifact-icon" />
          <span className="local-chat-artifact-body">
            <span className="local-chat-artifact-name">
              {artifact.name}
            </span>
            <span className="local-chat-artifact-meta">
              {[artifact.previewKind, artifact.size ? `${artifact.size} bytes` : null].filter(Boolean).join(' · ') || artifact.path}
            </span>
          </span>
          <span className="local-chat-artifact-action">
            {t('localAgents.download', 'Download')}
          </span>
        </a>
      ))}
    </div>
  );
}

function LocalChatMessageBubble({
  message,
  downloadContext,
}: {
  message: AgentChatMessage;
  downloadContext?: LocalArtifactDownloadContext;
}) {
  const isAssistant = message.role === 'assistant';
  return (
    <div className={`session-tui-message-row session-tui-message-row-${isAssistant ? 'assistant' : 'user'}`}>
      <div className="session-tui-message-bubble">
        {message.content && (
          <div>
            <MarkdownRenderer content={message.content} />
          </div>
        )}
        <LocalArtifactCards artifacts={message.artifacts} downloadContext={downloadContext} />
        {message.timestamp && (
          <div className={`local-chat-msg-time${isAssistant ? '' : ' is-user'}`}>
            {formatTime(message.timestamp)}
          </div>
        )}
      </div>
    </div>
  );
}

function localAttachmentFromUpload(upload: LocalAgentWorkspaceUpload): AttachedLocalFile {
  return {
    ...upload,
    attachmentPath: upload.workspace_path,
  };
}

function attachmentPayload(upload: AttachedLocalFile): Record<string, unknown> {
  return {
    type: 'artifact',
    name: upload.filename || upload.saved_filename,
    filename: upload.filename,
    saved_filename: upload.saved_filename,
    path: upload.attachmentPath,
    workspace_path: upload.workspace_path,
    size: upload.size,
    preview_text: upload.preview_text || upload.extracted_text,
    preview_kind: upload.workspace_path?.toLowerCase().endsWith('.md') ? 'markdown' : undefined,
    source: 'hive_local_agent_chat',
  };
}

function getLocalPermissionBadgeLabel(
  agentPermissions: AgentPermissions | null | undefined,
  t: (key: string, fallback: string) => string,
): string {
  const accessLevel = agentPermissions?.access_level;
  if (accessLevel === 'manage') return t('agent.chat.composer.manageAccess', 'Manage access');
  if (accessLevel === 'use') return t('agent.chat.composer.useAccess', 'Use access');
  if (accessLevel === 'read') return t('agent.chat.composer.readAccess', 'Read access');
  return t('agent.chat.composer.permissionUnknown', 'Access unknown');
}

function getLocalRuntimeBadgeLabel(agent: any, fallback: string): string {
  const candidates = [
    agent?.runtime_kind,
    agent?.runtime_type,
    agent?.local_runtime,
    agent?.client_kind,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
  }
  return fallback;
}

export default function LocalAgentChatSection({ agentId, agent, agentPermissions }: LocalAgentChatSectionProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textAreaRef = useRef<HTMLTextAreaElement | null>(null);
  const [liveEvents, setLiveEvents] = useState<LocalAgentChannelEvent[]>([]);
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<AttachedLocalFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [sending, setSending] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composerMenuOpen, setComposerMenuOpen] = useState(false);
  const [localPlanModeRequested, setLocalPlanModeRequested] = useState(false);
  const routeSessionId = routeSessionIdFromSearch(location.search);

  const routeSessionQuery = useQuery({
    queryKey: ['local-agent-detail-route-session', agentId, routeSessionId],
    queryFn: () => localBridgeApi.getAgentChannelSession(agentId, routeSessionId!),
    enabled: Boolean(agentId && routeSessionId),
    retry: false,
  });

  const defaultSessionQuery = useQuery({
    queryKey: ['local-agent-detail-default-session', agentId],
    queryFn: () => localBridgeApi.getAgentDefaultChannelSession(agentId),
    enabled: Boolean(agentId && !routeSessionId),
    retry: false,
  });

  const activeSession: LocalAgentChannelSession | null = (routeSessionQuery.data || defaultSessionQuery.data || null) as LocalAgentChannelSession | null;
  const channelSessionId = activeSession?.id || null;
  const displaySessionId = activeSession?.chat_session_id || activeSession?.id || null;

  const timelineQuery = useQuery({
    queryKey: ['local-agent-detail-channel-timeline', channelSessionId],
    queryFn: () => localBridgeApi.getChannelTimeline(channelSessionId!),
    enabled: Boolean(channelSessionId),
    refetchInterval: 5000,
  });

  const connectionsQuery = useQuery({
    queryKey: ['local-agent-detail-connections', agentId],
    queryFn: () => localBridgeApi.listAgentConnections(agentId),
    enabled: Boolean(agentId),
    refetchInterval: 10000,
  });

  useEffect(() => {
    if (!activeSession) return;
    const canonicalSessionId = activeSession.chat_session_id || activeSession.id;
    if (!canonicalSessionId || routeSessionId === canonicalSessionId) return;
    navigate(`/agents/${agentId}?session_id=${encodeURIComponent(canonicalSessionId)}#chat`, { replace: true });
  }, [activeSession, agentId, navigate, routeSessionId]);

  useEffect(() => {
    if (!channelSessionId) return;
    let cancelled = false;
    let socket: WebSocket | null = null;
    setWsConnected(false);
    setError(null);

    localBridgeApi.createBrowserChannelWsTicket(channelSessionId)
      .then((ticket) => {
        if (cancelled) return;
        socket = new WebSocket(browserChannelWsUrl(channelSessionId, ticket.ticket, window.location));
        socket.onopen = () => {
          if (!cancelled) setWsConnected(true);
        };
        socket.onclose = () => {
          if (!cancelled) setWsConnected(false);
        };
        socket.onerror = () => {
          if (!cancelled) {
            setWsConnected(false);
            setError(t('localAgents.chatWsError', 'Live channel disconnected. Messages still refresh from history.'));
          }
        };
        socket.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data?.type === 'event' && data.event?.id) {
              setLiveEvents((current) => mergeChannelEvents(current, [data.event]));
            }
          } catch {
            // Ignore malformed browser channel events; polling remains active.
          }
        };
      })
      .catch((exc: any) => {
        if (!cancelled) setError(exc?.message || t('localAgents.chatWsError', 'Live channel disconnected. Messages still refresh from history.'));
      });

    return () => {
      cancelled = true;
      socket?.close();
    };
  }, [channelSessionId, t]);

  useEffect(() => {
    setLiveEvents([]);
  }, [channelSessionId]);

  const channelEvents = useMemo(
    () => mergeChannelEvents(timelineQuery.data?.events || [], liveEvents),
    [liveEvents, timelineQuery.data?.events],
  );
  const messages = useMemo(() => localAgentChannelEventsToChatMessages(channelEvents), [channelEvents]);

  const headerModel: SessionWorkbenchHeaderModel = {
    sessionId: displaySessionId,
    title: activeSession?.title || agent?.name || t('localAgents.localAgentChat', 'Local Agent Chat'),
    status: sending ? 'running' : 'idle',
    modelLabel: t('nav.localBadge', 'Local'),
    providerLabel: 'Hive Connect',
    resumeHealth: localAgentRuntimeResumeHealth(
      connectionsQuery.data?.connections,
      connectionsQuery.isLoading || connectionsQuery.isError,
    ),
    permissionMode: null,
    governanceLabel: null,
    activeProjection: null,
    checkpointCount: 0,
    branchDepth: 0,
    compactionCount: 0,
    contextWindowStatusLabel: null,
    contextWindowTitle: null,
    activeRunStatus: sending ? 'sending' : null,
  };

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files ? Array.from(event.target.files) : [];
    event.target.value = '';
    if (files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const uploaded: AttachedLocalFile[] = [];
      for (const file of files) {
        uploaded.push(localAttachmentFromUpload(await localBridgeApi.uploadWorkspaceFile(file)));
      }
      setAttachments((current) => [...current, ...uploaded]);
    } catch (exc: any) {
      setError(exc?.message || t('localAgents.attachError', 'Failed to attach file.'));
    } finally {
      setUploading(false);
    }
  };

  const sendMessage = async () => {
    const rawContent = input.trim();
    if (!channelSessionId || sending || (!rawContent && attachments.length === 0)) return;
    const content = localPlanModeRequested && rawContent
      ? `${t('localAgents.planPrefix', 'Plan first: ')}${rawContent}`
      : rawContent;
    setSending(true);
    setError(null);
    try {
      await localBridgeApi.sendAgentChannelMessage(agentId, channelSessionId, {
        content: content || t('localAgents.fileOnlyMessage', 'Uploaded file.'),
        attachments: attachments.map(attachmentPayload),
        metadata: {
          source: 'local_agent_detail_chat',
          chat_session_id: activeSession?.chat_session_id || null,
        },
      });
      setInput('');
      setAttachments([]);
      setLocalPlanModeRequested(false);
      await timelineQuery.refetch();
    } catch (exc: any) {
      setError(exc?.message || t('localAgents.messageError', 'Failed to send local channel message.'));
    } finally {
      setSending(false);
    }
  };

  const loading = routeSessionQuery.isLoading || defaultSessionQuery.isLoading || timelineQuery.isLoading;
  const disabled = !channelSessionId || sending || uploading;
  const permissionBadgeLabel = getLocalPermissionBadgeLabel(agentPermissions, t);
  const composerIntentLabel = localPlanModeRequested ? t('agent.chat.composer.planModeActive', 'Plan Mode') : null;
  const runtimeBadgeLabel = getLocalRuntimeBadgeLabel(agent, 'Hive Connect');
  const composerPlaceholder =
    channelSessionId
      ? localPlanModeRequested
        ? t('agent.chat.composer.planPlaceholder', 'Describe what the agent should plan first...')
        : t('chat.placeholder', 'Type a message...')
      : t('common.loading', 'Loading');

  const setComposerAction = (action: LocalComposerActionKey) => {
    setComposerMenuOpen(false);
    if (action === 'upload') {
      fileInputRef.current?.click();
      return;
    }
    if (action === 'plan') {
      setLocalPlanModeRequested((current) => !current);
      setTimeout(() => textAreaRef.current?.focus(), 0);
      return;
    }
    setInput(composerShortcutText(action));
    setTimeout(() => textAreaRef.current?.focus(), 0);
  };

  return (
    <div
      data-testid="local-agent-chat-section"
      className="session-chat-workbench session-only local-chat-root"
    >
      <SessionWorkbenchHeader model={headerModel} />
      <div className="local-chat-history">
        {loading ? (
          <div className="local-chat-loading">
            {t('common.loading', 'Loading')}
          </div>
        ) : messages.length === 0 ? (
          <div className="local-chat-empty">
            <div className="local-chat-empty-title">
              {t('localAgents.noMessagesYet', 'No local conversation yet.')}
            </div>
            <div className="local-chat-empty-hint">
              {t('localAgents.startChatHint', 'Send a message after the Hive Connect background service is online.')}
            </div>
          </div>
        ) : (
          messages.map((message) => (
            <LocalChatMessageBubble
              key={message.id || `${message.role}:${message.timestamp}`}
              message={message}
              downloadContext={{ agentId: agent.id, sessionId: channelSessionId }}
            />
          ))
        )}
      </div>
      {error && (
        <div className="local-chat-error">
          {error}
        </div>
      )}
      {!wsConnected && !error && (
        <div className="local-chat-connecting">
          <span className="local-chat-connecting-dot" />
          {t('localAgents.connectingLiveChannel', 'Connecting live local channel...')}
        </div>
      )}
      <div
        data-testid="local-agent-session-composer"
        className="local-chat-composer"
      >
        {attachments.length > 0 && (
          <div
            data-testid="session-composer-attachments"
            className="local-chat-attachments"
          >
            {attachments.map((file, index) => (
              <div
                key={`${file.workspace_path}:${index}`}
                className="local-chat-attachment"
              >
                <IconFileText size={14} color="var(--text-tertiary)" />
                <span className="local-chat-attachment-name">{file.filename}</span>
                <button
                  type="button"
                  aria-label={t('agent.chat.removeAttachment', 'Remove attachment')}
                  title={t('agent.chat.removeAttachment', 'Remove attachment')}
                  onClick={() => setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                  className="local-chat-attachment-remove"
                >
                  <IconX size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
        <div
          data-testid="session-composer-shell"
          className="local-chat-shell"
        >
          <div
            data-testid="session-composer-plus-menu"
            hidden={!composerMenuOpen}
            className="local-chat-plus-menu"
          >
            {([
              {
                key: 'upload' as const,
                label: t('agent.chat.composer.uploadFile', 'Upload file'),
                description: t('agent.chat.composer.uploadFileDesc', 'Attach files or screenshots to this turn'),
                icon: uploading ? <IconLoader2 size={16} /> : <IconPaperclip size={16} />,
                disabled: disabled || attachments.length >= 10,
              },
              {
                key: 'plan' as const,
                label: t('agent.chat.composer.planMode', 'Plan Mode'),
                description: localPlanModeRequested
                  ? t('agent.chat.composer.planModeOnDesc', 'Next message will request a plan first')
                  : t('agent.chat.composer.planModeDesc', 'Ask the agent to plan before execution'),
                icon: <IconChecklist size={16} />,
                checked: localPlanModeRequested,
                disabled: !channelSessionId || sending,
              },
              {
                key: 'goal' as const,
                label: t('agent.chat.composer.goalMode', 'Goal mode'),
                description: t('agent.chat.composer.goalModeDesc', 'Start a session goal through the command surface'),
                icon: <IconTargetArrow size={16} />,
                disabled: !channelSessionId || sending,
              },
              {
                key: 'schedule' as const,
                label: t('agent.chat.composer.scheduledTask', 'Scheduled task'),
                description: t('agent.chat.composer.scheduledTaskDesc', 'Draft a scheduled task request for this agent'),
                icon: <IconCalendarTime size={16} />,
                disabled: !channelSessionId || sending,
              },
            ] satisfies Array<{
              key: LocalComposerActionKey;
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
                className={`local-chat-menu-item${action.checked === undefined ? '' : ' has-switch'}`}
              >
                <span className="local-chat-menu-item-icon">
                  {action.icon}
                </span>
                <span className="local-chat-menu-item-text">
                  <strong className="local-chat-menu-item-title">{action.label}</strong>
                  <span className="local-chat-menu-item-desc">{action.description}</span>
                </span>
                {action.checked !== undefined && (
                  <span
                    data-testid={`session-composer-action-${action.key}-switch`}
                    role="switch"
                    aria-checked={action.checked}
                    aria-label={action.label}
                    className="local-chat-switch"
                  >
                    <span className="local-chat-switch-knob" />
                  </span>
                )}
              </button>
            ))}
          </div>
          <input ref={fileInputRef} type="file" multiple onChange={handleFileChange} className="local-chat-file-input" />
          <textarea
            ref={textAreaRef}
            className="local-chat-textarea"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                void sendMessage();
              }
            }}
            placeholder={composerPlaceholder}
            disabled={disabled}
            rows={1}
          />
          <div className="local-chat-composer-bar">
            <button
              type="button"
              onClick={() => setComposerMenuOpen((open) => !open)}
              aria-label={t('agent.chat.composer.openMenu', 'Open composer actions')}
              aria-expanded={composerMenuOpen}
              title={t('agent.chat.composer.openMenu', 'Open composer actions')}
              disabled={!channelSessionId || sending}
              className="local-chat-plus-btn"
            >
              <IconPlus size={20} stroke={1.7} />
            </button>
            <span
              data-testid="session-composer-permission-badge"
              title={t('agent.chat.composer.permissionTitle', 'Backend access permission')}
              className="local-chat-perm-badge"
            >
              <IconShieldCheck size={15} stroke={1.8} />
              {permissionBadgeLabel}
            </span>
            {composerIntentLabel && (
              <span
                data-testid="session-composer-intent-badge"
                className="local-chat-intent-badge"
              >
                {composerIntentLabel}
              </span>
            )}
            {uploading && (
              <span className="local-chat-uploading">
                <IconLoader2 size={14} />
                {t('localAgents.attaching', 'Attaching...')}
              </span>
            )}
            <span className="local-chat-spacer" />
            <span
              data-testid="session-composer-model-badge"
              title={t('agent.chat.composer.modelTitle', 'Model information')}
              className="local-chat-model-badge"
            >
              <IconCircleDashedCheck size={17} stroke={1.9} color="var(--text-tertiary)" />
              <span className="local-chat-model-name">{runtimeBadgeLabel}</span>
            </span>
            <button
              className="btn btn-primary local-chat-send"
              type="button"
              data-testid="local-agent-send-button"
              disabled={disabled || (!input.trim() && attachments.length === 0)}
              onClick={() => void sendMessage()}
              aria-label={t('chat.send', 'Send')}
              title={t('chat.send', 'Send')}
            >
              {sending ? <IconLoader2 size={18} /> : <IconSend2 size={18} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

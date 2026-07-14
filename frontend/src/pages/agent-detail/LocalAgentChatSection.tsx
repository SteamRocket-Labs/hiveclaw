import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { IconFileText } from '@tabler/icons-react';

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
import { SessionComposer } from '../session-workbench/SessionComposer';
import type { ComposerActionKey } from '../session-workbench/SessionComposer';
import { SessionWorkbenchHeader } from '../session-workbench/SessionWorkbenchChrome';
import type { AgentChatMessage, ChatArtifactPart } from './chatRuntime';
import type { SessionWorkbenchHeaderModel } from '../session-workbench/timelineModel';
import { composerShortcutText } from './sessionComposerShortcuts';
import { saveBlob } from '../../utils/authenticatedResource';

type AttachedLocalFile = LocalAgentWorkspaceUpload & {
  attachmentPath: string;
};

type LocalAgentChatSectionProps = {
  agentId: string;
  agent: any;
  agentPermissions?: AgentPermissions | null;
};

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
  const [downloadError, setDownloadError] = useState<string | null>(null);
  if (!artifacts?.length) return null;
  const downloadArtifact = async (artifact: ChatArtifactPart) => {
    try {
      setDownloadError(null);
      const blob = downloadContext?.agentId && downloadContext.sessionId
        ? await localBridgeApi.downloadAgentSessionWorkspaceFile(
            downloadContext.agentId,
            downloadContext.sessionId,
            artifact.path,
          )
        : await localBridgeApi.downloadWorkspaceFile(artifact.path);
      saveBlob(blob, artifact.name);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="local-chat-artifacts">
      {artifacts.map((artifact) => (
        <button
          type="button"
          key={`${artifact.id || ''}:${artifact.path}`}
          className="local-chat-artifact"
          onClick={() => void downloadArtifact(artifact)}
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
        </button>
      ))}
      {downloadError ? <span role="alert" className="local-chat-artifact-error">{downloadError}</span> : null}
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
    content: upload.extracted_text || '',
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
  const channelCursorRef = useRef(0);
  const sendIdempotencyKeyRef = useRef<string | null>(null);
  const [liveEvents, setLiveEvents] = useState<LocalAgentChannelEvent[]>([]);
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<AttachedLocalFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [sending, setSending] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
          if (cancelled) return;
          setWsConnected(true);
          void localBridgeApi.getChannelTimeline(channelSessionId, channelCursorRef.current).then((delta) => {
            if (cancelled) return;
            channelCursorRef.current = Math.max(channelCursorRef.current, delta.next_cursor ?? 0);
            setLiveEvents((current) => mergeChannelEvents(current, delta.events));
          }).catch(() => undefined);
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
              channelCursorRef.current = Math.max(channelCursorRef.current, Number(data.event.sequence || 0));
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
    channelCursorRef.current = 0;
    sendIdempotencyKeyRef.current = null;
  }, [channelSessionId]);

  useEffect(() => {
    const eventCursor = Math.max(0, ...(timelineQuery.data?.events ?? []).map((event) => event.sequence));
    channelCursorRef.current = Math.max(
      channelCursorRef.current,
      timelineQuery.data?.next_cursor ?? 0,
      eventCursor,
    );
  }, [timelineQuery.data]);

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
      const idempotencyKey = sendIdempotencyKeyRef.current ?? `agent-detail:${crypto.randomUUID()}`;
      sendIdempotencyKeyRef.current = idempotencyKey;
      await localBridgeApi.sendAgentChannelMessage(agentId, channelSessionId, {
        content: content || t('localAgents.fileOnlyMessage', 'Uploaded file.'),
        attachments: attachments.map(attachmentPayload),
        metadata: {
          source: 'local_agent_detail_chat',
          chat_session_id: activeSession?.chat_session_id || null,
        },
        idempotencyKey,
      });
      sendIdempotencyKeyRef.current = null;
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

  const setComposerAction = (action: ComposerActionKey) => {
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
        <SessionComposer
          value={input}
          inputRef={textAreaRef}
          fileInputRef={fileInputRef}
          placeholder={composerPlaceholder}
          disabled={disabled}
          attachments={attachments.map((file) => ({ name: file.filename || file.saved_filename }))}
          permissionMode="default"
          permissionModeLabel={permissionBadgeLabel}
          permissionOptions={[{
            value: 'default',
            label: permissionBadgeLabel,
            description: t('agent.chat.composer.localPermissionDesc', 'Access is governed by the connected Local Agent policy.'),
          }]}
          modelLabel={runtimeBadgeLabel}
          modelTitle={t('agent.chat.composer.localModelTitle', 'Local Agent runtime')}
          intentLabel={composerIntentLabel}
          planModeRequested={localPlanModeRequested}
          goalModeRequested={false}
          goalModeAvailable={false}
          uploading={uploading}
          uploadProgress={uploading ? 101 : -1}
          running={false}
          onChange={setInput}
          onPaste={() => undefined}
          onSubmit={() => void sendMessage()}
          onStop={() => undefined}
          onAction={setComposerAction}
          onPermissionModeChange={() => undefined}
          onFilesSelected={handleFileChange}
          onRemoveAttachment={(index) => setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))}
          onCancelUpload={() => undefined}
        />
      </div>
    </div>
  );
}

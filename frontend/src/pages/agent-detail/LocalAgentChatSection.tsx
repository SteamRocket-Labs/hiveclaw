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

import MarkdownRenderer from '../../components/MarkdownRenderer';
import type { AgentPermissions } from '../../api/domains/agents';
import {
  localBridgeApi,
  type LocalAgentChannelEvent,
  type LocalAgentChannelSession,
  type LocalAgentWorkspaceUpload,
} from '../../api/domains/localBridge';
import { browserChannelWsUrl, mergeChannelEvents } from '../LocalAgents';
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
    <div style={{ display: 'grid', gap: '6px', marginTop: '8px' }}>
      {artifacts.map((artifact) => (
        <a
          key={`${artifact.id || ''}:${artifact.path}`}
          href={localAgentArtifactDownloadUrl(artifact.path, downloadContext)}
          download={artifact.name}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            border: '1px solid var(--border-subtle)',
            borderRadius: '8px',
            padding: '8px',
            background: 'var(--bg-elevated)',
            color: 'inherit',
            textDecoration: 'none',
          }}
        >
          <IconFileText size={16} color="var(--text-tertiary)" style={{ flexShrink: 0 }} />
          <span style={{ minWidth: 0, flex: 1 }}>
            <span
              style={{
                display: 'block',
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--text-primary)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {artifact.name}
            </span>
            <span
              style={{
                display: 'block',
                fontSize: '10px',
                color: 'var(--text-tertiary)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {[artifact.previewKind, artifact.size ? `${artifact.size} bytes` : null].filter(Boolean).join(' · ') || artifact.path}
            </span>
          </span>
          <span style={{ fontSize: '11px', color: 'var(--accent-primary)', flexShrink: 0 }}>
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
    <div
      style={{
        display: 'flex',
        justifyContent: isAssistant ? 'flex-start' : 'flex-end',
        padding: '8px 0',
      }}
    >
      <div
        style={{
          maxWidth: '76%',
          minWidth: 0,
          borderRadius: '12px',
          border: '1px solid var(--border-subtle)',
          background: isAssistant ? 'var(--bg-secondary)' : 'rgba(16,185,129,0.12)',
          padding: '10px 12px',
          color: 'var(--text-primary)',
          boxShadow: '0 1px 1px rgba(15, 23, 42, 0.03)',
        }}
      >
        {message.content && (
          <div style={{ fontSize: '14px', lineHeight: 1.65, wordBreak: 'break-word' }}>
            <MarkdownRenderer content={message.content} />
          </div>
        )}
        <LocalArtifactCards artifacts={message.artifacts} downloadContext={downloadContext} />
        {message.timestamp && (
          <div style={{ marginTop: '6px', fontSize: '10px', color: 'var(--text-tertiary)', textAlign: isAssistant ? 'left' : 'right' }}>
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
    resumeHealth: wsConnected ? 'online' : 'unknown',
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
      className="session-chat-workbench session-only"
      style={{
        display: 'flex',
        flexDirection: 'column',
        flex: 1,
        minHeight: 0,
        height: '100%',
        background: 'var(--bg-primary)',
        borderTop: '1px solid var(--border-subtle)',
      }}
    >
      <SessionWorkbenchHeader model={headerModel} />
      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
        {loading ? (
          <div style={{ padding: '48px 16px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '13px' }}>
            {t('common.loading', 'Loading')}
          </div>
        ) : messages.length === 0 ? (
          <div style={{ padding: '60px 20px', textAlign: 'center', color: 'var(--text-tertiary)' }}>
            <div style={{ fontSize: '13px', marginBottom: '4px' }}>
              {t('localAgents.noMessagesYet', 'No local conversation yet.')}
            </div>
            <div style={{ fontSize: '12px' }}>
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
        <div style={{ padding: '7px 16px', borderTop: '1px solid rgba(245,158,11,0.25)', background: 'rgba(245,158,11,0.08)', fontSize: '12px', color: 'rgb(180,100,0)' }}>
          {error}
        </div>
      )}
      {!wsConnected && !error && (
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
          {t('localAgents.connectingLiveChannel', 'Connecting live local channel...')}
        </div>
      )}
      <div
        data-testid="local-agent-session-composer"
        style={{
          background: 'var(--bg-primary)',
          padding: '14px 16px 16px',
        }}
      >
        {attachments.length > 0 && (
          <div
            data-testid="session-composer-attachments"
            style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', paddingBottom: '7px' }}
          >
            {attachments.map((file, index) => (
              <div
                key={`${file.workspace_path}:${index}`}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  fontSize: '11px',
                  background: 'var(--bg-secondary)',
                  padding: '4px 6px',
                  borderRadius: '6px',
                  border: '1px solid var(--border-subtle)',
                  maxWidth: '260px',
                }}
              >
                <IconFileText size={14} color="var(--text-tertiary)" />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.filename}</span>
                <button
                  type="button"
                  aria-label={t('agent.chat.removeAttachment', 'Remove attachment')}
                  title={t('agent.chat.removeAttachment', 'Remove attachment')}
                  onClick={() => setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                  style={{ border: 'none', background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', display: 'inline-flex', padding: 0 }}
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
          <input ref={fileInputRef} type="file" multiple onChange={handleFileChange} style={{ display: 'none' }} />
          <textarea
            ref={textAreaRef}
            className="chat-input"
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
              disabled={!channelSessionId || sending}
              style={{
                width: '32px',
                height: '32px',
                padding: 0,
                border: '1px solid transparent',
                borderRadius: '8px',
                background: composerMenuOpen ? 'var(--bg-secondary)' : 'transparent',
                color: channelSessionId && !sending ? 'var(--text-secondary)' : 'var(--text-tertiary)',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: channelSessionId && !sending ? 'pointer' : 'not-allowed',
                flexShrink: 0,
              }}
            >
              <IconPlus size={20} stroke={1.7} />
            </button>
            <span
              data-testid="session-composer-permission-badge"
              title={t('agent.chat.composer.permissionTitle', 'Backend access permission')}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                height: '30px',
                padding: '0 9px',
                borderRadius: '8px',
                color: 'rgb(194, 86, 0)',
                fontSize: '12px',
                fontWeight: 650,
                whiteSpace: 'nowrap',
              }}
            >
              <IconShieldCheck size={15} stroke={1.8} />
              {permissionBadgeLabel}
            </span>
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
            {uploading && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', color: 'var(--text-tertiary)', fontSize: '11px', whiteSpace: 'nowrap' }}>
                <IconLoader2 size={14} />
                {t('localAgents.attaching', 'Attaching...')}
              </span>
            )}
            <span style={{ flex: 1, minWidth: '12px' }} />
            <span
              data-testid="session-composer-model-badge"
              title={t('agent.chat.composer.modelTitle', 'Model information')}
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
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{runtimeBadgeLabel}</span>
            </span>
            <button
              className="btn btn-primary"
              type="button"
              data-testid="local-agent-send-button"
              disabled={disabled || (!input.trim() && attachments.length === 0)}
              onClick={() => void sendMessage()}
              aria-label={t('chat.send', 'Send')}
              title={t('chat.send', 'Send')}
              style={{
                width: '38px',
                height: '38px',
                padding: 0,
                borderRadius: '10px',
                minWidth: 'auto',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              {sending ? <IconLoader2 size={18} /> : <IconSend2 size={18} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

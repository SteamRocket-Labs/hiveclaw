import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router-dom';
import {
  localBridgeApi,
  type LocalAgentChannelEvent,
  type LocalAgentChannelSession,
  type LocalAgentChannelTimeline,
  type LocalAgentWorkspaceFile,
  type LocalAgentWorkspaceUpload,
  type LocalBridgeConnection,
  type LocalBridgeInstallGuide,
} from '../api/domains/localBridge';
import './LocalAgents.css';

const parseSeenAt = (value?: string | null) => {
  if (!value) return null;
  const ts = Date.parse(value);
  return Number.isFinite(ts) ? ts : null;
};

const formatSeenAt = (value?: string | null) => {
  const ts = parseSeenAt(value);
  return ts === null ? null : new Date(ts).toLocaleString();
};

export const connectionPresenceStatus = (connection: Pick<LocalBridgeConnection, 'status' | 'presence_status'>) => {
  if (connection.status !== 'active') return 'offline';
  if (connection.presence_status === 'online') return 'online';
  if (connection.presence_status === 'offline') return 'offline';
  return 'unknown';
};

export const isOnlineConnection = (connection: LocalBridgeConnection) => {
  if (connection.status !== 'active') return false;
  return connectionPresenceStatus(connection) === 'online';
};

export const canSendLocalAgentMessage = ({
  localAgentBound,
  messageBusy,
  content,
}: {
  localAgentBound: boolean;
  messageBusy: boolean;
  content: string;
}) => localAgentBound && !messageBusy && Boolean(content.trim());

const channelEventText = (event: LocalAgentChannelEvent) => {
  const payload = event.payload || {};
  return String(payload.text || payload.content || payload.output || payload.error || event.type);
};

export const mergeChannelEvents = (
  current: LocalAgentChannelEvent[] = [],
  incoming: LocalAgentChannelEvent[] = [],
) => {
  const byId = new Map<string, LocalAgentChannelEvent>();
  for (const event of current) {
    byId.set(event.id, event);
  }
  for (const event of incoming) {
    byId.set(event.id, event);
  }
  return Array.from(byId.values()).sort((left, right) => left.sequence - right.sequence);
};

export const browserChannelWsUrl = (
  sessionId: string,
  ticket: string,
  locationLike: Pick<Location, 'protocol' | 'host'> = window.location,
) => {
  const protocol = locationLike.protocol === 'https:' ? 'wss:' : 'ws:';
  const params = new URLSearchParams({ ticket });
  return `${protocol}//${locationLike.host}/ws/local-agents/sessions/${encodeURIComponent(sessionId)}?${params.toString()}`;
};

const formatBytes = (size: number) => {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
};

const parentWorkspacePath = (path: string) => {
  const parts = path.split('/').filter(Boolean);
  if (parts.length <= 1) return 'workspace';
  parts.pop();
  return parts.join('/') || 'workspace';
};

export const activationCodeFromSearch = (search: string) => {
  return new URLSearchParams(search).get('user_code')?.trim().toUpperCase() || '';
};

export const channelSessionIdFromSearch = (search: string) => {
  const params = new URLSearchParams(search);
  return params.get('session_id') || params.get('session') || '';
};

export const resolveActiveLocalChannelSessionId = ({
  explicitSessionId,
  routeChannelSession,
  defaultChannelSession,
}: {
  explicitSessionId?: string | null;
  routeChannelSession?: Pick<LocalAgentChannelSession, 'id'> | null;
  defaultChannelSession?: Pick<LocalAgentChannelSession, 'id'> | null;
}) => explicitSessionId || routeChannelSession?.id || defaultChannelSession?.id || null;

const initialActivationCodeFromUrl = () => {
  if (typeof window === 'undefined') return '';
  return activationCodeFromSearch(window.location.search);
};

export const DEFAULT_HIVE_CONNECT_INSTALL_GUIDE: LocalBridgeInstallGuide = {
  product_name: 'Hive Connect',
  skill_repo_url: 'https://github.com/rocky2431/hive-connect-skill',
  skill_name: 'hive-connect',
  npm_package: '@hiveclaw243/hive-connect',
  binary_name: 'hive-connect',
  install_skill_command: 'npx skills add https://github.com/rocky2431/hive-connect-skill --skill hive-connect',
  install_cli_command: 'npm install -g @hiveclaw243/hive-connect',
  login_command: 'hive-connect login',
  status_command: 'hive-connect status',
  run_command: 'hive-connect daemon install --config ~/.hive-connect/config.toml --force',
  user_prompt: '帮我安装 Hive Connect skill，并连接到 Hive。',
  instructions: [
    '帮我安装 Hive Connect skill，并连接到 Hive。',
    '',
    '请按下面流程自动完成：',
    '1. 执行 npx skills add https://github.com/rocky2431/hive-connect-skill --skill hive-connect 安装 Hive Connect skill。',
    '2. 按 skill 执行 npm install -g @hiveclaw243/hive-connect 安装本地 CLI。',
    '3. 执行 hive-connect login。',
    '4. 浏览器打开 Hive 后登录；Hive 会自动完成本地 Agent 认证，不需要复制任何一次性码。',
    '5. 执行 hive-connect daemon install --config ~/.hive-connect/config.toml --force，安装并启动后台常驻服务。',
    '6. 执行 hive-connect daemon status，确认后台服务正在运行。',
    '7. 可选：执行 hive-connect status，确认本机仍保留 Hive 登录绑定（这不代表在线）。',
    '8. 回到 Hive 页面查看本地 Agent 在线标记；如果离线，重新执行第 5-6 步，不要重复 login。',
  ],
};

export const buildSetupInstruction = (guide: LocalBridgeInstallGuide = DEFAULT_HIVE_CONNECT_INSTALL_GUIDE) => {
  const instructions = guide.instructions?.length
    ? guide.instructions
    : [
        guide.user_prompt,
        '',
        '请按下面流程自动完成：',
        `1. 执行 ${guide.install_skill_command} 安装 ${guide.product_name} skill。`,
        `2. 按 skill 执行 ${guide.install_cli_command} 安装本地 CLI。`,
        `3. 执行 ${guide.login_command}。`,
        '4. 浏览器打开 Hive 后登录；Hive 会自动完成本地 Agent 认证，不需要复制任何一次性码。',
        `5. 执行 ${guide.run_command}，安装并启动后台常驻服务。`,
        `6. 执行 ${guide.binary_name} daemon status，确认后台服务正在运行。`,
        `7. 可选：执行 ${guide.status_command}，确认本机仍保留 Hive 登录绑定（这不代表在线）。`,
        '8. 回到 Hive 页面查看本地 Agent 在线标记；如果离线，重新执行第 5-6 步，不要重复 login。',
      ];
  return instructions.join('\n');
};

interface LocalAgentsProps {
  agentId?: string;
  agentName?: string;
  embedded?: boolean;
  initialTab?: 'chat' | 'workspace';
}

export default function LocalAgents({ agentId, agentName, embedded = false, initialTab = 'chat' }: LocalAgentsProps = {}) {
  const { t } = useTranslation();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<'chat' | 'workspace'>(initialTab);
  const [copied, setCopied] = useState(false);
  const [activationCode] = useState(initialActivationCodeFromUrl);
  const [activationBusy, setActivationBusy] = useState(false);
  const [activationStatus, setActivationStatus] = useState<{ kind: 'success' | 'error'; message: string } | null>(null);
  const activationStartedRef = useRef(false);
  const channelCursorRef = useRef(0);
  const sendIdempotencyKeyRef = useRef<string | null>(null);
  const [channelSessionId, setChannelSessionId] = useState<string | null>(null);
  const [messageContent, setMessageContent] = useState(() =>
    t(
      'localAgents.defaultMessage',
      'Please prove you are the local agent on this computer: reply with the current working directory and confirm you are receiving messages through the Hive Connect background service.',
    ),
  );
  const [messageBusy, setMessageBusy] = useState(false);
  const [messageStatus, setMessageStatus] = useState<{ kind: 'success' | 'error'; message: string } | null>(null);
  const [pendingAttachments, setPendingAttachments] = useState<LocalAgentWorkspaceUpload[]>([]);
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const [workspacePath, setWorkspacePath] = useState('workspace');
  const [viewingWorkspaceFile, setViewingWorkspaceFile] = useState<string | null>(null);
  const [workspaceContent, setWorkspaceContent] = useState<{ path: string; content: string } | null>(null);
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [workspaceStatus, setWorkspaceStatus] = useState<{ kind: 'success' | 'error'; message: string } | null>(null);

  const {
    data: connectionData,
    isLoading: connectionsLoading,
    refetch: refetchConnections,
  } = useQuery({
    queryKey: agentId ? ['local-bridge-connections', agentId] : ['local-bridge-connections'],
    queryFn: () => (agentId ? localBridgeApi.listAgentConnections(agentId) : localBridgeApi.listConnections()),
    refetchInterval: 15000,
  });

  const { data: installGuide = DEFAULT_HIVE_CONNECT_INSTALL_GUIDE } = useQuery({
    queryKey: ['local-bridge-install-guide'],
    queryFn: localBridgeApi.getInstallGuide,
    staleTime: 5 * 60 * 1000,
  });

  const routeSessionId = agentId ? channelSessionIdFromSearch(location.search) : '';

  const { data: routeChannelSession } = useQuery({
    queryKey: ['local-agent-channel-route-session', agentId, routeSessionId],
    queryFn: () => localBridgeApi.getAgentChannelSession(agentId!, routeSessionId),
    enabled: Boolean(agentId && routeSessionId),
  });

  const { data: defaultChannelSession } = useQuery({
    queryKey: agentId ? ['local-agent-channel-default-session', agentId] : ['local-agent-channel-default-session'],
    queryFn: () =>
      agentId ? localBridgeApi.getAgentDefaultChannelSession(agentId) : localBridgeApi.getDefaultChannelSession(),
    enabled: !agentId || !routeSessionId,
  });

  const activeChannelSessionId = resolveActiveLocalChannelSessionId({
    explicitSessionId: channelSessionId,
    routeChannelSession,
    defaultChannelSession,
  });

  const {
    data: channelTimelineData,
    isLoading: channelEventsLoading,
    refetch: refetchChannelTimeline,
  } = useQuery({
    queryKey: ['local-agent-channel-timeline', activeChannelSessionId],
    queryFn: () => localBridgeApi.getChannelTimeline(activeChannelSessionId!),
    enabled: Boolean(activeChannelSessionId),
    refetchInterval: activeChannelSessionId ? 10000 : false,
  });

  const {
    data: workspaceFiles = [],
    isLoading: workspaceLoading,
    refetch: refetchWorkspaceFiles,
  } = useQuery({
    queryKey: ['local-agent-workspace-files', workspacePath],
    queryFn: () => localBridgeApi.listWorkspaceFiles(workspacePath),
    enabled: activeTab === 'workspace' && !viewingWorkspaceFile,
  });

  const activeConnections = (connectionData?.connections ?? []).filter((connection) => connection.status === 'active');
  const onlineConnections = activeConnections.filter(isOnlineConnection);
  const primaryConnection = onlineConnections[0] ?? activeConnections[0] ?? null;
  const localAgentOnline = onlineConnections.length > 0;
  const localAgentBound = activeConnections.length > 0;
  const primaryPresence = primaryConnection ? connectionPresenceStatus(primaryConnection) : 'unknown';
  const channelEvents = channelTimelineData?.events ?? [];
  const displayTitle = agentName || t('localAgents.title', 'Local Agent Channel');
  const canSendMessage = canSendLocalAgentMessage({
    localAgentBound,
    messageBusy,
    content: messageContent,
  });

  const setupInstruction = useMemo(() => buildSetupInstruction(installGuide), [installGuide]);

  const copyInstruction = async () => {
    await navigator.clipboard?.writeText(setupInstruction);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const approveActivationCode = useCallback(async (code: string) => {
    const normalizedCode = code.trim().toUpperCase();
    if (!normalizedCode) return;
    setActivationBusy(true);
    setActivationStatus(null);
    try {
      await localBridgeApi.approvePairing(normalizedCode);
      setActivationStatus({
        kind: 'success',
        message: t(
          'localAgents.autoAuthSuccess',
          'Hive Connect login approved. Return to the local agent and wait for it to show connected.',
        ),
      });
      await refetchConnections();
      if (typeof window !== 'undefined') {
        const url = new URL(window.location.href);
        url.searchParams.delete('user_code');
        window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
      }
    } catch (error: any) {
      setActivationStatus({
        kind: 'error',
        message: error?.message || t('localAgents.autoAuthError', 'Failed to finish Hive Connect login.'),
      });
    } finally {
      setActivationBusy(false);
    }
  }, [refetchConnections, t]);

  useEffect(() => {
    if (!activationCode || activationStartedRef.current) return;
    activationStartedRef.current = true;
    void approveActivationCode(activationCode);
  }, [activationCode, approveActivationCode]);

  useEffect(() => {
    if (defaultChannelSession?.id && !channelSessionId) {
      setChannelSessionId(defaultChannelSession.id);
    }
  }, [channelSessionId, defaultChannelSession?.id]);

  useEffect(() => {
    channelCursorRef.current = 0;
    sendIdempotencyKeyRef.current = null;
  }, [activeChannelSessionId]);

  useEffect(() => {
    const eventCursor = Math.max(0, ...(channelTimelineData?.events ?? []).map((event) => event.sequence));
    channelCursorRef.current = Math.max(
      channelCursorRef.current,
      channelTimelineData?.next_cursor ?? 0,
      eventCursor,
    );
  }, [channelTimelineData]);

  useEffect(() => {
    if (!activeChannelSessionId || typeof window === 'undefined') return;
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    const queryKey = ['local-agent-channel-timeline', activeChannelSessionId] as const;

    const connect = async () => {
      try {
        const ticket = await localBridgeApi.createBrowserChannelWsTicket(activeChannelSessionId);
        if (disposed) return;
        socket = new WebSocket(browserChannelWsUrl(activeChannelSessionId, ticket.ticket));
        socket.onopen = () => {
          void localBridgeApi.getChannelTimeline(activeChannelSessionId, channelCursorRef.current).then((delta) => {
            if (disposed) return;
            channelCursorRef.current = Math.max(channelCursorRef.current, delta.next_cursor ?? 0);
            queryClient.setQueryData<LocalAgentChannelTimeline | undefined>(queryKey, (current) => {
              if (!current) return delta;
              return {
                ...current,
                events: mergeChannelEvents(current.events, delta.events),
                next_cursor: Math.max(current.next_cursor ?? 0, delta.next_cursor ?? 0),
              };
            });
          }).catch(() => undefined);
        };
        socket.onclose = () => {
          if (!disposed) {
            reconnectTimer = window.setTimeout(() => void connect(), 2000);
          }
        };
        socket.onerror = () => {
          socket?.close();
        };
        socket.onmessage = (event) => {
          try {
            const payload = JSON.parse(event.data);
            if (payload?.type !== 'event' || !payload.event) return;
            const incomingEvent = payload.event as LocalAgentChannelEvent;
            channelCursorRef.current = Math.max(channelCursorRef.current, incomingEvent.sequence ?? 0);
            queryClient.setQueryData<LocalAgentChannelTimeline | undefined>(queryKey, (current) => {
              if (!current) return current;
              return {
                ...current,
                events: mergeChannelEvents(current.events, [incomingEvent]),
                next_cursor: Math.max(current.next_cursor ?? 0, incomingEvent.sequence ?? 0),
              };
            });
          } catch (_error) {
            // Ignore malformed transport messages; polling remains the fallback.
          }
        };
      } catch (_error) {
        // Polling remains the fallback when a browser websocket cannot be opened.
      }
    };

    void connect();
    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [activeChannelSessionId, queryClient]);

  const sendMessage = async () => {
    const content = messageContent.trim();
    if (!content) return;
    setMessageBusy(true);
    setMessageStatus(null);
    try {
      let sessionId = activeChannelSessionId;
      if (!sessionId) {
        const session = agentId
          ? await localBridgeApi.getAgentDefaultChannelSession(agentId)
          : await localBridgeApi.getDefaultChannelSession();
        sessionId = session.id;
        setChannelSessionId(session.id);
      }
      const messageInput = {
        content,
        attachments: pendingAttachments.map((attachment) => ({
          path: attachment.workspace_path,
          filename: attachment.saved_filename || attachment.filename,
          size: attachment.size,
          source: 'local_agent_workspace',
        })),
        metadata: {
          source: agentId ? 'local_agent_detail' : 'local_agents_page',
          purpose: 'direct_local_chat',
          attachment_count: pendingAttachments.length,
        },
        idempotencyKey: sendIdempotencyKeyRef.current ?? `browser:${crypto.randomUUID()}`,
      };
      sendIdempotencyKeyRef.current = messageInput.idempotencyKey;
      const result = agentId
        ? await localBridgeApi.sendAgentChannelMessage(agentId, sessionId, messageInput)
        : await localBridgeApi.sendChannelMessage(sessionId, messageInput);
      setPendingAttachments([]);
      sendIdempotencyKeyRef.current = null;
      setMessageStatus({
        kind: 'success',
        message: t('localAgents.messageQueued', 'Message queued: {{messageId}}', { messageId: result.id }),
      });
      await refetchConnections();
      await refetchChannelTimeline();
    } catch (error: any) {
      setMessageStatus({
        kind: 'error',
        message: error?.message || t('localAgents.messageError', 'Failed to send local channel message.'),
      });
    } finally {
      setMessageBusy(false);
    }
  };

  const attachChannelFiles = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.onchange = async () => {
      if (!input.files || input.files.length === 0) return;
      setAttachmentBusy(true);
      setMessageStatus(null);
      try {
        const uploaded: LocalAgentWorkspaceUpload[] = [];
        for (const file of Array.from(input.files)) {
          uploaded.push(await localBridgeApi.uploadWorkspaceFile(file));
        }
        setPendingAttachments((current) => [...current, ...uploaded]);
        setMessageStatus({
          kind: 'success',
          message: t('localAgents.attachSuccess', '{{count}} file(s) attached.', { count: uploaded.length }),
        });
      } catch (error: any) {
        setMessageStatus({
          kind: 'error',
          message: error?.message || t('localAgents.attachError', 'Failed to attach file.'),
        });
      } finally {
        setAttachmentBusy(false);
      }
    };
    input.click();
  };

  const openWorkspaceItem = async (item: LocalAgentWorkspaceFile) => {
    setWorkspaceStatus(null);
    if (item.is_dir) {
      setWorkspacePath(item.path);
      setViewingWorkspaceFile(null);
      setWorkspaceContent(null);
      return;
    }
    setWorkspaceBusy(true);
    try {
      const content = await localBridgeApi.readWorkspaceFile(item.path);
      setViewingWorkspaceFile(item.path);
      setWorkspaceContent(content);
    } catch (error: any) {
      setWorkspaceStatus({
        kind: 'error',
        message: error?.message || t('localAgents.workspaceReadError', 'Failed to read workspace file.'),
      });
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const downloadWorkspaceFile = async (path: string) => {
    setWorkspaceStatus(null);
    setWorkspaceBusy(true);
    try {
      const blob = await localBridgeApi.downloadWorkspaceFile(path);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = path.split('/').pop() || 'download';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (error: any) {
      setWorkspaceStatus({
        kind: 'error',
        message: error?.message || t('localAgents.workspaceDownloadError', 'Failed to download workspace file.'),
      });
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const uploadWorkspaceFiles = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.onchange = async () => {
      if (!input.files || input.files.length === 0) return;
      setWorkspaceBusy(true);
      setWorkspaceStatus(null);
      try {
        for (const file of Array.from(input.files)) {
          await localBridgeApi.uploadWorkspaceFile(file);
        }
        setWorkspacePath('workspace/uploads');
        setViewingWorkspaceFile(null);
        setWorkspaceContent(null);
        setWorkspaceStatus({
          kind: 'success',
          message: t('localAgents.workspaceUploadSuccess', 'Uploaded to Local Agent Workspace uploads.'),
        });
        await refetchWorkspaceFiles();
      } catch (error: any) {
        setWorkspaceStatus({
          kind: 'error',
          message: error?.message || t('localAgents.workspaceUploadError', 'Failed to upload workspace file.'),
        });
      } finally {
        setWorkspaceBusy(false);
      }
    };
    input.click();
  };

  return (
    <div className={embedded ? 'local-agents-root-embedded' : 'local-agents-root'}>
      <div className="local-agents-head">
        {embedded ? (
          <div className="local-agents-title-row">
            <h3 className="local-agents-title">{displayTitle}</h3>
            <span className="badge badge-success">
              {t('nav.localBadge', 'Local')}
            </span>
          </div>
        ) : (
          <h2 className="local-agents-title-h2">{displayTitle}</h2>
        )}
        <p className="local-agents-desc">
          {t(
            'localAgents.description',
            'Local agents behave like regular Hive agents with a local runtime. Keep them private or share them with your workspace through normal Agent permissions.',
          )}
        </p>
      </div>

      <div className="card local-agents-stack-card">
        <div className="local-agents-status-row">
          <div className="local-agents-status-main">
            <span className={`status-dot ${localAgentOnline ? 'running' : 'stopped'}`} />
            <div className="local-agents-min0">
              <div className="local-agents-status-title">
                {connectionsLoading
                  ? t('localAgents.statusLoading', 'Checking local agent...')
                  : localAgentOnline
                    ? t('localAgents.online', 'Local agent online')
                    : activeConnections.length > 0 && primaryPresence === 'unknown'
                      ? t('localAgents.unknown', 'Local agent status unknown')
                      : t('localAgents.offline', 'Local agent offline')}
              </div>
              <div className="local-agents-status-detail u-truncate">
                {primaryConnection
                  ? `${primaryConnection.device_name} · ${
                      primaryConnection.runtime_kind || primaryConnection.client_kind
                    } · ${
                      formatSeenAt(primaryConnection.presence_last_seen_at || primaryConnection.last_seen_at) ||
                      t('localAgents.neverSeen', 'No background service seen yet')
                    }`
                  : t('localAgents.noConnections', 'No linked local agent yet')}
              </div>
            </div>
          </div>
          <span className="local-agents-conn-count">
            {t('localAgents.connectionCount', '{{count}} linked', { count: activeConnections.length })}
          </span>
        </div>
      </div>

      <div className="local-agents-tabs">
        {(['chat', 'workspace'] as const).map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setActiveTab(tab)}
            className={`local-agents-tab${activeTab === tab ? ' active' : ''}`}
          >
            {tab === 'chat' ? t('localAgents.chatTab', 'Chat') : t('localAgents.workspaceTab', 'Workspace')}
          </button>
        ))}
      </div>

      {activeTab === 'chat' ? (
        <>
          <div className="card local-agents-stack-card">
            <div className="local-agents-section-label">
              {t('localAgents.setupInstruction', 'Setup instruction')}
            </div>
            <pre className="local-agents-pre">
              {setupInstruction}
            </pre>
            <button className="btn btn-secondary local-agents-copy-btn" onClick={copyInstruction}>
              {copied ? t('localAgents.copied', 'Copied') : t('localAgents.copyInstruction', 'Copy instruction')}
            </button>
          </div>

          <div className="card local-agents-stack-card">
            <div className="local-agents-section-label">
              {t('localAgents.autoAuthTitle', 'Automatic authentication')}
            </div>
            <div className="local-agents-hint">
              {activationCode
                ? t(
                    'localAgents.autoAuthDetected',
                    'Hive Connect login link detected. Hive is completing this local agent authentication automatically.',
                  )
                : t(
                    'localAgents.autoAuthHint',
                    'Run hive-connect login. The browser opens this page, you sign in, and Hive completes authentication automatically.',
                  )}
            </div>
            {activationBusy && (
              <div className="local-agents-busy-line">
                {t('localAgents.autoAuthCompleting', 'Completing Hive Connect login...')}
              </div>
            )}
            {activationStatus && (
              <div className={`local-agents-status-msg ${activationStatus.kind === 'success' ? 'local-agents-fb-success' : 'local-agents-fb-error'}`}>
                {activationStatus.message}
              </div>
            )}
            {activationCode && activationStatus?.kind === 'error' && (
              <button className="btn btn-secondary local-agents-retry-btn" disabled={activationBusy} onClick={() => approveActivationCode(activationCode)}>
                {t('localAgents.autoAuthRetry', 'Retry authentication')}
              </button>
            )}
          </div>

          <div className="card">
            <label className="local-agents-section-label">
              {t('localAgents.directChat', 'Direct local chat')}
            </label>
            <textarea
              className="input local-agents-message-input"
              disabled={messageBusy}
              value={messageContent}
              onChange={(event) => setMessageContent(event.target.value)}
              rows={4}
            />
            {pendingAttachments.length > 0 && (
              <div className="local-agents-attachments">
                {pendingAttachments.map((attachment) => (
                  <div
                    key={attachment.workspace_path}
                    className="local-agents-attachment"
                  >
                    <span className="local-agents-attachment-name">
                      {attachment.saved_filename || attachment.filename} · {formatBytes(attachment.size)}
                    </span>
                    <button
                      className="btn btn-secondary"
                      onClick={() => setPendingAttachments((items) => items.filter((item) => item.workspace_path !== attachment.workspace_path))}
                    >
                      {t('localAgents.removeAttachment', 'Remove')}
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="local-agents-actions">
              <button className="btn btn-secondary" disabled={attachmentBusy || messageBusy} onClick={attachChannelFiles}>
                {attachmentBusy ? t('localAgents.attaching', 'Attaching...') : t('localAgents.attachFile', 'Attach file')}
              </button>
              <button className="btn btn-primary" disabled={!canSendMessage} onClick={sendMessage}>
                {messageBusy ? t('localAgents.sending', 'Sending...') : t('localAgents.send', 'Send to local agent')}
              </button>
              <span className="local-agents-note">
                {localAgentOnline
                  ? t(
                      'localAgents.sendHint',
                      'When the background service is online, messages are delivered over WebSocket. If the computer sleeps, shuts down, or loses network, Hive shows the local agent as offline.',
                    )
                  : t(
                      'localAgents.offlineSendHint',
                      'The local agent is offline. Keep Hive Connect installed; it will reconnect automatically when the computer and background service are available.',
                    )}
              </span>
            </div>
            {messageStatus && (
              <div className={`local-agents-status-msg ${messageStatus.kind === 'success' ? 'local-agents-fb-success' : 'local-agents-fb-error'}`}>
                {messageStatus.message}
              </div>
            )}
            {activeChannelSessionId && (
              <div className="local-agents-transcript">
                <div className="local-agents-section-label">
                  {t('localAgents.transcript', 'Local channel transcript')}
                </div>
                {channelEventsLoading ? (
                  <div className="local-agents-note">
                    {t('localAgents.eventsLoading', 'Loading channel events...')}
                  </div>
                ) : channelEvents.length === 0 ? (
                  <div className="local-agents-note">
                    {t('localAgents.eventsEmpty', 'No channel events yet.')}
                  </div>
                ) : (
                  <div className="local-agents-events">
                    {channelEvents.slice(-10).map((event) => (
                      <div
                        key={event.id}
                        className="local-agents-event"
                      >
                        <strong>
                          {event.direction === 'local_to_hive' ? 'Local' : 'Hive'} · {event.type}:
                        </strong>{' '}
                        {channelEventText(event)}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="card">
          <div className="local-agents-ws-header">
            <div>
              <div className="local-agents-status-title">
                {viewingWorkspaceFile || workspacePath}
              </div>
              <div className="local-agents-ws-path">
                {t('localAgents.workspaceDescription', 'Workspace files for this local runtime. Shared callers can send files into the host runner through governed Agent permissions.')}
              </div>
            </div>
            <div className="local-agents-ws-actions">
              {viewingWorkspaceFile ? (
                <button
                  className="btn btn-secondary"
                  onClick={() => {
                    setViewingWorkspaceFile(null);
                    setWorkspaceContent(null);
                  }}
                >
                  {t('common.back', 'Back')}
                </button>
              ) : workspacePath !== 'workspace' ? (
                <button className="btn btn-secondary" onClick={() => setWorkspacePath(parentWorkspacePath(workspacePath))}>
                  {t('localAgents.workspaceUp', 'Up')}
                </button>
              ) : null}
              <button className="btn btn-secondary" disabled={workspaceBusy} onClick={() => refetchWorkspaceFiles()}>
                {t('common.refresh', 'Refresh')}
              </button>
              <button className="btn btn-primary" disabled={workspaceBusy} onClick={uploadWorkspaceFiles}>
                {t('localAgents.workspaceUpload', 'Upload')}
              </button>
            </div>
          </div>
          {workspaceStatus && (
            <div
              className={`local-agents-workspace-msg ${workspaceStatus.kind === 'success' ? 'local-agents-fb-success' : 'local-agents-fb-error'}`}
            >
              {workspaceStatus.message}
            </div>
          )}
          {viewingWorkspaceFile ? (
            <div>
              <div className="local-agents-ws-download-row">
                <button className="btn btn-secondary" disabled={workspaceBusy} onClick={() => downloadWorkspaceFile(viewingWorkspaceFile)}>
                  {t('common.download', 'Download')}
                </button>
              </div>
              <pre className="local-agents-file-view">
                {workspaceContent?.content || (workspaceBusy ? t('common.loading', 'Loading...') : '')}
              </pre>
            </div>
          ) : workspaceLoading ? (
            <div className="local-agents-ws-loading">
              {t('common.loading', 'Loading...')}
            </div>
          ) : workspaceFiles.length === 0 ? (
            <div className="local-agents-ws-empty">
              {t('localAgents.workspaceEmpty', 'No files in this local agent workspace yet.')}
            </div>
          ) : (
            <div className="local-agents-file-list">
              {workspaceFiles.map((item) => (
                <div
                  key={item.path}
                  className="local-agents-file-row"
                  onClick={() => openWorkspaceItem(item)}
                >
                  <div className="local-agents-min0">
                    <div className="local-agents-file-name">
                      {item.is_dir ? '/ ' : ''}{item.name}
                    </div>
                    <div className="local-agents-file-path">
                      {item.path}
                    </div>
                  </div>
                  <div className="local-agents-file-meta">
                    {!item.is_dir && (
                      <span className="local-agents-note">
                        {formatBytes(item.size)}
                      </span>
                    )}
                    {!item.is_dir && (
                      <button
                        className="btn btn-secondary"
                        disabled={workspaceBusy}
                        onClick={(event) => {
                          event.stopPropagation();
                          downloadWorkspaceFile(item.path);
                        }}
                      >
                        {t('common.download', 'Download')}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

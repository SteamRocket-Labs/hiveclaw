import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  localBridgeApi,
  type LocalAgentChannelEvent,
  type LocalAgentWorkspaceFile,
  type LocalAgentWorkspaceUpload,
  type LocalBridgeConnection,
} from '../api/domains/localBridge';

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

const channelEventText = (event: LocalAgentChannelEvent) => {
  const payload = event.payload || {};
  return String(payload.text || payload.content || payload.output || payload.error || event.type);
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

const initialActivationCodeFromUrl = () => {
  if (typeof window === 'undefined') return '';
  return activationCodeFromSearch(window.location.search);
};

export default function LocalAgents() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<'chat' | 'workspace'>('chat');
  const [copied, setCopied] = useState(false);
  const [activationCode] = useState(initialActivationCodeFromUrl);
  const [activationBusy, setActivationBusy] = useState(false);
  const [activationStatus, setActivationStatus] = useState<{ kind: 'success' | 'error'; message: string } | null>(null);
  const activationStartedRef = useRef(false);
  const [channelSessionId, setChannelSessionId] = useState<string | null>(null);
  const [messageContent, setMessageContent] = useState(() =>
    t(
      'localAgents.defaultMessage',
      'Please confirm that the local Hive Bridge runner received this message and report the local runtime name.',
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
    queryKey: ['local-bridge-connections'],
    queryFn: () => localBridgeApi.listConnections(),
    refetchInterval: 15000,
  });

  const {
    data: channelEventData,
    isLoading: channelEventsLoading,
    refetch: refetchChannelEvents,
  } = useQuery({
    queryKey: ['local-agent-channel-events', channelSessionId],
    queryFn: () => localBridgeApi.listChannelEvents(channelSessionId!),
    enabled: Boolean(channelSessionId),
    refetchInterval: channelSessionId ? 3000 : false,
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
  const primaryPresence = primaryConnection ? connectionPresenceStatus(primaryConnection) : 'unknown';
  const channelEvents = channelEventData?.events ?? [];

  const setupInstruction = useMemo(() => {
    return [
      '帮我安装 Hive Bridge skill，并连接到 Hive。',
      '',
      '请按下面流程自动完成：',
      '1. 执行 npx skills add https://github.com/rocky2431/hive-bridge-skill --skill hive-bridge 安装 Hive Bridge skill。',
      '2. 按 skill 执行 npm install -g @hiveclaw243/hive-bridge 安装本地 CLI。',
      '3. 执行 hive-bridge login。',
      '4. 浏览器打开 Hive 后登录；Hive 会自动完成本地 Agent 认证，不需要复制任何一次性码。',
      '5. 执行 hive-bridge status 验证连接。',
      '6. 执行 hive-bridge run --transport websocket，保持本地 Agent 在线。',
    ].join('\n');
  }, []);

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
          'Hive Bridge login approved. Return to the local agent and wait for it to show connected.',
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
        message: error?.message || t('localAgents.autoAuthError', 'Failed to finish Hive Bridge login.'),
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

  const sendMessage = async () => {
    const content = messageContent.trim();
    if (!content) return;
    setMessageBusy(true);
    setMessageStatus(null);
    try {
      let sessionId = channelSessionId;
      if (!sessionId) {
        const session = await localBridgeApi.createChannelSession({
          source: 'web',
          title: 'Local Agent Channel',
        });
        sessionId = session.id;
        setChannelSessionId(session.id);
      }
      const result = await localBridgeApi.sendChannelMessage(sessionId, {
        content,
        attachments: pendingAttachments.map((attachment) => ({
          path: attachment.workspace_path,
          filename: attachment.saved_filename || attachment.filename,
          size: attachment.size,
          source: 'local_agent_workspace',
        })),
        metadata: {
          source: 'local_agents_page',
          purpose: 'direct_local_chat',
          attachment_count: pendingAttachments.length,
        },
      });
      setPendingAttachments([]);
      setMessageStatus({
        kind: 'success',
        message: t('localAgents.messageQueued', 'Message queued: {{messageId}}', { messageId: result.id }),
      });
      await refetchConnections();
      await refetchChannelEvents();
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
    <div style={{ padding: '24px 36px', maxWidth: '1120px', margin: '0 auto' }}>
      <div style={{ marginBottom: '18px' }}>
        <h2 style={{ margin: '0 0 6px' }}>{t('localAgents.title', 'Local Agent Channel')}</h2>
        <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: '13px', lineHeight: 1.5 }}>
          {t(
            'localAgents.description',
            'Connect your local agents to Hive as a user-level IM channel. Your cloud agents can call this channel only through your owner identity.',
          )}
        </p>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
            <span
              className={localAgentOnline ? 'status-dot running' : undefined}
              style={{
                width: '8px',
                height: '8px',
                borderRadius: '50%',
                background: localAgentOnline ? 'var(--success)' : 'var(--text-quaternary)',
                flexShrink: 0,
              }}
            />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: '13px', fontWeight: 600 }}>
                {connectionsLoading
                  ? t('localAgents.statusLoading', 'Checking local agent...')
                  : localAgentOnline
                    ? t('localAgents.online', 'Local agent online')
                    : activeConnections.length > 0 && primaryPresence === 'unknown'
                      ? t('localAgents.unknown', 'Local agent status unknown')
                      : t('localAgents.offline', 'Local agent offline')}
              </div>
              <div
                style={{
                  fontSize: '12px',
                  color: 'var(--text-tertiary)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {primaryConnection
                  ? `${primaryConnection.device_name} · ${
                      primaryConnection.runtime_kind || primaryConnection.client_kind
                    } · ${
                      formatSeenAt(primaryConnection.presence_last_seen_at || primaryConnection.last_seen_at) ||
                      t('localAgents.neverSeen', 'No runner seen yet')
                    }`
                  : t('localAgents.noConnections', 'No linked local agent yet')}
              </div>
            </div>
          </div>
          <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
            {t('localAgents.connectionCount', '{{count}} linked', { count: activeConnections.length })}
          </span>
        </div>
      </div>

      <div
        style={{
          display: 'flex',
          gap: '18px',
          borderBottom: '1px solid var(--border-subtle)',
          margin: '6px 0 16px',
        }}
      >
        {(['chat', 'workspace'] as const).map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setActiveTab(tab)}
            style={{
              appearance: 'none',
              border: 0,
              background: 'transparent',
              padding: '10px 0',
              borderBottom: activeTab === tab ? '2px solid var(--text-primary)' : '2px solid transparent',
              color: activeTab === tab ? 'var(--text-primary)' : 'var(--text-tertiary)',
              fontSize: '14px',
              fontWeight: activeTab === tab ? 600 : 500,
              cursor: 'pointer',
            }}
          >
            {tab === 'chat' ? t('localAgents.chatTab', 'Chat') : t('localAgents.workspaceTab', 'Workspace')}
          </button>
        ))}
      </div>

      {activeTab === 'chat' ? (
        <>
          <div className="card" style={{ marginBottom: '12px' }}>
            <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>
              {t('localAgents.setupInstruction', 'Setup instruction')}
            </div>
            <pre
              style={{
                margin: 0,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontSize: '12px',
                lineHeight: 1.55,
                color: 'var(--text-secondary)',
                fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
              }}
            >
              {setupInstruction}
            </pre>
            <button className="btn btn-secondary" onClick={copyInstruction} style={{ marginTop: '12px' }}>
              {copied ? t('localAgents.copied', 'Copied') : t('localAgents.copyInstruction', 'Copy instruction')}
            </button>
          </div>

          <div className="card" style={{ marginBottom: '12px' }}>
            <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>
              {t('localAgents.autoAuthTitle', 'Automatic authentication')}
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.55 }}>
              {activationCode
                ? t(
                    'localAgents.autoAuthDetected',
                    'Hive Bridge login link detected. Hive is completing this local agent authentication automatically.',
                  )
                : t(
                    'localAgents.autoAuthHint',
                    'Run hive-bridge login. The browser opens this page, you sign in, and Hive completes authentication automatically.',
                  )}
            </div>
            {activationBusy && (
              <div style={{ marginTop: '8px', fontSize: '12px', color: 'var(--text-tertiary)' }}>
                {t('localAgents.autoAuthCompleting', 'Completing Hive Bridge login...')}
              </div>
            )}
            {activationStatus && (
              <div style={{ marginTop: '8px', fontSize: '12px', color: activationStatus.kind === 'success' ? 'var(--success)' : 'var(--error)' }}>
                {activationStatus.message}
              </div>
            )}
            {activationCode && activationStatus?.kind === 'error' && (
              <button className="btn btn-secondary" disabled={activationBusy} onClick={() => approveActivationCode(activationCode)} style={{ marginTop: '10px' }}>
                {t('localAgents.autoAuthRetry', 'Retry authentication')}
              </button>
            )}
          </div>

          <div className="card">
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>
              {t('localAgents.directChat', 'Direct local chat')}
            </label>
            <textarea
              className="input"
              disabled={messageBusy}
              value={messageContent}
              onChange={(event) => setMessageContent(event.target.value)}
              rows={4}
              style={{ width: '100%', resize: 'vertical', fontSize: '12px', lineHeight: 1.5 }}
            />
            {pendingAttachments.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '10px' }}>
                {pendingAttachments.map((attachment) => (
                  <div
                    key={attachment.workspace_path}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      gap: '10px',
                      alignItems: 'center',
                      padding: '8px 10px',
                      border: '1px solid var(--border-subtle)',
                      borderRadius: '8px',
                      fontSize: '12px',
                    }}
                  >
                    <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
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
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginTop: '10px' }}>
              <button className="btn btn-secondary" disabled={attachmentBusy || messageBusy} onClick={attachChannelFiles}>
                {attachmentBusy ? t('localAgents.attaching', 'Attaching...') : t('localAgents.attachFile', 'Attach file')}
              </button>
              <button className="btn btn-primary" disabled={messageBusy || !messageContent.trim()} onClick={sendMessage}>
                {messageBusy ? t('localAgents.sending', 'Sending...') : t('localAgents.send', 'Send to local agent')}
              </button>
              <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                {t(
                  'localAgents.sendHint',
                  'Online runners receive it over WebSocket; fallback poll remains available.',
                )}
              </span>
            </div>
            {messageStatus && (
              <div style={{ marginTop: '8px', fontSize: '12px', color: messageStatus.kind === 'success' ? 'var(--success)' : 'var(--error)' }}>
                {messageStatus.message}
              </div>
            )}
            {channelSessionId && (
              <div style={{ marginTop: '14px', borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
                <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>
                  {t('localAgents.transcript', 'Local channel transcript')}
                </div>
                {channelEventsLoading ? (
                  <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                    {t('localAgents.eventsLoading', 'Loading channel events...')}
                  </div>
                ) : channelEvents.length === 0 ? (
                  <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                    {t('localAgents.eventsEmpty', 'No channel events yet.')}
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {channelEvents.slice(-10).map((event) => (
                      <div
                        key={event.id}
                        style={{
                          fontSize: '12px',
                          color: 'var(--text-secondary)',
                          lineHeight: 1.5,
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                        }}
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
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'center', marginBottom: '12px' }}>
            <div>
              <div style={{ fontSize: '13px', fontWeight: 600 }}>
                {viewingWorkspaceFile || workspacePath}
              </div>
              <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                {t('localAgents.workspaceDescription', 'Shared user-level workspace for every local agent linked to this Hive account.')}
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
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
              style={{
                fontSize: '12px',
                color: workspaceStatus.kind === 'success' ? 'var(--success)' : 'var(--error)',
                marginBottom: '10px',
              }}
            >
              {workspaceStatus.message}
            </div>
          )}
          {viewingWorkspaceFile ? (
            <div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '10px' }}>
                <button className="btn btn-secondary" disabled={workspaceBusy} onClick={() => downloadWorkspaceFile(viewingWorkspaceFile)}>
                  {t('common.download', 'Download')}
                </button>
              </div>
              <pre
                style={{
                  margin: 0,
                  minHeight: '260px',
                  maxHeight: '560px',
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontSize: '12px',
                  lineHeight: 1.55,
                  color: 'var(--text-secondary)',
                  fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: '8px',
                  padding: '12px',
                  background: 'var(--bg-secondary)',
                }}
              >
                {workspaceContent?.content || (workspaceBusy ? t('common.loading', 'Loading...') : '')}
              </pre>
            </div>
          ) : workspaceLoading ? (
            <div style={{ padding: '24px 0', color: 'var(--text-tertiary)', textAlign: 'center', fontSize: '13px' }}>
              {t('common.loading', 'Loading...')}
            </div>
          ) : workspaceFiles.length === 0 ? (
            <div style={{ padding: '36px 0', color: 'var(--text-tertiary)', textAlign: 'center', fontSize: '13px' }}>
              {t('localAgents.workspaceEmpty', 'No files in this local agent workspace yet.')}
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {workspaceFiles.map((item) => (
                <div
                  key={item.path}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: '12px',
                    padding: '10px 12px',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: '8px',
                    cursor: 'pointer',
                  }}
                  onClick={() => openWorkspaceItem(item)}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '13px', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {item.is_dir ? '/ ' : ''}{item.name}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {item.path}
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
                    {!item.is_dir && (
                      <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
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

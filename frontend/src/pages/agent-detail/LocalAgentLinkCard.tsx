import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { localBridgeApi, type LocalBridgeWorkRequest } from '../../api/domains/localBridge';

interface LocalAgentLinkCardProps {
  agentId: string;
  canManage: boolean;
}

const ONLINE_WINDOW_MS = 2 * 60 * 1000;

const parseSeenAt = (value?: string | null) => {
  if (!value) return null;
  const ts = Date.parse(value);
  return Number.isFinite(ts) ? ts : null;
};

const formatDateTime = (value?: string | null) => {
  if (!value) return null;
  const ts = Date.parse(value);
  return Number.isFinite(ts) ? new Date(ts).toLocaleString() : null;
};

const attachmentLabel = (attachment: Record<string, unknown>) => {
  return String(attachment.path || attachment.filename || attachment.name || attachment.url || 'artifact');
};

const reportRuntime = (request: LocalBridgeWorkRequest) => {
  const report = request.metadata?.report;
  if (report && typeof report === 'object' && 'runtime' in report) {
    return String((report as Record<string, unknown>).runtime || '');
  }
  return String(request.metadata?.runtime || '');
};

export default function LocalAgentLinkCard({ agentId, canManage }: LocalAgentLinkCardProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [pairingCode, setPairingCode] = useState('');
  const [actionBusy, setActionBusy] = useState(false);
  const [actionStatus, setActionStatus] = useState<{ kind: 'success' | 'error'; message: string } | null>(null);
  const [workRequestContent, setWorkRequestContent] = useState(() =>
    t(
      'agent.settings.localBridge.defaultWorkRequest',
      'Please confirm that the local Hive Bridge runner received this cloud work request and report back with the local runtime name.',
    ),
  );
  const [workRequestBusy, setWorkRequestBusy] = useState(false);
  const [workRequestStatus, setWorkRequestStatus] = useState<{ kind: 'success' | 'error'; message: string } | null>(
    null,
  );
  const [lastQueuedWorkRequestId, setLastQueuedWorkRequestId] = useState<string | null>(null);
  const {
    data: connectionData,
    isLoading: connectionsLoading = false,
    refetch: refetchConnections,
  } = useQuery({
    queryKey: ['local-bridge-connections', agentId],
    queryFn: () => localBridgeApi.listConnections(agentId),
    enabled: canManage,
    refetchInterval: 15000,
  });
  const {
    data: workRequestData,
    isLoading: workRequestsLoading = false,
    refetch: refetchWorkRequests,
  } = useQuery({
    queryKey: ['local-bridge-work-requests', agentId],
    queryFn: () => localBridgeApi.listWorkRequests(agentId),
    enabled: canManage,
    refetchInterval: 5000,
  });
  const activeConnections = (connectionData?.connections ?? []).filter((connection) => connection.status === 'active');
  const onlineConnections = activeConnections.filter((connection) => {
    const seenAt = parseSeenAt(connection.last_seen_at);
    return seenAt !== null && Date.now() - seenAt <= ONLINE_WINDOW_MS;
  });
  const primaryConnection = onlineConnections[0] ?? activeConnections[0] ?? null;
  const primarySeenAt = parseSeenAt(primaryConnection?.last_seen_at);
  const lastSeenLabel =
    primarySeenAt === null
      ? t('agent.settings.localBridge.neverSeen', 'No heartbeat yet')
      : new Date(primarySeenAt).toLocaleString();
  const localAgentOnline = onlineConnections.length > 0;
  const workRequests = workRequestData?.work_requests ?? [];

  const setupInstruction = useMemo(() => {
    return [
      '帮我安装 Hive Bridge skill，并连接到 Hive。',
      '',
      '请按下面流程自动完成：',
      '1. 安装 hive-bridge 本地包。',
      '2. 配置 MCP：command = ["hive-bridge", "mcp"]。',
      '3. 执行 hive-bridge login。',
      '4. 在 Hive 页面选择当前 agent 并批准 Local Agent Link。',
      '5. 执行 hive-bridge status 验证连接。',
      '',
      `Hive agent id: ${agentId}`,
    ].join('\n');
  }, [agentId]);

  const copyInstruction = async () => {
    await navigator.clipboard?.writeText(setupInstruction);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const normalizedPairingCode = pairingCode.trim().toUpperCase();

  const approvePairing = async () => {
    if (!normalizedPairingCode) return;
    setActionBusy(true);
    setActionStatus(null);
    try {
      await localBridgeApi.approvePairing(agentId, normalizedPairingCode);
      setPairingCode('');
      setActionStatus({
        kind: 'success',
        message: t('agent.settings.localBridge.approveSuccess', 'Local link approved. The local agent can now finish login.'),
      });
      await refetchConnections?.();
    } catch (error: any) {
      setActionStatus({
        kind: 'error',
        message: error?.message || t('agent.settings.localBridge.approveError', 'Failed to approve local link.'),
      });
    } finally {
      setActionBusy(false);
    }
  };

  const rejectPairing = async () => {
    if (!normalizedPairingCode) return;
    setActionBusy(true);
    setActionStatus(null);
    try {
      await localBridgeApi.rejectPairing(agentId, normalizedPairingCode);
      setPairingCode('');
      setActionStatus({
        kind: 'success',
        message: t('agent.settings.localBridge.rejectSuccess', 'Local link request rejected.'),
      });
    } catch (error: any) {
      setActionStatus({
        kind: 'error',
        message: error?.message || t('agent.settings.localBridge.rejectError', 'Failed to reject local link.'),
      });
    } finally {
      setActionBusy(false);
    }
  };

  const sendWorkRequest = async () => {
    const content = workRequestContent.trim();
    if (!content) return;
    setWorkRequestBusy(true);
    setWorkRequestStatus(null);
    try {
      const result = await localBridgeApi.createWorkRequest(agentId, content, {
        source: 'local_agent_link_card',
        purpose: 'cloud_to_local_test',
      });
      setLastQueuedWorkRequestId(result.message_id);
      setWorkRequestStatus({
        kind: 'success',
        message: t('agent.settings.localBridge.workRequestSuccess', 'Work request queued: {{messageId}}', {
          messageId: result.message_id,
        }),
      });
      await refetchConnections?.();
      await refetchWorkRequests?.();
    } catch (error: any) {
      setWorkRequestStatus({
        kind: 'error',
        message: error?.message || t('agent.settings.localBridge.workRequestError', 'Failed to queue work request.'),
      });
    } finally {
      setWorkRequestBusy(false);
    }
  };

  const statusLabel = (status: string) => {
    if (status === 'completed') return t('agent.settings.localBridge.statusCompleted', 'Completed');
    if (status === 'delivered') return t('agent.settings.localBridge.statusDelivered', 'Delivered');
    if (status === 'pending') return t('agent.settings.localBridge.statusPending', 'Pending');
    return status;
  };

  const statusColor = (status: string) => {
    if (status === 'completed') return 'var(--success)';
    if (status === 'delivered') return 'var(--warning)';
    return 'var(--text-tertiary)';
  };

  return (
    <div className="card" style={{ marginBottom: '12px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'flex-start' }}>
        <div>
          <h4 style={{ margin: '0 0 6px' }}>{t('agent.settings.localBridge.title', 'Local Agent Link')}</h4>
          <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>
            {t(
              'agent.settings.localBridge.description',
              'Connect a local agent through Hive Bridge for outbound polling, MCP tools, one-file upload, and cloud work requests.',
            )}
          </p>
        </div>
        <span
          className="badge"
          style={{
            whiteSpace: 'nowrap',
            background: 'rgba(16,185,129,0.12)',
            color: 'rgb(16,185,129)',
            border: '1px solid rgba(16,185,129,0.25)',
          }}
        >
          {t('agent.settings.localBridge.p0', 'P0')}
        </span>
      </div>

      <div
        style={{
          marginTop: '12px',
          border: '1px solid var(--border-subtle)',
          borderRadius: '8px',
          background: 'var(--bg-elevated)',
          padding: '10px 12px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '12px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
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
            <div style={{ fontSize: '12px', fontWeight: 600 }}>
              {connectionsLoading
                ? t('agent.settings.localBridge.statusLoading', 'Checking local agent...')
                : localAgentOnline
                  ? t('agent.settings.localBridge.localAgentOnline', 'Local agent online')
                  : t('agent.settings.localBridge.localAgentOffline', 'Local agent offline')}
            </div>
            <div
              style={{
                fontSize: '11px',
                color: 'var(--text-tertiary)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {primaryConnection
                ? `${primaryConnection.device_name} · ${primaryConnection.client_kind} · ${lastSeenLabel}`
                : t('agent.settings.localBridge.noConnections', 'No linked local agent yet')}
            </div>
          </div>
        </div>
        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
          {t('agent.settings.localBridge.connectionCount', '{{count}} linked', {
            count: activeConnections.length,
          })}
        </span>
      </div>

      <div
        style={{
          marginTop: '12px',
          border: '1px solid var(--border-subtle)',
          borderRadius: '8px',
          background: 'var(--bg-elevated)',
          padding: '10px 12px',
        }}
      >
        <div style={{ fontSize: '12px', fontWeight: 600, marginBottom: '8px' }}>
          {t('agent.settings.localBridge.setupInstruction', 'Setup instruction')}
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
      </div>

      <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginTop: '12px', flexWrap: 'wrap' }}>
        <button className="btn btn-secondary" disabled={!canManage} onClick={copyInstruction}>
          {copied
            ? t('agent.settings.localBridge.copied', 'Copied')
            : t('agent.settings.localBridge.copyInstruction', 'Copy instruction')}
        </button>
        <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
          {canManage
            ? t('agent.settings.localBridge.approvalHint', 'Pending pairing requests are approved from this card.')
            : t('agent.settings.localBridge.manageRequired', 'Manage access is required to approve local links.')}
        </span>
      </div>

      <div
        style={{
          marginTop: '12px',
          borderTop: '1px solid var(--border-subtle)',
          paddingTop: '12px',
          display: 'flex',
          flexDirection: 'column',
          gap: '8px',
        }}
      >
        <label style={{ display: 'block', fontSize: '12px', fontWeight: 600 }}>
          {t('agent.settings.localBridge.pairingCode', 'Pairing code')}
        </label>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            className="input"
            disabled={!canManage || actionBusy}
            value={pairingCode}
            onChange={(event) => setPairingCode(event.target.value)}
            placeholder={t('agent.settings.localBridge.pairingCodePlaceholder', 'HIVE-XXXX-XXXX')}
            style={{ flex: '1 1 220px', minWidth: '180px', textTransform: 'uppercase' }}
          />
          <button
            className="btn btn-primary"
            disabled={!canManage || actionBusy || !normalizedPairingCode}
            onClick={approvePairing}
          >
            {actionBusy
              ? t('agent.settings.localBridge.approving', 'Approving...')
              : t('agent.settings.localBridge.approveLink', 'Approve link')}
          </button>
          <button
            className="btn btn-secondary"
            disabled={!canManage || actionBusy || !normalizedPairingCode}
            onClick={rejectPairing}
          >
            {t('agent.settings.localBridge.rejectLink', 'Reject')}
          </button>
        </div>
        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>
          {t(
            'agent.settings.localBridge.pairingCodeHint',
            'Run hive-bridge login locally, then paste the HIVE code here to bind that device to this agent.',
          )}
        </div>
        {actionStatus && (
          <div
            style={{
              fontSize: '12px',
              color: actionStatus.kind === 'success' ? 'var(--success)' : 'var(--error)',
            }}
          >
            {actionStatus.message}
          </div>
        )}
      </div>

      <div
        style={{
          marginTop: '12px',
          borderTop: '1px solid var(--border-subtle)',
          paddingTop: '12px',
          display: 'flex',
          flexDirection: 'column',
          gap: '8px',
        }}
      >
        <label style={{ display: 'block', fontSize: '12px', fontWeight: 600 }}>
          {t('agent.settings.localBridge.workRequestTitle', 'Cloud work request')}
        </label>
        <textarea
          className="input"
          disabled={!canManage || workRequestBusy}
          value={workRequestContent}
          onChange={(event) => setWorkRequestContent(event.target.value)}
          rows={3}
          style={{ width: '100%', resize: 'vertical', fontSize: '12px', lineHeight: 1.5 }}
        />
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
          <button
            className="btn btn-secondary"
            disabled={!canManage || workRequestBusy || !workRequestContent.trim()}
            onClick={sendWorkRequest}
          >
            {workRequestBusy
              ? t('agent.settings.localBridge.sendingWorkRequest', 'Sending...')
              : t('agent.settings.localBridge.sendWorkRequest', 'Send work request')}
          </button>
          <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
            {t(
              'agent.settings.localBridge.workRequestHint',
              'Queues one pending task for the linked local agent runner to poll and report.',
            )}
          </span>
        </div>
        {workRequestStatus && (
          <div
            style={{
              fontSize: '12px',
              color: workRequestStatus.kind === 'success' ? 'var(--success)' : 'var(--error)',
            }}
          >
            {workRequestStatus.message}
          </div>
        )}
      </div>

      <div
        style={{
          marginTop: '12px',
          borderTop: '1px solid var(--border-subtle)',
          paddingTop: '12px',
          display: 'flex',
          flexDirection: 'column',
          gap: '10px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
          <label style={{ display: 'block', fontSize: '12px', fontWeight: 600 }}>
            {t('agent.settings.localBridge.workbenchTitle', 'Local Agent Workbench')}
          </label>
          <button
            className="btn btn-secondary"
            disabled={!canManage || workRequestsLoading}
            onClick={() => refetchWorkRequests?.()}
            style={{ padding: '5px 10px', fontSize: '11px' }}
          >
            {t('agent.settings.localBridge.workbenchRefresh', 'Refresh')}
          </button>
        </div>

        {workRequestsLoading ? (
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
            {t('agent.settings.localBridge.workbenchLoading', 'Loading local work requests...')}
          </div>
        ) : workRequests.length === 0 ? (
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
            {t('agent.settings.localBridge.workbenchEmpty', 'No local work requests yet.')}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {workRequests.slice(0, 5).map((request) => {
              const createdAt = formatDateTime(request.created_at);
              const completedAt = formatDateTime(request.completed_at);
              const runtime = reportRuntime(request);
              const isLastQueued = lastQueuedWorkRequestId === request.id;
              return (
                <div
                  key={request.id}
                  style={{
                    border: isLastQueued ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                    borderRadius: '8px',
                    background: 'var(--bg-elevated)',
                    padding: '10px 12px',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      gap: '8px',
                      marginBottom: '6px',
                    }}
                  >
                    <div
                      style={{
                        fontSize: '12px',
                        fontWeight: 600,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {t('agent.settings.localBridge.workbenchRequest', 'Request')} · {request.id.slice(0, 8)}
                    </div>
                    <span style={{ fontSize: '11px', color: statusColor(request.status), whiteSpace: 'nowrap' }}>
                      {statusLabel(request.status)}
                    </span>
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                    {request.content}
                  </div>
                  <div style={{ marginTop: '6px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    {createdAt
                      ? `${t('agent.settings.localBridge.workbenchCreatedAt', 'Created')} ${createdAt}`
                      : request.conversation_id}
                    {completedAt ? ` · ${completedAt}` : ''}
                    {runtime
                      ? ` · ${t('agent.settings.localBridge.workbenchRuntime', 'Runtime')}: ${runtime}`
                      : ''}
                  </div>
                  {request.result && (
                    <div
                      style={{
                        marginTop: '8px',
                        padding: '8px',
                        borderRadius: '6px',
                        background: 'var(--bg-primary)',
                        fontSize: '12px',
                        lineHeight: 1.5,
                        color: 'var(--text-secondary)',
                        whiteSpace: 'pre-wrap',
                      }}
                    >
                      <strong>{t('agent.settings.localBridge.workbenchResult', 'Result')}:</strong> {request.result}
                    </div>
                  )}
                  {request.attachments.length > 0 && (
                    <div style={{ marginTop: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>
                      <strong>{t('agent.settings.localBridge.workbenchArtifacts', 'Artifacts')}:</strong>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', marginTop: '4px' }}>
                        {request.attachments.map((attachment, index) => (
                          <code
                            key={`${request.id}-attachment-${index}`}
                            style={{
                              fontSize: '11px',
                              whiteSpace: 'pre-wrap',
                              wordBreak: 'break-word',
                              color: 'var(--text-tertiary)',
                            }}
                          >
                            {attachmentLabel(attachment)}
                          </code>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { localBridgeApi } from '../api/domains/localBridge';
import { useAuthStore } from '../stores';

function tenantFor(fallback: string) {
  try { return localStorage.getItem('current_tenant_id') || fallback; } catch { return fallback; }
}

export default function LocalAgentDeviceControls({ agentId }: { agentId?: string }) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1);
    };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  const tenant = tenantFor(user?.tenant_id || '');
  if (!user || !tenant) return null;
  return <Devices key={`${user.id}:${tenant}:${agentId || ''}:${generation}`}
    agentId={agentId} userId={user.id} tenantId={tenant} />;
}

function Devices({ agentId, userId, tenantId }: { agentId?: string; userId: string; tenantId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const query = useQuery({ queryKey: ['local-device-controls', userId, tenantId, agentId], enabled: open, retry: false,
    queryFn: async () => {
      const value = await (agentId ? localBridgeApi.listAgentConnections(agentId) : localBridgeApi.listConnections());
      if (value.connections.some((connection) => connection.tenant_id !== tenantId || (agentId
        ? connection.agent_id !== agentId : connection.user_id !== userId))) throw new Error('Device scope changed');
      return value;
    } });
  async function refresh() {
    const result = await query.refetch();
    if (!result.isError) setUnknown(false);
  }
  async function revoke(id: string) {
    const current = useAuthStore.getState().user;
    if (!current || current.id !== userId || tenantFor(current.tenant_id || '') !== tenantId) { setUnknown(true); return; }
    setBusy(true);
    try {
      const scope = { expected_user_id: userId, expected_tenant_id: tenantId };
      const result = await (agentId ? localBridgeApi.revokeAgentConnection(agentId, id, scope) : localBridgeApi.revokeConnection(id, scope));
      if (result.status !== 'revoked' || result.connection_id !== id
        || result.work_control?.connection_id !== id || result.work_control.schema !== 'hive.local_connection_control.v1') {
        throw new Error('Device receipt changed');
      }
      await client.invalidateQueries({ queryKey: ['local-agent-requests', userId, tenantId] });
      await refresh();
    } catch { setUnknown(true); } finally { setBusy(false); }
  }
  return <details className="card" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('localDevices.title', 'Device access')}</summary>
    {open && <>
      <p>{t('localDevices.scope', 'Revocation blocks this device from new Hive requests and gateway access. Work already running on it may continue. Inspect or stop that work on the original device.')}</p>
      {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
      {(query.isError || unknown) && <p role="alert">{t('localDevices.unknown', 'Could not confirm device access or revocation. Refresh before acting again. Device controls require management access.')}</p>}
      {!query.isError && query.data?.connections.map((connection) => <div key={connection.id}>
        <p>{connection.device_name} · {connection.client_kind} · {connection.status}</p>
        {connection.work_control && <>
          <p>{t('localDevices.counts', 'Cancelled before dispatch: {{cancelled}}. Execution needs review: {{unknown}}.', {
            cancelled: connection.work_control.request_counts.cancelled_before_dispatch,
            unknown: connection.work_control.request_counts.needs_reconciliation,
          })}</p>
          <p role="status">{t('localDevices.unconfirmed', 'Device access revoked. Host execution remains unknown; a device stop has not been confirmed.')}</p>
        </>}
        {(connection.status === 'active' || (connection.status === 'revoked' && !connection.work_control)) && <button type="button"
          className="btn btn-secondary" disabled={busy || unknown} onClick={() => { void revoke(connection.id); }}>
          {connection.status === 'revoked' ? t('localDevices.reconcile', 'Reconcile queued work') : t('localDevices.revoke', 'Revoke this device access')}
        </button>}
      </div>)}
      <button type="button" className="btn btn-secondary" disabled={busy || query.isFetching}
        onClick={() => { void refresh(); }}>{t('common.refresh', 'Refresh')}</button>
    </>}
  </details>;
}

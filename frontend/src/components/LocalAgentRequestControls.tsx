import { useEffect, useRef, useState } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { localBridgeApi, type LocalAgentRequestPage } from '../api/domains/localBridge';
import { useAuthStore } from '../stores';

function currentTenant(fallback: string) {
  try { return localStorage.getItem('current_tenant_id') || fallback; } catch { return fallback; }
}

export default function LocalAgentRequestControls({ sessionId }: { sessionId: string | null }) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1);
    };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  const tenantId = currentTenant(user?.tenant_id || '');
  if (!user || !tenantId || !sessionId) return null;
  return <RequestList key={`${user.id}:${tenantId}:${sessionId}:${generation}`}
    sessionId={sessionId} actorId={user.id} tenantId={tenantId} />;
}

function RequestList({ sessionId, actorId, tenantId }: { sessionId: string; actorId: string; tenantId: string }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<string | null>(null);
  const [unknown, setUnknown] = useState(false);
  const keys = useRef(new Map<string, string>());
  function checkScope(page: LocalAgentRequestPage) {
    if (page.scope.actor_user_id !== actorId || page.scope.tenant_id !== tenantId || page.scope.session_id !== sessionId) {
      throw new Error('Local request scope changed');
    }
    return page;
  }
  const query = useInfiniteQuery({ queryKey: ['local-agent-requests', actorId, tenantId, sessionId],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => checkScope(await localBridgeApi.listChannelRequests(sessionId, pageParam)),
    getNextPageParam: (page) => page.next_cursor || undefined,
    refetchInterval: 5000, retry: false });
  async function refresh() {
    const result = await query.refetch();
    if (!result.isError) setUnknown(false);
  }
  async function control(request: LocalAgentRequestPage['requests'][number], action: 'cancel' | 'abandon' | 'retry_source') {
    const current = useAuthStore.getState().user;
    if (!current || current.id !== actorId || currentTenant(current.tenant_id || '') !== tenantId) {
      setUnknown(true);
      return;
    }
    const commandId = `${action}:${request.id}`;
    const key = keys.current.get(commandId) || crypto.randomUUID();
    keys.current.set(commandId, key);
    setBusy(request.id);
    try {
      const command = { expected_user_id: actorId, expected_tenant_id: tenantId,
        request_hash: request.request_hash, idempotency_key: key };
      const receipt = await (action === 'cancel' ? localBridgeApi.cancelChannelRequest(sessionId, request.id, command)
        : action === 'abandon' ? localBridgeApi.abandonChannelRequest(sessionId, request.id, command)
          : localBridgeApi.retryChannelSourceReturn(sessionId, request.id, { expected_user_id: actorId,
            expected_tenant_id: tenantId, request_hash: request.request_hash }));
      if (receipt.schema !== (action === 'cancel' ? 'hive.local_message_control.v1' : action === 'abandon' ? 'hive.local_work_disposition.v1' : 'hive.local_source_return.v1')
        || (action !== 'abandon' && receipt.actor_user_id !== actorId)
        || receipt.session_id !== sessionId || receipt.message_id !== request.id) throw new Error('Local control scope changed');
      await refresh();
    } catch { setUnknown(true); } finally { setBusy(null); }
  }
  function stateLabel(request: LocalAgentRequestPage['requests'][number]) {
    if (request.tracking_state === 'abandoned') return request.next_action === 'review_late_result'
      ? t('localRequests.abandonedLate', 'Tracking ended. A late result is available here for review; execution was not repeated.')
      : t('localRequests.abandoned', 'Tracking ended. External execution remains unknown.');
    if (request.next_action === 'review_late_result') return t('localRequests.late', 'A late result arrived. Review it in this work; the earlier control remains unconfirmed.');
    if (request.status === 'cancelled' && request.execution_state === 'not_dispatched') return t('localRequests.cancelled', 'Cancelled before dispatch');
    if (request.status === 'needs_reconciliation') return t('localRequests.unknown', 'Further dispatch withheld. Execution is unknown; inspect or stop this request on the original device.');
    if (request.status === 'rejected') return t('localRequests.rejected', 'Action was not released');
    if (request.status === 'waiting_approval') return t('localRequests.approval', 'Waiting for approval');
    if (request.status === 'pending') return t('localRequests.queued', 'Queued');
    if (request.status === 'delivered') return t('localRequests.delivered', 'Delivered to the original device');
    if (request.status === 'completed') return t('localRequests.completed', 'Device reported completion');
    if (request.status === 'failed') return t('localRequests.failed', 'Device reported failure');
    return request.status;
  }
  return <details className="card">
    <summary>{t('localRequests.title', 'Local requests and cancellation')}</summary>
    <p>{t('localRequests.scope', 'Cancel individual requests here. A request already delivered to a device may still be running; Hive cannot confirm a device stop.')}</p>
    {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
    {(query.isError || unknown) && <p role="alert">{t('localRequests.unavailable', 'The request state or control outcome could not be confirmed. Refresh before acting again.')}</p>}
    {!query.isError && query.data?.pages.flatMap((page) => page.requests).map((request) => <div key={request.id}>
      <p>{request.preview}</p><p role="status">{stateLabel(request)}</p>
      {request.status === 'waiting_approval' && request.target_agent_id && request.approval_id
        && <a href={`/agents/${encodeURIComponent(request.target_agent_id)}#approvals`}>
          {t('localRequests.reviewApproval', 'Review this employee’s approvals')}</a>}
      {request.decision_reason && <p>{request.decision_reason}</p>}
      {request.source_delivery?.status === 'needs_reconciliation' && <p role="status">{t('localRequests.sourceHeld', 'The local result is retained. Return to the original work needs reconciliation. Review its access and binding, then retry the return.')}</p>}
      {request.source_delivery?.status === 'queued' && <p>{t('localRequests.sourceRecorded', 'The original-work return is recorded. Execution is not repeated.')}</p>}
      {request.can_retry_source_return && <button type="button" className="btn btn-secondary" disabled={!!busy || unknown}
        onClick={() => { void control(request, 'retry_source'); }}>{t('localRequests.retrySource', 'Retry original-work return')}</button>}
      {request.can_cancel && <button type="button" className="btn btn-secondary" disabled={!!busy || unknown}
        onClick={() => { void control(request, 'cancel'); }}>{t('localRequests.cancel', 'Cancel this request')}</button>}
      {request.can_abandon && <>
        <p>{t('localRequests.abandonHint', 'End tracking without stopping or repeating device execution. Any late result received remains here for review.')}</p>
        <button type="button" className="btn btn-secondary" disabled={!!busy || unknown}
          onClick={() => { void control(request, 'abandon'); }}>{t('localRequests.abandon', 'End tracking this request')}</button>
      </>}
    </div>)}
    <button type="button" className="btn btn-secondary" disabled={!!busy || query.isFetching}
      onClick={() => { void refresh(); }}>{t('common.refresh', 'Refresh')}</button>
    {query.hasNextPage && <button type="button" className="btn btn-secondary" disabled={query.isFetching}
      onClick={() => { void query.fetchNextPage(); }}>{t('localRequests.older', 'Load older requests')}</button>}
  </details>;
}

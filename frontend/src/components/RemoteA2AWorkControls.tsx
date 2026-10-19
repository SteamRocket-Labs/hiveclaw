import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../api/core';
import { remoteA2AApi, type RemoteA2ATask, type RemoteA2ATaskInput, type RemoteA2ATaskOperation } from '../api/domains/remoteA2A';
import { useAuthStore } from '../stores';

function company(fallback: string) {
  try { return localStorage.getItem('current_tenant_id') || fallback; } catch { return fallback; }
}

export default function RemoteA2AWorkControls({ agentId, sessionId }: { agentId: string; sessionId: string }) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1);
    };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  const tenantId = company(user?.tenant_id || '');
  if (!user || !tenantId) return null;
  return <Work key={`${agentId}:${sessionId}:${user.id}:${tenantId}:${generation}`}
    agentId={agentId} sessionId={sessionId} actorId={user.id} tenantId={tenantId} />;
}

type Pending = { taskId?: string; body: RemoteA2ATaskInput | RemoteA2ATaskOperation };
function Work({ agentId, sessionId, actorId, tenantId }: { agentId: string; sessionId: string; actorId: string; tenantId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [effects, setEffects] = useState(false);
  const [cost, setCost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<RemoteA2ATask | null>(null);
  const [files, setFiles] = useState<{ taskId: string; available: number; unavailable: number; conflicts: number } | null>(null);
  const pending = useRef<Pending | null>(null);
  const current = useRef(true);
  const matches = () => current.current && useAuthStore.getState().user?.id === actorId
    && company(useAuthStore.getState().user?.tenant_id || '') === tenantId;
  useEffect(() => { current.current = true;return () => { current.current = false;pending.current = null; }; }, []);
  const query = useQuery({ queryKey: ['remote-a2a-work', actorId, tenantId, agentId, sessionId],
    enabled: open, retry: false, refetchOnWindowFocus: false,
    queryFn: async () => {
      const result = await remoteA2AApi.tasks(agentId, sessionId);
      if (!matches() || result.agent_id !== agentId || result.source_session_id !== sessionId) throw new Error('Remote work scope changed');
      return result;
    } });
  const frame = () => ({ expected_user_id: actorId, expected_tenant_id: tenantId,
    expected_version: query.data?.connection.version ?? null });
  async function read(taskId: string) {
    try {
      const task = await remoteA2AApi.task(agentId, taskId);
      if (matches() && task.agent_id === agentId && task.source_session_id === sessionId) setSelected(task);
    } catch { if (matches()) setError(true); }
  }
  async function refresh() {
    const result = await query.refetch();
    if (!matches() || result.isError) return;
    const request = pending.current;
    const nonce = request && ('request_id' in request.body ? request.body.request_id : request.body.operation_id);
    const accepted = result.data?.items.find((task) => task.request_id === nonce || task.latest_operation_id === nonce);
    if (accepted) { pending.current = null;setUnknown(false);setText('');await read(accepted.id); }
    else if (selected) await read(selected.id);
  }
  async function recoverFiles() {
    if (!selected || !matches()) return;
    const taskId = selected.id;
    setBusy(true);setError(false);
    try {
      const result = await remoteA2AApi.recoverArtifacts(agentId, taskId, {
        expected_user_id: actorId, expected_tenant_id: tenantId });
      if (!matches()) return;
      if (result.agent_id !== agentId || result.task_id !== taskId || result.source_session_id !== sessionId) throw new Error('Remote file scope changed');
      setFiles({ taskId, available: result.parts.length,
        unavailable: result.file_returns.filter((file) => file.status === 'unavailable').length,
        conflicts: result.file_returns.filter((file) => file.status === 'preserved_conflict').length });
    } catch { if (matches()) setError(true); }
    finally { if (matches()) setBusy(false); }
  }
  async function submit(request: Pending) {
    if (!matches()) return;
    pending.current = request;setBusy(true);setError(false);
    try {
      const task = request.taskId
        ? await remoteA2AApi.operateTask(agentId, request.taskId, request.body as RemoteA2ATaskOperation)
        : await remoteA2AApi.startTask(agentId, request.body as RemoteA2ATaskInput);
      if (!matches()) return;
      if (task.agent_id !== agentId || task.source_session_id !== sessionId) throw new Error('Remote work scope changed');
      pending.current = null;setUnknown(false);setText('');setSelected(task);setEffects(false);setCost(false);
      await query.refetch();
    } catch (failure) {
      if (!matches()) return;
      // These statuses are trusted pre-effect request denials. Transport/5xx
      // failures keep the exact body and nonce until saved state reconciles it.
      if (failure instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(failure.status)) {
        pending.current = null;setUnknown(false);setError(true);
      } else setUnknown(true);
    } finally { if (matches()) setBusy(false); }
  }
  function start(event: FormEvent) {
    event.preventDefault();
    if (!text.trim() || !effects || !cost || !query.data?.connection.ready || pending.current) return;
    void submit({ body: { ...frame(), request_id: crypto.randomUUID(), source_session_id: sessionId,
      uncontrolled_effects_acknowledged: true, unknown_cost_acknowledged: true,
      params: { message: { messageId: crypto.randomUUID(), role: 'ROLE_USER', parts: [{ text }] } } } });
  }
  function operate(action: RemoteA2ATaskOperation['action']) {
    if (!selected || pending.current) return;
    const params = action === 'send_message' ? { message: { messageId: crypto.randomUUID(), role: 'ROLE_USER',
      taskId: selected.remote_task_id, contextId: selected.remote_context_id, parts: [{ text }] } } : {};
    void submit({ taskId: selected.id, body: { ...frame(), operation_id: crypto.randomUUID(), action, params } });
  }
  function status(task: RemoteA2ATask) {
    if (task.status === 'suspended' && task.remote_state === 'TASK_STATE_INPUT_REQUIRED' && !task.error_code) return t('remoteWork.inputRequired', 'Remote reply needed');
    if (task.status === 'suspended' && task.remote_state === 'TASK_STATE_AUTH_REQUIRED' && !task.error_code) return t('remoteWork.authRequired', 'Remote authorization required');
    const labels: Record<string, string> = {
      pending: t('remoteWork.queued', 'Queued'), running: t('remoteWork.contacting', 'Contacting remote endpoint'),
      suspended: t('remoteWork.waiting', 'Remote task waiting'), completed: t('remoteWork.completed', 'Remote reports completion'),
      failed: t('remoteWork.reportedFailure', 'Remote reports failure'), killed: t('remoteWork.cancelConfirmed', 'Remote cancellation confirmed'),
      skipped: t('remoteWork.abandoned', 'Locally abandoned'), needs_reconciliation: t('remoteWork.unconfirmed', 'Remote state unconfirmed'),
    };
    return labels[task.status] || labels.needs_reconciliation;
  }
  return <details className="card-pad-sm" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('remoteWork.title', 'Remote A2A requests in this work')}</summary>
    <p>{t('remoteWork.boundary', 'Send an explicit request through this employee’s configured remote endpoint. Its internal actions cannot be intercepted here; external charges remain unknown. The employee’s selected native model is unchanged.')}</p>
    {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
    {(query.isError || error) && <p role="alert">{t('remoteWork.failed', 'The request was denied or saved state is unavailable. Check access, connection and work identity.')}</p>}
    {unknown && <p role="alert">{t('remoteWork.unknown', 'The request could not be confirmed. Read saved state or retry the exact request; it will not create a new dispatch.')}</p>}
    <button className="btn-secondary" type="button" disabled={busy} onClick={() => { void refresh(); }}>{t('remoteWork.read', 'Read saved state')}</button>
    {unknown && <button className="btn-secondary" type="button" disabled={busy || !pending.current}
      onClick={() => { if (pending.current) void submit(pending.current); }}>{t('remoteWork.retry', 'Retry exact request')}</button>}
    {query.data && <>
      {!query.data.connection.ready && <p>{t('remoteWork.unavailable', 'A current, discovered connection is required for new remote requests. Existing receipts remain readable.')}</p>}
      <form onSubmit={start}>
        <fieldset disabled={busy || !!pending.current}>
          <label>{t('remoteWork.message', 'Remote request or reply')}<textarea value={text} onChange={(event) => setText(event.target.value)} /></label>
          <label><input type="checkbox" checked={effects} onChange={(event) => setEffects(event.target.checked)} />{t('remoteWork.effects', 'I authorize this exact remote request with unverified internal effect controls.')}</label>
          <label><input type="checkbox" checked={cost} onChange={(event) => setCost(event.target.checked)} />{t('remoteWork.cost', 'I accept unknown external charges for this request; no hard spend limit is claimed.')}</label>
          <button className="btn-primary" type="submit" disabled={!query.data.connection.ready || !text.trim() || !effects || !cost}>
            {t('remoteWork.start', 'Send new remote request')}
          </button>
        </fieldset>
      </form>
      <ul>{query.data.items.map((task) => <li key={task.id}>
        <button type="button" className="btn-secondary" disabled={busy} onClick={() => { void read(task.id); }}>{task.id}</button>
        {' · '}{status(task)}
      </li>)}</ul>
    </>}
    {selected && <section aria-label={t('remoteWork.receipt', 'Saved remote receipt')}>
      <p>{status(selected)}</p>
      <p>{t('remoteWork.costUnknown', 'External cost: unknown. Internal action control: unverified.')}</p>
      {selected.error_code && <p role="alert"><code>{selected.error_code}</code></p>}
      <div>
        <button type="button" className="btn-secondary" disabled={busy} onClick={() => { void recoverFiles(); }}>{t('remoteWork.recoverFiles', 'Recover files from saved replies')}</button>
        {selected.recovery_actions.includes('refresh') && <button type="button" className="btn-secondary" disabled={busy || unknown} onClick={() => operate('refresh')}>{t('remoteWork.refresh', 'Check remote task')}</button>}
        {selected.recovery_actions.includes('send_message') && <button type="button" className="btn-primary" disabled={busy || unknown || !text.trim()} onClick={() => operate('send_message')}>{t('remoteWork.reply', 'Reply to this remote task')}</button>}
        {selected.recovery_actions.includes('cancel') && <button type="button" className="btn-secondary" disabled={busy || unknown} onClick={() => operate('cancel')}>{t('remoteWork.cancel', 'Request remote cancellation')}</button>}
        {selected.recovery_actions.includes('abandon') && <button type="button" className="btn-secondary" disabled={busy || unknown} onClick={() => operate('abandon')}>{t('remoteWork.abandon', 'Abandon locally; remote effects remain unconfirmed')}</button>}
      </div>
      {files?.taskId === selected.id && <p role="status">{t('remoteWork.filesRecovered', 'Files returned to this work: {{count}}. Separate copies preserving existing files: {{conflicts}}. Unavailable parts: {{unavailable}}.',
        { count: files.available, conflicts: files.conflicts, unavailable: files.unavailable })}</p>}
      {selected.observation && <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(selected.observation, null, 2)}</pre>}
    </section>}
  </details>;
}

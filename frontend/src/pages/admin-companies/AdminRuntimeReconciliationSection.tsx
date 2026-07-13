import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  adminApi,
  type RuntimeReconciliationAction,
  type RuntimeReconciliationFrameDecision,
  type RuntimeNotificationDeliveryReconciliation,
  type RuntimeReconciliationOperation,
  type RuntimeReconciliationTask,
  type RuntimeRecoveryEvidence,
} from '../../api/domains/admin';
import './AdminRuntimeReconciliationSection.css';

type Props = {
  initialTenantId?: string;
  initialTasks?: RuntimeReconciliationTask[];
  initialDeliveries?: RuntimeNotificationDeliveryReconciliation[];
};

function formatDate(value?: string | null) {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function taskLabel(task: RuntimeReconciliationTask) {
  return task.child_agent_name || task.child_agent_id || task.parent_agent_id || task.task_id;
}

function compact(value: unknown, length = 12) {
  const text = String(value || '');
  return text ? text.slice(0, length) : '-';
}

function resumableOperation(task: RuntimeReconciliationTask): RuntimeReconciliationOperation | null {
  const operation = task.reconciliation_operation;
  return operation?.status === 'prepared' || operation?.status === 'failed' ? operation : null;
}

export function buildFrameDecisions(
  evidence: RuntimeRecoveryEvidence,
  action: RuntimeReconciliationAction,
): RuntimeReconciliationFrameDecision[] {
  return evidence.frames
    .map((frame) => ({
      runtime_task_id: frame.runtime_task_id,
      tool_call_id: frame.tool_call_id,
      tool_name: frame.tool_name,
      decision: action,
    }))
    .sort((left, right) => (
      `${left.runtime_task_id}\u0000${left.tool_call_id}\u0000${left.tool_name}`
        .localeCompare(`${right.runtime_task_id}\u0000${right.tool_call_id}\u0000${right.tool_name}`)
    ));
}

export function visibleOperationRoots(
  tasks: RuntimeReconciliationTask[],
): RuntimeReconciliationTask[] {
  return tasks.filter((task) => {
    const rootTaskId = task.reconciliation_operation?.group_root_task_id;
    return !rootTaskId || rootTaskId === task.task_id;
  });
}

export default function AdminRuntimeReconciliationSection({
  initialTenantId = '',
  initialTasks = [],
  initialDeliveries = [],
}: Props) {
  const { t } = useTranslation();
  const [tenantId, setTenantId] = useState(() => {
    if (initialTenantId) return initialTenantId;
    if (typeof localStorage === 'undefined') return '';
    return localStorage.getItem('current_tenant_id') || '';
  });
  const [tasks, setTasks] = useState<RuntimeReconciliationTask[]>(() => (
    visibleOperationRoots(initialTasks)
  ));
  const [deliveries, setDeliveries] = useState<RuntimeNotificationDeliveryReconciliation[]>(initialDeliveries);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});

  const load = async () => {
    if (!tenantId.trim()) return;
    setLoading(true);
    setError(null);
    setWarning(null);
    try {
      const [loadedTasks, loadedDeliveries, loadedWorkflowDeliveries] = await Promise.all([
        adminApi.listRuntimeReconciliation({ tenantId: tenantId.trim(), limit: 50 }),
        adminApi.listRuntimeNotificationDeliveries({ tenantId: tenantId.trim(), limit: 50 }),
        adminApi.listWorkflowCompletionDeliveries({ tenantId: tenantId.trim(), limit: 50 }),
      ]);
      setTasks(visibleOperationRoots(loadedTasks));
      setDeliveries([...loadedDeliveries, ...loadedWorkflowDeliveries]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const retryDelivery = async (delivery: RuntimeNotificationDeliveryReconciliation) => {
    const key = `delivery:${delivery.delivery_id}`;
    const reason = (notes[key] || '').trim();
    if (!tenantId.trim() || !delivery.retryable || reason.length < 8 || !confirmed[key]) return;
    setLoading(true);
    setError(null);
    setWarning(null);
    try {
      const retry = delivery.source_kind === 'workflow_completion'
        ? adminApi.retryWorkflowCompletionDelivery
        : adminApi.retryRuntimeNotificationDelivery;
      const retried = await retry(delivery.delivery_id, { tenantId: tenantId.trim(), reason, confirmed: true });
      setDeliveries((current) => (
        retried.status === 'dead_letter'
          ? current.map((item) => (item.delivery_id === retried.delivery_id ? retried : item))
          : current.filter((item) => item.delivery_id !== retried.delivery_id)
      ));
      setNotes((current) => ({ ...current, [key]: '' }));
      setConfirmed((current) => ({ ...current, [key]: false }));
      try {
        const [loaded, loadedWorkflow] = await Promise.all([
          adminApi.listRuntimeNotificationDeliveries({ tenantId: tenantId.trim(), limit: 50 }),
          adminApi.listWorkflowCompletionDeliveries({ tenantId: tenantId.trim(), limit: 50 }),
        ]);
        setDeliveries([...loaded, ...loadedWorkflow]);
      } catch (refreshError) {
        const detail = refreshError instanceof Error ? refreshError.message : String(refreshError);
        setWarning(`${t(
          'admin.reconciliation.deliveryRefreshWarning',
          'Delivery was retried, but refreshing the queue failed',
        )}: ${detail}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const applyAction = async (
    task: RuntimeReconciliationTask,
    requestedAction?: RuntimeReconciliationAction,
  ) => {
    const evidence = task.recovery_evidence;
    const operation = resumableOperation(task);
    const action = operation?.action || requestedAction;
    if (!tenantId.trim() || !action || !evidence?.evidence_complete) return;
    if (!confirmed[task.task_id]) return;
    if (operation && operation.evidence_digest !== evidence.digest) return;
    const reason = operation?.reason || (notes[task.task_id] || '').trim();
    if (reason.length < 8) return;
    const decisions = operation?.frame_decisions || buildFrameDecisions(evidence, action);
    setLoading(true);
    setError(null);
    try {
      await adminApi.applyRuntimeReconciliationAction(task.task_id, {
        tenantId: tenantId.trim(),
        action,
        reason,
        confirmed: true,
        evidenceDigest: operation?.evidence_digest || evidence.digest,
        frameDecisions: decisions,
        ...(operation ? { operationId: operation.operation_id } : {}),
      });
      const loaded = await adminApi.listRuntimeReconciliation({ tenantId: tenantId.trim(), limit: 50 });
      setTasks(visibleOperationRoots(loaded));
      setNotes((current) => ({ ...current, [task.task_id]: '' }));
      setConfirmed((current) => ({ ...current, [task.task_id]: false }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="card card-pad-none admin-reconcile-card">
      <div className="admin-reconcile-header">
        <div>
          <div className="admin-reconcile-title">
            {t('admin.reconciliation.title', 'Runtime Reconciliation')}
          </div>
          <div className="admin-reconcile-count">
            {tasks.length + deliveries.length} {t('admin.reconciliation.openItems', 'open items')}
          </div>
        </div>
        <div className="admin-reconcile-search">
          <input
            className="admin-reconcile-input"
            value={tenantId}
            onChange={(event) => setTenantId(event.target.value)}
            placeholder={t('admin.reconciliation.tenantPlaceholder', 'Tenant ID')}
          />
          <button className="btn-secondary" onClick={load} disabled={loading || !tenantId.trim()}>
            {loading ? t('common.loading', 'Loading...') : t('admin.reconciliation.refresh', 'Refresh')}
          </button>
        </div>
      </div>
      {error && <div className="admin-reconcile-error">{error}</div>}
      {warning && <div className="admin-reconcile-warning">{warning}</div>}
      {tasks.length === 0 ? (
        <div className="admin-reconcile-empty">
          {t('admin.reconciliation.empty', 'No runtime tasks need reconciliation.')}
        </div>
      ) : (
        <div className="admin-reconcile-scroll">
          {tasks.map((task) => {
            const evidence = task.recovery_evidence;
            const targets = evidence?.targets || [];
            const frames = evidence?.frames || [];
            const operation = resumableOperation(task);
            const evidenceComplete = Boolean(evidence?.evidence_complete);
            const operationEvidenceMatches = !operation || operation.evidence_digest === evidence?.digest;
            const reason = operation?.reason || notes[task.task_id] || '';
            const actionReady = evidenceComplete
              && operationEvidenceMatches
              && reason.trim().length >= 8
              && Boolean(confirmed[task.task_id]);
            return (
              <div key={task.task_id} className="admin-reconcile-row">
                <div>
                  <div className="admin-reconcile-label">{taskLabel(task)}</div>
                  <div className="admin-reconcile-sub-mono">
                    {task.task_type || '-'} · {task.task_id.slice(0, 8)}
                  </div>
                </div>
                <div>
                  <div className="admin-reconcile-reason">{task.reason || task.result_summary || '-'}</div>
                  <div className="admin-reconcile-sub">
                    {task.side_effect_risk || '-'} · {formatDate(task.created_at)}
                  </div>
                  <div className="admin-reconcile-evidence">
                    <div className="admin-reconcile-evidence-title">
                      {t('admin.reconciliation.targets', 'Recovery targets')}: {targets.length}
                    </div>
                    {targets.map((target) => (
                      <div
                        className="admin-reconcile-evidence-line"
                        key={`${target.runtime_task_id}-${target.agent_id}-${target.session_id}`}
                      >
                        <div>
                          {target.source} · run {target.runtime_task_id} · agent {target.agent_id} · session {target.session_id}
                        </div>
                        <div>
                          state {target.expected_manifest_state || '-'} · ref {target.expected_manifest_ref || '-'}
                        </div>
                        <div>
                          sha256 {target.expected_sha256 || '-'} · checkpoint{' '}
                          {String(target.expected_checkpoint_seq ?? '-')}
                        </div>
                        <div>
                          claim version {String(target.expected_claim_version ?? '-')} · claim worker{' '}
                          {target.expected_claim_worker_id || '-'}
                        </div>
                      </div>
                    ))}
                    <div className="admin-reconcile-evidence-title">
                      {t('admin.reconciliation.frames', 'Unknown-side-effect frames')}: {frames.length}
                    </div>
                    {frames.map((frame) => (
                      <div
                        className="admin-reconcile-evidence-line"
                        key={`${frame.runtime_task_id}-${frame.tool_call_id}-${frame.tool_name}`}
                      >
                        {frame.source} · {compact(frame.runtime_task_id, 8)} · {frame.tool_name} · {frame.tool_call_id}
                      </div>
                    ))}
                    <div className="admin-reconcile-evidence-line">
                      digest {compact(evidence?.digest, 16)}
                    </div>
                    {!evidenceComplete && (
                      <div className="admin-reconcile-evidence-warning">
                        {t('admin.reconciliation.evidenceIncomplete', 'Evidence incomplete')}: {' '}
                        {(evidence?.incomplete_reasons || ['canonical_evidence_missing']).join(', ')}
                      </div>
                    )}
                    {!operationEvidenceMatches && (
                      <div className="admin-reconcile-evidence-warning">
                        {t(
                          'admin.reconciliation.evidenceChanged',
                          'Evidence changed since this operation was prepared',
                        )}
                      </div>
                    )}
                  </div>
                  {operation && (
                    <div className="admin-reconcile-operation">
                      <div><strong>{t('admin.reconciliation.operationId', 'Operation ID')}:</strong> {operation.operation_id}</div>
                      <div><strong>{t('admin.reconciliation.operationAction', 'Action')}:</strong> {operation.action}</div>
                      <div><strong>{t('admin.reconciliation.operationReason', 'Reason')}:</strong> {operation.reason}</div>
                      {operation.error && <div className="admin-reconcile-operation-error">{operation.error}</div>}
                    </div>
                  )}
                </div>
                <div>
                  <span className="badge badge-warning">{task.status}</span>
                </div>
                <div className="admin-reconcile-actions">
                  {!operation && (
                    <textarea
                      className="admin-reconcile-notes"
                      aria-label={t('admin.reconciliation.evidenceLabel', 'Operator evidence')}
                      placeholder={t(
                        'admin.reconciliation.evidencePlaceholder',
                        'Describe the evidence checked for every recovery target',
                      )}
                      value={reason}
                      disabled={!evidenceComplete}
                      onChange={(event) => setNotes((current) => ({
                        ...current,
                        [task.task_id]: event.target.value,
                      }))}
                    />
                  )}
                  <label className="admin-reconcile-confirm">
                    <input
                      type="checkbox"
                      checked={Boolean(confirmed[task.task_id])}
                      disabled={!evidenceComplete || !operationEvidenceMatches}
                      onChange={(event) => setConfirmed((current) => ({
                        ...current,
                        [task.task_id]: event.target.checked,
                      }))}
                    />
                    {operation
                      ? t('admin.reconciliation.confirmResume', 'I confirm resuming this immutable operation')
                      : t(
                        'admin.reconciliation.confirmEvidence',
                        'I verified every listed target and frame',
                      )}
                  </label>
                  <div className="admin-reconcile-action-buttons">
                    {operation ? (
                      <button
                        className="btn-secondary"
                        onClick={() => applyAction(task)}
                        disabled={loading || !actionReady}
                      >
                        {t('admin.reconciliation.resume', 'Resume')}
                      </button>
                    ) : (
                      <>
                        <button
                          className="btn-secondary"
                          onClick={() => applyAction(task, 'mark_resolved')}
                          disabled={loading || !actionReady}
                        >
                          {t('admin.reconciliation.resolve', 'Resolve')}
                        </button>
                        <button
                          className="btn-secondary"
                          onClick={() => applyAction(task, 'archive')}
                          disabled={loading || !actionReady}
                        >
                          {t('admin.reconciliation.archive', 'Archive')}
                        </button>
                        {task.retry_allowed && (
                          <button
                            className="btn-secondary"
                            onClick={() => applyAction(task, 'retry')}
                            disabled={loading || !actionReady}
                          >
                            {t('admin.reconciliation.retry', 'Retry')}
                          </button>
                        )}
                      </>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div className="admin-reconcile-header">
        <div>
          <div className="admin-reconcile-title">
            {t('admin.reconciliation.deliveryTitle', 'Completion delivery reconciliation')}
          </div>
          <div className="admin-reconcile-count">
            {deliveries.length} {t('admin.reconciliation.deliveryOpenItems', 'delivery failures')}
          </div>
        </div>
      </div>
      {deliveries.length === 0 ? (
        <div className="admin-reconcile-empty">
          {t('admin.reconciliation.deliveryEmpty', 'No completion deliveries need reconciliation.')}
        </div>
      ) : (
        <div className="admin-reconcile-scroll">
          {deliveries.map((delivery) => {
            const key = `delivery:${delivery.delivery_id}`;
            const reason = notes[key] || '';
            const retryReady = delivery.retryable
              && delivery.authority_snapshot?.valid === true
              && reason.trim().length >= 8
              && Boolean(confirmed[key]);
            return (
              <div key={delivery.delivery_id} className="admin-reconcile-row">
                <div>
                  <div className="admin-reconcile-label">{delivery.source_kind}</div>
                  <div className="admin-reconcile-sub-mono">{delivery.source_run_id}</div>
                </div>
                <div>
                  <div className="admin-reconcile-reason">
                    {delivery.last_error || delivery.summary || '-'}
                  </div>
                  <div className="admin-reconcile-sub">
                    {t('admin.reconciliation.executionTruth', 'Execution truth')}: {' '}
                    {delivery.execution_terminal_status}
                  </div>
                  <div className="admin-reconcile-evidence">
                    <div className="admin-reconcile-evidence-line">
                      {t('admin.reconciliation.deliveryTenant', 'Tenant')}: {delivery.tenant_id}
                    </div>
                    <div className="admin-reconcile-evidence-line">
                      {t('admin.reconciliation.deliveryTargetAgent', 'Target agent')}: {' '}
                      {delivery.agent_id || delivery.parent_agent_id || '-'}
                    </div>
                    <div className="admin-reconcile-evidence-line">
                      {t('admin.reconciliation.deliveryTargetUser', 'Target user')}: {' '}
                      {delivery.parent_user_id || '-'}
                    </div>
                    <div className="admin-reconcile-evidence-line">
                      {t('admin.reconciliation.deliveryParentSession', 'Parent session')}: {' '}
                      {delivery.parent_session_id || '-'}
                    </div>
                    <div className="admin-reconcile-evidence-line">
                      {t('admin.reconciliation.deliveryChildSession', 'Child session')}: {' '}
                      {delivery.child_session_id || '-'}
                    </div>
                    <div className="admin-reconcile-evidence-line">
                      {t('admin.reconciliation.deliveryAuthorityValid', 'Authority valid')}: {' '}
                      {String(delivery.authority_snapshot?.valid ?? '-')}
                    </div>
                    {delivery.authority_snapshot && (
                      <div className="admin-reconcile-evidence-line admin-reconcile-sub-mono">
                        authority snapshot {JSON.stringify(delivery.authority_snapshot)}
                      </div>
                    )}
                  </div>
                  <div className="admin-reconcile-evidence-warning">
                    {delivery.source_kind === 'subagent'
                      ? t(
                        'admin.reconciliation.deliveryOnlySubagent',
                        'Does not rerun the completed Subagent',
                      )
                      : delivery.source_kind === 'workflow_completion'
                        ? t(
                          'admin.reconciliation.deliveryOnlyWorkflow',
                          'Does not rerun the completed Workflow',
                        )
                      : t(
                        'admin.reconciliation.deliveryOnlyExecution',
                        'Does not rerun the source execution',
                      )}
                  </div>
                </div>
                <div>
                  <span className="badge badge-warning">
                    {t('admin.reconciliation.deliveryOnly', 'Delivery only')}
                  </span>
                </div>
                <div className="admin-reconcile-actions">
                  <textarea
                    className="admin-reconcile-notes"
                    aria-label={t('admin.reconciliation.deliveryReasonLabel', 'Delivery retry evidence')}
                    placeholder={t(
                      'admin.reconciliation.deliveryReasonPlaceholder',
                      'Describe the repaired delivery target authority',
                    )}
                    value={reason}
                    onChange={(event) => setNotes((current) => ({
                      ...current,
                      [key]: event.target.value,
                    }))}
                  />
                  <label className="admin-reconcile-confirm">
                    <input
                      type="checkbox"
                      checked={Boolean(confirmed[key])}
                      onChange={(event) => setConfirmed((current) => ({
                        ...current,
                        [key]: event.target.checked,
                      }))}
                    />
                    {t(
                      'admin.reconciliation.confirmDeliveryOnly',
                      'I confirm this retries delivery only and will not rerun execution',
                    )}
                  </label>
                  <button
                    className="btn-secondary"
                    onClick={() => retryDelivery(delivery)}
                    disabled={loading || !retryReady}
                  >
                    {t('admin.reconciliation.retryDelivery', 'Retry delivery')}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

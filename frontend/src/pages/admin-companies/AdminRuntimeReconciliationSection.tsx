import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { adminApi, type RuntimeProjectionRepairReceipt, type RuntimeReconciliationAction, type RuntimeReconciliationTask } from '../../api/domains/admin';
import './AdminRuntimeReconciliationSection.css';

type Props = {
  initialTenantId?: string;
  initialTasks?: RuntimeReconciliationTask[];
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

export default function AdminRuntimeReconciliationSection({
  initialTenantId = '',
  initialTasks = [],
}: Props) {
  const { t } = useTranslation();
  const [tenantId, setTenantId] = useState(() => {
    if (initialTenantId) return initialTenantId;
    if (typeof localStorage === 'undefined') return '';
    return localStorage.getItem('current_tenant_id') || '';
  });
  const [tasks, setTasks] = useState<RuntimeReconciliationTask[]>(initialTasks);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repairing, setRepairing] = useState(false);
  const [repairReceipt, setRepairReceipt] = useState<RuntimeProjectionRepairReceipt | null>(null);

  // Unified busy boundary: while any reconciliation request is in flight, the
  // tenant input and every operator control are disabled so no tenant change or
  // semantic action can race the in-flight request or its follow-up reload.
  const busy = loading || repairing;

  const onTenantChange = (value: string) => {
    setTenantId(value);
    // A receipt belongs to the tenant it was produced for; never show a stale one.
    setRepairReceipt(null);
  };

  const load = async () => {
    if (!tenantId.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setTasks(await adminApi.listRuntimeReconciliation({ tenantId: tenantId.trim(), limit: 50 }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const applyAction = async (task: RuntimeReconciliationTask, action: RuntimeReconciliationAction) => {
    if (!tenantId.trim()) return;
    setLoading(true);
    setError(null);
    try {
      await adminApi.applyRuntimeReconciliationAction(task.task_id, {
        tenantId: tenantId.trim(),
        action,
        reason: `operator ${action}`,
      });
      setTasks(await adminApi.listRuntimeReconciliation({ tenantId: tenantId.trim(), limit: 50 }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  // Exact-code idempotent projection repair (RC-10B). The endpoint preserves
  // status=needs_reconciliation, so the truthful receipt IS the result; this
  // never resolves, archives, or retries any task.
  const repairProjections = async () => {
    const trimmedTenantId = tenantId.trim();
    if (!trimmedTenantId) return;
    setRepairing(true);
    setError(null);
    setRepairReceipt(null);
    try {
      setRepairReceipt(await adminApi.repairRuntimeReconciliationProjections({ tenantId: trimmedTenantId }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setRepairing(false);
      return;
    }
    // The receipt stays visible even when this follow-up reload fails; the
    // reload error is shown alongside it instead of hiding the server truth.
    try {
      setTasks(await adminApi.listRuntimeReconciliation({ tenantId: trimmedTenantId, limit: 50 }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRepairing(false);
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
            {tasks.length} {t('admin.reconciliation.openItems', 'open items')}
          </div>
        </div>
        <div className="admin-reconcile-search">
          <input
            className="admin-reconcile-input"
            value={tenantId}
            onChange={(event) => onTenantChange(event.target.value)}
            placeholder={t('admin.reconciliation.tenantPlaceholder', 'Tenant ID')}
            disabled={busy}
          />
          <button className="btn-secondary" onClick={load} disabled={busy || !tenantId.trim()}>
            {loading ? t('common.loading', 'Loading...') : t('admin.reconciliation.refresh', 'Refresh')}
          </button>
          <button
            className="btn-secondary"
            onClick={repairProjections}
            disabled={busy || !tenantId.trim()}
          >
            {repairing
              ? t('admin.reconciliation.repairing', 'Repairing...')
              : t('admin.reconciliation.repair', 'Repair projections')}
          </button>
        </div>
      </div>
      {error && (
        <div className="admin-reconcile-error">{error}</div>
      )}
      {repairReceipt && (
        <div className="admin-reconcile-receipt" role="status">
          {t(
            'admin.reconciliation.repairReceipt',
            'Projection repair finished: examined {{examined}} candidates, repaired {{repaired}} projections.',
            { examined: repairReceipt.examined, repaired: repairReceipt.repaired_task_ids.length },
          )}
        </div>
      )}
      {tasks.length === 0 ? (
        <div className="admin-reconcile-empty">
          {t('admin.reconciliation.empty', 'No runtime tasks need reconciliation.')}
        </div>
      ) : (
        <div className="admin-reconcile-scroll">
          {tasks.map((task) => (
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
              </div>
              <div>
                <span className="badge badge-warning">{task.status}</span>
              </div>
              <div className="admin-reconcile-actions">
                <button className="btn-secondary" onClick={() => applyAction(task, 'mark_resolved')} disabled={busy}>
                  {t('admin.reconciliation.resolve', 'Resolve')}
                </button>
                <button className="btn-secondary" onClick={() => applyAction(task, 'archive')} disabled={busy}>
                  {t('admin.reconciliation.archive', 'Archive')}
                </button>
                {task.retry_allowed && (
                  <button className="btn-secondary" onClick={() => applyAction(task, 'retry')} disabled={busy}>
                    {t('admin.reconciliation.retry', 'Retry')}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

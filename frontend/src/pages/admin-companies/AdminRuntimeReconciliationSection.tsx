import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { adminApi, type RuntimeReconciliationAction, type RuntimeReconciliationTask } from '../../api/domains/admin';
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
            onChange={(event) => setTenantId(event.target.value)}
            placeholder={t('admin.reconciliation.tenantPlaceholder', 'Tenant ID')}
          />
          <button className="btn-secondary" onClick={load} disabled={loading || !tenantId.trim()}>
            {loading ? t('common.loading', 'Loading...') : t('admin.reconciliation.refresh', 'Refresh')}
          </button>
        </div>
      </div>
      {error && (
        <div className="admin-reconcile-error">{error}</div>
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
                <button className="btn-secondary" onClick={() => applyAction(task, 'mark_resolved')} disabled={loading}>
                  {t('admin.reconciliation.resolve', 'Resolve')}
                </button>
                <button className="btn-secondary" onClick={() => applyAction(task, 'archive')} disabled={loading}>
                  {t('admin.reconciliation.archive', 'Archive')}
                </button>
                {task.retry_allowed && (
                  <button className="btn-secondary" onClick={() => applyAction(task, 'retry')} disabled={loading}>
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

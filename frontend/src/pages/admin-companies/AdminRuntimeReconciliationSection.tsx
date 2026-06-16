import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { adminApi, type RuntimeReconciliationAction, type RuntimeReconciliationTask } from '../../api/domains/admin';

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
    <div className="card" style={{ padding: '0', overflow: 'hidden' }}>
      <div
        style={{
          padding: '20px',
          borderBottom: '1px solid var(--border-subtle)',
          display: 'flex',
          justifyContent: 'space-between',
          gap: '16px',
          alignItems: 'center',
          flexWrap: 'wrap',
        }}
      >
        <div>
          <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-secondary)' }}>
            {t('admin.reconciliation.title', 'Runtime Reconciliation')}
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
            {tasks.length} {t('admin.reconciliation.openItems', 'open items')}
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', minWidth: '320px' }}>
          <input
            value={tenantId}
            onChange={(event) => setTenantId(event.target.value)}
            placeholder={t('admin.reconciliation.tenantPlaceholder', 'Tenant ID')}
            style={{
              flex: 1,
              minWidth: '180px',
              padding: '8px 10px',
              border: '1px solid var(--border-subtle)',
              borderRadius: '6px',
              background: 'var(--bg-primary)',
              color: 'var(--text-primary)',
              fontSize: '12px',
            }}
          />
          <button className="btn-secondary" onClick={load} disabled={loading || !tenantId.trim()}>
            {loading ? t('common.loading', 'Loading...') : t('admin.reconciliation.refresh', 'Refresh')}
          </button>
        </div>
      </div>
      {error && (
        <div style={{ padding: '12px 20px', color: 'var(--status-error)', fontSize: '12px' }}>{error}</div>
      )}
      {tasks.length === 0 ? (
        <div style={{ padding: '20px', color: 'var(--text-tertiary)', fontSize: '12px', textAlign: 'center' }}>
          {t('admin.reconciliation.empty', 'No runtime tasks need reconciliation.')}
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          {tasks.map((task) => (
            <div
              key={task.task_id}
              style={{
                display: 'grid',
                gridTemplateColumns: 'minmax(180px, 1.2fr) minmax(180px, 1.4fr) 120px 180px',
                gap: '12px',
                alignItems: 'center',
                padding: '14px 20px',
                borderBottom: '1px solid var(--border-subtle)',
                fontSize: '12px',
              }}
            >
              <div>
                <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{taskLabel(task)}</div>
                <div style={{ color: 'var(--text-tertiary)', marginTop: '3px', fontFamily: 'var(--font-mono)' }}>
                  {task.task_type || '-'} · {task.task_id.slice(0, 8)}
                </div>
              </div>
              <div>
                <div style={{ color: 'var(--text-primary)' }}>{task.reason || task.result_summary || '-'}</div>
                <div style={{ color: 'var(--text-tertiary)', marginTop: '3px' }}>
                  {task.side_effect_risk || '-'} · {formatDate(task.created_at)}
                </div>
              </div>
              <div>
                <span className="badge badge-warning">{task.status}</span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', flexWrap: 'wrap' }}>
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

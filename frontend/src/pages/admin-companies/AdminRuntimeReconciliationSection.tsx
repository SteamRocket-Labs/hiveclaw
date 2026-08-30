import React, { useEffect, useRef, useState } from 'react';
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

function resolveInitialTenantId(initialTenantId: string) {
  if (initialTenantId) return initialTenantId;
  if (typeof localStorage === 'undefined') return '';
  return localStorage.getItem('current_tenant_id') || '';
}

export default function AdminRuntimeReconciliationSection({
  initialTenantId = '',
  initialTasks,
}: Props) {
  const { t } = useTranslation();
  // The initial tenant is resolved exactly once per real mount, so render and
  // the mount effect can never re-read different localStorage values.
  const initialTenantRef = useRef<{ raw: string; trimmed: string } | null>(null);
  if (initialTenantRef.current === null) {
    const raw = resolveInitialTenantId(initialTenantId);
    initialTenantRef.current = { raw, trimmed: raw.trim() };
  }
  const initialTenant = initialTenantRef.current;
  // Single-flight holder for the initial request: React.StrictMode
  // double-invokes mount effects (setup → synthetic cleanup → setup), and the
  // surviving second setup must consume the same in-flight request instead of
  // issuing a duplicate. A real unmount/remount gets a fresh ref and may
  // issue a new request.
  const initialLoadRef = useRef<Promise<RuntimeReconciliationTask[]> | null>(null);
  const [tenantId, setTenantId] = useState(initialTenant.raw);
  const [tasks, setTasks] = useState<RuntimeReconciliationTask[]>(initialTasks ?? []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repairing, setRepairing] = useState(false);
  const [repairReceipt, setRepairReceipt] = useState<RuntimeProjectionRepairReceipt | null>(null);
  const [effectEvidenceNotes, setEffectEvidenceNotes] = useState<Record<string, string>>({});
  // The tenant the rendered queue truthfully belongs to. null means nothing
  // has been successfully loaded for the current tenant binding, so neither a
  // count nor an empty conclusion may be claimed. Explicitly seeded
  // initialTasks are authoritative (SSR/tests) and count as already loaded.
  const [loadedTenant, setLoadedTenant] = useState<string | null>(() =>
    initialTasks !== undefined ? initialTenant.trimmed : null,
  );

  // Unified busy boundary: while any reconciliation request is in flight, the
  // tenant input and every operator control are disabled so no tenant change or
  // semantic action can race the in-flight request or its follow-up reload.
  const busy = loading || repairing;
  const bound = loadedTenant !== null && loadedTenant === tenantId.trim();

  // Initial auto-load: PlatformDashboard mounts this section with no props, so
  // the production path resolves the tenant from localStorage and must load
  // the queue on its own instead of rendering the default [] as a false
  // authoritative empty. Explicitly seeded initialTasks never trigger a
  // duplicate fetch. Later tenant edits load only via explicit Refresh —
  // never per keystroke. Late completions after a real unmount are ignored.
  useEffect(() => {
    if (initialTasks !== undefined) return;
    const trimmed = initialTenant.trimmed;
    if (!trimmed) return;
    let active = true;
    if (initialLoadRef.current === null) {
      setLoading(true);
      setError(null);
      initialLoadRef.current = adminApi.listRuntimeReconciliation({ tenantId: trimmed, limit: 50 });
    }
    initialLoadRef.current.then(
      (rows) => {
        if (!active) return;
        setTasks(rows);
        setLoadedTenant(trimmed);
        setLoading(false);
      },
      (err) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      },
    );
    return () => {
      // StrictMode's synthetic cleanup only detaches this setup's handlers;
      // the shared in-flight request is left for the surviving setup.
      active = false;
    };
    // Mount-only by design: tenant edits are loaded explicitly via Refresh.
  }, []);

  const onTenantChange = (value: string) => {
    setTenantId(value);
    // Rows, errors, and receipts belong to the tenant they were produced for;
    // never show stale-tenant truth under a newly typed tenant.
    setTasks([]);
    setError(null);
    setRepairReceipt(null);
    setEffectEvidenceNotes({});
    setLoadedTenant(null);
  };

  const load = async () => {
    const trimmed = tenantId.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    try {
      const rows = await adminApi.listRuntimeReconciliation({ tenantId: trimmed, limit: 50 });
      setTasks(rows);
      setLoadedTenant(trimmed);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const applyAction = async (task: RuntimeReconciliationTask, action: RuntimeReconciliationAction) => {
    const trimmed = tenantId.trim();
    if (!trimmed) return;
    const reason = action === 'acknowledge_tool_effect'
      ? String(effectEvidenceNotes[task.task_id] || '').trim()
      : `operator ${action}`;
    if (!reason) return;
    setLoading(true);
    setError(null);
    try {
      await adminApi.applyRuntimeReconciliationAction(task.task_id, {
        tenantId: trimmed,
        action,
        reason,
      });
      const rows = await adminApi.listRuntimeReconciliation({ tenantId: trimmed, limit: 50 });
      setTasks(rows);
      setLoadedTenant(trimmed);
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
      const rows = await adminApi.listRuntimeReconciliation({ tenantId: trimmedTenantId, limit: 50 });
      setTasks(rows);
      setLoadedTenant(trimmedTenantId);
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
            {bound
              ? `${tasks.length} ${t('admin.reconciliation.openItems', 'open items')}`
              : loading
                ? t('common.loading', 'Loading...')
                : t('admin.reconciliation.notLoaded', 'Queue not loaded')}
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
      {!bound ? (
        loading ? (
          <div className="admin-reconcile-empty">{t('common.loading', 'Loading...')}</div>
        ) : error ? null : (
          <div className="admin-reconcile-empty">
            {t('admin.reconciliation.loadPrompt', "Refresh to load this tenant's reconciliation queue.")}
          </div>
        )
      ) : tasks.length === 0 ? (
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
                {task.tool_effect_reconciliation_required ? (
                  <>
                    <input
                      className="admin-reconcile-input"
                      value={effectEvidenceNotes[task.task_id] || ''}
                      onChange={(event) => setEffectEvidenceNotes((current) => ({
                        ...current,
                        [task.task_id]: event.target.value,
                      }))}
                      placeholder={t(
                        'admin.reconciliation.effectEvidenceNote',
                        'Required effect evidence note',
                      )}
                      disabled={busy}
                    />
                    <button
                      className="btn-secondary"
                      onClick={() => applyAction(task, 'acknowledge_tool_effect')}
                      disabled={busy || !String(effectEvidenceNotes[task.task_id] || '').trim()}
                    >
                      {t(
                        'admin.reconciliation.acknowledgeToolEffect',
                        'Acknowledge effect and stop',
                      )}
                    </button>
                  </>
                ) : (
                  <>
                    {(task.supported_actions?.includes('mark_resolved') ?? true) && (
                      <button className="btn-secondary" onClick={() => applyAction(task, 'mark_resolved')} disabled={busy}>
                        {t('admin.reconciliation.resolve', 'Resolve')}
                      </button>
                    )}
                    {(task.supported_actions?.includes('archive') ?? true) && (
                      <button className="btn-secondary" onClick={() => applyAction(task, 'archive')} disabled={busy}>
                        {t('admin.reconciliation.archive', 'Archive')}
                      </button>
                    )}
                    {task.retry_allowed && (task.supported_actions?.includes('retry') ?? true) && (
                      <button className="btn-secondary" onClick={() => applyAction(task, 'retry')} disabled={busy}>
                        {t('admin.reconciliation.retry', 'Retry')}
                      </button>
                    )}
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

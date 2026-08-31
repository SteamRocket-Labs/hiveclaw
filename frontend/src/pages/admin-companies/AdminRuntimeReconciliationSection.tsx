import React, { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  adminApi,
  type RuntimeProjectionRepairReceipt,
  type RuntimeReconciliationAction,
  type RuntimeReconciliationTask,
  type RuntimeTriggerDisposition,
} from '../../api/domains/admin';
import './AdminRuntimeReconciliationSection.css';

const TRIGGER_DISPOSITION_CONTROLS: Array<{
  disposition: RuntimeTriggerDisposition;
  action: RuntimeReconciliationAction;
}> = [
  {
    disposition: 'confirmed_success',
    action: 'mark_resolved',
  },
  {
    disposition: 'confirmed_failure',
    action: 'mark_resolved',
  },
  {
    disposition: 'release',
    action: 'archive',
  },
];

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

function isUuid(value?: string | null): value is string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value || '');
}

function workflowTriggerResults(task: RuntimeReconciliationTask) {
  const value = task.metadata?.workflow_trigger_results;
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    : [];
}

function triggerSettlementEntries(task: RuntimeReconciliationTask) {
  const settlement = task.metadata?.trigger_settlement;
  const canonical = settlement && typeof settlement === 'object' && !Array.isArray(settlement)
    ? (settlement as Record<string, unknown>).trigger_outcomes
    : undefined;
  const value = canonical && typeof canonical === 'object' && !Array.isArray(canonical)
    ? canonical
    : task.metadata?.trigger_settlement_overrides;
  return value && typeof value === 'object' && !Array.isArray(value)
    ? Object.entries(value as Record<string, unknown>)
    : [];
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
  const tenantInputId = useId();
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

  const applyAction = async (
    task: RuntimeReconciliationTask,
    action: RuntimeReconciliationAction,
    triggerDisposition?: RuntimeTriggerDisposition,
  ) => {
    const trimmed = tenantId.trim();
    if (!trimmed) return;
    const reason = String(effectEvidenceNotes[task.task_id] || '').trim();
    if (!reason) return;
    setLoading(true);
    setError(null);
    try {
      await adminApi.applyRuntimeReconciliationAction(task.task_id, {
        tenantId: trimmed,
        action,
        reason,
        ...(triggerDisposition ? { triggerDisposition } : {}),
      });
      setEffectEvidenceNotes((current) => {
        const next = { ...current };
        delete next[task.task_id];
        return next;
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

  const renderTriggerDispositionControls = (
    task: RuntimeReconciliationTask,
    options: { acknowledgeToolEffect?: boolean; disabled?: boolean } = {},
  ) => {
    const supported = task.supported_trigger_dispositions || [];
    if (supported.length === 0) {
      return (
        <span className="admin-reconcile-sub">
          {t(
            'admin.reconciliation.triggerDispositionUnavailable',
            'Typed trigger reconciliation is unavailable',
          )}
        </span>
      );
    }
    const label = (disposition: RuntimeTriggerDisposition) => {
      if (disposition === 'confirmed_success') {
        return t('admin.reconciliation.confirmTriggerSuccess', 'Confirm success');
      }
      if (disposition === 'confirmed_failure') {
        return t('admin.reconciliation.confirmTriggerFailure', 'Confirm failure');
      }
      return t('admin.reconciliation.releaseTriggerHold', 'Release hold');
    };
    return TRIGGER_DISPOSITION_CONTROLS
      .filter(({ disposition }) => supported.includes(disposition))
      .map(({ disposition, action }) => (
        <button
          type="button"
          key={disposition}
          className="btn-secondary"
          onClick={() => applyAction(
            task,
            options.acknowledgeToolEffect ? 'acknowledge_tool_effect' : action,
            disposition,
          )}
          disabled={
            busy
            || options.disabled
            || task.trigger_disposition_readiness?.ready !== true
            || !String(effectEvidenceNotes[task.task_id] || '').trim()
          }
          aria-label={`${label(disposition)} — ${taskLabel(task)}`}
        >
          {label(disposition)}
        </button>
      ));
  };

  const copyEvidence = async (value: string) => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable');
      await navigator.clipboard.writeText(value);
    } catch {
      setError(t('admin.reconciliation.copyFailed', 'Could not copy evidence.'));
    }
  };

  const renderEvidenceRef = (
    task: RuntimeReconciliationTask,
    label: string,
    value?: string | null,
    href?: string | null,
  ) => {
    const normalized = String(value || '').trim();
    return (
      <div className="admin-reconcile-evidence-ref">
        <span className="admin-reconcile-evidence-label">{label}</span>
        {normalized ? (
          <>
            <code>{normalized}</code>
            <span className="admin-reconcile-ref-actions">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => void copyEvidence(normalized)}
                aria-label={`${t('admin.reconciliation.copyEvidence', 'Copy')} ${label} — ${taskLabel(task)}`}
              >
                {t('admin.reconciliation.copyEvidence', 'Copy')}
              </button>
              {href ? (
                <a href={href} aria-label={`${t('admin.reconciliation.openEvidence', 'Open')} ${label} — ${taskLabel(task)}`}>
                  {t('admin.reconciliation.openEvidence', 'Open')}
                </a>
              ) : (
                <span className="admin-reconcile-unavailable">
                  {t('admin.reconciliation.openUnavailable', 'Open unavailable')}
                </span>
              )}
            </span>
          </>
        ) : (
          <span className="admin-reconcile-unavailable">
            {t('admin.reconciliation.evidenceUnavailable', 'Unavailable')}
          </span>
        )}
      </div>
    );
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
          <label className="admin-reconcile-field-label" htmlFor={tenantInputId}>
            {t('admin.reconciliation.tenantLabel', 'Tenant ID')}
          </label>
          <input
            id={tenantInputId}
            className="admin-reconcile-input"
            value={tenantId}
            onChange={(event) => onTenantChange(event.target.value)}
            placeholder={t('admin.reconciliation.tenantPlaceholder', 'Tenant ID')}
            disabled={busy}
            required
            aria-required="true"
          />
          <button type="button" className="btn-secondary" onClick={load} disabled={busy || !tenantId.trim()}>
            {loading ? t('common.loading', 'Loading...') : t('admin.reconciliation.refresh', 'Refresh')}
          </button>
          <button
            type="button"
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
        <div className="admin-reconcile-error" role="alert">{error}</div>
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
          {tasks.map((task) => {
            const workflowResults = workflowTriggerResults(task);
            const settlementEntries = triggerSettlementEntries(task);
            const label = taskLabel(task);
            const evidenceInputId = `admin-reconcile-evidence-${task.task_id}`;
            const evidenceLabel = task.tool_effect_reconciliation_required
              ? t('admin.reconciliation.effectEvidenceNote', 'Required effect evidence note')
              : task.task_type === 'trigger' && task.status === 'needs_reconciliation'
                ? t('admin.reconciliation.triggerEvidenceNote', 'Required trigger evidence note')
                : t('admin.reconciliation.reconciliationEvidenceNote', 'Required reconciliation evidence note');
            const evidenceReady = Boolean(String(effectEvidenceNotes[task.task_id] || '').trim());
            const sessionAgentId = task.task_type === 'delegation' ? task.child_agent_id : task.parent_agent_id;
            const childSessionHref = isUuid(task.child_session_id) && isUuid(sessionAgentId)
              ? `/agents/${encodeURIComponent(sessionAgentId)}/sessions/${encodeURIComponent(task.child_session_id)}`
              : null;
            const artifactValue = task.output_artifact
              ? `${task.output_artifact.schema} · ${task.output_artifact.path}`
              : null;
            const readiness = task.trigger_disposition_readiness;
            return (
              <div key={task.task_id} className="admin-reconcile-row">
              <div>
                <div className="admin-reconcile-label">{label}</div>
                <div className="admin-reconcile-sub-mono">
                  {task.task_type || '-'} · {task.task_id.slice(0, 8)}
                </div>
              </div>
              <div>
                <div className="admin-reconcile-reason">{task.reason || task.result_summary || '-'}</div>
                <div className="admin-reconcile-sub">
                  {task.side_effect_risk || '-'} · {formatDate(task.created_at)}
                </div>
                {workflowResults.map((result, index) => (
                  <div className="admin-reconcile-evidence" key={String(result.run_id || result.trigger_id || index)}>
                    {t('admin.reconciliation.workflowEvidence', 'Workflow child')}{' '}
                    {String(result.trigger_name || result.trigger_id || '-')}: {String(result.status || '-')}
                    {' · '}{t('admin.reconciliation.run', 'run')} {String(result.run_id || '-')}
                    {' · '}{t('admin.reconciliation.session', 'session')} {String(result.session_id || '-')}
                    {result.run_status ? ` · ${String(result.run_status)}` : ''}
                    {result.reason ? ` · ${String(result.reason)}` : ''}
                  </div>
                ))}
                {settlementEntries.map(([triggerId, outcome]) => (
                  <div className="admin-reconcile-evidence" key={triggerId}>
                    {t('admin.reconciliation.triggerSettlement', 'Trigger settlement')}{' '}
                    {triggerId}: {String(outcome)}
                  </div>
                ))}
                {task.task_type === 'trigger' && task.status === 'needs_reconciliation' && (
                  <div className={readiness?.ready ? 'admin-reconcile-readiness' : 'admin-reconcile-blocker'}>
                    {readiness?.ready
                      ? t('admin.reconciliation.triggerDispositionReady', 'Disposition evidence is ready.')
                      : `${t(
                        'admin.reconciliation.triggerDispositionBlocked',
                        'Disposition actions blocked',
                      )}: ${readiness?.blocker || 'readiness_unavailable'}`}
                  </div>
                )}
                {renderEvidenceRef(
                  task,
                  t('admin.reconciliation.childSessionId', 'Child session ID'),
                  task.child_session_id,
                  childSessionHref,
                )}
                {renderEvidenceRef(
                  task,
                  t('admin.reconciliation.traceId', 'Trace ID'),
                  task.trace_id,
                )}
                {renderEvidenceRef(
                  task,
                  t('admin.reconciliation.outputArtifact', 'Output artifact'),
                  artifactValue,
                )}
                {renderEvidenceRef(
                  task,
                  t('admin.reconciliation.completionOutboxId', 'Completion outbox ID'),
                  task.completion_outbox_id,
                )}
                {renderEvidenceRef(
                  task,
                  t('admin.reconciliation.settlementAuditRef', 'Settlement audit ref'),
                  task.settlement_audit_ref?.id,
                )}
              </div>
              <div>
                <span className="badge badge-warning">{task.status}</span>
              </div>
              <div className="admin-reconcile-actions">
                <label className="admin-reconcile-field-label" htmlFor={evidenceInputId}>
                  {evidenceLabel}
                </label>
                <input
                  id={evidenceInputId}
                  className="admin-reconcile-input"
                  value={effectEvidenceNotes[task.task_id] || ''}
                  onChange={(event) => setEffectEvidenceNotes((current) => ({
                    ...current,
                    [task.task_id]: event.target.value,
                  }))}
                  placeholder={evidenceLabel}
                  disabled={busy}
                  required
                  aria-required="true"
                />
                {task.tool_effect_reconciliation_required ? (
                  <>
                    {task.task_type === 'trigger' && task.status === 'needs_reconciliation' ? (
                      renderTriggerDispositionControls(
                        task,
                        {
                          acknowledgeToolEffect: true,
                          disabled: !evidenceReady,
                        },
                      )
                    ) : (
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => applyAction(task, 'acknowledge_tool_effect')}
                        disabled={busy || !evidenceReady}
                        aria-label={`${t('admin.reconciliation.acknowledgeToolEffect', 'Acknowledge effect and stop')} — ${label}`}
                      >
                        {t(
                          'admin.reconciliation.acknowledgeToolEffect',
                          'Acknowledge effect and stop',
                        )}
                      </button>
                    )}
                  </>
                ) : task.task_type === 'trigger' && task.status === 'needs_reconciliation' ? (
                  renderTriggerDispositionControls(task)
                ) : (
                  <>
                    {(task.supported_actions?.includes('mark_resolved') ?? true) && (
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => applyAction(task, 'mark_resolved')}
                        disabled={busy || !evidenceReady}
                        aria-label={`${t('admin.reconciliation.resolve', 'Resolve')} — ${label}`}
                      >
                        {t('admin.reconciliation.resolve', 'Resolve')}
                      </button>
                    )}
                    {(task.supported_actions?.includes('archive') ?? true) && (
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => applyAction(task, 'archive')}
                        disabled={busy || !evidenceReady}
                        aria-label={`${t('admin.reconciliation.archive', 'Archive')} — ${label}`}
                      >
                        {t('admin.reconciliation.archive', 'Archive')}
                      </button>
                    )}
                    {task.retry_allowed && (task.supported_actions?.includes('retry') ?? true) && (
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => applyAction(task, 'retry')}
                        disabled={busy || !evidenceReady}
                        aria-label={`${t('admin.reconciliation.retry', 'Retry')} — ${label}`}
                      >
                        {t('admin.reconciliation.retry', 'Retry')}
                      </button>
                    )}
                  </>
                )}
              </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

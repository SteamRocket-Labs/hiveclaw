import React, { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import ConfirmModal from '../../components/ConfirmModal';
import {
  adminApi,
  type RuntimeTerminalBoundary,
} from '../../api/domains/admin';
import './AdminRuntimeReconciliationSection.css';

// The GET endpoint only accepts these exact outbox statuses; there is no
// "all" bucket, so the filter always pins one concrete state (dead_letter by
// default, which is the only redriveable one).
const STATUS_FILTERS = ['dead_letter', 'pending', 'processing', 'delivered'] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

type PendingRedrive = {
  boundary: RuntimeTerminalBoundary;
  reason: string;
  retrySummary: boolean;
};

type Props = {
  initialTenantId?: string;
  initialBoundaries?: RuntimeTerminalBoundary[];
  initialStatus?: StatusFilter;
};

function formatDate(value?: string | null) {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function boundaryLabel(boundary: RuntimeTerminalBoundary) {
  return boundary.session_id || boundary.id;
}

function resolveInitialTenantId(initialTenantId: string) {
  if (initialTenantId) return initialTenantId;
  if (typeof localStorage === 'undefined') return '';
  return localStorage.getItem('current_tenant_id') || '';
}

export default function AdminTerminalBoundariesSection({
  initialTenantId = '',
  initialBoundaries,
  initialStatus = 'dead_letter',
}: Props) {
  const { t } = useTranslation();
  const tenantInputId = useId();
  // Resolved exactly once per real mount so render and the mount effect can
  // never disagree about which selected company the queue belongs to.
  const initialTenantRef = useRef<{ raw: string; trimmed: string } | null>(null);
  if (initialTenantRef.current === null) {
    const raw = resolveInitialTenantId(initialTenantId);
    initialTenantRef.current = { raw, trimmed: raw.trim() };
  }
  const initialTenant = initialTenantRef.current;
  // Single-flight holder for the initial request (React.StrictMode safe),
  // mirroring AdminRuntimeReconciliationSection.
  const initialLoadRef = useRef<Promise<RuntimeTerminalBoundary[]> | null>(null);
  const [tenantId, setTenantId] = useState(initialTenant.raw);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(initialStatus);
  const [boundaries, setBoundaries] = useState<RuntimeTerminalBoundary[]>(initialBoundaries ?? []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [retrySummaries, setRetrySummaries] = useState<Record<string, boolean>>({});
  const [pendingRedrive, setPendingRedrive] = useState<PendingRedrive | null>(null);
  const [redrivingId, setRedrivingId] = useState<string | null>(null);
  const [redriveReceipt, setRedriveReceipt] = useState<string | null>(null);
  // The tenant the rendered rows truthfully belong to. null means nothing has
  // been successfully loaded for the current tenant binding, so neither a
  // count nor an empty conclusion may be claimed. Explicitly seeded
  // initialBoundaries count as already loaded.
  const [loadedTenant, setLoadedTenant] = useState<string | null>(() =>
    initialBoundaries !== undefined ? initialTenant.trimmed : null,
  );

  // Unified busy boundary: while any request is in flight (or a confirmation
  // is open), the tenant input, status filter, and every operator control are
  // disabled so nothing can race the in-flight request or double-submit.
  const busy = loading || redrivingId !== null || pendingRedrive !== null;
  const bound = loadedTenant !== null && loadedTenant === tenantId.trim();

  // Initial auto-load: listing is read-only and never mutates anything — in
  // particular it never sends summary_disposition. Explicitly seeded rows
  // never trigger a duplicate fetch. Later tenant/status edits load only via
  // explicit Refresh.
  useEffect(() => {
    if (initialBoundaries !== undefined) return;
    const trimmed = initialTenant.trimmed;
    if (!trimmed) return;
    let active = true;
    if (initialLoadRef.current === null) {
      setLoading(true);
      setError(null);
      initialLoadRef.current = adminApi.listRuntimeTerminalBoundaries({
        tenantId: trimmed,
        status: initialStatus,
        limit: 100,
      });
    }
    initialLoadRef.current.then(
      (rows) => {
        if (!active) return;
        setBoundaries(rows);
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
      active = false;
    };
    // Mount-only by design: tenant/status edits are loaded via Refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onTenantChange = (value: string) => {
    setTenantId(value);
    // Rows, errors, and receipts belong to the tenant they were produced for;
    // never show stale-tenant truth under a newly typed tenant, and never let
    // an old-company row execute under a different selected company.
    setBoundaries([]);
    setError(null);
    setRedriveReceipt(null);
    setReasons({});
    setRetrySummaries({});
    setLoadedTenant(null);
  };

  const onStatusChange = (value: StatusFilter) => {
    setStatusFilter(value);
    // A different status bucket has not been loaded; keep the tenant binding
    // but drop the rows so nothing stale is shown as truth for the new filter.
    setBoundaries([]);
    setError(null);
    setRedriveReceipt(null);
    setLoadedTenant(null);
  };

  const load = async () => {
    const trimmed = tenantId.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    // Invalidate the loaded binding before the request: if this refresh fails,
    // the previous rows must stop being presented as current truth, so neither
    // a count nor an empty conclusion may be claimed until a load succeeds.
    setLoadedTenant(null);
    try {
      const rows = await adminApi.listRuntimeTerminalBoundaries({
        tenantId: trimmed,
        status: statusFilter,
        limit: 100,
      });
      setBoundaries(rows);
      setLoadedTenant(trimmed);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const requestRedrive = (boundary: RuntimeTerminalBoundary) => {
    const reason = String(reasons[boundary.id] || '').trim();
    if (!reason || boundary.status !== 'dead_letter') return;
    setPendingRedrive({
      boundary,
      reason,
      retrySummary: retrySummaries[boundary.id] === true,
    });
  };

  const executeRedrive = async () => {
    if (!pendingRedrive) return;
    const { boundary, reason, retrySummary } = pendingRedrive;
    const trimmed = tenantId.trim();
    if (!trimmed) return;
    setPendingRedrive(null);
    setRedrivingId(boundary.id);
    setError(null);
    let result: RuntimeTerminalBoundary;
    try {
      result = await adminApi.redriveRuntimeTerminalBoundary(boundary.id, {
        tenantId: trimmed,
        reason,
        ...(retrySummary ? { summaryDisposition: 'retry' as const } : {}),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setRedrivingId(null);
      return;
    }
    // Pending means requeued, not delivered and not business success.
    setRedriveReceipt(t(
      'admin.terminalBoundaries.redriveReceipt',
      'Boundary {{id}} requeued with status {{status}}. Requeued means it will be attempted again — not delivered and not business success.',
      { id: result.id, status: result.status },
    ));
    setReasons((current) => {
      const next = { ...current };
      delete next[boundary.id];
      return next;
    });
    setRetrySummaries((current) => {
      const next = { ...current };
      delete next[boundary.id];
      return next;
    });
    // The receipt stays visible even when this follow-up reload fails; the
    // reload error is shown alongside it instead of hiding the server truth.
    // The loaded binding is invalidated first so a failed reload leaves the
    // old dead-letter rows non-actionable instead of actionable-as-refreshed.
    setLoadedTenant(null);
    try {
      const rows = await adminApi.listRuntimeTerminalBoundaries({
        tenantId: trimmed,
        status: statusFilter,
        limit: 100,
      });
      setBoundaries(rows);
      setLoadedTenant(trimmed);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRedrivingId(null);
    }
  };

  return (
    <div className="card card-pad-none admin-reconcile-card">
      <div className="admin-reconcile-header">
        <div>
          <div className="admin-reconcile-title">
            {t('admin.terminalBoundaries.title', 'Terminal Boundary Recovery')}
          </div>
          <div className="admin-reconcile-count">
            {bound
              ? `${boundaries.length} ${t('admin.terminalBoundaries.openItems', 'boundaries')}`
              : loading
                ? t('common.loading', 'Loading...')
                : t('admin.terminalBoundaries.notLoaded', 'Queue not loaded')}
          </div>
        </div>
        <div className="admin-reconcile-search">
          <label className="admin-reconcile-field-label" htmlFor={tenantInputId}>
            {t('admin.terminalBoundaries.tenantLabel', 'Tenant ID')}
          </label>
          <input
            id={tenantInputId}
            className="admin-reconcile-input"
            value={tenantId}
            onChange={(event) => onTenantChange(event.target.value)}
            placeholder={t('admin.terminalBoundaries.tenantPlaceholder', 'Tenant ID')}
            disabled={busy}
            required
            aria-required="true"
          />
          <label className="admin-reconcile-field-label" htmlFor="admin-terminal-boundary-status">
            {t('admin.terminalBoundaries.statusLabel', 'Status')}
          </label>
          <select
            id="admin-terminal-boundary-status"
            className="admin-reconcile-input"
            value={statusFilter}
            onChange={(event) => onStatusChange(event.target.value as StatusFilter)}
            disabled={busy}
          >
            {STATUS_FILTERS.map((status) => (
              <option key={status} value={status}>{status}</option>
            ))}
          </select>
          <button type="button" className="btn-secondary" onClick={load} disabled={busy || !tenantId.trim()}>
            {loading ? t('common.loading', 'Loading...') : t('admin.terminalBoundaries.refresh', 'Refresh')}
          </button>
        </div>
      </div>
      <div className="admin-reconcile-sub">
        {t(
          'admin.terminalBoundaries.explanation',
          'A dead-letter terminal boundary stopped after exhausting its automatic delivery attempts. Redrive requeues exactly one boundary for a new delivery attempt; a pending result means requeued, not delivered or business success. The original provider delivery state is unknown.',
        )}
      </div>
      {error && (
        <div className="admin-reconcile-error" role="alert">{error}</div>
      )}
      {redriveReceipt && (
        <div className="admin-reconcile-receipt" role="status">{redriveReceipt}</div>
      )}
      {!bound ? (
        loading ? (
          <div className="admin-reconcile-empty">{t('common.loading', 'Loading...')}</div>
        ) : error ? null : (
          <div className="admin-reconcile-empty">
            {t('admin.terminalBoundaries.loadPrompt', "Refresh to load this tenant's terminal-boundary queue.")}
          </div>
        )
      ) : boundaries.length === 0 ? (
        <div className="admin-reconcile-empty">
          {t('admin.terminalBoundaries.empty', 'No terminal boundaries in this state.')}
        </div>
      ) : (
        <div className="admin-reconcile-scroll">
          {boundaries.map((boundary) => {
            const label = boundaryLabel(boundary);
            const reasonInputId = `admin-terminal-boundary-reason-${boundary.id}`;
            const reasonReady = Boolean(String(reasons[boundary.id] || '').trim());
            const rowBusy = busy || boundary.status !== 'dead_letter';
            return (
              <div key={boundary.id} className="admin-reconcile-row">
                <div>
                  <div className="admin-reconcile-label">
                    {t('admin.terminalBoundaries.sessionLabel', 'session')} {boundary.session_id || '-'}
                  </div>
                  <div className="admin-reconcile-sub-mono">
                    {boundary.event_kind} · {boundary.terminal_status} · {t('admin.terminalBoundaries.attempts', 'attempts')} {boundary.attempt_count}
                  </div>
                </div>
                <div>
                  <div className="admin-reconcile-evidence">
                    {t('admin.terminalBoundaries.boundaryId', 'Boundary')} {boundary.id}
                  </div>
                  <div className="admin-reconcile-evidence">
                    {t('admin.terminalBoundaries.taskId', 'Runtime task')} {boundary.runtime_task_id}
                  </div>
                  <div className="admin-reconcile-reason">
                    {t('admin.terminalBoundaries.lastError', 'Last error')}: {boundary.last_error || '-'}
                  </div>
                  <div className="admin-reconcile-sub">{formatDate(boundary.updated_at)}</div>
                </div>
                <div>
                  <span className={`badge ${boundary.status === 'dead_letter' ? 'badge-danger' : 'badge-warning'}`}>
                    {boundary.status}
                  </span>
                </div>
                <div className="admin-reconcile-actions">
                  <label className="admin-reconcile-field-label" htmlFor={reasonInputId}>
                    {t('admin.terminalBoundaries.reasonLabel', 'Required audit reason')}
                  </label>
                  <input
                    id={reasonInputId}
                    className="admin-reconcile-input"
                    value={reasons[boundary.id] || ''}
                    onChange={(event) => setReasons((current) => ({
                      ...current,
                      [boundary.id]: event.target.value,
                    }))}
                    placeholder={t('admin.terminalBoundaries.reasonLabel', 'Required audit reason')}
                    maxLength={1000}
                    disabled={busy}
                    required
                    aria-required="true"
                  />
                  <label className="admin-reconcile-field-label">
                    <input
                      type="checkbox"
                      checked={retrySummaries[boundary.id] === true}
                      onChange={(event) => setRetrySummaries((current) => ({
                        ...current,
                        [boundary.id]: event.target.checked,
                      }))}
                      disabled={busy}
                    />
                    {' '}
                    {t('admin.terminalBoundaries.retrySummaryLabel', 'Recompute the terminal summary')}
                  </label>
                  <div className="admin-reconcile-sub">
                    {t(
                      'admin.terminalBoundaries.retrySummaryWarning',
                      'Summary recomputation can repeat previously accepted summary subcalls and incur extra usage.',
                    )}
                  </div>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => requestRedrive(boundary)}
                    disabled={rowBusy || !reasonReady}
                    aria-label={`${t('admin.terminalBoundaries.redrive', 'Redrive')} — ${label}`}
                  >
                    {redrivingId === boundary.id
                      ? t('admin.terminalBoundaries.redriving', 'Redriving...')
                      : t('admin.terminalBoundaries.redrive', 'Redrive')}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <ConfirmModal
        open={pendingRedrive !== null}
        danger
        title={t('admin.terminalBoundaries.confirmTitle', 'Redrive this terminal boundary')}
        message={pendingRedrive
          ? t(
            'admin.terminalBoundaries.confirmMessage',
            'Requeue boundary {{id}} (session {{session}}) for one new delivery attempt? This is an explicit operator recovery, not an automatic retry.{{retry}} A requeued boundary is retried by the normal consumer; success here does not mean delivered.',
            {
              id: pendingRedrive.boundary.id,
              session: pendingRedrive.boundary.session_id,
              retry: pendingRedrive.retrySummary
                ? ' ' + t(
                  'admin.terminalBoundaries.confirmRetrySuffix',
                  'Summary recomputation is requested and can repeat previously accepted summary subcalls, incurring extra usage.',
                )
                : '',
            },
          )
          : ''}
        confirmLabel={t('admin.terminalBoundaries.redrive', 'Redrive')}
        onConfirm={() => void executeRedrive()}
        onCancel={() => setPendingRedrive(null)}
      />
    </div>
  );
}

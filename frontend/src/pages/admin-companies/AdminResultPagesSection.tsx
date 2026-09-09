import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ConfirmModal from '../../components/ConfirmModal';
import { adminApi, type RuntimeResultPage } from '../../api/domains/admin';
import './AdminRuntimeReconciliationSection.css';

export default function AdminResultPagesSection({ initialTenantId }: { initialTenantId?: string }) {
  const { t } = useTranslation();
  const id = useId();
  const [tenantId, setTenantId] = useState(() => initialTenantId
    ?? (typeof localStorage === 'undefined' ? '' : localStorage.getItem('current_tenant_id') ?? ''));
  const [sessionId, setSessionId] = useState('');
  const [rows, setRows] = useState<RuntimeResultPage[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [receipt, setReceipt] = useState('');
  const [reason, setReason] = useState('');
  const [pending, setPending] = useState<RuntimeResultPage | null>(null);
  const locked = busy || pending !== null;

  const clear = () => {
    setRows(null);
    setError('');
    setReceipt('');
    setReason('');
  };
  const load = async () => {
    setBusy(true);
    setRows(null);
    setError('');
    try {
      setRows(await adminApi.listRuntimeResultPages({ tenantId: tenantId.trim(), parentSessionId: sessionId.trim() }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const redrive = async () => {
    if (!pending || !reason.trim()) return;
    setBusy(true);
    setPending(null);
    setError('');
    try {
      const result = await adminApi.redriveRuntimeResultPage(pending.id, { tenantId: tenantId.trim(), reason: reason.trim() });
      setReceipt(t('admin.resultPages.receipt', 'Page {{id}} requeued ({{status}}). This is not delivery or business completion.', {
        id: result.id, status: result.status,
      }));
      setReason('');
      await load();
    } catch (err) {
      // A failed request can have an unknown outcome; refresh before retrying.
      setRows(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card card-pad-none admin-reconcile-card" aria-labelledby={`${id}-title`}>
      <div className="admin-reconcile-header">
        <h3 id={`${id}-title`} className="admin-reconcile-title">{t('admin.resultPages.title', 'Child Result Delivery Recovery')}</h3>
        <button type="button" className="btn-secondary" onClick={load} disabled={locked || !tenantId.trim() || !sessionId.trim()}>
          {t('admin.resultPages.refresh', 'Load result pages')}
        </button>
      </div>
      <div className="admin-reconcile-sub">
        {t('admin.resultPages.help', 'Inspect one parent session. Redrive retries its saved result delivery, not child execution; existing budgets and permissions still apply.')}
      </div>
      <div className="admin-reconcile-search">
        <label htmlFor={`${id}-tenant`}>{t('admin.resultPages.tenant', 'Tenant ID')}</label>
        <input id={`${id}-tenant`} className="admin-reconcile-input" value={tenantId} disabled={locked}
          onChange={(event) => { setTenantId(event.target.value); clear(); }} />
        <label htmlFor={`${id}-session`}>{t('admin.resultPages.session', 'Parent session ID')}</label>
        <input id={`${id}-session`} className="admin-reconcile-input" value={sessionId} disabled={locked}
          onChange={(event) => { setSessionId(event.target.value); clear(); }} />
      </div>
      {error && <div className="admin-reconcile-error" role="alert">{error}</div>}
      {receipt && <div className="admin-reconcile-receipt" role="status">{receipt}</div>}
      {rows !== null && <div className="admin-reconcile-count">{t('admin.resultPages.count', '{{count}} result pages (up to 100)', { count: rows.length })}</div>}
      {rows?.map((page) => (
        <div key={page.id} style={{ padding: 'var(--space-4) var(--space-5)', borderTop: '1px solid var(--border-subtle)', overflowWrap: 'anywhere' }}>
          <div>{page.id} · epoch {page.integration_epoch} · {page.status} · {page.delivery_mode}</div>
          <div>{t('admin.resultPages.attempts', '{{items}} results; {{attempts}} attempts', { items: page.item_count, attempts: page.attempt_count })}</div>
          {page.bound_item_count != null && <div>{t('admin.resultPages.bindings', '{{count}} queue items still bound to this page', { count: page.bound_item_count })}</div>}
          <code>{page.manifest_sha256}</code>
          {page.last_error && <div>{page.last_error}</div>}
          {page.delivered_at && <div>{page.delivered_at}</div>}
          {page.status === 'dead_letter' && <button type="button" className="btn-secondary" disabled={locked || !reason.trim() || page.bound_item_count !== page.item_count}
            onClick={() => setPending(page)}>{t('admin.resultPages.redrive', 'Redrive result page')}</button>}
        </div>
      ))}
      {rows?.some((page) => page.status === 'dead_letter') && <div className="admin-reconcile-search">
        <label htmlFor={`${id}-reason`}>{t('admin.resultPages.reason', 'Recovery reason')}</label>
        <input id={`${id}-reason`} className="admin-reconcile-input" value={reason} maxLength={1000} disabled={locked}
          onChange={(event) => setReason(event.target.value)} />
      </div>}
      <ConfirmModal open={pending !== null} title={t('admin.resultPages.confirm', 'Redrive saved result delivery?')}
        message={t('admin.resultPages.confirmMessage', 'Retry only page {{id}}. Child work is not rerun. A parent continuation may run only within its existing authority and budget.', { id: pending?.id })}
        confirmLabel={t('admin.resultPages.redrive', 'Redrive result page')} onConfirm={redrive} onCancel={() => setPending(null)} />
    </section>
  );
}

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { enterpriseApi, type StorageRetention } from '../../api/domains/enterprise';

export default function WorkspaceStorageSection({ selectedTenantId }: { selectedTenantId: string }) {
  const { t } = useTranslation();
  const generation = useRef(0);
  const tenant = useRef(selectedTenantId);
  tenant.current = selectedTenantId;
  const [saved, setSaved] = useState<{ tenantId: string; value: StorageRetention } | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [days, setDays] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<'load' | 'save' | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const current = saved?.tenantId === selectedTenantId ? saved.value : null;

  async function load() {
    const target = selectedTenantId;
    const ticket = ++generation.current;
    setBusy(true);
    setError(null);
    setConfirmed(false);
    try {
      const value = await enterpriseApi.getStorageRetention(target);
      if (ticket !== generation.current || tenant.current !== target) return;
      setSaved({ tenantId: target, value });
      setEnabled(value.policy.enabled);
      setDays(String(value.policy.retention_days));
    } catch {
      if (ticket === generation.current && tenant.current === target) setError('load');
    } finally {
      if (ticket === generation.current && tenant.current === target) setBusy(false);
    }
  }

  useEffect(() => {
    setSaved(null);
    if (selectedTenantId) void load();
    return () => { generation.current += 1; };
    // Company changes invalidate every outstanding read or save.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTenantId]);

  async function save() {
    if (!current || busy || error) return;
    const target = selectedTenantId;
    const ticket = ++generation.current;
    setBusy(true);
    setConfirmed(false);
    try {
      const value = await enterpriseApi.updateStorageRetention(target, {
        enabled, retention_days: Number(days), expected_version: current.policy.version,
      });
      if (ticket !== generation.current || tenant.current !== target) return;
      setSaved({ tenantId: target, value });
      setEnabled(value.policy.enabled);
      setDays(String(value.policy.retention_days));
      setConfirmed(true);
    } catch {
      if (ticket === generation.current && tenant.current === target) setError('save');
    } finally {
      if (ticket === generation.current && tenant.current === target) setBusy(false);
    }
  }

  const valid = days.trim() !== '' && Number.isFinite(Number(days)) && Number(days) >= 0;
  return <section className="card ws-info-card" aria-label={t('enterprise.storage.title')}>
    <h3 className="ws-info-heading">{t('enterprise.storage.title')}</h3>
    <p className="ws-info-desc">{t('enterprise.storage.scope')}</p>
    {current ? <>
      <p>{current.policy.source === 'installation_default'
        ? t('enterprise.storage.default') : t('enterprise.storage.version', { version: current.policy.version })}</p>
      <label><input type="checkbox" checked={enabled} disabled={busy}
        onChange={(event) => { setEnabled(event.target.checked); setConfirmed(false); }} /> {t('enterprise.storage.enabled')}</label>
      <label>{t('enterprise.storage.days')}
        <input className="form-input" type="number" min="0" step="any" value={days} disabled={busy}
          onChange={(event) => { setDays(event.target.value); setConfirmed(false); }} />
      </label>
      <p className="ws-info-desc">{t('enterprise.storage.effective')}</p>
      {current.capacity.status === 'available' ? <p>{t('enterprise.storage.capacity', {
        used: (current.capacity.used_bytes! / 2 ** 30).toFixed(1),
        total: (current.capacity.total_bytes! / 2 ** 30).toFixed(1),
        free: (current.capacity.free_bytes! / 2 ** 30).toFixed(1),
      })}</p> : current.capacity.status === 'unavailable'
        ? <p role="status">{t('enterprise.storage.capacityUnavailable')}</p> : null}
      <button className="btn btn-primary" disabled={busy || !valid || Boolean(error)} onClick={() => void save()}>
        {busy ? t('common.loading') : t('common.save')}
      </button>
    </> : busy ? <p role="status">{t('common.loading')}</p> : null}
    {error ? <p role="alert">{error === 'save' ? t('enterprise.storage.saveUnknown') : t('enterprise.storage.loadFailed')}</p> : null}
    {confirmed ? <p role="status">{t('enterprise.storage.saved')}</p> : null}
    <button className="btn" disabled={busy || !selectedTenantId} onClick={() => void load()}>
      {t('enterprise.storage.reload')}
    </button>
  </section>;
}

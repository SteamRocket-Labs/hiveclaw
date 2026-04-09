import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { enterpriseApi } from '../../api/domains/enterprise';

interface QuotaForm {
  default_tokens_per_day: number | null;
  default_tokens_per_month: number | null;
  min_heartbeat_interval_minutes: number;
  default_max_triggers: number;
  min_poll_interval_floor: number;
  max_webhook_rate_ceiling: number;
}

const DEFAULT_FORM: QuotaForm = {
  default_tokens_per_day: null,
  default_tokens_per_month: null,
  min_heartbeat_interval_minutes: 120,
  default_max_triggers: 20,
  min_poll_interval_floor: 5,
  max_webhook_rate_ceiling: 5,
};

const formatTokens = (n: number) => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
};

export default function WorkspaceQuotasSection() {
  const { t } = useTranslation();
  const [form, setForm] = useState<QuotaForm>(DEFAULT_FORM);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    enterpriseApi.getTenantQuotas().then((data) => {
      if (data && Object.keys(data).length > 0) {
        setForm((prev) => ({ ...prev, ...data }));
      }
    }).catch(() => {});
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await enterpriseApi.updateTenantQuotas(form as unknown as Record<string, unknown>);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      alert('Failed to save');
    }
    setSaving(false);
  };

  return (
    <div>
      <h3 style={{ marginBottom: '4px' }}>
        {t('enterprise.quotas.title', 'Employee Token Quotas')}
      </h3>
      <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
        {t('enterprise.quotas.subtitle', 'Default token limits for new employees. Admins can override per-user in User Management.')}
      </p>

      {/* Token Quotas */}
      <div className="card" style={{ padding: '16px', marginBottom: '16px' }}>
        <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '12px' }}>
          {t('workspace.quotas.tokenQuotas')}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
          <div className="form-group">
            <label className="form-label">{t('workspace.quotas.dailyTokenLimit')}</label>
            <input
              className="form-input"
              type="number"
              min={0}
              placeholder={t('workspace.quotas.unlimited')}
              value={form.default_tokens_per_day ?? ''}
              onChange={(e) => setForm({ ...form, default_tokens_per_day: e.target.value ? Number(e.target.value) : null })}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {form.default_tokens_per_day
                ? t('workspace.quotas.dailyTokenDesc', { amount: formatTokens(form.default_tokens_per_day) })
                : t('workspace.quotas.dailyTokenDescUnlimited')}
            </div>
          </div>
          <div className="form-group">
            <label className="form-label">{t('workspace.quotas.monthlyTokenLimit')}</label>
            <input
              className="form-input"
              type="number"
              min={0}
              placeholder={t('workspace.quotas.unlimited')}
              value={form.default_tokens_per_month ?? ''}
              onChange={(e) => setForm({ ...form, default_tokens_per_month: e.target.value ? Number(e.target.value) : null })}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {form.default_tokens_per_month
                ? t('workspace.quotas.monthlyTokenDesc', { amount: formatTokens(form.default_tokens_per_month) })
                : t('workspace.quotas.monthlyTokenDescUnlimited')}
            </div>
          </div>
        </div>
      </div>

      {/* System Settings */}
      <div className="card" style={{ padding: '16px' }}>
        <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '12px' }}>
          {t('workspace.quotas.systemSettings')}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px' }}>
          <div className="form-group">
            <label className="form-label">{t('workspace.quotas.minHeartbeatInterval')}</label>
            <input
              className="form-input"
              type="number"
              min={1}
              value={form.min_heartbeat_interval_minutes}
              onChange={(e) => setForm({ ...form, min_heartbeat_interval_minutes: Number(e.target.value) })}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('workspace.quotas.minHeartbeatDesc')}
            </div>
          </div>
          <div className="form-group">
            <label className="form-label">{t('workspace.quotas.defaultMaxTriggers')}</label>
            <input
              className="form-input"
              type="number"
              min={1}
              max={100}
              value={form.default_max_triggers}
              onChange={(e) => setForm({ ...form, default_max_triggers: Number(e.target.value) })}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('workspace.quotas.defaultMaxTriggersDesc')}
            </div>
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
          <div className="form-group">
            <label className="form-label">{t('workspace.quotas.minPollInterval')}</label>
            <input
              className="form-input"
              type="number"
              min={1}
              max={60}
              value={form.min_poll_interval_floor}
              onChange={(e) => setForm({ ...form, min_poll_interval_floor: Number(e.target.value) })}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('workspace.quotas.minPollDesc')}
            </div>
          </div>
          <div className="form-group">
            <label className="form-label">{t('workspace.quotas.maxWebhookRate')}</label>
            <input
              className="form-input"
              type="number"
              min={1}
              max={60}
              value={form.max_webhook_rate_ceiling}
              onChange={(e) => setForm({ ...form, max_webhook_rate_ceiling: Number(e.target.value) })}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('workspace.quotas.maxWebhookDesc')}
            </div>
          </div>
        </div>
      </div>

      <div style={{ marginTop: '16px', display: 'flex', gap: '8px', alignItems: 'center' }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? t('common.saving') : t('common.save')}
        </button>
        {saved && <span style={{ color: 'var(--success)', fontSize: '12px' }}>{t('workspace.quotas.saved')}</span>}
      </div>
    </div>
  );
}

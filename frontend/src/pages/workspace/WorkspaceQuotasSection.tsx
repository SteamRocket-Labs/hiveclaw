import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { enterpriseApi } from '../../api/domains/enterprise';
import { showAppToast } from '../../components/AppDialogs';
import './WorkspaceQuotasSection.css';

interface QuotaForm {
  default_tokens_per_day: number | null;
  default_tokens_per_month: number | null;
  default_max_triggers: number;
  min_poll_interval_floor: number;
  max_webhook_rate_ceiling: number;
}

const DEFAULT_FORM: QuotaForm = {
  default_tokens_per_day: null,
  default_tokens_per_month: null,
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
      showAppToast(t('common.saveFailed', 'Failed to save'), 'error');
    }
    setSaving(false);
  };

  return (
    <div>
      <h3 className="ws-quotas-title">
        {t('enterprise.quotas.title', 'Employee Token Quotas')}
      </h3>
      <p className="ws-quotas-subtitle">
        {t('enterprise.quotas.subtitle', 'Default token limits for new employees. Admins can override per-user in User Management.')}
      </p>

      {/* Token Quotas */}
      <div className="card ws-quotas-card">
        <div className="ws-quotas-group-title">
          {t('workspace.quotas.tokenQuotas')}
        </div>
        <div className="ws-quotas-grid">
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
            <div className="ws-quotas-hint">
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
            <div className="ws-quotas-hint">
              {form.default_tokens_per_month
                ? t('workspace.quotas.monthlyTokenDesc', { amount: formatTokens(form.default_tokens_per_month) })
                : t('workspace.quotas.monthlyTokenDescUnlimited')}
            </div>
          </div>
        </div>
      </div>

      {/* System Settings */}
      <div className="card">
        <div className="ws-quotas-group-title">
          {t('workspace.quotas.systemSettings')}
        </div>
        <div className="ws-quotas-grid ws-quotas-grid-mb">
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
            <div className="ws-quotas-hint">
              {t('workspace.quotas.defaultMaxTriggersDesc')}
            </div>
          </div>
        </div>
        <div className="ws-quotas-grid">
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
            <div className="ws-quotas-hint">
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
            <div className="ws-quotas-hint">
              {t('workspace.quotas.maxWebhookDesc')}
            </div>
          </div>
        </div>
      </div>

      <div className="ws-quotas-actions">
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? t('common.saving') : t('common.save')}
        </button>
        {saved && <span className="ws-quotas-saved">{t('workspace.quotas.saved')}</span>}
      </div>
    </div>
  );
}

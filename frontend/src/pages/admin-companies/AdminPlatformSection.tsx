import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { adminApi } from '../../api/domains/admin';
import { enterpriseApi } from '../../api/domains/enterprise';
import WorkspaceRuntimeBudgetsSection from '../workspace/WorkspaceRuntimeBudgetsSection';
import './AdminPlatformSection.css';

export default function AdminPlatformSection() {
  const { t } = useTranslation();

  const [settings, setSettings] = useState<any>({});
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [nbEnabled, setNbEnabled] = useState(false);
  const [nbText, setNbText] = useState('');
  const [nbSaving, setNbSaving] = useState(false);
  const [nbSaved, setNbSaved] = useState(false);
  const [publicBaseUrl, setPublicBaseUrl] = useState('');
  const [urlSaving, setUrlSaving] = useState(false);
  const [urlSaved, setUrlSaved] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);

  const showToast = (msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  useEffect(() => {
    adminApi.getPlatformSettings().then(setSettings).catch(() => {});
    enterpriseApi
      .getSetting('notification_bar')
      .then((data) => {
        if (data?.value) {
          setNbEnabled(!!data.value.enabled);
          setNbText(typeof data.value.text === 'string' ? data.value.text : '');
        }
      })
      .catch(() => {});
    enterpriseApi
      .getSetting('platform')
      .then((data) => {
        if (typeof data.value?.public_base_url === 'string') setPublicBaseUrl(data.value.public_base_url);
      })
      .catch(() => {});
  }, []);

  const handleToggleSetting = async (key: string, value: boolean) => {
    setSettingsLoading(true);
    try {
      await adminApi.updatePlatformSettings({ [key]: value });
      setSettings((current: any) => ({ ...current, [key]: value }));
      showToast('Setting updated');
    } catch (e: any) {
      showToast(e.message || 'Failed', 'error');
    }
    setSettingsLoading(false);
  };

  const saveNotificationBar = async () => {
    setNbSaving(true);
    try {
      await enterpriseApi.updateSetting('notification_bar', { enabled: nbEnabled, text: nbText });
      setNbSaved(true);
      setTimeout(() => setNbSaved(false), 2000);
    } catch {}
    setNbSaving(false);
  };

  const savePublicUrl = async () => {
    setUrlSaving(true);
    try {
      await enterpriseApi.updateSetting('platform', { public_base_url: publicBaseUrl });
      setUrlSaved(true);
      setTimeout(() => setUrlSaved(false), 2000);
    } catch {
      showToast('Failed to save', 'error');
    }
    setUrlSaving(false);
  };

  return (
    <>
      {toast && (
        <div
          className="admin-platform-toast"
          style={{ background: toast.type === 'success' ? 'var(--success)' : 'var(--error)' }}
        >
          {toast.msg}
        </div>
      )}

      <div className="card admin-platform-card">
        <div className="admin-platform-settings-list">
          {[
            {
              key: 'allow_self_create_company',
              label: t('admin.allowSelfCreate', 'Allow users to create their own companies'),
              desc: t('admin.allowSelfCreateDesc', 'When disabled, only platform admins can create companies.'),
            },
          ].map((setting) => (
            <div key={setting.key} className="admin-platform-setting-row">
              <div>
                <div className="admin-platform-setting-label">{setting.label}</div>
                <div className="admin-platform-setting-desc">{setting.desc}</div>
              </div>
              <label className={`admin-platform-switch${settingsLoading ? ' is-disabled' : ''}`}>
                <input
                  type="checkbox"
                  className="admin-platform-switch-input"
                  checked={!!settings[setting.key]}
                  onChange={(e) => handleToggleSetting(setting.key, e.target.checked)}
                  disabled={settingsLoading}
                />
                <span className="admin-platform-switch-track">
                  <span className="admin-platform-switch-thumb" />
                </span>
              </label>
            </div>
          ))}
        </div>
      </div>

      <div className="card admin-platform-card">
        <div className="admin-platform-row">
          <div>
            <div className="admin-platform-card-title">
              {t('enterprise.notificationBar.title', 'Notification Bar')}
            </div>
            <div className="admin-platform-setting-desc">
              {t('enterprise.notificationBar.description', 'Display a notification bar at the top of the page, visible to all users.')}
            </div>
          </div>
          <label className="admin-platform-switch">
            <input type="checkbox" className="admin-platform-switch-input" checked={nbEnabled} onChange={(e) => setNbEnabled(e.target.checked)} />
            <span className="admin-platform-switch-track">
              <span className="admin-platform-switch-thumb" />
            </span>
          </label>
        </div>
        <div className={`admin-platform-collapse${nbEnabled ? ' is-open' : ''}`}>
          <div className="admin-platform-collapse-body">
            <label className="form-label">{t('enterprise.notificationBar.text', 'Notification text')}</label>
            <input
              className="form-input"
              value={nbText}
              onChange={(e) => setNbText(e.target.value)}
              placeholder={t('enterprise.notificationBar.textPlaceholder', 'e.g. v2.1 released with new features!')}
            />
          </div>
          <div className="admin-platform-save-row">
            <button className="btn btn-primary" onClick={saveNotificationBar} disabled={nbSaving}>
              {nbSaving ? t('common.loading') : t('common.save', 'Save')}
            </button>
            {nbSaved && <span className="admin-platform-saved">{t('enterprise.config.saved', 'Saved')}</span>}
          </div>
        </div>
      </div>

      <div className="card admin-platform-card">
        <div className="admin-platform-card-title admin-platform-title-mb">
          {t('admin.publicUrl.title', 'Public URL')}
        </div>
        <div className="admin-platform-url-desc">
          {t('admin.publicUrl.desc', 'The external URL used for webhook callbacks (Slack, Feishu, Discord, etc.) and published page links. Include the protocol (e.g. https://example.com).')}
        </div>
        <div className="admin-platform-url-input-wrap">
          <input
            className="form-input"
            value={publicBaseUrl}
            onChange={(e) => setPublicBaseUrl(e.target.value)}
            placeholder="https://your-domain.com"
          />
        </div>
        <div className="admin-platform-save-row">
          <button className="btn btn-primary" onClick={savePublicUrl} disabled={urlSaving}>
            {urlSaving ? t('common.loading') : t('common.save', 'Save')}
          </button>
          {urlSaved && <span className="admin-platform-saved">{t('enterprise.config.saved', 'Saved')}</span>}
        </div>
      </div>

      <div className="admin-platform-runtime">
        <WorkspaceRuntimeBudgetsSection />
      </div>
    </>
  );
}

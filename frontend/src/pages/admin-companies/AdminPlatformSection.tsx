import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { adminApi } from '../../api/domains/admin';
import { enterpriseApi } from '../../api/domains/enterprise';

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

  const switchStyle = (disabled?: boolean): React.CSSProperties => ({
    position: 'relative',
    display: 'inline-block',
    width: '40px',
    height: '22px',
    cursor: disabled ? 'not-allowed' : 'pointer',
    flexShrink: 0,
  });

  const switchTrack = (checked: boolean): React.CSSProperties => ({
    position: 'absolute',
    inset: 0,
    background: checked ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
    borderRadius: '11px',
    transition: 'background 0.2s',
  });

  const switchThumb = (checked: boolean): React.CSSProperties => ({
    position: 'absolute',
    left: checked ? '20px' : '2px',
    top: '2px',
    width: '18px',
    height: '18px',
    background: '#fff',
    borderRadius: '50%',
    transition: 'left 0.2s',
  });

  return (
    <>
      {toast && (
        <div
          style={{
            position: 'fixed',
            top: '20px',
            right: '20px',
            padding: '10px 20px',
            borderRadius: '8px',
            background: toast.type === 'success' ? 'var(--success)' : 'var(--error)',
            color: '#fff',
            fontSize: '13px',
            zIndex: 9999,
            boxShadow: '0 4px 12px rgba(0,0,0,0.2)',
          }}
        >
          {toast.msg}
        </div>
      )}

      <div className="card" style={{ padding: '16px', marginBottom: '16px' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {[
            {
              key: 'allow_self_create_company',
              label: t('admin.allowSelfCreate', 'Allow users to create their own companies'),
              desc: t('admin.allowSelfCreateDesc', 'When disabled, only platform admins can create companies.'),
            },
          ].map((setting) => (
            <div key={setting.key} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 0' }}>
              <div>
                <div style={{ fontSize: '13px', fontWeight: 500 }}>{setting.label}</div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{setting.desc}</div>
              </div>
              <label style={switchStyle(settingsLoading)}>
                <input
                  type="checkbox"
                  checked={!!settings[setting.key]}
                  onChange={(e) => handleToggleSetting(setting.key, e.target.checked)}
                  disabled={settingsLoading}
                  style={{ opacity: 0, width: 0, height: 0 }}
                />
                <span style={switchTrack(!!settings[setting.key])}>
                  <span style={switchThumb(!!settings[setting.key])} />
                </span>
              </label>
            </div>
          ))}
        </div>
      </div>

      <div className="card" style={{ padding: '16px', marginBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-secondary)' }}>
              {t('enterprise.notificationBar.title', 'Notification Bar')}
            </div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
              {t('enterprise.notificationBar.description', 'Display a notification bar at the top of the page, visible to all users.')}
            </div>
          </div>
          <label style={switchStyle()}>
            <input type="checkbox" checked={nbEnabled} onChange={(e) => setNbEnabled(e.target.checked)} style={{ opacity: 0, width: 0, height: 0 }} />
            <span style={switchTrack(nbEnabled)}>
              <span style={switchThumb(nbEnabled)} />
            </span>
          </label>
        </div>
        <div
          style={{
            maxHeight: nbEnabled ? '200px' : '0',
            opacity: nbEnabled ? 1 : 0,
            overflow: 'hidden',
            transition: 'max-height 0.3s ease, opacity 0.25s ease',
          }}
        >
          <div style={{ marginBottom: '12px', paddingTop: '16px' }}>
            <label className="form-label">{t('enterprise.notificationBar.text', 'Notification text')}</label>
            <input
              className="form-input"
              value={nbText}
              onChange={(e) => setNbText(e.target.value)}
              placeholder={t('enterprise.notificationBar.textPlaceholder', 'e.g. v2.1 released with new features!')}
              style={{ fontSize: '13px' }}
            />
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <button className="btn btn-primary" onClick={saveNotificationBar} disabled={nbSaving}>
              {nbSaving ? t('common.loading') : t('common.save', 'Save')}
            </button>
            {nbSaved && <span style={{ color: 'var(--success)', fontSize: '12px' }}>{t('enterprise.config.saved', 'Saved')}</span>}
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: '16px', marginBottom: '16px' }}>
        <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '4px', color: 'var(--text-secondary)' }}>
          {t('admin.publicUrl.title', 'Public URL')}
        </div>
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('admin.publicUrl.desc', 'The external URL used for webhook callbacks (Slack, Feishu, Discord, etc.) and published page links. Include the protocol (e.g. https://example.com).')}
        </div>
        <div style={{ marginBottom: '12px' }}>
          <input
            className="form-input"
            value={publicBaseUrl}
            onChange={(e) => setPublicBaseUrl(e.target.value)}
            placeholder="https://your-domain.com"
            style={{ fontSize: '13px' }}
          />
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <button className="btn btn-primary" onClick={savePublicUrl} disabled={urlSaving}>
            {urlSaving ? t('common.loading') : t('common.save', 'Save')}
          </button>
          {urlSaved && <span style={{ color: 'var(--success)', fontSize: '12px' }}>{t('enterprise.config.saved', 'Saved')}</span>}
        </div>
      </div>
    </>
  );
}

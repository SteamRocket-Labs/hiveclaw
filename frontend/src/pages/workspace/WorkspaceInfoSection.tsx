import type { ReactNode } from 'react';

import { useTranslation } from 'react-i18next';

interface WorkspaceInfoSectionProps {
  selectedTenantId: string;
  companyNameEditor: ReactNode;
  companyTimezoneEditor: ReactNode;
  companyIntro: string;
  onCompanyIntroChange: (value: string) => void;
  onSaveCompanyIntro: () => void;
  companyIntroSaving: boolean;
  companyIntroSaved: boolean;
  kbBrowser: ReactNode;
  themeColorPicker: ReactNode;
  broadcastSection: ReactNode;
  onDeleteCompany: () => void;
}

export default function WorkspaceInfoSection({
  selectedTenantId,
  companyNameEditor,
  companyTimezoneEditor,
  companyIntro,
  onCompanyIntroChange,
  onSaveCompanyIntro,
  companyIntroSaving,
  companyIntroSaved,
  kbBrowser,
  themeColorPicker,
  broadcastSection,
  onDeleteCompany,
}: WorkspaceInfoSectionProps) {
  const { t } = useTranslation();

  return (
    <div>
      <h3 style={{ marginBottom: '8px' }}>{t('enterprise.companyName.title', 'Company Name')}</h3>
      <div key={`name-${selectedTenantId}`}>{companyNameEditor}</div>

      <div key={`tz-${selectedTenantId}`}>{companyTimezoneEditor}</div>

      <h3 style={{ marginBottom: '8px' }}>{t('enterprise.companyIntro.title', 'Company Intro')}</h3>
      <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
        {t('enterprise.companyIntro.description', 'Describe your company\'s mission, products, and culture. This information is included in every agent conversation as context.')}
      </p>
      <div className="card" style={{ padding: '16px', marginBottom: '24px' }}>
        <textarea
          className="form-input"
          value={companyIntro}
          onChange={(event) => onCompanyIntroChange(event.target.value)}
          placeholder={`# Company Name\nHiveClaw\n\n# About\nOpenClaw\uD83E\uDD9E For Teams\nOpen Source \u00B7 Multi-OpenClaw Collaboration\n\nOpenClaw empowers individuals.\nHiveClaw scales it to frontier organizations.`}
          style={{
            minHeight: '200px',
            resize: 'vertical',
            fontFamily: 'var(--font-mono)',
            fontSize: '13px',
            lineHeight: '1.6',
            whiteSpace: 'pre-wrap',
          }}
        />
        <div style={{ marginTop: '12px', display: 'flex', gap: '8px', alignItems: 'center' }}>
          <button className="btn btn-primary" onClick={onSaveCompanyIntro} disabled={companyIntroSaving}>
            {companyIntroSaving ? t('common.loading') : t('common.save', 'Save')}
          </button>
          {companyIntroSaved ? <span style={{ color: 'var(--success)', fontSize: '12px' }}>✅ {t('enterprise.config.saved', 'Saved')}</span> : null}
          <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>
            💡 {t('enterprise.companyIntro.hint', 'This content appears in every agent\'s system prompt')}
          </span>
        </div>
      </div>

      <h3 style={{ marginBottom: '8px' }}>{t('enterprise.kb.title')}</h3>
      <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
        {t('enterprise.kb.description', 'Shared files accessible to all agents via enterprise_info/ directory.')}
      </p>
      <div className="card" style={{ marginBottom: '24px', padding: '16px' }}>
        {kbBrowser}
      </div>

      {themeColorPicker}
      {broadcastSection}

      <div style={{ marginTop: '32px', padding: '16px', border: '1px solid var(--status-error, #e53e3e)', borderRadius: '8px' }}>
        <h3 style={{ marginBottom: '4px', color: 'var(--status-error, #e53e3e)' }}>{t('enterprise.dangerZone', 'Danger Zone')}</h3>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('enterprise.deleteCompanyDesc', 'Permanently delete this company and all its data including agents, models, tools, and skills. This action cannot be undone.')}
        </p>
        <button
          className="btn"
          onClick={onDeleteCompany}
          style={{
            background: 'transparent',
            color: 'var(--status-error, #e53e3e)',
            border: '1px solid var(--status-error, #e53e3e)',
            borderRadius: '6px',
            padding: '6px 16px',
            fontSize: '13px',
            cursor: 'pointer',
          }}
        >
          {t('enterprise.deleteCompany', 'Delete This Company')}
        </button>
      </div>
    </div>
  );
}

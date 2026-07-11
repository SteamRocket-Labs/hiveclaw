import type { ReactNode } from 'react';

import { useTranslation } from 'react-i18next';

import './WorkspaceInfoSection.css';

interface WorkspaceInfoSectionProps {
  selectedTenantId: string;
  companyNameEditor: ReactNode;
  companyTimezoneEditor: ReactNode;
  companyIntro: string;
  onCompanyIntroChange: (value: string) => void;
  onSaveCompanyIntro: () => void;
  companyIntroSaving: boolean;
  companyIntroSaved: boolean;
  legacyCompanyFilesCard: ReactNode;
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
  legacyCompanyFilesCard,
  themeColorPicker,
  broadcastSection,
  onDeleteCompany,
}: WorkspaceInfoSectionProps) {
  const { t } = useTranslation();

  return (
    <div>
      <h3 className="ws-info-heading">{t('enterprise.companyName.title', 'Company Name')}</h3>
      <div key={`name-${selectedTenantId}`}>{companyNameEditor}</div>

      <div key={`tz-${selectedTenantId}`}>{companyTimezoneEditor}</div>

      <h3 className="ws-info-heading">{t('enterprise.companyIntro.title', 'Company Intro')}</h3>
      <p className="ws-info-desc">
        {t('enterprise.companyIntro.description', 'Describe your company\'s mission, products, and culture. This information is included in every agent conversation as context.')}
      </p>
      <div className="card ws-info-card">
        <textarea
          className="form-input ws-info-textarea"
          value={companyIntro}
          onChange={(event) => onCompanyIntroChange(event.target.value)}
          placeholder={`# Company Name\nHiveClaw\n\n# About\nAI agents for teams\nOpen Source · Multi-agent collaboration\n\nHive helps individuals and teams operate digital employees at company scale.`}
        />
        <div className="ws-info-actions">
          <button className="btn btn-primary" onClick={onSaveCompanyIntro} disabled={companyIntroSaving}>
            {companyIntroSaving ? t('common.loading') : t('common.save', 'Save')}
          </button>
          {companyIntroSaved ? <span className="ws-info-saved">✅ {t('enterprise.config.saved', 'Saved')}</span> : null}
          <span className="ws-info-hint">
            💡 {t('enterprise.companyIntro.hint', 'This content appears in every agent\'s system prompt')}
          </span>
        </div>
      </div>

      {legacyCompanyFilesCard}

      {themeColorPicker}
      {broadcastSection}

      <div className="ws-info-danger">
        <h3 className="ws-info-danger-title">{t('enterprise.dangerZone', 'Danger Zone')}</h3>
        <p className="ws-info-desc">
          {t('enterprise.deleteCompanyDesc', 'Permanently delete this company and all its data including agents, models, tools, and skills. This action cannot be undone.')}
        </p>
        <button
          className="btn ws-info-delete-btn"
          onClick={onDeleteCompany}
        >
          {t('enterprise.deleteCompany', 'Delete This Company')}
        </button>
      </div>
    </div>
  );
}

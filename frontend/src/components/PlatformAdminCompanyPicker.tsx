/**
 * Platform-administrator company picker — the plain, actionable recovery when
 * a platform admin reaches a company-scoped surface without a valid selected
 * company. Selecting a company writes the canonical `current_tenant_id` key
 * (the same channel the sidebar selector uses) and notifies the shell; the
 * server still validates the selection on every request.
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { adminApi, isActiveCompany, type Company } from '../api/domains/admin';

interface PlatformAdminCompanyPickerProps {
  onSelected: (tenantId: string) => void;
}

export default function PlatformAdminCompanyPicker({ onSelected }: PlatformAdminCompanyPickerProps) {
  const { t } = useTranslation();
  const [companies, setCompanies] = useState<Company[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [selected, setSelected] = useState('');

  const load = () => {
    setLoadFailed(false);
    adminApi.listCompanies()
      .then((rows) => setCompanies(rows.filter(isActiveCompany)))
      .catch(() => {
        setCompanies(null);
        setLoadFailed(true);
      });
  };

  useEffect(load, []);

  const confirm = () => {
    if (!selected) return;
    localStorage.setItem('current_tenant_id', selected);
    window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: selected }));
    onSelected(selected);
  };

  return (
    <div className="platform-company-picker" role="group" aria-label={t('companyPicker.title', 'Select a company first')}>
      <h2>{t('companyPicker.title', 'Select a company first')}</h2>
      <p>
        {t(
          'companyPicker.description',
          'Platform administrators act inside one selected company. Choose the company whose employees and data you want to manage; nothing is selected automatically.',
        )}
      </p>
      {loadFailed ? (
        <div className="workbench-error" role="alert">
          {t('companyPicker.loadFailed', 'Could not load companies. ')}
          <button type="button" className="btn btn-secondary" onClick={load}>
            {t('common.retry', 'Retry')}
          </button>
        </div>
      ) : companies === null ? (
        <div>{t('common.loading', 'Loading...')}</div>
      ) : companies.length === 0 ? (
        <div>{t('companyPicker.empty', 'No active company is available for this account.')}</div>
      ) : (
        <div>
          <select
            className="form-input"
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
            aria-label={t('companyPicker.selectLabel', 'Company')}
          >
            <option value="">{t('companyPicker.placeholder', 'Choose a company…')}</option>
            {companies.map((company) => (
              <option key={company.id} value={company.id}>{company.name}</option>
            ))}
          </select>
          <button type="button" className="btn btn-primary" onClick={confirm} disabled={!selected}>
            {t('companyPicker.confirm', 'Continue with this company')}
          </button>
        </div>
      )}
    </div>
  );
}

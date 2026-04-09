import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useAuthStore } from '../stores';
import PlatformDashboard from './PlatformDashboard';
import AdminCompaniesSection from './admin-companies/AdminCompaniesSection';
import AdminPlatformSection from './admin-companies/AdminPlatformSection';

export default function AdminCompanies() {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const [activeTab, setActiveTab] = useState<'dashboard' | 'platform' | 'companies'>('dashboard');

  if (user?.role !== 'platform_admin') {
    return (
      <div style={{ padding: '60px', textAlign: 'center', color: 'var(--text-tertiary)' }}>
        {t('common.noPermission', 'You do not have permission to access this page.')}
      </div>
    );
  }

  const tabs = [
    { key: 'dashboard' as const, label: t('admin.tab.dashboard', 'Dashboard') },
    { key: 'platform' as const, label: t('admin.tab.platform', 'Platform') },
    { key: 'companies' as const, label: t('admin.tab.companies', 'Companies') },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 64px)' }}>
      <div className="page-header">
        <div>
          <h1 className="page-title">{t('admin.platformSettings', 'Platform Settings')}</h1>
          <p className="page-subtitle">{t('admin.platformSettingsDesc', 'Manage platform-wide settings and company tenants.')}</p>
        </div>
      </div>

      <div className="tabs">
        {tabs.map((tab) => (
          <div key={tab.key} className={`tab ${activeTab === tab.key ? 'active' : ''}`} onClick={() => setActiveTab(tab.key)}>
            {tab.label}
          </div>
        ))}
      </div>

      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {activeTab === 'dashboard' && <PlatformDashboard />}
        {activeTab === 'platform' && <AdminPlatformSection />}
        {activeTab === 'companies' && <AdminCompaniesSection />}
      </div>
    </div>
  );
}

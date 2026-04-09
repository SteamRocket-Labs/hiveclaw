import type { ReactNode } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuthStore } from '../../stores';

export interface SurfaceNavItem {
  to: string;
  labelKey: string;
  fallbackLabel: string;
  icon?: ReactNode;
  end?: boolean;
}

interface SurfaceLayoutProps {
  headingKey: string;
  headingFallback: string;
  navItems: SurfaceNavItem[];
}

export default function SurfaceLayout({
  headingKey,
  headingFallback,
  navItems,
}: SurfaceLayoutProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);

  return (
    <div className="app-layout">
      <nav className="sidebar">
        <div className="sidebar-top">
          <div className="sidebar-logo">
            <span className="sidebar-logo-text" style={{ display: 'inline-flex', opacity: 1 }}>
              HiveClaw
            </span>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-section-title">
              {t(headingKey, headingFallback)}
            </div>

            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}
                title={t(item.labelKey, item.fallbackLabel)}
              >
                {item.icon ? (
                  <span className="sidebar-item-icon" style={{ display: 'flex' }}>
                    {item.icon}
                  </span>
                ) : null}
                <span className="sidebar-item-text">
                  {t(item.labelKey, item.fallbackLabel)}
                </span>
              </NavLink>
            ))}
          </div>
        </div>

        <div className="sidebar-bottom">
          <div className="sidebar-section" style={{ borderBottom: '1px solid var(--border-subtle)', paddingBottom: '8px', marginBottom: 0 }}>
            <NavLink
              to="/dashboard"
              className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}
              title={t('nav.dashboard')}
            >
              <span className="sidebar-item-text">{t('nav.dashboard')}</span>
            </NavLink>
          </div>

          <div className="sidebar-footer">
            <div
              className="sidebar-account-row"
              role="button"
              tabIndex={0}
              onClick={() => {
                logout();
                navigate('/login');
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  logout();
                  navigate('/login');
                }
              }}
            >
              <div className="sidebar-footer-user-info" style={{ minWidth: 0 }}>
                <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {user?.display_name || user?.username || 'User'}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                  {t('nav.logout')}
                </div>
              </div>
            </div>
          </div>
        </div>
      </nav>

      <main className="main-content">
        <Outlet />
      </main>
    </div>
  );
}

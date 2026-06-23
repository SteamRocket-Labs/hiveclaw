import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'zh', changeLanguage: vi.fn() },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('../stores', () => ({
  useAuthStore: () => ({
    user: { id: 'user-1', tenant_id: null, role: 'member' },
    setAuth: vi.fn(),
    token: 'token',
  }),
}));

vi.mock('../api/domains/auth', () => ({
  authApi: {
    getMe: vi.fn(),
  },
}));

vi.mock('../api/domains/system', () => ({
  systemApi: {
    getRegistrationConfig: vi.fn(),
    joinTenant: vi.fn(),
    createTenant: vi.fn(),
  },
}));

import CompanySetup from './CompanySetup';

describe('CompanySetup design surface', () => {
  it('uses the same CC design auth-flow shell as Login', () => {
    const markup = renderToStaticMarkup(<CompanySetup />);

    expect(markup).toContain('login-page');
    expect(markup).toContain('login-brand-panel');
    expect(markup).toContain('login-auth-surface');
    expect(markup).toContain('company-setup-card');
    expect(markup).toContain('company-choice-card');
    expect(markup).not.toContain('company-setup-panels');
    expect(markup).not.toContain('company-setup-container');
  });
});

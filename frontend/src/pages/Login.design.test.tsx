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
  useSearchParams: () => [new URLSearchParams()],
}));

vi.mock('../stores', () => ({
  useAuthStore: (selector?: any) => {
    const state = { setAuth: vi.fn() };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('../api/domains/auth', () => ({
  authApi: {
    login: vi.fn(),
    register: vi.fn(),
    feishuSsoInit: vi.fn(),
    feishuSsoPoll: vi.fn(),
  },
}));

vi.mock('../components/AppDialogs', () => ({
  showAppToast: vi.fn(),
}));

import Login from './Login';

describe('Login design surface', () => {
  it('uses the CC design auth-flow layout instead of the old marketing hero', () => {
    const markup = renderToStaticMarkup(<Login />);

    expect(markup).toContain('login-brand-panel');
    expect(markup).toContain('login-mode-switch');
    expect(markup).toContain('login-auth-card');
    expect(markup).toContain('先计划后执行');
    expect(markup).toContain('A2A 协作');
    expect(markup).toContain('企业级治理');
    expect(markup).not.toContain('login-hero-features');
    expect(markup).not.toContain('login-hero-feature-icon');
  });
});

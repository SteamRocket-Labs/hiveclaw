// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import CompanySetup from './CompanySetup';

const mocks = vi.hoisted(() => ({
  getMe: vi.fn(),
  getRegistrationConfig: vi.fn(),
  joinTenant: vi.fn(),
  navigate: vi.fn(),
  setAuth: vi.fn(),
  user: {
    id: 'user-1',
    username: 'invitee',
    email: 'invitee@example.com',
    display_name: 'Invitee',
    role: 'member',
    tenant_id: undefined,
    is_active: true,
  },
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => mocks.navigate,
}));

vi.mock('../stores', () => ({
  useAuthStore: Object.assign(
    () => ({ user: mocks.user, setAuth: mocks.setAuth }),
    { getState: () => ({ token: 'old-token' }) },
  ),
}));

vi.mock('../api/domains/auth', () => ({
  authApi: { getMe: mocks.getMe },
}));

vi.mock('../api/domains/system', () => ({
  systemApi: {
    createTenant: vi.fn(),
    getRegistrationConfig: mocks.getRegistrationConfig,
    joinTenant: mocks.joinTenant,
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    i18n: { language: 'en', changeLanguage: vi.fn() },
    t: (_key: string, fallback?: string) => fallback || '',
  }),
}));

vi.mock('./auth/AuthShell', () => ({
  AuthShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe.each([
  ['administrator', 'org_admin', 'ADMIN-CODE'],
  ['member', 'member', 'MEMBER-CODE'],
] as const)('CompanySetup %s invitation auth refresh', (_label, role, code) => {
  beforeEach(() => {
    mocks.getMe.mockReset();
    mocks.getRegistrationConfig.mockReset();
    mocks.joinTenant.mockReset();
    mocks.navigate.mockReset();
    mocks.setAuth.mockReset();
    mocks.getRegistrationConfig.mockResolvedValue({ allow_self_create_company: false });
    mocks.joinTenant.mockResolvedValue({
      tenant: { id: 'tenant-1', name: 'Acme', slug: 'acme', im_provider: 'web_only', is_active: true },
      role,
      access_token: `${role}-token`,
    });
  });

  afterEach(cleanup);

  it('installs the returned tenant role and access token before entering the app', async () => {
    render(<CompanySetup />);
    fireEvent.change(screen.getByRole('textbox', { name: 'Invitation Code' }), {
      target: { value: code.toLowerCase() },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Join Company' }));

    await waitFor(() => expect(mocks.joinTenant).toHaveBeenCalledWith({ invitation_code: code }));
    expect(mocks.setAuth).toHaveBeenCalledWith(
      expect.objectContaining({ tenant_id: 'tenant-1', role }),
      `${role}-token`,
    );
    expect(mocks.navigate).toHaveBeenCalledWith('/');
  });
});

describe('CompanySetup invitation recovery', () => {
  beforeEach(() => {
    mocks.getRegistrationConfig.mockReset();
    mocks.joinTenant.mockReset();
    mocks.navigate.mockReset();
    mocks.setAuth.mockReset();
    mocks.getRegistrationConfig.mockResolvedValue({ allow_self_create_company: false });
  });

  afterEach(cleanup);

  it('announces a failed join and restores the submit action', async () => {
    mocks.joinTenant.mockRejectedValue(new Error('Invitation code has reached its usage limit'));
    render(<CompanySetup />);
    fireEvent.change(screen.getByRole('textbox', { name: 'Invitation Code' }), {
      target: { value: 'USED-CODE' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Join Company' }));

    expect((await screen.findByRole('alert')).textContent).toContain('Invitation code has reached its usage limit');
    expect((screen.getByRole('button', { name: 'Join Company' }) as HTMLButtonElement).disabled).toBe(false);
    expect(mocks.setAuth).not.toHaveBeenCalled();
    expect(mocks.navigate).not.toHaveBeenCalled();
  });
});

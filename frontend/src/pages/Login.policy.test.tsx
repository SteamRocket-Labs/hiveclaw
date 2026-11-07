// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { translateFromCatalog } from '../test/i18nMock';
import en from '../i18n/en.json';

const { api, setAuth } = vi.hoisted(() => ({
  api: { getRegistrationConfig: vi.fn(), getOidcConfig: vi.fn(), register: vi.fn(), login: vi.fn() },
  setAuth: vi.fn(),
}));
vi.mock('../api/domains/auth', () => ({ authApi: api, feishuConflictMessage: vi.fn() }));
vi.mock('../stores', () => ({ useAuthStore: (selector: any) => selector({ setAuth }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: any, options?: any) => translateFromCatalog(en, key, fallback, options),
  i18n: { language: 'en', changeLanguage: vi.fn() },
}) }));
import Login from './Login';

beforeEach(() => {
  vi.clearAllMocks();
  api.getRegistrationConfig.mockResolvedValue({ registration_enabled: true, install_token_required: false });
  api.getOidcConfig.mockResolvedValue({ configured: false });
});
afterEach(cleanup);

function mount(path = '/login') {
  return render(<MemoryRouter initialEntries={[path]}><Login /></MemoryRouter>);
}

describe('Login policy', () => {
  it('shows configured company SSO and discovers by explicit tenant slug', async () => {
    api.getOidcConfig.mockResolvedValue({ configured: true, display_name: 'Company sign-in' });
    mount('/login?tenant_slug=acme');
    expect(await screen.findByRole('button', { name: 'Company sign-in' })).toBeTruthy();
    expect(api.getOidcConfig).toHaveBeenCalledWith('acme');
  });

  it('omits company SSO when unconfigured and uses the default company without a slug', async () => {
    mount();
    await waitFor(() => expect(api.getOidcConfig).toHaveBeenCalledWith(undefined));
    expect(screen.queryByRole('button', { name: 'Company sign-in' })).toBeNull();
  });

  it('hides both registration entries when registration is closed', async () => {
    api.getRegistrationConfig.mockResolvedValue({ registration_enabled: false, install_token_required: false });
    mount();
    await waitFor(() => expect(api.getRegistrationConfig).toHaveBeenCalled());
    expect(screen.queryByRole('tab', { name: 'Create Account' })).toBeNull();
    expect(screen.queryByText('Create Account')).toBeNull();
    expect(screen.getByRole('button', { name: /Sign In/ })).toBeTruthy();
  });

  it('shows and posts an install token only for the first account', async () => {
    api.getRegistrationConfig.mockResolvedValue({ registration_enabled: true, install_token_required: true });
    api.register.mockRejectedValue(new Error('A valid install token is required for the first administrator.'));
    const { container } = mount();
    fireEvent.click(await screen.findByRole('tab', { name: 'Create Account' }));
    fireEvent.change(screen.getByLabelText('Install token'), { target: { value: 'local-token' } });
    const inputs = container.querySelectorAll('input');
    fireEvent.change(inputs[0], { target: { value: 'new-member' } });
    fireEvent.change(container.querySelector('input[type=email]')!, { target: { value: 'new@example.com' } });
    fireEvent.change(container.querySelector('input[type=password]')!, { target: { value: 'password123' } });
    fireEvent.submit(container.querySelector('form')!);
    await waitFor(() => expect(api.register).toHaveBeenCalledWith(expect.objectContaining({ install_token: 'local-token' })));
    expect(await screen.findByText('A valid install token is required for the first administrator.')).toBeTruthy();
  });

  it('omits the install-token field after an account exists', async () => {
    mount();
    fireEvent.click(await screen.findByRole('tab', { name: 'Create Account' }));
    expect(screen.queryByLabelText('Install token')).toBeNull();
  });
});

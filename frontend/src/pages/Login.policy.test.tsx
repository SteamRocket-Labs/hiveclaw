// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { translateFromCatalog } from '../test/i18nMock';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import { ApiError } from '../api/core/errors';

const { api, setAuth, language } = vi.hoisted(() => ({
  api: { getRegistrationConfig: vi.fn(), getOidcConfig: vi.fn(), register: vi.fn(), login: vi.fn() },
  setAuth: vi.fn(),
  language: { value: 'en' },
}));
vi.mock('../api/domains/auth', () => ({ authApi: api, feishuConflictMessage: vi.fn() }));
vi.mock('../stores', () => ({ useAuthStore: (selector: any) => selector({ setAuth }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: any, options?: any) => translateFromCatalog(language.value === 'zh' ? zh : en, key, fallback, options),
  i18n: { language: language.value, changeLanguage: vi.fn() },
}) }));
import Login from './Login';

beforeEach(() => {
  vi.clearAllMocks();
  language.value = 'en';
  api.getRegistrationConfig.mockResolvedValue({ registration_enabled: true, install_token_required: false });
  api.getOidcConfig.mockResolvedValue({ configured: false });
});
afterEach(cleanup);

function mount(path = '/login') {
  return render(<MemoryRouter initialEntries={[path]}><Login /></MemoryRouter>);
}

describe('Login policy', () => {
  it.each([
    [new ApiError(0, '网络连接失败，请检查网络后重试。'), '无法连接服务器，请检查网络连接后重试。'],
    [new TypeError('A localized network failure'), '无法连接服务器，请检查网络连接后重试。'],
    [new ApiError(503, '服务暂时没有响应，请稍后再试。', { parseError: 'Unexpected HTML response' }), '服务正在启动中，请稍后重试。'],
    [new ApiError(502, '服务暂时没有响应，请稍后再试。'), '服务正在启动中，请稍后重试。'],
    [new ApiError(503, '账号服务维护中，请联系值班人员。', '账号服务维护中，请联系值班人员。'), '账号服务维护中，请联系值班人员。'],
  ])('keeps login-specific recovery copy by error type and status: %s', async (error, expected) => {
    language.value = 'zh';
    api.login.mockRejectedValue(error);
    const { container } = mount();
    fireEvent.change(container.querySelector('form input')!, { target: { value: 'member' } });
    fireEvent.change(container.querySelector('input[type=password]')!, { target: { value: 'local-password' } });
    fireEvent.submit(container.querySelector('form')!);
    expect(await screen.findByText(expected)).toBeTruthy();
    expect(setAuth).not.toHaveBeenCalled();
  });

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

  it('offers first-admin registration and the install-token field on a closed empty installation', async () => {
    api.getRegistrationConfig.mockResolvedValue({ registration_enabled: false, install_token_required: true });
    mount();
    expect(await screen.findByRole('link', { name: 'Create Account' })).toBeTruthy();
    fireEvent.click(await screen.findByRole('tab', { name: 'Create Account' }));
    expect(screen.getByLabelText('Install token')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Create Account/ })).toBeTruthy();
  });

  it.each([
    ['Public registration is closed. Sign in with an existing account or SSO.', '公开注册已关闭，请使用已有账号或公司登录。'],
    ['A valid install token is required for the first administrator.', '创建首位管理员需要有效的安装口令。'],
    ['Set INSTALL_TOKEN on the server before creating the first administrator.', '首次管理员设置尚未就绪，请联系安装人员。'],
  ])('translates the registration refusal in Chinese: %s', async (serverMessage, expected) => {
    language.value = 'zh';
    api.register.mockRejectedValue(new Error(serverMessage));
    const { container } = mount();
    fireEvent.click(await screen.findByRole('tab', { name: '注册账号' }));
    fireEvent.change(container.querySelector('form input')!, { target: { value: 'new-member' } });
    fireEvent.change(container.querySelector('input[type=email]')!, { target: { value: 'new@example.com' } });
    fireEvent.change(container.querySelector('input[type=password]')!, { target: { value: 'password123' } });
    fireEvent.submit(container.querySelector('form')!);
    expect(await screen.findByText(expected)).toBeTruthy();
  });
});

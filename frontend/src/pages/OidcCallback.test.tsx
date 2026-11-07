// @vitest-environment jsdom
import { StrictMode } from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { translateFromCatalog } from '../test/i18nMock';
import en from '../i18n/en.json';
import { OIDC_ATTEMPT_KEY, prepareOidcLogin } from '../routing/oidcLogin';
import type { OidcConfig } from '../api/domains/auth';

const { callback, setAuth } = vi.hoisted(() => ({ callback: vi.fn(), setAuth: vi.fn() }));
vi.mock('../api/domains/auth', () => ({ authApi: { oidcCallback: callback } }));
vi.mock('../stores', () => ({ useAuthStore: (selector: any) => selector({ setAuth }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: any, options?: any) => translateFromCatalog(en, key, fallback, options),
  i18n: { language: 'en', changeLanguage: vi.fn() },
}) }));
import OidcCallback from './OidcCallback';

const config: OidcConfig = { configured: true, authorization_endpoint: 'http://localhost/authorize',
  client_id: 'public-client', scopes: 'openid profile email', display_name: 'Company SSO', tenant_id: 'tenant-id' };
function attempt() {
  const url = new URL(prepareOidcLogin(config, '/projects', '/login?tenant_slug=acme'));
  return { url, stored: JSON.parse(sessionStorage.getItem(OIDC_ATTEMPT_KEY)!) };
}
function mount(query: string) {
  return render(<StrictMode><MemoryRouter initialEntries={[`/auth/oidc/callback${query}`]}><Routes>
    <Route path="/auth/oidc/callback" element={<OidcCallback />} />
    <Route path="/projects" element={<p>Signed in product</p>} />
  </Routes></MemoryRouter></StrictMode>);
}
beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear(); });
afterEach(cleanup);

describe('OIDC browser attempt', () => {
  it('uses independent cryptographic state and nonce for each attempt', () => {
    const random = vi.spyOn(crypto, 'getRandomValues');
    const first = attempt();
    const second = attempt();
    expect(random).toHaveBeenCalledTimes(4);
    for (const key of ['state', 'nonce']) {
      expect(first.url.searchParams.get(key)).toBe(first.stored[key]);
      expect(first.stored[key]).toMatch(/^[a-f0-9]{64}$/);
      expect(first.stored[key]).not.toBe(second.stored[key]);
    }
    expect(first.stored.state).not.toBe(first.stored.nonce);
    expect(first.url.searchParams.get('response_type')).toBe('code');
    expect(first.url.searchParams.get('redirect_uri')).toBe(`${window.location.origin}/auth/oidc/callback`);
    random.mockRestore();
  });

  it.each(['', '?code=code', '?code=code&state=wrong'])('rejects absent or different state without a POST: %s', async query => {
    attempt();
    mount(query);
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', en.auth.oidc.stateMismatch);
    expect(callback).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(OIDC_ATTEMPT_KEY)).toBeNull();
    expect(screen.getByRole('link', { name: 'Sign In' }).getAttribute('href')).toBe('/login?tenant_slug=acme');
  });

  it('rejects a callback when no attempt was stored', async () => {
    mount('?code=code&state=unissued');
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(callback).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(OIDC_ATTEMPT_KEY)).toBeNull();
  });

  it('posts the browser nonce exactly once and clears both values on success', async () => {
    const { stored } = attempt();
    const user = { id: 'member-id' };
    callback.mockResolvedValue({ access_token: 'body-token', user });
    mount(`?code=provider-code&state=${stored.state}`);
    expect(await screen.findByText('Signed in product')).toBeTruthy();
    expect(callback).toHaveBeenCalledTimes(1);
    expect(callback).toHaveBeenCalledWith({ code: 'provider-code', nonce: stored.nonce,
      redirect_uri: stored.redirect_uri, tenant_id: config.tenant_id });
    expect(setAuth).toHaveBeenCalledWith(user, 'body-token');
    expect(sessionStorage.getItem(OIDC_ATTEMPT_KEY)).toBeNull();
  });

  it('translates the known backend refusal, consumes the attempt, and offers login', async () => {
    const { stored } = attempt();
    callback.mockRejectedValue(new Error('OIDC authentication failed'));
    mount(`?code=altered-token&state=${stored.state}`);
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', en.auth.oidc.failed);
    expect(callback).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(OIDC_ATTEMPT_KEY)).toBeNull();
    expect(screen.getByRole('link', { name: 'Sign In' })).toBeTruthy();
  });

  it('consumes a provider-declined attempt without posting', async () => {
    const { stored } = attempt();
    mount(`?error=access_denied&state=${stored.state}`);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(en.auth.oidc.failed));
    expect(callback).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(OIDC_ATTEMPT_KEY)).toBeNull();
  });
});

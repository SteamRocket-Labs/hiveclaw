// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';
import McpOAuthSettings from './McpOAuthSettings';

const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });
const registration = { authorization_endpoint: 'https://auth.test/authorize', token_endpoint: 'https://auth.test/token',
  client_id: 'client', redirect_uri: 'https://hive.test/api/enterprise/mcp/oauth/callback', scope: 'documents:read' };
const stored = { server_id: 'server', auth_status: 'configured', attempt: null, registration, client_secret_configured: true, authorized_agents: [{ id: 'employee', name: 'Existing research employee' }] };
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

describe('shared MCP OAuth editor', () => {
  it('uses the real adapter, keeps the secret out of storage, and requires explicit status readback', async () => {
    const posts: unknown[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      if (init?.method === 'POST') { posts.push(JSON.parse(String(init.body))); return response({ authorization_url: 'https://auth.test/authorize?state=random', attempt_id: 'attempt', expires_at: 2e9 }); }
      return response(stored);
    });
    const reviewed = vi.fn();
    render(<McpOAuthSettings serverId="server" onReviewed={reviewed} />);
    await waitFor(() => expect((screen.getByLabelText('Client ID') as HTMLInputElement).value).toBe('client'));
    expect(screen.getByText('Existing research employee')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Client secret (if required)'), { target: { value: 'private-client-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create authorization link' }));
    const link = await screen.findByRole('link', { name: 'Authorize with the application' });
    expect(link.getAttribute('rel')).toContain('noreferrer');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(posts).toEqual([{ ...registration, client_secret: 'private-client-secret' }]);
    expect((screen.getByLabelText('Client secret (if required)') as HTMLInputElement).value).toBe('');
    expect(Object.values(localStorage)).not.toContain('private-client-secret');
    expect(reviewed).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Read authorization status' }));
    await waitFor(() => expect(reviewed).toHaveBeenCalledTimes(1));
  });

  it('preserves the draft on failure and refuses a non-HTTP authorization link', async () => {
    let attempt = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      if (init?.method !== 'POST') return response(stored);
      attempt += 1;
      return attempt === 1 ? response({ detail: { code: 'oauth_encryption_unavailable', message: 'Storage unavailable' } }, 503)
        : response({ authorization_url: 'javascript:alert(1)', attempt_id: 'unsafe', expires_at: 2e9 });
    });
    render(<McpOAuthSettings serverId="server" />);
    await waitFor(() => expect((screen.getByLabelText('Client ID') as HTMLInputElement).value).toBe('client'));
    fireEvent.change(screen.getByLabelText('Requested scopes (optional)'), { target: { value: 'documents:read documents:write' } });
    fireEvent.change(screen.getByLabelText('Client secret (if required)'), { target: { value: 'private-draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create authorization link' }));
    expect((await screen.findByRole('alert')).textContent).toContain('platform administrator');
    expect((screen.getByLabelText('Requested scopes (optional)') as HTMLInputElement).value).toBe('documents:read documents:write');
    expect((screen.getByLabelText('Client secret (if required)') as HTMLInputElement).value).toBe('private-draft');
    fireEvent.click(screen.getByRole('button', { name: 'Create authorization link' }));
    await waitFor(() => expect(attempt).toBe(2));
    await screen.findByRole('alert');
    expect(screen.queryByRole('link')).toBeNull();
  });
});

it('uses owner-account OAuth routes without exposing or submitting application registration fields', async () => {
  const calls: string[] = []; const posts: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    calls.push(String(url));
    if (init?.method === 'POST') { posts.push(init.body ? JSON.parse(String(init.body)) : null); return response({ authorization_url: 'https://auth.test/authorize?state=personal', attempt_id: 'personal' }); }
    return response({ auth_status: 'none', authorization_method: 'oauth' });
  });
  render(<McpOAuthSettings serverId="personal" personal />);
  await waitFor(() => expect((screen.getByRole('button', { name: 'Create authorization link' }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByLabelText('Client ID')).toBeNull();
  expect(screen.queryByLabelText('Token endpoint')).toBeNull();
  expect(screen.queryByLabelText('Client secret (if required)')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Create authorization link' }));
  await screen.findByRole('link', { name: 'Authorize with the application' });
  expect(calls).toEqual(['/api/mcp-accounts/personal/oauth', '/api/mcp-accounts/personal/oauth/start']);
  expect(posts).toEqual([null]);
});

it('stores a personal token through the secure form, clears it, and reports completion without OAuth', async () => {
  const posts: unknown[] = []; const reviewed = vi.fn(); const open = vi.spyOn(window, 'open');
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    if (init?.method === 'PUT') { expect(String(_url)).toBe('/api/mcp-accounts/personal/token'); posts.push(JSON.parse(String(init.body))); return response({ auth_status: 'configured' }); }
    return response({ auth_status: 'none', authorization_method: 'token' });
  });
  render(<McpOAuthSettings serverId="personal" personal inline onReviewed={reviewed} />);
  const field = await screen.findByLabelText('Access token (stored securely and hidden from colleagues)');
  expect(screen.queryByRole('status')).toBeNull();
  expect((field as HTMLInputElement).type).toBe('password');
  fireEvent.change(field, { target: { value: 'personal-token' } });
  fireEvent.click(screen.getByRole('button', { name: 'Connect my account' }));
  await waitFor(() => expect(reviewed).toHaveBeenCalledTimes(1));
  expect(posts).toEqual([{ access_token: 'personal-token' }]);
  expect((field as HTMLInputElement).value).toBe('');
  expect(document.body.textContent).not.toContain('personal-token');
  expect(Object.values(localStorage)).not.toContain('personal-token');
  expect(open).not.toHaveBeenCalled();
});

it('opens OAuth without an opener and needs no inline callback script or BroadcastChannel', async () => {
  // S1: production script-src self blocks the old inline notification.
  const open = vi.spyOn(window, 'open').mockReturnValue(null);
  vi.stubGlobal('BroadcastChannel', undefined);
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => init?.method === 'POST'
    ? response({ authorization_url: 'https://auth.test/authorize', attempt_id: 'attempt' })
    : response({ auth_status: 'none', authorization_method: 'oauth' }));
  render(<McpOAuthSettings serverId="personal" personal inline />);
  await waitFor(() => expect((screen.getByRole('button', { name: 'Connect my account' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Connect my account' }));
  await waitFor(() => expect(open).toHaveBeenCalledWith('https://auth.test/authorize', '_blank', 'noopener,noreferrer'));
});

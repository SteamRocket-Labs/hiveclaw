// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';
import McpOAuthSettings from './McpOAuthSettings';

const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });
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

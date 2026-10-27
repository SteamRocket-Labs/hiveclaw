/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import InlineChannelConfigurationCard from './InlineChannelConfigurationCard';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(),
  auth: { user: { id: 'owner', tenant_id: 'home-company', role: 'platform_admin' } } }));
vi.mock('../../api/core', async (original) => ({ ...await original<typeof import('../../api/core')>(),
  get: api.get, post: api.post, put: api.put, del: api.del }));
vi.mock('../../stores', () => ({ useAuthStore: (selector: (state: typeof api.auth) => unknown) => selector(api.auth) }));
vi.mock('react-i18next', () => {
  const t = (key: string, fallback: unknown) => typeof fallback === 'string' ? fallback : key;
  return { useTranslation: () => ({ t }) };
});

const endpoint = '/tools/agents/source/sessions/work/questions/question/channel';
const original = { id: 'installation', updated_at: '2026-10-16T00:00:00+00:00', channel_type: 'feishu', scope: 'company' };
const saved = { id: original.id, tenant_id: 'target-company', channel_type: 'feishu', app_id: 'cli_company', revision: 'version-1',
  is_active: true, has_app_secret: true, has_encrypt_key: false, has_verification_token: true, extra_config: {} };
const waiting = { state: 'waiting', target_agent_id: 'colleague', target_agent_name: 'Researcher',
  target_tenant_id: 'target-company', configuration: original };
let readback: typeof waiting;
const clients: QueryClient[] = [];
function mount(answer = vi.fn().mockResolvedValue({})) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><MemoryRouter><InlineChannelConfigurationCard agentId="source" sessionId="work"
    questionId="question" channelType="feishu" question="Review the company connection" onAnswer={answer} /></MemoryRouter></QueryClientProvider>);
  return answer;
}
beforeEach(() => {
  readback = { ...waiting };
  api.auth.user = { id: 'owner', tenant_id: 'home-company', role: 'platform_admin' };
  localStorage.setItem('current_tenant_id', 'unrelated-company');
  api.get.mockImplementation(async (url: string) => {
    if (url === endpoint) return readback;
    if (url.startsWith('/tenant-channels?')) return [saved];
    if (url.startsWith('/tenant-channels/feishu/webhook-url?')) return { tenant_id: saved.tenant_id, webhook_url: 'https://example.test/callback' };
    if (url === '/agents/colleague/channel') return { is_configured: true, app_id: 'cli_employee', extra_config: { connection_mode: 'webhook' } };
    if (url === '/agents/colleague/channel/webhook-url') return { webhook_url: 'https://example.test/employee' };
    if (url === '/tools/agents/colleague/runtime/feishu-status') return null;
    throw new Error(`Unexpected request: ${url}`);
  });
  api.post.mockImplementation(async (url: string) => {
    if (url !== endpoint + '/confirm') throw new Error(`Unexpected write: ${url}`);
    return { ...readback, state: 'ready', receipt_ref: 'confirmed-current-company' };
  });
});
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); localStorage.clear(); vi.resetAllMocks(); });

it('edits the displayed company through its real editor, then confirms the new snapshot in the original work', async () => {
  let finish!: (value: unknown) => void;
  api.put.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const answer = mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit channel configuration' }));
  await screen.findByRole('heading', { name: 'Company Feishu / Lark bot' });
  await waitFor(() => expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe(saved.app_id));
  fireEvent.change(screen.getByLabelText('App secret'), { target: { value: 'SYNTHETIC_COMPANY_SECRET' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith('/tenant-channels/feishu', {
    expected_tenant_id: saved.tenant_id, expected_user_id: 'owner', expected_revision: saved.revision,
    app_id: saved.app_id, is_active: true, platform_region: 'feishu_cn', app_secret: 'SYNTHETIC_COMPANY_SECRET',
  }));
  expect((screen.getByRole('button', { name: 'Use this configuration and continue' }) as HTMLButtonElement).disabled).toBe(true);
  expect(answer).not.toHaveBeenCalled();
  readback = { ...waiting, configuration: { ...original, updated_at: '2026-10-16T00:01:00+00:00' } };
  await act(async () => { finish({ ...saved, revision: 'version-2' }); });
  await waitFor(() => expect(api.get.mock.calls.filter(([url]) => url === endpoint)).toHaveLength(2));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Use this configuration and continue' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Use this configuration and continue' }));
  await waitFor(() => expect(answer).toHaveBeenCalledWith('question', 'Continue with the configured channel.'));
  expect(api.post).toHaveBeenCalledWith(endpoint + '/confirm', { config_id: original.id, updated_at: readback.configuration.updated_at });
  expect(api.get.mock.calls.some(([url]) => String(url).startsWith('/agents/colleague/'))).toBe(false);
  expect(JSON.stringify(api.post.mock.calls)).not.toContain('SYNTHETIC_COMPANY_SECRET');
});

it('lets an employee owner use the existing company connection without exposing company editing', async () => {
  api.auth.user.role = 'member';
  const answer = mount();
  await screen.findByText(/Existing company channel/);
  expect(screen.queryByRole('button', { name: 'Edit channel configuration' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Use this configuration and continue' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
  expect(api.put).not.toHaveBeenCalled();
  expect(api.get.mock.calls.some(([url]) => String(url).startsWith('/tenant-channels'))).toBe(false);
});

it('requires a refreshed target company for an older company readback instead of opening an employee editor', async () => {
  readback = { ...waiting, target_tenant_id: '' };
  mount();
  const edit = await screen.findByRole('button', { name: 'Edit channel configuration' });
  expect((edit as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(edit);
  expect(api.get.mock.calls.some(([url]) => String(url).startsWith('/agents/colleague/'))).toBe(false);
  readback = waiting;
  fireEvent.click(screen.getByRole('button', { name: 'Review current configuration' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Edit channel configuration' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Edit channel configuration' }));
  await screen.findByRole('heading', { name: 'Company Feishu / Lark bot' });
});

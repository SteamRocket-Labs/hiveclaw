// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TeamMemorySummaryCard from './TeamMemorySummaryCard';
import { memoryApi, type TeamMemoryEntry, type TeamMemoryWorkspace } from '../../api/domains/memory';
import { ApiError } from '../../api/core/errors';
import { useAuthStore } from '../../stores';
import type { User } from '../../types';

vi.mock('../../api/domains/memory', () => ({ memoryApi: {
  listWorkspaces: vi.fn(), listShared: vi.fn(), getShared: vi.fn(), upsertShared: vi.fn(), deleteShared: vi.fn(),
} }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string | Record<string, unknown>) => typeof fallback === 'string' ? fallback : key,
}) }));

const a = 'business_team:00000000-0000-0000-0000-00000000000a';
const b = 'business_team:00000000-0000-0000-0000-00000000000b';
const scopes: TeamMemoryWorkspace[] = [a, b].map((workspace_key, index) => ({
  workspace_key, name: `Team ${index ? 'B' : 'A'}`, kind: 'business_team', status: 'active', can_write: true, can_delete: false,
}));
function entry(workspace = a, tenant = 'tenant-a'): TeamMemoryEntry {
  return { tenant_id: tenant, workspace_key: workspace, key: 'release', title: workspace === a ? 'Release A' : 'Release B',
    content: workspace === a ? 'Body for A' : 'Body for B', snippet: 'Release reference', updated_at: '', revision: 1,
    path: 'release.md', absolute_path: '/fixture/release.md' };
}
const clients: QueryClient[] = [];
function mount(client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })) {
  clients.push(client);
  return { client, view: render(<QueryClientProvider client={client}><TeamMemorySummaryCard agentId="agent" section="aware" /></QueryClientProvider>) };
}
async function choose(key: string) {
  const picker = await screen.findByLabelText('agent.workspace.sharedMemoryTeam');
  await waitFor(() => expect(picker.querySelector(`option[value="${key}"]`)).toBeTruthy());
  fireEvent.change(picker, { target: { value: key } });
}

beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  localStorage.setItem('current_tenant_id', 'tenant-a');
  useAuthStore.setState({ user: { id: 'user', tenant_id: 'tenant-a', role: 'platform_admin' } as User });
  vi.mocked(memoryApi.listWorkspaces).mockImplementation(async (_agent, tenant) => ({
    tenant_id: tenant, user_id: 'user', workspaces: scopes, unavailable: false,
  }));
  vi.mocked(memoryApi.listShared).mockImplementation(async (workspace, tenant) => [entry(workspace, tenant)]);
  vi.mocked(memoryApi.getShared).mockImplementation(async (_key, workspace, tenant) => entry(workspace, tenant));
});
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); sessionStorage.clear(); localStorage.clear(); useAuthStore.setState({ user: null }); });

it('requires an explicit team choice and saves from the real aware-card editor', async () => {
  mount();
  await screen.findByRole('option', { name: 'Team A' });
  expect(memoryApi.listShared).not.toHaveBeenCalled();
  await choose(a);
  await screen.findByText('Body for A');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  const editor = await screen.findByPlaceholderText('Write the shared note or playbook here');
  fireEvent.change(editor, { target: { value: 'Revised group release cadence' } });
  const result = { ...entry(), content: 'Revised group release cadence', revision: 2 };
  vi.mocked(memoryApi.upsertShared).mockResolvedValue(result);
  vi.mocked(memoryApi.getShared).mockResolvedValue(result);
  fireEvent.click(screen.getByRole('button', { name: 'Save to shared memory' }));
  await waitFor(() => expect(memoryApi.upsertShared).toHaveBeenCalledWith(expect.objectContaining({
    workspace_key: a, key: 'release', base_revision: 1, content: 'Revised group release cadence',
  }), 'tenant-a'));
  await waitFor(() => expect(screen.getByText('Revised group release cadence', { selector: '.team-memory-detail-content' })).toBeTruthy());
});

it('keeps an unknown save draft through workspace switches and remount without another write', async () => {
  const { client, view } = mount();
  await choose(a);
  await screen.findByText('Body for A');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(await screen.findByPlaceholderText('Write the shared note or playbook here'), { target: { value: 'Unconfirmed draft A' } });
  vi.mocked(memoryApi.upsertShared).mockRejectedValueOnce(new ApiError(503, 'Save needs reconciliation', { outcome: 'unknown' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save to shared memory' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Save to shared memory' }) as HTMLButtonElement).disabled).toBe(true));
  await choose(b);
  await screen.findByText('Body for B');
  expect(screen.queryByDisplayValue('Unconfirmed draft A')).toBeNull();
  view.unmount();
  mount(client);
  await choose(a);
  await screen.findByText('Body for A');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  expect(await screen.findByDisplayValue('Unconfirmed draft A')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Save to shared memory' }) as HTMLButtonElement).disabled).toBe(true);
  expect(memoryApi.upsertShared).toHaveBeenCalledTimes(1);
  vi.mocked(memoryApi.getShared).mockResolvedValue({ ...entry(), content: 'Unconfirmed draft A', revision: 2 });
  fireEvent.click(screen.getByRole('button', { name: 'agent.workspace.sharedMemoryCheckSaved' }));
  await waitFor(() => expect(screen.getByText('Shared memory synced')).toBeTruthy());
  expect(memoryApi.upsertShared).toHaveBeenCalledTimes(1);
});

it('withholds a late old-company detail after the selected company changes', async () => {
  let release!: (value: TeamMemoryEntry) => void;
  vi.mocked(memoryApi.getShared).mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  mount();
  await choose(a);
  await waitFor(() => expect(memoryApi.getShared).toHaveBeenCalledTimes(1));
  await act(async () => {
    localStorage.setItem('current_tenant_id', 'tenant-b');
    window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'tenant-b' }));
  });
  await waitFor(() => expect(memoryApi.listWorkspaces).toHaveBeenCalledWith('agent', 'tenant-b'));
  await act(async () => { release({ ...entry(), content: 'OLD_COMPANY_PRIVATE_BODY' }); });
  expect(screen.queryByText('OLD_COMPANY_PRIVATE_BODY')).toBeNull();
  expect(memoryApi.listShared).toHaveBeenCalledTimes(1);
});

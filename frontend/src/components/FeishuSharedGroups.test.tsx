// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { channelApi } from '../api/domains/channels';
import FeishuSharedGroups from './FeishuSharedGroups';

const auth = vi.hoisted(() => ({ user: { id: 'owner', tenant_id: 'company-a' } }));
vi.mock('../stores', () => ({ useAuthStore: (selector: (state: typeof auth) => unknown) => selector(auth) }));
vi.mock('../api/domains/channels', () => ({ channelApi: { feishuSharedGroups: vi.fn(), saveFeishuSharedGroups: vi.fn() } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const value = (revision = 'v1', chat_ids = ['oc_first']) => ({ installation_ref: 'installation-a', revision, chat_ids });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><FeishuSharedGroups agentId="employee" /></QueryClientProvider>);
}
function open() {
  const details = screen.getByText('Shared group work').closest('details')!;
  details.open = true; fireEvent(details, new Event('toggle'));
}
beforeEach(() => { vi.clearAllMocks(); localStorage.setItem('current_tenant_id', 'company-a'); });
afterEach(cleanup);

it('saves explicit group entry against the read revision without changing credentials', async () => {
  vi.mocked(channelApi.feishuSharedGroups).mockResolvedValue(value());
  vi.mocked(channelApi.saveFeishuSharedGroups).mockResolvedValue(value('v2', ['oc_second']));
  mount(); expect(channelApi.feishuSharedGroups).not.toHaveBeenCalled(); open();
  const field = await screen.findByLabelText('Group IDs, one per line');
  await waitFor(() => expect((field as HTMLTextAreaElement).value).toBe('oc_first'));
  fireEvent.change(field, { target: { value: 'oc_second\n' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('Group entry setting saved.');
  expect(channelApi.saveFeishuSharedGroups).toHaveBeenCalledWith('employee', 'v1', ['oc_second']);
  expect((field as HTMLTextAreaElement).value).toBe('oc_second');
});

it('keeps the draft after a conflicting save and refreshes the revision only on request', async () => {
  vi.mocked(channelApi.feishuSharedGroups).mockResolvedValueOnce(value()).mockResolvedValueOnce(value('v2', ['oc_remote']));
  vi.mocked(channelApi.saveFeishuSharedGroups).mockRejectedValueOnce(new Error('409')).mockResolvedValueOnce(value('v3', ['oc_local']));
  mount(); open();
  const field = await screen.findByLabelText('Group IDs, one per line');
  await waitFor(() => expect((field as HTMLTextAreaElement).value).toBe('oc_first'));
  fireEvent.change(field, { target: { value: 'oc_local' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByRole('alert');
  expect((field as HTMLTextAreaElement).value).toBe('oc_local');
  expect(screen.queryByText('Group entry setting saved.')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByText(/oc_remote/);
  expect((field as HTMLTextAreaElement).value).toBe('oc_local');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('Group entry setting saved.');
  expect(channelApi.saveFeishuSharedGroups).toHaveBeenLastCalledWith('employee', 'v2', ['oc_local']);
});

it('hides old confirmed values on a failed read while keeping the draft', async () => {
  vi.mocked(channelApi.feishuSharedGroups).mockResolvedValueOnce(value()).mockRejectedValueOnce(new Error('unavailable'));
  mount(); open();
  const field = await screen.findByLabelText('Group IDs, one per line');
  await waitFor(() => expect((field as HTMLTextAreaElement).value).toBe('oc_first'));
  fireEvent.change(field, { target: { value: 'oc_local' } });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByRole('alert');
  expect(screen.queryByText(/Saved groups/)).toBeNull();
  expect((field as HTMLTextAreaElement).value).toBe('oc_local');
  expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
});

it('drops a previous company response and draft when scope changes', async () => {
  let finish!: (result: ReturnType<typeof value>) => void;
  vi.mocked(channelApi.feishuSharedGroups).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
    .mockResolvedValueOnce(value('other', ['oc_company_b']));
  mount(); open();
  await waitFor(() => expect(channelApi.feishuSharedGroups).toHaveBeenCalledTimes(1));
  localStorage.setItem('current_tenant_id', 'company-b');
  fireEvent(window, new StorageEvent('storage', { key: 'current_tenant_id' }));
  open();
  await screen.findByText(/oc_company_b/);
  finish(value('old', ['oc_company_a']));
  await waitFor(() => expect((screen.getByLabelText('Group IDs, one per line') as HTMLTextAreaElement).value).toBe('oc_company_b'));
  expect(screen.queryByText(/oc_company_a/)).toBeNull();
});

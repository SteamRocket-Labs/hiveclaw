// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TenantFeishuGroupRoutes from './TenantFeishuGroupRoutes';

const mocks = vi.hoisted(() => ({ read: vi.fn(), save: vi.fn(), employees: vi.fn() }));
vi.mock('../api/domains/channels', () => ({ channelApi: { tenantFeishuGroupRoutes: mocks.read, saveTenantFeishuGroupRoutes: mocks.save } }));
vi.mock('../api/domains/agents', () => ({ agentApi: { list: mocks.employees } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const scope = { expected_tenant_id: 'company', expected_user_id: 'owner' };
const settings = { tenant_id: 'company', installation_ref: 'company-bot', revision: 'v1', routes: [{ chat_id: 'oc_saved', agent_id: 'employee' }] };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><TenantFeishuGroupRoutes scope={scope} scopeKey="company-owner" /></QueryClientProvider>);
  const details = view.container.querySelector('details')!;
  details.open = true; fireEvent(details, new Event('toggle'));
  return view;
}
beforeEach(() => {
  mocks.read.mockResolvedValue(settings);
  mocks.employees.mockResolvedValue([{ id: 'employee', tenant_id: 'company', name: 'Research employee' },
    { id: 'foreign', tenant_id: 'other', name: 'Foreign employee' }]);
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('saves the explicit group/employee and captured scope and revision', async () => {
  mount();
  await screen.findByDisplayValue('oc_saved');
  expect(screen.queryByText('Foreign employee')).toBeNull();
  fireEvent.change(screen.getByLabelText('Group ID 1'), { target: { value: 'oc_chosen' } });
  const saved = { ...settings, revision: 'v2', routes: [{ chat_id: 'oc_chosen', agent_id: 'employee' }] };
  mocks.save.mockImplementation(async () => { mocks.read.mockResolvedValue(saved); return saved; });
  fireEvent.click(screen.getByRole('button', { name: 'Save group routes' }));
  await screen.findByText('Company group routes saved.');
  expect(mocks.save).toHaveBeenCalledWith(scope, 'v1', saved.routes);
});

it('preserves a conflicting draft and refreshes its saved comparison', async () => {
  mount(); await screen.findByDisplayValue('oc_saved');
  fireEvent.change(screen.getByLabelText('Group ID 1'), { target: { value: 'oc_draft' } });
  mocks.save.mockRejectedValueOnce(new Error('409'));
  fireEvent.click(screen.getByRole('button', { name: 'Save group routes' }));
  await screen.findByRole('alert');
  mocks.read.mockResolvedValue({ ...settings, revision: 'v2', routes: [{ chat_id: 'oc_remote', agent_id: 'employee' }] });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByText('Saved routes: oc_remote → Research employee');
  expect((screen.getByLabelText('Group ID 1') as HTMLInputElement).value).toBe('oc_draft');
  mocks.save.mockResolvedValue({ ...settings, revision: 'v3' });
  fireEvent.click(screen.getByRole('button', { name: 'Save group routes' }));
  await waitFor(() => expect(mocks.save).toHaveBeenLastCalledWith(scope, 'v2', [{ chat_id: 'oc_draft', agent_id: 'employee' }]));
});

it('does not consume another company response or enable a save after a failed read', async () => {
  mocks.read.mockResolvedValue({ ...settings, tenant_id: 'foreign', routes: [{ chat_id: 'FOREIGN_ROUTE_CANARY', agent_id: 'foreign' }] });
  mount(); await screen.findByRole('alert');
  expect(screen.queryByText(/FOREIGN_ROUTE_CANARY/)).toBeNull();
  expect((screen.getByRole('button', { name: 'Save group routes' }) as HTMLButtonElement).disabled).toBe(true);
  expect(mocks.save).not.toHaveBeenCalled();
});

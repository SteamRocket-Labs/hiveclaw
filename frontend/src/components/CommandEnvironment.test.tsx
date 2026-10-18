// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import CommandEnvironment from './CommandEnvironment';

const mocks = vi.hoisted(() => ({ read: vi.fn(), control: vi.fn(), auth: { user: { id: 'owner', role: 'member', tenant_id: 'company' } } }));
vi.mock('../stores', () => ({ useAuthStore: (selector: (value: typeof mocks.auth) => unknown) => selector(mocks.auth) }));
vi.mock('../api/domains/environments', () => ({ environmentApi: { read: mocks.read, control: mocks.control } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const env = { id: 'environment', provider_key: 'local_os_sandbox', generation: 1, revision: 1, desired_state: 'ready', observed_state: 'ready', active_commands: 1 };
const view = { agent_id: 'employee', tenant_id: 'company', can_manage: true, environment: env };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = render(<QueryClientProvider client={client}><CommandEnvironment agentId="employee" /></QueryClientProvider>);
  const details = tree.container.querySelector('details')!;
  details.open = true; fireEvent(details, new Event('toggle'));
  return tree;
}
beforeEach(() => { mocks.read.mockResolvedValue(view); mocks.auth.user = { id: 'owner', role: 'member', tenant_id: 'company' }; localStorage.setItem('current_tenant_id', 'company'); });
afterEach(() => { cleanup(); vi.resetAllMocks(); localStorage.clear(); });

it('keeps stopping distinct from stopped and sends the captured scope and revision', async () => {
  mount(); await screen.findByText('Ready for supervised commands');
  const stopping = { ...view, environment: { ...env, revision: 2, desired_state: 'stopped', observed_state: 'stopping' } };
  mocks.control.mockImplementation(async () => { mocks.read.mockResolvedValue(stopping); return stopping; });
  fireEvent.click(screen.getByRole('button', { name: 'Stop supervised commands' }));
  await screen.findByText('Stopping: waiting for active commands and work settlement');
  expect(mocks.control).toHaveBeenCalledWith('employee', { expected_tenant_id: 'company', expected_user_id: 'owner' }, 'stop', 1);
  expect((screen.getByRole('button', { name: 'Resume command environment' }) as HTMLButtonElement).disabled).toBe(true);
  mocks.read.mockResolvedValue({ ...stopping, environment: { ...stopping.environment, active_commands: 0, observed_state: 'stopped' } });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByText('Stopped');
  expect((screen.getByRole('button', { name: 'Resume command environment' }) as HTMLButtonElement).disabled).toBe(false);
});

it('hides stale authority after an unknown control response and requires a fresh observation', async () => {
  mount(); await screen.findByText('Ready for supervised commands');
  mocks.control.mockRejectedValue(new Error('response lost'));
  fireEvent.click(screen.getByRole('button', { name: 'Stop supervised commands' }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: 'Stop supervised commands' })).toBeNull();
  mocks.read.mockResolvedValue({ ...view, environment: { ...env, revision: 2, active_commands: 0, desired_state: 'stopped', observed_state: 'stopped' } });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByText('Stopped');
  expect(mocks.control).toHaveBeenCalledTimes(1);
});

it('shows read-only resource facts without management controls', async () => {
  mocks.read.mockResolvedValue({ ...view, can_manage: false });
  mount(); await screen.findByText('Ready for supervised commands');
  expect(screen.queryByRole('button', { name: 'Stop supervised commands' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Resume command environment' })).toBeNull();
});

it('drops old-company facts and a late response when the selected company changes', async () => {
  const tree = mount(); await screen.findByText('Ready for supervised commands');
  let finish!: (value: unknown) => void;
  mocks.read.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  localStorage.setItem('current_tenant_id', 'other');
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'other' })));
  await act(async () => { finish(view); });
  expect(screen.queryByText('Ready for supervised commands')).toBeNull();
  expect(tree.container.querySelector('details')!.open).toBe(false);
  expect(mocks.control).not.toHaveBeenCalled();
});

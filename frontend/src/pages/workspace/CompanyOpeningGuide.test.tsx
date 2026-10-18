// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { systemApi, type CompanyOnboardingStatus } from '../../api/domains/system';
import CompanyOpeningGuide from './CompanyOpeningGuide';

const auth = vi.hoisted(() => ({ user: { id: 'admin', tenant_id: 'company-a', role: 'org_admin' } }));
vi.mock('../../stores', () => ({ useAuthStore: (selector: (value: typeof auth) => unknown) => selector(auth) }));
vi.mock('../../api/domains/system', () => ({ systemApi: { getOnboardingStatus: vi.fn() } }));
vi.mock('../../components/EmployeeReadinessControls', () => ({ default: ({ agentId }: { agentId: string }) => <p>Check {agentId}</p> }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const status = (tenant: string): CompanyOnboardingStatus => ({ tenant_id: tenant, tenant_name: `Name ${tenant}`,
  authenticated_user_id: 'admin', configuration_only: true, employees: [{ id: `${tenant}-employee`, name: `Employee ${tenant}`, agent_type: 'native' }],
  items: [{ key: 'llm', title: 'Model', completed: true, required: false, link: '/enterprise/llm' }], completed: 1, total: 5 });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<MemoryRouter><QueryClientProvider client={client}><CompanyOpeningGuide /></QueryClientProvider></MemoryRouter>);
}
function open() {
  const details = screen.getByText('Get started with your company').closest('details')!;
  details.open = true; fireEvent(details, new Event('toggle'));
}
beforeEach(() => { vi.clearAllMocks(); auth.user.role = 'org_admin'; localStorage.setItem('current_tenant_id', 'company-a'); });
afterEach(cleanup);

it('drops an old in-flight response when the authenticated company selection changes', async () => {
  let finish!: (value: CompanyOnboardingStatus) => void;
  vi.mocked(systemApi.getOnboardingStatus).mockImplementation((tenant) => tenant === 'company-a'
    ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(status('company-b')));
  mount(); open();
  await waitFor(() => expect(systemApi.getOnboardingStatus).toHaveBeenCalledWith('company-a'));
  localStorage.setItem('current_tenant_id', 'company-b');
  fireEvent(window, new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'company-b' }));
  open();
  await screen.findByText('Name company-b');
  finish(status('company-a'));
  await waitFor(() => expect(screen.queryByText('Name company-a')).toBeNull());
  expect(screen.getByText('Check company-b-employee')).toBeTruthy();
});

it('hides prior facts on a failed refresh instead of displaying an empty successful checklist', async () => {
  vi.mocked(systemApi.getOnboardingStatus).mockResolvedValueOnce(status('company-a')).mockRejectedValueOnce(new Error('database unavailable'));
  mount(); open();
  await screen.findByText('Name company-a');
  fireEvent.click(screen.getByRole('button', { name: /^Refresh$/ }));
  await screen.findByRole('alert');
  expect(screen.queryByText('Name company-a')).toBeNull();
  expect(screen.queryByText('Configured')).toBeNull();
  expect(screen.queryByText('Check company-a-employee')).toBeNull();
});

it('does not read administrator setup for an ordinary member', () => {
  auth.user.role = 'member'; mount();
  expect(screen.queryByText('Get started with your company')).toBeNull();
  expect(systemApi.getOnboardingStatus).not.toHaveBeenCalled();
});

it('offers the real creation entry when a company has no employee yet', async () => {
  const empty = { ...status('company-a'), employees: [], items: [{ key: 'agent' as const, title: 'Create First Agent', completed: false, required: true, link: '/agents/new' }] };
  vi.mocked(systemApi.getOnboardingStatus).mockResolvedValue(empty);
  mount(); open();
  const create = await screen.findByRole('link', { name: 'Create a digital employee' });
  expect(create.getAttribute('href')).toBe('/agents/new');
  expect(screen.queryByRole('combobox', { name: 'Employee to check' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'Open employee work' })).toBeNull();
});

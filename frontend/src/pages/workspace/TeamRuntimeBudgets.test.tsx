// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TeamRuntimeBudgets from './TeamRuntimeBudgets';
import WorkspaceRuntimeBudgetsSection from './WorkspaceRuntimeBudgetsSection';
import { runtimeBudgetApi } from '../../api/domains/runtimeBudgets';

vi.mock('../../api/domains/runtimeBudgets', () => ({ runtimeBudgetApi: {
  listTeamRuns: vi.fn(), requestIncrease: vi.fn(), withdrawIncrease: vi.fn(),
  listPolicies: vi.fn(async () => []), listRuns: vi.fn(), listTransitionDeliveries: vi.fn(async () => []),
  approveOverrun: vi.fn(), rejectOverrun: vi.fn(),
} }));
vi.mock('../../stores', () => ({ useAuthStore: (select: (state: unknown) => unknown) => select({ user: { id: 'lead' } }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));

const request = { id: 'request', approval_episode_id: 'episode', business_team_id: 'team', requester_user_id: 'lead',
  requester_name: 'Avery', limits: { max_tokens: 30, max_subagents: 3 }, reason: 'Finish the analysis', status: 'pending', can_withdraw: true };
const run = { id: 'run', agent_name: 'Analyst', status: 'waiting_budget_approval', approval_episode_id: 'episode',
  limits: { max_tokens: 10, max_subagents: 1 }, usage: { tokens: { used: 0, reserved: 0 }, subagents: { used: 0, reserved: 0 } }, increase_requests: [] };
function mount(component: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}>{component}</QueryClientProvider>);
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(runtimeBudgetApi.listTeamRuns).mockResolvedValue({ tenant_id: 'tenant', business_team_id: 'team', runs: [run], next_offset: null } as never);
});
afterEach(cleanup);

it('reuses the exact request after an unknown receipt, then shows a withdrawable server receipt', async () => {
  vi.mocked(runtimeBudgetApi.requestIncrease).mockRejectedValueOnce(new Error('receipt lost')).mockImplementationOnce(async () => {
    vi.mocked(runtimeBudgetApi.listTeamRuns).mockResolvedValue({ runs: [{ ...run, increase_requests: [request] }] } as never);
    return request as never;
  });
  vi.mocked(runtimeBudgetApi.withdrawIncrease).mockImplementationOnce(async () => {
    vi.mocked(runtimeBudgetApi.listTeamRuns).mockResolvedValue({ runs: [{ ...run,
      increase_requests: [{ ...request, status: 'withdrawn', can_withdraw: false }] }] } as never);
    return { ...request, status: 'withdrawn' } as never;
  });
  mount(<TeamRuntimeBudgets teamId="team" tenantId="tenant" />);
  expect(await screen.findByText('Analyst')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('Total token limit'), { target: { value: '30' } });
  fireEvent.change(screen.getByLabelText('Max subagents'), { target: { value: '3' } });
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Finish the analysis' } });
  fireEvent.click(screen.getByRole('button', { name: 'Request an increase' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Request an increase' }));
  await screen.findByText('Awaiting administrator decision');
  const calls = vi.mocked(runtimeBudgetApi.requestIncrease).mock.calls;
  expect(calls).toHaveLength(2);
  expect(calls[0]).toEqual(calls[1]);
  expect(calls[1]).toEqual(['team', 'run', { request_id: expect.any(String), approval_episode_id: 'episode',
    limits: { max_tokens: 30, max_subagents: 3 }, reason: 'Finish the analysis' }]);
  fireEvent.click(screen.getByRole('button', { name: 'Withdraw request' }));
  await screen.findByText('Request withdrawn');
  expect(runtimeBudgetApi.withdrawIncrease).toHaveBeenCalledWith('team', 'run', 'request');
});

it('hides cached team facts when the current scoped query fails', async () => {
  vi.mocked(runtimeBudgetApi.listTeamRuns).mockRejectedValue(new Error('membership revoked'));
  mount(<TeamRuntimeBudgets teamId="other-team" tenantId="tenant" />);
  await screen.findByRole('alert');
  expect(screen.queryByText('Analyst')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Request an increase' })).toBeNull();
  expect(runtimeBudgetApi.listTeamRuns).toHaveBeenCalledWith('other-team', 0);
});

it('binds the administrator decision to the exact displayed proposal and episode', async () => {
  vi.mocked(runtimeBudgetApi.listRuns).mockResolvedValue([{ ...run, increase_requests: [request] }] as never);
  vi.mocked(runtimeBudgetApi.approveOverrun).mockResolvedValue({} as never);
  vi.mocked(runtimeBudgetApi.rejectOverrun).mockResolvedValue({} as never);
  mount(<WorkspaceRuntimeBudgetsSection />);
  fireEvent.click(await screen.findByRole('button', { name: 'Approve requested increase' }));
  await waitFor(() => expect(runtimeBudgetApi.approveOverrun).toHaveBeenCalledWith('run', {
    approval_episode_id: 'episode', increase_request_id: 'request', max_tokens: 30, max_subagents: 3,
    reason: 'operator reviewed the exact team budget increase request',
  }));
  fireEvent.click(screen.getByRole('button', { name: 'Reject request and stop waiting work' }));
  await waitFor(() => expect(runtimeBudgetApi.rejectOverrun).toHaveBeenCalledWith('run', {
    approval_episode_id: 'episode', increase_request_id: 'request',
    reason: 'operator reviewed the exact team budget increase request',
  }));
  expect(screen.getByRole('button', { name: 'Approve only the required capacity' })).toBeTruthy();
});

it('removes the previous company proposal while the new company query is pending', async () => {
  localStorage.setItem('current_tenant_id', 'company-a');
  vi.mocked(runtimeBudgetApi.listRuns).mockResolvedValue([{ ...run, increase_requests: [request] }] as never);
  mount(<WorkspaceRuntimeBudgetsSection />);
  await screen.findByText(/Finish the analysis/);
  vi.mocked(runtimeBudgetApi.listRuns).mockImplementation(() => new Promise(() => {}));
  localStorage.setItem('current_tenant_id', 'company-b');
  fireEvent(window, new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'company-b' }));
  await waitFor(() => expect(screen.queryByText(/Finish the analysis/)).toBeNull());
});

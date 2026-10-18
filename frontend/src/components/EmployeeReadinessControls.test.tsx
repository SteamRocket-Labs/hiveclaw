// @vitest-environment jsdom
import { useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import EmployeeReadinessControls from './EmployeeReadinessControls';
import { employeeReadinessApi } from '../api/domains/employeeReadiness';

vi.mock('../api/domains/employeeReadiness', () => ({ employeeReadinessApi: {
  latest: vi.fn(), get: vi.fn(), start: vi.fn(), cancel: vi.fn(),
} }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const pending = { id: 'probe', agent_id: 'agent', request_id: 'request', execution_status: 'pending', checks: [],
  created_at: '2026-10-18T00:00:00Z', observed_at: null, error_code: null, claim_version: 0 };
function Wrapper({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }));
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(employeeReadinessApi.latest).mockResolvedValue(null);
});
afterEach(cleanup);

it('keeps a real queued acknowledgement visible while the first read is pending', async () => {
  vi.mocked(employeeReadinessApi.start).mockResolvedValue(pending);
  vi.mocked(employeeReadinessApi.get).mockImplementation(() => new Promise(() => {}));
  render(<EmployeeReadinessControls agentId="agent" teamId="team" scopeKey="tenant:lead" />, { wrapper: Wrapper });
  fireEvent.click(screen.getByRole('button', { name: 'Employee readiness' }));
  fireEvent.click(screen.getByRole('button', { name: 'Run a new check' }));
  await screen.findByText('Waiting for a worker; an administrator may need to check worker capacity.');
  expect(screen.getByRole('button', { name: 'Run a new check' }).hasAttribute('disabled')).toBe(true);
  expect(screen.getByRole('button', { name: 'Cancel my check' })).toBeTruthy();
  expect(screen.queryByText('Verified')).toBeNull();
});

it('retries an unknown request with the same id and preserves unverified provider status', async () => {
  const checked = { ...pending, execution_status: 'completed', observed_at: '2026-10-18T01:00:00Z',
    checks: [{ key: 'worker', status: 'passed' as const }, { key: 'provider_inference', status: 'unverified' as const }] };
  vi.mocked(employeeReadinessApi.start).mockRejectedValueOnce(new Error('receipt lost')).mockResolvedValueOnce(checked);
  vi.mocked(employeeReadinessApi.get).mockResolvedValue(checked);
  render(<EmployeeReadinessControls agentId="agent" teamId="team" scopeKey="tenant:lead" />, { wrapper: Wrapper });
  fireEvent.click(screen.getByRole('button', { name: 'Employee readiness' }));
  fireEvent.click(screen.getByRole('button', { name: 'Run a new check' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Retry this check request' }));
  await screen.findByText('Checks recorded. Review each result.');
  expect(vi.mocked(employeeReadinessApi.start).mock.calls[0]).toEqual(vi.mocked(employeeReadinessApi.start).mock.calls[1]);
  expect(screen.getByText('Not verified by this check')).toBeTruthy();
});

it('reconciles an exact committed request through readback after its acknowledgement is lost', async () => {
  vi.mocked(employeeReadinessApi.start).mockImplementation(async (_agent, requestId) => {
    vi.mocked(employeeReadinessApi.latest).mockResolvedValue({ ...pending, request_id: requestId });
    throw new Error('committed acknowledgement lost');
  });
  vi.mocked(employeeReadinessApi.get).mockResolvedValue(pending);
  render(<EmployeeReadinessControls agentId="agent" scopeKey="tenant:owner" />, { wrapper: Wrapper });
  fireEvent.click(screen.getByRole('button', { name: 'Employee readiness' }));
  fireEvent.click(screen.getByRole('button', { name: 'Run a new check' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  await screen.findByText('Waiting for a worker; an administrator may need to check worker capacity.');
  expect(screen.queryByRole('button', { name: 'Retry this check request' })).toBeNull();
  expect(employeeReadinessApi.start).toHaveBeenCalledTimes(1);
});

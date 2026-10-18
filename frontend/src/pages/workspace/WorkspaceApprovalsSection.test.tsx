// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import WorkspaceApprovalsSection from './WorkspaceApprovalsSection';

const { list, resolve, grant, revoke } = vi.hoisted(() => ({ list: vi.fn(), resolve: vi.fn(), grant: vi.fn(), revoke: vi.fn() }));
vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: { listApprovals: list, resolveApproval: resolve,
  grantApprovalReviewer: grant, revokeApprovalReviewer: revoke } }));
vi.mock('../../stores', () => ({ useAuthStore: (select: (state: unknown) => unknown) => select({ user: { id: 'lead' } }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const pending = { id: 'approval', agent_id: 'employee', agent_name: 'Analyst', status: 'pending',
  action_type: 'workspace.file.write', tool_name: 'write_file', details: { path: 'report.md' },
  created_at: '2026-10-18T00:00:00Z' };

it('reviews only the selected team and keeps a denied decision recoverable', async () => {
  list.mockResolvedValue([pending]);
  resolve.mockRejectedValue(new Error('authority revoked'));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <WorkspaceApprovalsSection selectedTenantId="company" businessTeamId="team" />
  </QueryClientProvider>);
  expect(await screen.findByText('report.md')).toBeTruthy();
  expect(list).toHaveBeenCalledWith('company', 'team');
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  expect(await screen.findByRole('alert')).toBeTruthy();
  expect(resolve).toHaveBeenCalledWith('approval', { action: 'approve' });
  expect(screen.getByText('report.md')).toBeTruthy();
  list.mockResolvedValue([]);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(screen.queryByText('report.md')).toBeNull());
  expect(screen.queryByRole('alert')).toBeNull();
  client.clear();
});

it('does not retain another team list while a new team loads', async () => {
  list.mockImplementation((_company, team) => team === 'first' ? Promise.resolve([pending]) : new Promise(() => {}));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const surface = (team: string) => <QueryClientProvider client={client}>
    <WorkspaceApprovalsSection selectedTenantId="company" businessTeamId={team} />
  </QueryClientProvider>;
  const view = render(surface('first'));
  expect(await screen.findByText('report.md')).toBeTruthy();
  view.rerender(surface('second'));
  expect(screen.queryByText('report.md')).toBeNull();
  await waitFor(() => expect(list).toHaveBeenCalledWith('company', 'second'));
  client.clear();
});

it('lets the owner authorize and remove a reviewer for this exact approval', async () => {
  const reviewers = [{ user_id: 'lead', name: 'Blair', grant_id: null as string | null }];
  list.mockResolvedValue([{ ...pending, team_reviewers: reviewers }]);
  grant.mockImplementation(async () => {
    list.mockResolvedValue([{ ...pending, team_reviewers: [{ ...reviewers[0], grant_id: 'grant' }] }]);
    return { id: 'grant', revoked: false };
  });
  revoke.mockImplementation(async () => { list.mockResolvedValue([{ ...pending, team_reviewers: reviewers }]); });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <WorkspaceApprovalsSection selectedTenantId="company" businessTeamId="team" />
  </QueryClientProvider>);
  fireEvent.change(await screen.findByRole('combobox', { name: 'Team reviewer' }), { target: { value: 'lead' } });
  fireEvent.click(screen.getByRole('button', { name: 'Authorize for this approval' }));
  await waitFor(() => expect(grant).toHaveBeenCalledWith('approval', { user_id: 'lead', request_id: expect.any(String) }));
  fireEvent.click(await screen.findByRole('button', { name: /Remove authorization/ }));
  await waitFor(() => expect(revoke).toHaveBeenCalledWith('approval', 'grant'));
  expect(await screen.findByRole('combobox', { name: 'Team reviewer' })).toBeTruthy();
  client.clear();
});

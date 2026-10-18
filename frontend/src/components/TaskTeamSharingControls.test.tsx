// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TaskTeamSharingControls from './TaskTeamSharingControls';
import TeamBusinessTasks from '../pages/workspace/TeamBusinessTasks';
import { businessTeamsApi } from '../api/domains/businessTeams';
import { teamTasksApi } from '../api/domains/teamTasks';
import type { Task } from '../types';

vi.mock('../api/domains/businessTeams', () => ({ businessTeamsApi: { list: vi.fn(), read: vi.fn() } }));
vi.mock('../api/domains/teamTasks', () => ({ teamTasksApi: { list: vi.fn(), share: vi.fn(), handover: vi.fn() } }));
vi.mock('../api/domains/chat', () => ({ chatApi: { createSession: vi.fn() } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const task = { id: 'task', agent_id: 'agent', title: 'Reviewed title', description: 'Reviewed goal',
  updated_at: '2026-10-18T00:00:00Z', created_by: 'owner', can_manage_team_sharing: true } as Task;
const shared = { ...task, agent_name: 'Analyst', status: 'pending', responsible_user_id: 'owner',
  responsible_name: 'Avery', can_handover: true };
function wrapper({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>{children}</QueryClientProvider>;
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(businessTeamsApi.list).mockResolvedValue({ teams: [{ id: 'team', name: 'Delivery', status: 'active' }] } as never);
  vi.mocked(businessTeamsApi.read).mockResolvedValue({ members: [{ id: 'member', user_id: 'owner', display_name: 'Avery', authority: true }] } as never);
  vi.mocked(teamTasksApi.list).mockResolvedValue({ tasks: [shared], assignees: [{ id: 'successor', name: 'Blair' }], next_offset: null } as never);
});
afterEach(cleanup);

it('keeps the reviewed source version and exact command across a lost receipt', async () => {
  const changed = vi.fn(async () => {});
  vi.mocked(teamTasksApi.share).mockRejectedValueOnce(new Error('receipt lost')).mockResolvedValueOnce({} as never);
  const view = render(<TaskTeamSharingControls task={task} scopeKey="tenant:owner" onChanged={changed} />, { wrapper });
  fireEvent.click(screen.getByRole('button', { name: 'Share task with a team' }));
  await screen.findByRole('option', { name: 'Delivery' });
  fireEvent.change(screen.getByLabelText('Team'), { target: { value: 'team' } });
  await screen.findByRole('option', { name: 'Avery' });
  fireEvent.change(screen.getByLabelText('Responsible member'), { target: { value: 'owner' } });
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Publish the reviewed goal' } });
  view.rerender(<TaskTeamSharingControls task={{ ...task, updated_at: '2026-10-18T01:00:00Z', description: 'New private edit' }} scopeKey="tenant:owner" onChanged={changed} />);
  expect(screen.getByText('Reviewed goal')).toBeTruthy();
  expect(screen.queryByText('New private edit')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Share reviewed task details' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Share reviewed task details' }));
  await waitFor(() => expect(teamTasksApi.share).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(teamTasksApi.share).mock.calls;
  expect(calls[0]).toEqual(calls[1]);
  expect(calls[0]).toEqual(['agent', 'task', { request_id: expect.any(String), business_team_id: 'team', responsible_user_id: 'owner',
    expected_updated_at: task.updated_at, reason: 'Publish the reviewed goal' }]);
});

it('retries the exact reviewed handover while refetching current team facts', async () => {
  vi.mocked(teamTasksApi.handover).mockRejectedValueOnce(new Error('receipt lost')).mockResolvedValueOnce({ paused_routine_count: 1 } as never);
  render(<TeamBusinessTasks teamId="team" scopeKey="tenant:lead" />, { wrapper });
  fireEvent.click(await screen.findByRole('button', { name: 'Hand over responsibility' }));
  fireEvent.change(screen.getByLabelText('New responsible member'), { target: { value: 'successor' } });
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Continue with Blair' } });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm responsibility handover' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Confirm responsibility handover' }));
  await screen.findByRole('status');
  const calls = vi.mocked(teamTasksApi.handover).mock.calls;
  expect(calls).toHaveLength(2);
  expect(calls[0]).toEqual(calls[1]);
  expect(calls[0]).toEqual(['team', 'task', { request_id: expect.any(String), responsible_user_id: 'successor',
    expected_updated_at: task.updated_at, reason: 'Continue with Blair' }]);
});

it('hides team tasks on an authority error', async () => {
  vi.mocked(teamTasksApi.list).mockRejectedValue(new Error('membership revoked'));
  render(<TeamBusinessTasks teamId="team" scopeKey="tenant:former-lead" />, { wrapper });
  await screen.findByRole('alert');
  expect(screen.queryByText('Reviewed title')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Hand over responsibility' })).toBeNull();
});

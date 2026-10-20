// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import TeamBusinessTasks from './TeamBusinessTasks';

const start = vi.fn();
const blank = vi.fn();
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_: string, fallback: string) => fallback }) }));
const context = { type: 'text', text: 'Reviewed shared goal', team_task_source: { disclosure_id: 'share-1' } };
vi.mock('../../api/domains/teamTasks', () => ({ teamTasksApi: {
  list: async () => ({ tasks: [{ id: 'task-1', agent_id: 'agent-1', title: 'Shared goal', description: 'Reviewed description',
    status: 'needs_reconciliation', work_context: context }], assignees: [] }),
} }));
vi.mock('../../api/domains/chat', () => ({ chatApi: {
  createSession: (...args: unknown[]) => blank(...args),
  createSessionRun: (...args: unknown[]) => start(...args),
} }));

afterEach(() => { cleanup(); vi.clearAllMocks(); });
function view() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <TeamBusinessTasks teamId="team-1" scopeKey="tenant-1:user-1" />
  </QueryClientProvider>);
}

it('reviews the shared goal before an explicit first input and retains its exact retry identity', async () => {
  start.mockRejectedValue(new Error('Unknown transport outcome'));
  view();
  fireEvent.click(await screen.findByRole('button', { name: 'Open my own work' }));
  expect(blank).not.toHaveBeenCalled();
  expect(start).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Your instructions'), { target: { value: 'Continue with a reviewed draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start in my own work' }));
  await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
  expect(start.mock.calls[0]).toEqual(['agent-1', expect.objectContaining({
    content: 'Continue with a reviewed draft', parts: [context], input_id: expect.any(String),
  })]);
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Start in my own work' }));
  await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
  expect(start.mock.calls[1]).toEqual(start.mock.calls[0]);
});

it('does not publish a late old-company work into a changed scope', async () => {
  let finish!: (value: unknown) => void;
  start.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (scopeKey: string) => <QueryClientProvider client={client}>
    <TeamBusinessTasks teamId="team-1" scopeKey={scopeKey} />
  </QueryClientProvider>;
  const page = render(content('tenant-1:user-1'));
  fireEvent.click(await screen.findByRole('button', { name: 'Open my own work' }));
  fireEvent.change(screen.getByLabelText('Your instructions'), { target: { value: 'Prepare a draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start in my own work' }));
  await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
  page.rerender(content('tenant-2:user-2'));
  await act(async () => finish({ session: { id: 'old-company-session' }, run: { run_id: 'run-1' } }));
  expect(screen.queryByRole('link', { name: 'Open submitted work' })).toBeNull();
});

it('opens the actual returned private work after the explicit submission', async () => {
  start.mockResolvedValue({ session: { id: 'my-work' }, run: { run_id: 'run-1' } });
  view();
  fireEvent.click(await screen.findByRole('button', { name: 'Open my own work' }));
  fireEvent.change(screen.getByLabelText('Your instructions'), { target: { value: 'Prepare a draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start in my own work' }));
  expect((await screen.findByRole('link', { name: 'Open submitted work' })).getAttribute('href'))
    .toBe('/agents/agent-1?session_id=my-work#chat');
});

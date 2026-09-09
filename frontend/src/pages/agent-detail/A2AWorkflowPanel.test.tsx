// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { a2aWorkflowApi } from '../../api/domains/a2aWorkflows';
import A2AWorkflowPanel from './A2AWorkflowPanel';

vi.mock('../../api/domains/chat', () => ({ chatApi: {
  listSessions: vi.fn(async () => [{ id: 'owned', title: 'Owned' }, { id: 'readonly', title: 'Read only', read_only: true }]),
  createSession: vi.fn(),
} }));
vi.mock('../../api/domains/a2aWorkflows', () => ({ a2aWorkflowApi: {
  list: vi.fn(async () => []), read: vi.fn(), preview: vi.fn(), start: vi.fn(), control: vi.fn(),
} }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('requires preview, retains the exact run identity after an ambiguous start, and invalidates edited previews', async () => {
  const preview = { definition: { name: 'Graph' }, args: { topic: 'x' }, definition_hash: 'hash',
    node_order: ['research', 'delivery'], admissible: true, collaboration_checks: [] };
  vi.mocked(a2aWorkflowApi.preview).mockResolvedValue(preview);
  vi.mocked(a2aWorkflowApi.start).mockRejectedValue(new Error('Transport unknown'));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><A2AWorkflowPanel agentId="coordinator" /></QueryClientProvider>);
  await screen.findByRole('option', { name: 'Owned' });
  expect(screen.queryByRole('option', { name: 'Read only' })).toBeNull();
  const start = screen.getByRole('button', { name: 'Start confirmed graph' }) as HTMLButtonElement;
  expect(start.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('A2A root session'), { target: { value: 'owned' } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview graph' }));
  await waitFor(() => expect(start.disabled).toBe(false));
  fireEvent.click(start);
  await screen.findByRole('alert');
  fireEvent.click(start);
  await waitFor(() => expect(a2aWorkflowApi.start).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(a2aWorkflowApi.start).mock.calls;
  expect(calls[0]).toEqual(calls[1]);
  expect(calls[0]).toEqual(['coordinator', 'owned', expect.any(String), preview]);
  await waitFor(() => expect(start.disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('A2A arguments'), { target: { value: '{"topic":"changed"}' } });
  expect(start.disabled).toBe(true);
  client.clear();
});

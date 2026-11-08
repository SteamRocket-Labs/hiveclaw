/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';
import { isDedicatedToolCardMessage } from './chatDisclosureReducer';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const api = vi.hoisted(() => ({ getApproval: vi.fn(), resolveApproval: vi.fn() }));
vi.mock('../../api/domains/chat', () => ({ chatApi: api }));
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });

it.each([
  ['requester_inactive', 'This account is inactive. No changes were made.'],
  ['user_prompt_submit_blocked', 'This input was blocked by a rule. Work did not continue.'],
  ['admission_refused', 'This input was not accepted. Work did not continue.'],
  ['agent_execute_denied', 'You do not have access to this employee.'],
  ['origin_run_not_suspended', 'The conversation has stopped or ended. Work did not continue.'],
  ['approval_origin_unavailable', 'The original conversation is unavailable. Work did not continue.'],
  ['human_input_secret_boundary_unavailable', 'Credential protection is unavailable. Try again later.'],
])('shows the known continuation failure %s on the card', async (reason, message) => {
  api.getApproval.mockResolvedValue({ id: 'ticket', status: 'approved', tool_name: 'write_file',
    execution_status: 'succeeded', can_resolve: false,
    execution_receipt: { continuation_status: 'needs_reconciliation', continuation_reason: reason } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const normalized = normalizeToolCallResult('write_file', { status: 'approval_required', approval_id: 'ticket' });
  render(<QueryClientProvider client={client}><StructuredToolResultBody agentId="agent" sessionId="work"
    toolMeta={normalized.toolMeta} /></QueryClientProvider>);
  await screen.findByText(message);
  expect(screen.queryByText(reason)).toBeNull();
});

it('keeps company approval in the original work and reconciles a failed response before retry', async () => {
  const pending = { id: 'exact-ticket', status: 'pending', tool_name: 'delegate_to_agent',
    execution_status: 'pending', can_resolve: true, action_type: 'agent.message.send', details: { reason: 'Send the recorded first task' } };
  api.getApproval.mockResolvedValue(pending);
  api.resolveApproval.mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValue({
    ...pending, status: 'approved', execution_status: 'queued', can_resolve: false,
  });
  const normalized = normalizeToolCallResult('delegate_to_agent', JSON.stringify({ status: 'approval_required', approval_id: 'exact-ticket' }));
  expect(normalized.toolMeta?.kind).toBe('enterprise_approval');
  expect(isDedicatedToolCardMessage({ role: 'tool_call', content: '', toolMeta: normalized.toolMeta })).toBe(true);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(<QueryClientProvider client={client}><StructuredToolResultBody agentId="original-agent" sessionId="original-work"
    toolMeta={normalized.toolMeta} /></QueryClientProvider>);
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
  expect((await screen.findByRole('alert')).textContent).toContain('Connection lost');
  await waitFor(() => expect(api.getApproval).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
  await screen.findByText('Queued');
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  expect(api.resolveApproval).toHaveBeenNthCalledWith(2, 'original-agent', 'original-work', 'exact-ticket', 'approve');
  api.getApproval.mockResolvedValue({ ...pending, status: 'approved', execution_status: 'succeeded', can_resolve: false,
    execution_receipt: { continuation_status: 'retrying' } });
  await client.invalidateQueries({ queryKey: ['session-approval', 'original-agent', 'original-work', 'exact-ticket'] });
  await screen.findByText('Succeeded');
  await screen.findByText('Continuation retrying');
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['hr-recoverable-drafts'] });
  expect(screen.queryByText('Approval result returned')).toBeNull(); // B1: only delivered results get this label.
});

it('shows the actual decision without offering authority to a requester who cannot approve', async () => {
  api.getApproval.mockResolvedValue({ id: 'ticket', status: 'pending', execution_status: 'pending', can_resolve: false });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const normalized = normalizeToolCallResult('read_file', { status: 'approval_required', approval_id: 'ticket' });
  render(<QueryClientProvider client={client}><StructuredToolResultBody agentId="agent" sessionId="work"
    toolMeta={normalized.toolMeta} /></QueryClientProvider>);
  await screen.findByText('Waiting for the responsible approver.');
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  expect(api.resolveApproval).not.toHaveBeenCalled();
});

it('keeps a pending requester line polling and updates it when the decision lands', async () => {
  // The requester's waiting line polls exactly as on the pre-trim baseline:
  // company approvals resolve through their own notification path, so the line
  // itself must observe the outcome (P3 keeps it a single plain line).
  api.getApproval.mockResolvedValueOnce({ id: 'ticket', status: 'pending', execution_status: 'pending', can_resolve: false })
    .mockResolvedValue({ id: 'ticket', status: 'approved', execution_status: 'queued', can_resolve: false });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const normalized = normalizeToolCallResult('read_file', { status: 'approval_required', approval_id: 'ticket' });
  // Fake timers must be live before the first fetch so the poll interval is
  // scheduled on the mocked clock.
  vi.useFakeTimers();
  render(<QueryClientProvider client={client}><StructuredToolResultBody agentId="agent" sessionId="work" toolMeta={normalized.toolMeta} /></QueryClientProvider>);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText('Waiting for the responsible approver.')).toBeTruthy();
  expect(api.getApproval).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(3_100); });
  vi.useRealTimers();
  expect(screen.getByText('Company approval')).toBeTruthy();
  expect(screen.getByText('Approved')).toBeTruthy();
  expect(api.getApproval.mock.calls.length).toBeGreaterThanOrEqual(2);
  expect(screen.queryByText('Waiting for the responsible approver.')).toBeNull();
});

/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';
import { isDedicatedToolCardMessage } from './chatDisclosureReducer';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const api = vi.hoisted(() => ({ getApproval: vi.fn(), resolveApproval: vi.fn() }));
vi.mock('../../api/domains/chat', () => ({ chatApi: api }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

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
  expect(screen.queryByText('Original session resumed')).toBeNull();
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

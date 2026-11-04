/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { approvalResolutionEventId, useApprovalResolutionRefresh } from './useApprovalResolutionRefresh';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';
import type { AgentChatMessage } from './chatRuntime';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const api = vi.hoisted(() => ({ getApproval: vi.fn(), resolveApproval: vi.fn() }));
vi.mock('../../api/domains/chat', () => ({ chatApi: api }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const resolutionEvent = (id: string): AgentChatMessage => ({ role: 'event', content: '', id, transcriptEventId: id,
  eventType: 'permission_resolved', eventStatus: 'session_permission_decision' });

it('identifies the newest approval-completed event and nothing else', () => {
  const noise: AgentChatMessage[] = [
    { role: 'user', content: 'continue', id: 'input-1' },
    { role: 'assistant', content: 'working', id: 'reply-1' },
  ];
  expect(approvalResolutionEventId(noise)).toBe('');
  expect(approvalResolutionEventId([...noise, resolutionEvent('decision-1')])).toBe('decision-1');
  expect(approvalResolutionEventId([...noise, resolutionEvent('decision-1'), resolutionEvent('decision-2')])).toBe('decision-2');
});

it('invalidates once per distinct approval-completed event', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  const { rerender } = renderHook(
    ({ messages }: { messages: AgentChatMessage[] }) => useApprovalResolutionRefresh('agent', 'work', messages, client),
    { initialProps: { messages: [] as AgentChatMessage[] }, wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> },
  );
  expect(invalidate).not.toHaveBeenCalled();
  act(() => rerender({ messages: [resolutionEvent('decision-1')] }));
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['session-approval', 'agent', 'work'] });
  expect(invalidate).toHaveBeenCalledTimes(1);
  // Re-renders with the same newest event do not invalidate again.
  act(() => rerender({ messages: [{ role: 'user', content: 'later', id: 'input-2' }, resolutionEvent('decision-1')] }));
  expect(invalidate).toHaveBeenCalledTimes(1);
  act(() => rerender({ messages: [resolutionEvent('decision-1'), resolutionEvent('decision-2')] }));
  expect(invalidate).toHaveBeenCalledTimes(2);
});

it('refreshes a requester waiting line from the approval-completed session event without polling', async () => {
  api.getApproval.mockResolvedValue({ id: 'ticket', status: 'pending', execution_status: 'pending', can_resolve: false });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const normalized = normalizeToolCallResult('read_file', { status: 'approval_required', approval_id: 'ticket' });
  expect(normalized.toolMeta?.kind).toBe('enterprise_approval');
  function Page({ messages }: { messages: AgentChatMessage[] }) {
    return <QueryClientProvider client={client}>
      <ApprovalRefreshHarness client={client} agentId="agent" sessionId="work" messages={messages} />
      <StructuredToolResultBody agentId="agent" sessionId="work" toolMeta={normalized.toolMeta} />
    </QueryClientProvider>;
  }
  const view = render(<Page messages={[]} />);
  await screen.findByText('Waiting for the responsible approver.');
  expect(api.getApproval).toHaveBeenCalledTimes(1);
  // Someone else decides; the decision arrives as a session event.
  api.getApproval.mockResolvedValue({ id: 'ticket', status: 'approved', execution_status: 'queued', can_resolve: false });
  view.rerender(<Page messages={[resolutionEvent('decision-1')]} />);
  await screen.findByText('Company approval');
  await screen.findByText('Approved');
  expect(api.getApproval).toHaveBeenCalledTimes(2);
});

function ApprovalRefreshHarness({ client, agentId, sessionId, messages }: {
  client: QueryClient; agentId: string; sessionId: string; messages: AgentChatMessage[];
}) {
  useApprovalResolutionRefresh(agentId, sessionId, messages, client);
  return null;
}

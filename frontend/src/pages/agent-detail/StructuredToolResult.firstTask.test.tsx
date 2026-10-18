/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const hr = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: hr }));
const chat = vi.hoisted(() => ({ answerQuestion: vi.fn() }));
vi.mock('../../api/domains/chat', () => ({ chatApi: chat }));

import { StructuredToolResultBody } from './StructuredToolResult';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { normalizeToolCallResult } from './toolResultEnvelope';
import { useAuthStore } from '../../stores';

afterEach(() => { cleanup(); vi.clearAllMocks(); useAuthStore.setState({ user: null }); });

it('refreshes the existing creation and directory after scoped first-task approval', async () => {
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(<QueryClientProvider client={client}><StructuredToolResultBody
    agentId="original-source" toolName="delegate_to_agent"
    toolMeta={{ kind: 'employee_first_task', blueprintId: 'same-draft', runtimeTaskId: 'same-child' }}
  /></QueryClientProvider>);
  expect(screen.getByText('Task dispatched. Updates and results return to this work.')).toBeTruthy();
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['hr-creation-draft', 'original-source', 'same-draft'] }));
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['hr-recoverable-drafts'] });
  expect(screen.queryByRole('link')).toBeNull();
});

it('replaces the blocked first-task state on the already mounted original card', async () => {
  const draft = { status: 'preview', blueprint_id: 'same-draft', draft_status: 'completed',
    blueprint: { name: 'Research colleague', first_task_autostart: false },
    creation_authorization: { mode: 'policy_direct', first_task: 'Draft a comparison' },
    provisioning: { first_task: { status: 'blocked' } }, recovery: { can_retry: true } };
  const preview = normalizeToolCallResult('create_employee_from_request', draft).toolMeta;
  if (preview?.kind !== 'hr_preview') throw new Error('Missing creation preview');
  hr.get.mockResolvedValue(draft);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const card = <HrBlueprintPreviewCard agentId="original-source" preview={preview} />;
  const view = render(<QueryClientProvider client={client}>{card}</QueryClientProvider>);
  await screen.findByRole('button', { name: 'Retry first task' });
  hr.get.mockResolvedValue({ ...draft, provisioning: { first_task: { status: 'dispatched' } }, recovery: { can_retry: false } });
  view.rerender(<QueryClientProvider client={client}>{card}<StructuredToolResultBody
    agentId="original-source" toolName="delegate_to_agent"
    toolMeta={{ kind: 'employee_first_task', blueprintId: 'same-draft', runtimeTaskId: 'same-child' }}
  /></QueryClientProvider>);
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry first task' })).toBeNull());
  expect(screen.getByText('Draft a comparison')).toBeTruthy();
  expect(hr.get).toHaveBeenCalledWith('original-source', 'same-draft');
});

it('answers in the original creation card and reuses the input ID after a network failure', async () => {
  const draft = { status: 'preview', blueprint_id: 'same-draft', draft_status: 'completed',
    requested_by_user_id: 'owner', blueprint: { name: 'Research colleague' }, creation_authorization: { mode: 'policy_direct', first_task: 'Draft a comparison' },
    first_task_work: { runtime_task_id: 'same-child', agent_id: 'created-employee', session_id: 'child-session',
      status: 'suspended', can_answer: true, question: { item_id: 'child-question', payload: {
        status: 'awaiting_user_clarification', questions: [{ header: 'Audience', question: 'Which audience?',
          options: [{ label: 'Team' }, { label: 'Personal' }] }],
      } } } };
  const preview = normalizeToolCallResult('create_employee_from_request', draft).toolMeta;
  if (preview?.kind !== 'hr_preview') throw new Error('Missing creation preview');
  useAuthStore.setState({ user: { id: 'owner' } as any });
  hr.get.mockResolvedValue(draft);
  chat.answerQuestion.mockRejectedValueOnce(new Error('Network interrupted'))
    .mockResolvedValueOnce({ admission_state: 'rejected', status: 'rejected' }).mockImplementationOnce(async () => {
    hr.get.mockResolvedValue({ ...draft, first_task_work: { ...draft.first_task_work, status: 'running', question: null } });
    return { admission_state: 'admitted' };
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="original-source" preview={preview} /></QueryClientProvider>);
  await screen.findByText('Which audience?');
  fireEvent.click(screen.getByRole('radio', { name: /Team/ }));
  fireEvent.click(screen.getByRole('button', { name: /Submit/ }));
  await screen.findByText('Network interrupted');
  fireEvent.click(screen.getByRole('button', { name: /Submit/ }));
  await screen.findByText('The answer is stored but could not be applied. Retry or review this work.');
  fireEvent.click(screen.getByRole('button', { name: /Submit/ }));
  await screen.findByText('Working');
  expect(chat.answerQuestion).toHaveBeenCalledTimes(3);
  expect(chat.answerQuestion.mock.calls[0]).toEqual(chat.answerQuestion.mock.calls[1]);
  expect(chat.answerQuestion.mock.calls[1]).toEqual(chat.answerQuestion.mock.calls[2]);
  expect(chat.answerQuestion.mock.calls[0]).toEqual(['created-employee', 'child-session', 'child-question', 'Audience: Team', expect.any(String)]);
  expect(screen.queryByText('Which audience?')).toBeNull();
  expect(screen.queryByRole('link')).toBeNull();
});

it('keeps a readable question read-only when execution access is unavailable', async () => {
  const draft = { status: 'preview', blueprint_id: 'same-draft', draft_status: 'completed', blueprint: { name: 'Colleague' },
    requested_by_user_id: 'owner',
    creation_authorization: { mode: 'policy_direct', first_task: 'Draft a comparison' },
    first_task_work: { runtime_task_id: 'same-child', agent_id: 'created-employee', session_id: 'child-session', status: 'suspended',
      can_answer: false, question: { item_id: 'child-question', payload: { status: 'awaiting_user_clarification',
        questions: [{ header: 'Audience', question: 'Which audience?', options: [{ label: 'Team' }] }] } } } };
  const preview = normalizeToolCallResult('create_employee_from_request', draft).toolMeta;
  if (preview?.kind !== 'hr_preview') throw new Error('Missing creation preview');
  useAuthStore.setState({ user: { id: 'owner' } as any });
  hr.get.mockResolvedValue(draft);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="original-source" preview={preview} /></QueryClientProvider>);
  await screen.findByText('Which audience?');
  expect(screen.queryByRole('button', { name: /Submit/ })).toBeNull();
  expect(chat.answerQuestion).not.toHaveBeenCalled();
  hr.get.mockRejectedValue(new Error('Access denied'));
  act(() => useAuthStore.setState({ user: { id: 'other-user' } as any }));
  await waitFor(() => expect(screen.queryByText('Which audience?')).toBeNull());
});

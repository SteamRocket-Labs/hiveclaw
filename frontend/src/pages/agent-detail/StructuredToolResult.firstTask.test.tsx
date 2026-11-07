/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
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
  expect(hr.get).toHaveBeenCalledWith('original-source', 'same-draft');
});

it.each([true, false])('opens a normal conversation with the employee (first task can answer: %s)', async (canAnswer) => {
  const draft = { status: 'preview', blueprint_id: 'same-draft', draft_status: 'completed',
    requested_by_user_id: 'owner', created_agent_id: 'created-employee', blueprint: { name: 'Research colleague' },
    creation_authorization: { mode: 'policy_direct', first_task: 'Draft a comparison' },
    provisioning: { first_task: { status: 'dispatched', session_id: 'child-session' } },
    first_task_work: { runtime_task_id: 'same-child', agent_id: 'created-employee', session_id: 'child-session',
      status: 'suspended', can_answer: canAnswer, question: { item_id: 'child-question', payload: {
        status: 'awaiting_user_clarification', questions: [{ header: 'Audience', question: 'Which audience?',
          options: [{ label: 'Team' }] }],
      } } } };
  const preview = normalizeToolCallResult('create_employee_from_request', draft).toolMeta;
  if (preview?.kind !== 'hr_preview') throw new Error('Missing creation preview');
  useAuthStore.setState({ user: { id: 'owner' } as any });
  hr.get.mockResolvedValue(draft);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="original-source" preview={preview} /></QueryClientProvider>);
  expect((await screen.findByRole('link', { name: 'Talk to it' })).getAttribute('href'))
    .toBe('/agents/created-employee#chat');
  expect(screen.queryByText('Which audience?')).toBeNull();
  expect(screen.queryByRole('button', { name: /Submit/ })).toBeNull();
  expect(chat.answerQuestion).not.toHaveBeenCalled();
});

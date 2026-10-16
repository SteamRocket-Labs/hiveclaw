/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const hr = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: hr }));

import { StructuredToolResultBody } from './StructuredToolResult';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { normalizeToolCallResult } from './toolResultEnvelope';

afterEach(cleanup);

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

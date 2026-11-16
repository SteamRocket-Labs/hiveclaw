// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { parsePreviewAgentBlueprintResult } from './toolResultEnvelope';

const api = vi.hoisted(() => ({ get: vi.fn(), retry: vi.fn() }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: api }));
vi.mock('../../stores', () => ({ useAuthStore: () => undefined }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => { await i18n.init({ lng: 'zh', resources: { zh: { translation: zh }, en: { translation: en } }, initImmediate: false }); });
let client: QueryClient;
afterEach(() => { cleanup(); client?.clear(); vi.clearAllMocks(); });

// Run23 step10 create_employee_from_request receipt (seq 69) starts with
// waiting_for_employee. The card then sees provisioning before dispatch.
const receipt = { status: 'preview', blueprint_id: 'draft-x7f', blueprint_version: 1,
  draft_status: 'awaiting_confirmation', blueprint: { name: '报告助手', permission_scope: 'self' },
  creation_authorization: { mode: 'policy_direct', first_task: 'Prepare a report.' },
  provisioning: { first_task: { status: 'waiting_for_employee', auto_dispatch: true, authorization: 'original_human_request' } } };
const preview = parsePreviewAgentBlueprintResult(receipt)!;
const queryKey = ['hr-creation-draft', 'source-x7f', 'draft-x7f', undefined];

it.each(['zh', 'en'])('X7f: %s never offers first-task retry during creation, pending work or dispatch', async language => {
  await i18n.changeLanguage(language);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  const draft = { ...receipt, draft_status: 'confirmed', recovery: { can_retry: false } };
  api.get.mockResolvedValue(draft);
  client.setQueryData(queryKey, draft);
  const view = render(<MemoryRouter><QueryClientProvider client={client}>
    <HrBlueprintPreviewCard agentId="source-x7f" preview={preview} />
  </QueryClientProvider></MemoryRouter>);
  for (const [draftStatus, firstStatus] of [
    ['confirmed', 'waiting_for_employee'], ['creating', 'waiting_for_employee'],
    ['provisioning', 'waiting_for_employee'], ['provisioning', 'pending'],
    ['provisioning', 'running'], ['completed', 'dispatched'], ['completed', 'running'],
  ]) {
    await act(async () => { client.setQueryData(queryKey, { ...draft, draft_status: draftStatus,
      created_agent_id: 'employee-x7f', provisioning: { first_task: { status: firstStatus } } }); });
    expect.soft(view.queryByRole('button', { name: i18n.t('agent.chat.toolResults.retryFirstTask') }),
      `${draftStatus}/${firstStatus}`).toBeNull();
  }
  expect(api.retry).not.toHaveBeenCalled();
});

it.each(['blocked', 'unknown'])('X7f: preserves the existing explicit retry contract for a %s first task', async status => {
  await i18n.changeLanguage('zh');
  client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  const draft = { ...receipt, draft_status: 'completed', created_agent_id: 'employee-x7f',
    provisioning: { first_task: { status } }, recovery: { can_retry: true, task_status: 'failed' } };
  api.get.mockResolvedValue(draft);
  api.retry.mockResolvedValue({ ...draft, recovery: { can_retry: false } });
  client.setQueryData(queryKey, draft);
  const view = render(<MemoryRouter><QueryClientProvider client={client}>
    <HrBlueprintPreviewCard agentId="source-x7f" preview={preview} />
  </QueryClientProvider></MemoryRouter>);
  await act(async () => { fireEvent.click(view.getByRole('button', { name: '重试首任务' })); });
  expect(api.retry).toHaveBeenCalledExactlyOnceWith('source-x7f', 'draft-x7f');
});

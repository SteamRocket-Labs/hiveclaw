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
const surfaces = ['zh', 'en'].flatMap(language => [true, false].map(direct => ({ language, direct })));
function draftClient(draft: unknown) {
  const next = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnMount: false } } });
  api.get.mockResolvedValue(draft);
  next.setQueryData(queryKey, draft);
  return next;
}
const card = (value = preview) => <MemoryRouter><QueryClientProvider client={client}>
  <HrBlueprintPreviewCard agentId="source-x7f" preview={value} />
</QueryClientProvider></MemoryRouter>;

it.each(surfaces)('X7f: $language direct=$direct hides first-task retry without recovery permission live and after remount', async ({ language, direct }) => {
  await i18n.changeLanguage(language);
  const draft = { ...receipt, creation_authorization: direct ? receipt.creation_authorization : undefined,
    draft_status: 'confirmed', recovery: { can_retry: false } };
  const surfacePreview = parsePreviewAgentBlueprintResult(draft)!;
  client = draftClient(draft);
  let view = render(card(surfacePreview));
  for (const [draftStatus, firstStatus] of [
    ['confirmed', 'waiting_for_employee'], ['creating', 'waiting_for_employee'],
    ['provisioning', 'waiting_for_employee'], ['provisioning', 'pending'],
    ['provisioning', 'running'], ['completed', 'dispatched'], ['completed', 'running'],
    ['failed', 'waiting_for_employee'],
  ]) {
    const nextDraft = { ...draft, draft_status: draftStatus,
      created_agent_id: 'employee-x7f', provisioning: { first_task: { status: firstStatus } } };
    await act(async () => { client.setQueryData(queryKey, nextDraft); });
    view.rerender(card(surfacePreview));
    expect.soft(view.queryByRole('button', { name: i18n.t('agent.chat.toolResults.retryFirstTask') }),
      `live ${draftStatus}/${firstStatus}`).toBeNull();
    view.unmount();
    client.clear();
    client = draftClient(nextDraft);
    view = render(card(surfacePreview));
    expect.soft(view.queryByRole('button', { name: i18n.t('agent.chat.toolResults.retryFirstTask') }),
      `remount ${draftStatus}/${firstStatus}`).toBeNull();
  }
  expect(api.retry).not.toHaveBeenCalled();
});

it.each(surfaces)('X7f: $language direct=$direct retries an explicitly retryable failed first task live and after remount', async ({ language, direct }) => {
  await i18n.changeLanguage(language);
  const draft = { ...receipt, creation_authorization: direct ? receipt.creation_authorization : undefined,
    draft_status: 'confirmed', created_agent_id: 'employee-x7f', recovery: { can_retry: false } };
  const retryable = { ...draft, draft_status: 'provisioning', provisioning: { first_task: { status: 'failed' } },
    recovery: { can_retry: true, task_status: 'failed' } };
  const surfacePreview = parsePreviewAgentBlueprintResult(draft)!;
  client = draftClient(draft);
  let view = render(card(surfacePreview));
  for (const phase of ['live', 'remount']) {
    if (phase === 'live') {
      await act(async () => { client.setQueryData(queryKey, retryable); });
    } else {
      view.unmount();
      client.clear();
      client = draftClient(retryable);
      view = render(card(surfacePreview));
    }
    api.retry.mockClear();
    api.retry.mockResolvedValue({ ...retryable, recovery: { can_retry: false } });
    const retry = await view.findByRole('button', { name: i18n.t('agent.chat.toolResults.retryFirstTask') });
    expect(retry.hasAttribute('disabled')).toBe(false);
    await act(async () => { fireEvent.click(retry); });
    expect(api.retry).toHaveBeenCalledExactlyOnceWith('source-x7f', 'draft-x7f');
  }
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

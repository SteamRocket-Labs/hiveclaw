// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

import type { HrCreationDraft } from '../../api/domains/hrCreation';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { parsePreviewAgentBlueprintResult } from './toolResultEnvelope';

const api = vi.hoisted(() => ({ get: vi.fn(), confirm: vi.fn(), retry: vi.fn() }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: api }));
vi.mock('../../stores', () => ({ useAuthStore: () => undefined }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: Parameters<typeof translateFromCatalog>[2]) => translateFromCatalog(zh, key, fallback),
}) }));

// direct_employee_creation writes this placeholder before returning the proposal.
const proposal: HrCreationDraft = {
  status: 'preview', blueprint_id: 'draft-x7g', blueprint_version: 3, blueprint_hash: 'hash-x7g',
  draft_status: 'awaiting_confirmation', blueprint: { name: '报告助手', permission_scope: 'self' },
  creation_authorization: { mode: 'policy_direct', first_task: '用一句话介绍自己能做什么' },
  provisioning: { first_task: {
    status: 'waiting_for_employee', auto_dispatch: true, authorization: 'original_human_request',
  } },
};
const clients: QueryClient[] = [];

function mount(draft: HrCreationDraft) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  api.get.mockResolvedValue(draft);
  const preview = parsePreviewAgentBlueprintResult(proposal)!;
  return render(<MemoryRouter><QueryClientProvider client={client}>
    <HrBlueprintPreviewCard agentId="source-x7g" preview={preview} />
  </QueryClientProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  clients.splice(0).forEach(client => client.clear());
  vi.clearAllMocks();
});

it.each([undefined, false])('X7g: confirms a fetched first-task proposal with can_retry=%s initially and after reopening', async canRetry => {
  const draft = { ...proposal, ...(canRetry === undefined ? {} : { recovery: {
    can_retry: canRetry, can_resume: false, can_abandon: true, requires_operator: false,
  } }) };
  const confirmed = { ...draft, draft_status: 'confirmed' };
  api.confirm.mockResolvedValue(confirmed);
  let view = mount(draft);
  for (const phase of ['initial', 'reopened']) {
    if (phase === 'reopened') {
      view.unmount();
      view = mount(draft);
    }
    const confirm = await screen.findByRole('button', { name: '确认并创建' });
    expect((confirm as HTMLButtonElement).disabled, phase).toBe(false);
    expect(screen.getByText('用一句话介绍自己能做什么')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重试首任务' })).toBeNull();
    expect(api.confirm).not.toHaveBeenCalled();
  }
  fireEvent.click(screen.getByRole('button', { name: '确认并创建' }));
  await waitFor(() => expect(api.confirm).toHaveBeenCalledExactlyOnceWith('source-x7g', 'draft-x7g', { blueprint_version: 3 }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '确认并创建' })).toBeNull());
  expect(api.retry).not.toHaveBeenCalled();
});

const unchangedStatuses = [
  ['confirmed', '正在创建…'], ['provisioning', '正在创建'], ['completed', '已创建'],
  ['failed', '需要处理'], ['rejected', '已拒绝'], ['expired', '已过期'],
] as const;

it.each(unchangedStatuses)('X7g: keeps the %s primary button unchanged with a first-task placeholder', async (status, label) => {
  mount({ ...proposal, draft_status: status, creation_authorization: null });
  const button = await screen.findByRole('button', { name: label });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole('button', { name: '确认并创建' })).toBeNull();
  expect(screen.queryByRole('button', { name: '重试首任务' })).toBeNull();
  expect(api.confirm).not.toHaveBeenCalled();
  expect(api.retry).not.toHaveBeenCalled();
});

it.each(unchangedStatuses)('X7g: keeps the direct %s card without creation or retry buttons', async (status) => {
  const { container } = mount({ ...proposal, draft_status: status });
  await waitFor(() => expect(container.querySelector('footer')).not.toBeNull());
  expect(screen.queryAllByRole('button')).toHaveLength(0);
  expect(api.confirm).not.toHaveBeenCalled();
  expect(api.retry).not.toHaveBeenCalled();
});

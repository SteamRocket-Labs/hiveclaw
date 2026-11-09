/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import type { HrCreationDraft } from '../../api/domains/hrCreation';

const api = vi.hoisted(() => ({ get: vi.fn(), confirm: vi.fn() }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: api }));
vi.mock('../../stores', () => ({ useAuthStore: () => undefined }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: Parameters<typeof translateFromCatalog>[2]) => translateFromCatalog(zh, key, fallback),
}) }));

import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { parsePreviewAgentBlueprintResult } from './toolResultEnvelope';

const pendingDraft: HrCreationDraft = {
  blueprint_id: 'draft', blueprint_version: 1, blueprint_hash: 'hash',
  draft_status: 'awaiting_confirmation', blueprint: { name: '周报助手', permission_scope: 'self' },
};
const preview = parsePreviewAgentBlueprintResult({ status: 'preview', ...pendingDraft })!;

function mount(snapshot = preview) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<MemoryRouter><QueryClientProvider client={client}>
    <HrBlueprintPreviewCard agentId="source" preview={snapshot} onSendMessage={vi.fn()} />
  </QueryClientProvider></MemoryRouter>);
  return { ...view, client };
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('never renders stale confirmation text or actions while reopening a created draft', async () => {
  let resolveDraft!: (draft: HrCreationDraft) => void;
  api.get.mockReturnValue(new Promise<HrCreationDraft>((resolve) => { resolveDraft = resolve; }));
  const { container } = mount();
  const frames = [container.innerHTML];
  const observer = new MutationObserver(() => frames.push(container.innerHTML));
  observer.observe(container, { subtree: true, childList: true, characterData: true, attributes: true });
  try {
    expect(screen.queryByText('等待你确认')).toBeNull();
    expect(screen.getByText('加载中...')).toBeTruthy();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    await act(async () => resolveDraft({ ...pendingDraft, draft_status: 'completed', created_agent_id: 'employee' }));
    await screen.findByRole('button', { name: '前往详情页' });
    expect(container.textContent).toContain('已创建');
    frames.push(container.innerHTML);
    for (const frame of frames) {
      expect(frame).not.toContain('等待你确认');
      expect(frame).not.toContain('确认并创建');
      expect(frame).not.toContain('要求修改');
      expect(frame).not.toContain('拒绝');
    }
    expect(api.confirm).not.toHaveBeenCalled();
  } finally {
    observer.disconnect();
  }
});

it('confirms a fresh unconfirmed draft after its current record arrives', async () => {
  api.get.mockResolvedValue(pendingDraft);
  api.confirm.mockResolvedValue({ ...pendingDraft, draft_status: 'completed', created_agent_id: 'employee' });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: '确认并创建' }));
  await screen.findByRole('button', { name: '前往详情页' });
  expect(api.confirm).toHaveBeenCalledExactlyOnceWith('source', 'draft', { blueprint_version: 1 });
});

it('keeps a settled preview visible without confirmation actions during loading', () => {
  api.get.mockReturnValue(new Promise(() => {}));
  mount({ ...preview, status: 'completed' });
  expect(screen.getAllByText('已创建').length).toBeGreaterThan(0);
  expect(screen.queryByText('等待你确认')).toBeNull();
  expect(screen.queryByRole('button', { name: '确认并创建' })).toBeNull();
});

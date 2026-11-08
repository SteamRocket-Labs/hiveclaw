/** @vitest-environment jsdom */
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { ApiError } from '../../api/core';

const api = vi.hoisted(() => ({ get: vi.fn(), confirm: vi.fn() }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: api }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string) => translateFromCatalog(zh, key, fallback),
}) }));

import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { normalizeToolCallResult, parsePreviewAgentBlueprintResult } from './toolResultEnvelope';

function mount(status = 'awaiting_confirmation', d1 = true, failure?: Record<string, unknown>) {
  const proposal = { status: 'preview', blueprint_id: 'draft', blueprint_version: 1,
    draft_status: 'awaiting_confirmation', blueprint: { name: '周报助手', permission_scope: 'self' },
    message: '创建方案在上面的卡片里。',
    ...(d1 ? { creation_authorization: { mode: 'policy_direct' } } : {}) };
  api.get.mockResolvedValue({ ...proposal, draft_status: status, created_agent_id: status === 'completed' ? 'employee' : null, failure });
  const preview = parsePreviewAgentBlueprintResult(proposal)!;
  const savedReply = normalizeToolCallResult('create_employee_from_request', proposal).displayResult;
  return render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <p>{savedReply}</p><HrBlueprintPreviewCard agentId="source" preview={preview} onSendMessage={vi.fn()} />
  </QueryClientProvider></MemoryRouter>);
}
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('offers confirm and reject without a revise action on a pending D1 card with no first task', async () => {
  mount();
  expect(await screen.findByRole('button', { name: '确认并创建' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '拒绝' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '要求修改' })).toBeNull();
});

it('keeps revision available on the HR preview path', async () => {
  mount('awaiting_confirmation', false);
  expect(await screen.findByRole('button', { name: '要求修改' })).toBeTruthy();
});

it.each(['completed', 'rejected'])('keeps the saved pending reply true after reload into %s', async (status) => {
  mount(status);
  await screen.findByText(status === 'completed' ? '已创建' : '已拒绝');
  expect(screen.getAllByText('创建方案在上面的卡片里。')).toHaveLength(1);
  expect(document.body.textContent).not.toContain('请在卡片中确认并创建数字员工。');
  expect(screen.queryByRole('button', { name: '确认并创建' })).toBeNull();
});

it.each([
  ['expired', '创建方案已过期，请重新提出创建请求。'],
  ['missing_gates', '创建方案的信息不完整，请补充后再确认。'],
  ['capability_denied', '公司规则不允许创建数字员工，请联系管理员。'],
  ['approval_required', '创建数字员工需要公司批准，请联系管理员。'],
])('shows the known %s reason in Chinese', async (code, message) => {
  api.confirm.mockRejectedValue(new ApiError(409, 'creation gates incomplete: activation bp_private',
    { error: code, reason_code: code, message: 'internal English reason' }));
  mount();
  fireEvent.click(await screen.findByRole('button', { name: '确认并创建' }));
  expect((await screen.findByRole('alert')).textContent).toBe(message);
  expect(document.body.textContent).not.toMatch(/creation gates|activation|bp_private|刷新/);
});

it.each([
  ['capability_denied', '公司规则不允许创建数字员工，请联系管理员。'],
  ['approval_required', '创建数字员工需要公司批准，请联系管理员。'],
])('uses the known %s reason on a failed card without a stored sentence', async (code, message) => {
  mount('failed', true, { code });
  expect((await screen.findByRole('alert')).textContent).toBe(message);
  expect(screen.getByRole('alert').textContent).not.toMatch(/重试|刷新/);
});

// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import HrCreationRecoveryPanel from './HrCreationRecoveryPanel';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { ApiError } from '../../api/core';
import { useAuthStore } from '../../stores';
import type { User } from '../../types';

const mocks = vi.hoisted(() => ({
  getHr: vi.fn(), listMine: vi.fn(), listRecoverable: vi.fn(), retry: vi.fn(), abandon: vi.fn(),
  confirm: vi.fn(), showAppToast: vi.fn(),
}));
vi.mock('../../api/domains/agents', () => ({ agentApi: { getHrAgent: mocks.getHr } }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: mocks }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  // The panel is a member surface (P3): assertions run against the zh catalog.
  t: (key: string, fallback?: any, options?: any) => translateFromCatalog(zh, key, fallback, options),
}) }));
vi.mock('../../components/AppDialogs', () => ({ requestAppConfirm: mocks.confirm, showAppToast: mocks.showAppToast }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const draft = {
  blueprint_id: 'draft-origin', hr_agent_id: 'my-assistant', session_id: 'original-work',
  draft_status: 'failed', blueprint: { name: 'Research colleague' },
  recovery: { can_resume: true, can_retry: true },
};

function mount(records: unknown[] = [draft]) {
  useAuthStore.setState({ user: { id: "owner", tenant_id: "company", role: "member" } as User });
  mocks.getHr.mockResolvedValue({ id: 'system-hr' });
  mocks.listMine.mockResolvedValue(records);
  mocks.listRecoverable.mockResolvedValue([draft]);
  mocks.retry.mockResolvedValue(draft);
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><HrCreationRecoveryPanel /></MemoryRouter>
  </QueryClientProvider>);
}

it('reopens the actual originating work without requiring a System HR session', async () => {
  mount();
  const link = await screen.findByRole('link');
  expect(link.getAttribute('href')).toBe('/agents/my-assistant?session_id=original-work#chat');
  expect(mocks.listMine).toHaveBeenCalled();
  // zh catalog value of agent.chat.toolResults.statusFailed (the mock now reads zh).
  expect(screen.getByText('需要处理')).toBeTruthy();
  expect(mocks.getHr).not.toHaveBeenCalled();
});

it('retries the draft under its stored originating employee', async () => {
  mount();
  fireEvent.click(await screen.findByRole('button', { name: '重试创建' }));
  await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith('my-assistant', 'draft-origin'));
});

it('recovers the first task without removing or reopening employee provisioning', async () => {
  const completed = { ...draft, draft_status: 'completed', provisioning: { first_task: { status: 'blocked' } } };
  mount([completed]);
  fireEvent.click(await screen.findByRole('button', { name: '重试首任务' }));
  await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith('my-assistant', 'draft-origin'));
  // zh catalog value of employees.hrRecovery.remove.
  expect(screen.queryByRole('button', { name: '放弃未完成的创建' })).toBeNull();
});

it('maps the refused abandon to one Chinese sentence from the error code, never the server text', async () => {
  mount([{ ...draft, recovery: { can_resume: true, can_retry: true, can_abandon: true } }]);
  mocks.confirm.mockResolvedValue(true);
  mocks.abandon.mockRejectedValue(new ApiError(409, 'Employee retirement requires a company administrator.', {
    code: 'agent_retirement_administrator_required',
    recovery: { employee_id: 'created', management_path: '/enterprise/digital-employees', can_manage_retirement: false },
  }));
  fireEvent.click(await screen.findByRole('button', { name: '放弃未完成的创建' }));
  await waitFor(() => expect(mocks.showAppToast).toHaveBeenCalledTimes(1));
  const [toastText] = mocks.showAppToast.mock.calls[0];
  expect(toastText).toBe(zh.employees.hrRecovery.retirementAdministrator);
  expect(toastText).not.toMatch(/administrator|retirement|provisioning/i);
});

it('maps an unrecognized refusal code to a plain Chinese retry line', async () => {
  mount([{ ...draft, recovery: { can_resume: true, can_retry: true, can_abandon: true } }]);
  mocks.confirm.mockResolvedValue(true);
  mocks.abandon.mockRejectedValue(new ApiError(409, 'Provisioning cannot be abandoned from provisioning.', {
    error: 'totally_new_code', message: 'Provisioning cannot be abandoned from provisioning.',
  }));
  fireEvent.click(await screen.findByRole('button', { name: '放弃未完成的创建' }));
  await waitFor(() => expect(mocks.showAppToast).toHaveBeenCalledTimes(1));
  const [toastText] = mocks.showAppToast.mock.calls[0];
  expect(toastText).toBe(zh.employees.hrRecovery.errorGeneric);
  expect(toastText).not.toMatch(/provisioning|abandon/i);
});

it('shows the stored failure through its stable code in Chinese instead of the server sentence', async () => {
  mount([{ ...draft, failure: { code: 'missing_confirmation_evidence', message: 'Provisioning cannot resume without authenticated blueprint confirmation.' } }]);
  await screen.findByRole('button', { name: '重试创建' });
  expect(screen.getByText(zh.employees.hrRecovery.failureMissingConfirmation)).toBeTruthy();
  expect(screen.queryByText(/Provisioning cannot resume/)).toBeNull();
});

it('keeps a legacy failed draft without a failure code on the generic Chinese line', async () => {
  mount([{ ...draft, failure: null, recovery: { can_resume: true, can_retry: true, reason: 'Legacy failed HR draft requires an explicit retry.' } }]);
  await screen.findByRole('button', { name: '重试创建' });
  expect(screen.getByText(zh.employees.hrRecovery.failureGeneric)).toBeTruthy();
  expect(screen.queryByText(/Legacy failed HR draft/)).toBeNull();
});

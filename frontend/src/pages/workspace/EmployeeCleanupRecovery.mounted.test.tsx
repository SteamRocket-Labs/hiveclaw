// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/core';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const api = vi.hoisted(() => ({
  list: vi.fn(),
  listPendingCleanup: vi.fn(),
  remove: vi.fn(),
  getHrAgent: vi.fn(),
  listRecoverable: vi.fn(),
  abandon: vi.fn(),
  confirm: vi.fn(),
  toast: vi.fn(),
  role: 'org_admin',
}));

vi.mock('../../api/domains/agents', () => ({ agentApi: api }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: api }));
vi.mock('../../api/domains/users', () => ({ usersApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock('../../components/AppDialogs', () => ({ requestAppConfirm: api.confirm, showAppToast: api.toast }));
vi.mock('../../stores', () => ({
  useAuthStore: (selector: any) => selector({ user: { id: 'admin', role: api.role, tenant_id: 'tenant-1' } }),
}));

import HrCreationRecoveryPanel from '../employee-directory/HrCreationRecoveryPanel';
import WorkspaceDigitalEmployeesSection from './WorkspaceDigitalEmployeesSection';

const clients: QueryClient[] = [];
async function mount(kind: 'admin' | 'hr', locale: 'en' | 'zh') {
  const i18n = createInstance();
  await i18n.init({ lng: locale, resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          {kind === 'admin'
            ? <WorkspaceDigitalEmployeesSection selectedTenantId="tenant-1" />
            : <HrCreationRecoveryPanel />}
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.role = 'org_admin';
  api.confirm.mockResolvedValue(true);
  api.getHrAgent.mockResolvedValue({ id: 'hr-1', name: '__system_hr__' });
  api.list.mockResolvedValue([]);
  api.listPendingCleanup.mockResolvedValue([]);
  api.listRecoverable.mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe.each(['en', 'zh'] as const)('committed deletion recovery (%s)', (locale) => {
  const words = (locale === 'en' ? en : zh);

  it('recovers employee cleanup through the same DELETE after a fresh mount', async () => {
    const employee = { id: 'employee-1', name: 'Recovery employee', status: 'idle' };
    const pending = { id: employee.id, name: employee.name, deleted_at: '2026-09-10T00:00:00Z' };
    api.list.mockResolvedValue([employee]);
    api.remove.mockImplementationOnce(async () => {
      api.list.mockResolvedValue([]);
      api.listPendingCleanup.mockResolvedValue([pending]);
      throw new ApiError(503, 'Deletion committed', { code: 'agent_cleanup_pending' });
    }).mockImplementationOnce(async () => { api.listPendingCleanup.mockResolvedValue([]); });

    const first = await mount('admin', locale);
    fireEvent.click(await screen.findByRole('button', { name: words.workspace.digitalEmployees.deleteEmployeeButton }));
    await screen.findByRole('button', { name: words.workspace.digitalEmployees.retryCleanup });
    expect(api.toast).toHaveBeenCalledWith(words.workspace.digitalEmployees.cleanupPending, 'error');
    first.unmount();

    await mount('admin', locale);
    const retry = await screen.findByRole('button', { name: words.workspace.digitalEmployees.retryCleanup });
    expect(screen.getByText(words.workspace.digitalEmployees.cleanupPending)).toBeTruthy();
    expect(screen.queryByRole('link', { name: employee.name })).toBeNull();
    fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByRole('button', { name: words.workspace.digitalEmployees.retryCleanup })).toBeNull());
    expect(api.listPendingCleanup).toHaveBeenCalledWith('tenant-1');
    expect(api.remove.mock.calls).toEqual([[employee.id], [employee.id]]);
    expect(api.confirm).toHaveBeenCalledTimes(1);
  });

  it('keeps an abandoned HR draft recoverable after reload without repeating abandonment confirmation', async () => {
    const draft = {
      blueprint_id: 'draft-1', draft_status: 'failed', blueprint: { name: 'Recovery employee' },
      recovery: { can_abandon: true, can_retry: false, can_resume: false, requires_operator: false },
    };
    api.listRecoverable.mockResolvedValue([draft]);
    api.abandon.mockImplementationOnce(async () => {
      api.listRecoverable.mockResolvedValue([{ ...draft, draft_status: 'superseded', recovery: { ...draft.recovery, cleanup_pending: true } }]);
      throw new ApiError(503, 'Removal committed', { code: 'hr_abandon_cleanup_pending' });
    }).mockImplementationOnce(async () => { api.listRecoverable.mockResolvedValue([]); });

    const first = await mount('hr', locale);
    fireEvent.click(await screen.findByRole('button', { name: words.employees.hrRecovery.remove }));
    await screen.findByRole('button', { name: words.employees.hrRecovery.retryCleanup });
    first.unmount();

    await mount('hr', locale);
    const retry = await screen.findByRole('button', { name: words.employees.hrRecovery.retryCleanup });
    expect(screen.getByText(words.employees.hrRecovery.cleanupPending)).toBeTruthy();
    expect(screen.getByText(words.employees.hrRecovery.cleanupStatus)).toBeTruthy();
    expect(screen.queryByRole('button', { name: words.employees.hrRecovery.retry })).toBeNull();
    fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByText('Recovery employee')).toBeNull());
    expect(api.abandon.mock.calls).toEqual([['hr-1', 'draft-1'], ['hr-1', 'draft-1']]);
    expect(api.confirm).toHaveBeenCalledTimes(1);
  });
});

it('does not query the administrator cleanup inventory for a member', async () => {
  api.role = 'member';
  await mount('admin', 'en');
  await act(async () => {});
  expect(api.listPendingCleanup).not.toHaveBeenCalled();
  expect(screen.getByText(en.workspace.digitalEmployees.adminOnly)).toBeTruthy();
});

it('shows a retryable load error instead of hiding the cleanup inventory', async () => {
  api.listPendingCleanup.mockRejectedValueOnce(new Error('unavailable')).mockResolvedValue([]);
  await mount('admin', 'en');
  expect(await screen.findByRole('alert')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: en.common.retry }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(api.listPendingCleanup).toHaveBeenCalledTimes(2);
});

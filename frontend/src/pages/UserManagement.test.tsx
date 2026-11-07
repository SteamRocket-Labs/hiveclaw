// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import UserManagement from './UserManagement';
import { usersApi } from '../api/domains/users';
import { ApiError } from '../api/core';

vi.mock('react-i18next', () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t, i18n: { language: 'en' } }) };
});
vi.mock('../stores', () => ({ useAuthStore: () => ({ user: { id: 'admin', role: 'org_admin' }, setUser: vi.fn() }) }));
vi.mock('./ExternalPrincipalBindingsPanel', () => ({ default: () => null }));
vi.mock('../api/domains/externalPrincipals', () => ({ externalPrincipalsApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock('../components/ui', () => ({ Modal: ({ open, children, footer }: any) => open
  ? <div role="dialog">{children}{footer}</div> : null }));
vi.mock('../api/domains/users', () => ({ usersApi: {
  list: vi.fn().mockResolvedValue([{ id: 'leaver', username: 'Leaver', email: 'leaver@example.test',
    role: 'member', is_active: true, agents_count: 1, created_at: '2026-11-06',
    tokens_used_today: 0, tokens_used_month: 0 }]),
  previewOffboarding: vi.fn().mockResolvedValue({ owned_agents: [{ id: 'bot', name: 'Company bot' }],
    handover: { knowledge: [{ id: 'guide', title: 'Company guide' }],
      work: [{ id: 'work', title: 'Pending report', receiver_user_id: null }], triggers: [{ id: 'daily', name: 'Daily report' }] },
    eligible_successors: [{ id: 'successor', display_name: 'Successor' }], default_successor_id: 'successor',
    revocations: { agent_permissions: 0, resource_permissions: 0, knowledge_grants: 0, refresh_tokens: 0,
      external_principals: 0, local_bridge_connections: 0, runtime_tasks: 0, pending_approvals: 0 }, blockers: [] }),
  offboard: vi.fn(),
} }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('shows processing as retryable and keeps the same offboarding form for retry', async () => {
  const message = '知识仍在处理中，请一两分钟后重试。';
  vi.mocked(usersApi.offboard).mockRejectedValueOnce(new ApiError(409, message, {
    code: 'offboarding_knowledge_processing', retryable: true, message,
  })).mockResolvedValueOnce({ transferred_agent_count: 1 } as any);
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText('Company bot');
  expect(screen.getByText('Company guide')).toBeTruthy();
  expect(screen.getByText('Pending report')).toBeTruthy();
  expect(screen.getByText('Daily report')).toBeTruthy();
  expect(screen.getByText('userManagement.handoverCredentials')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('userManagement.offboardingReason'), { target: { value: 'Employment ended' } });
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }));
  const status = await screen.findByRole('status');
  expect(status.textContent).toBe(`⏳ ${message}`);
  expect(status.style.background).toBe('var(--warning)');
  expect(screen.getByRole('dialog')).toBeTruthy();
  expect((screen.getByLabelText('userManagement.offboardingReason') as HTMLTextAreaElement).value).toBe('Employment ended');
  await waitFor(() => expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }));
  await waitFor(() => expect(usersApi.offboard).toHaveBeenCalledTimes(2));
  expect(vi.mocked(usersApi.offboard).mock.calls[1]).toEqual(vi.mocked(usersApi.offboard).mock.calls[0]);
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('refreshes the handover list for the chosen receiving member before submitting', async () => {
  const preview = { owned_agents: [{ id: 'bot', name: 'Company bot' }],
    eligible_successors: [{ id: 'first', display_name: 'First' }, { id: 'receiver', display_name: 'Receiver' }],
    default_successor_id: 'first', blockers: [],
    revocations: { agent_permissions: 0, resource_permissions: 0, knowledge_grants: 0, refresh_tokens: 0,
      external_principals: 0, local_bridge_connections: 0, runtime_tasks: 0, pending_approvals: 0 },
    handover: { knowledge: [{ id: 'source', title: 'Original copy' }], work: [], triggers: [] } };
  vi.mocked(usersApi.previewOffboarding).mockResolvedValueOnce(preview as any).mockResolvedValueOnce({ ...preview,
    handover: { ...preview.handover, knowledge: [{ id: 'existing', title: 'Receiver copy', merged: true }] } } as any);
  vi.mocked(usersApi.offboard).mockResolvedValue({ transferred_agent_count: 1 } as any);
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText('Original copy');
  fireEvent.change(screen.getByLabelText('userManagement.successorAdmin'), { target: { value: 'receiver' } });
  await screen.findByText('Receiver copy');
  expect(screen.queryByText('Original copy')).toBeNull();
  expect(usersApi.previewOffboarding).toHaveBeenLastCalledWith('leaver', undefined, 'receiver');
  fireEvent.change(screen.getByLabelText('userManagement.offboardingReason'), { target: { value: 'Employment ended' } });
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }));
  await waitFor(() => expect(usersApi.offboard).toHaveBeenCalledWith('leaver', expect.objectContaining({ successor_user_id: 'receiver' }), undefined));
});

it('labels the existing handover action as offboarding rather than deletion', async () => {
  const zh = (await import('../i18n/zh.json')).default;
  const en = (await import('../i18n/en.json')).default;
  expect(zh.userManagement.deactivateMember).toBe('办理离职');
  expect(zh.userManagement).toMatchObject({ deactivateMemberTitle: '办理成员离职', offboardingReason: '离职原因',
    offboardingPreviewFailed: '加载离职交接清单失败。', offboardingFailed: '办理成员离职失败。' });
  expect(en.userManagement.deactivateMember).toBe('Offboard member');
});

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

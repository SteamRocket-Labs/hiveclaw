// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import WorkspaceAuditSection from './WorkspaceAuditSection';

const mocks = vi.hoisted(() => ({ logs: vi.fn(), users: vi.fn() }));
vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: {
  getAuditLogs: mocks.logs, getSecurityAuditEvents: async () => ({ items: [] }),
} }));
vi.mock('../../api/domains/users', () => ({ usersApi: { list: mocks.users } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: any, values?: any) =>
  typeof fallback === 'string' ? fallback.replace(/\{\{(\w+)\}\}/g, (_, key) => values?.[key] ?? '') : _key }) }));
afterEach(cleanup);

it('finds company offboarding and role changes and describes who, what, when and result', async () => {
  mocks.users.mockResolvedValue([{ id: 'admin', display_name: 'Company admin' },
    { id: 'leaver', display_name: 'Departing member' }, { id: 'receiver', display_name: 'Receiving member' }]);
  mocks.logs.mockResolvedValue([
    { id: 'offboard', user_id: 'admin', action: 'user:offboarded', created_at: '2026-11-06T12:00:00Z',
      details: { target_user_id: 'leaver', successor_user_id: 'receiver', reason: 'PRIVATE REASON', schema: 'private-schema' } },
    { id: 'role', user_id: 'admin', action: 'user:role_changed', created_at: '2026-11-06T11:00:00Z',
      details: { target_user_id: 'receiver', from_role: 'member', to_role: 'org_admin' } },
  ]);
  const { container } = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceAuditSection selectedTenantId="company" />
  </QueryClientProvider>);
  await screen.findByText('Member offboarded');
  expect(container.textContent).toContain('Company admin');
  expect(container.textContent).toContain('Departing member');
  expect(container.textContent).toContain('Receiving member');
  expect(container.textContent).toContain('Role changed');
  expect(container.textContent).toContain('Member → Company administrator');
  expect(screen.getAllByText('Completed')).toHaveLength(2);
  expect(container.querySelectorAll('time')).toHaveLength(2);
  expect(container.textContent).not.toMatch(/user:offboarded|user:role_changed|org_admin|PRIVATE REASON|schema/);
  fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
  expect(screen.getByText('Member offboarded')).toBeTruthy();
  expect(screen.getByText('Role changed')).toBeTruthy();
  expect(mocks.logs).toHaveBeenCalledWith('limit=200&tenant_id=company');
});

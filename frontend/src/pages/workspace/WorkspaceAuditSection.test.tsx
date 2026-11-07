// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import WorkspaceAuditSection from './WorkspaceAuditSection';

const mocks = vi.hoisted(() => ({ logs: vi.fn(), users: vi.fn(), security: vi.fn().mockResolvedValue({ items: [] }) }));
vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: {
  getAuditLogs: mocks.logs, getSecurityAuditEvents: mocks.security,
} }));
vi.mock('../../api/domains/users', () => ({ usersApi: { list: mocks.users } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: any, values?: any) =>
  typeof fallback === 'string' ? fallback.replace(/\{\{(\w+)\}\}/g, (_, key) => values?.[key] ?? '') : _key }) }));
afterEach(() => { cleanup(); mocks.security.mockResolvedValue({ items: [] }); });

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

it('names the digital employee and existing handover and deactivation actions without internal payloads', async () => {
  mocks.users.mockResolvedValue([{ id: 'admin', display_name: 'Company admin' }]);
  const actions = ['agent:handover', 'agent:retirement_requested', 'agent:retirement_tracking_ended', 'agent:retirement_work_withdrawal'];
  mocks.logs.mockResolvedValue(actions.map((action) => ({ id: action, user_id: 'admin', agent_id: 'bot', agent_name: 'Research assistant', action,
    created_at: '2026-11-06T12:00:00Z', details: { probe_id: 'probe-private', schema: 'private-schema' } })));
  const { container } = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceAuditSection selectedTenantId="company" />
  </QueryClientProvider>);
  await screen.findByText('Digital employee owner changed');
  expect(screen.getByText('Digital employee deactivated')).toBeTruthy();
  expect(screen.getByText('Digital employee task tracking ended')).toBeTruthy();
  expect(screen.getByText('Digital employee work withdrawn')).toBeTruthy();
  expect(screen.getAllByText('Research assistant')).toHaveLength(4);
  expect(container.textContent).not.toMatch(/agent:|probe_id|probe-private|schema/);
});

it('describes a member already inactive without claiming a new completed handover', async () => {
  mocks.users.mockResolvedValue([{ id: 'leaver', display_name: 'Former member' },
    { id: 'receiver', display_name: 'Receiving member' }]);
  mocks.logs.mockResolvedValue([{ id: 'inactive', action: 'user:offboarded', created_at: '2026-11-06T12:00:00Z',
    details: { target_user_id: 'leaver', successor_user_id: 'receiver', already_inactive: true } }]);
  const { container } = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceAuditSection selectedTenantId="company" />
  </QueryClientProvider>);
  await screen.findByText('Already inactive');
  expect(container.textContent).toContain('Former member was already inactive before this offboarding.');
  expect(container.textContent).not.toMatch(/Completed|handed to|already_inactive/);
});

it('G3f review 2: labels a named restore and omits duplicate lifecycle security rows', async () => {
  mocks.users.mockResolvedValue([{ id: 'admin', display_name: 'Company admin' }]);
  mocks.logs.mockResolvedValue(['agent:retirement_requested', 'agent:restored'].map(action => ({
    id: action, user_id: 'admin', agent_id: 'bot', agent_name: 'Research assistant', action,
    created_at: '2026-11-07T12:00:00Z', details: {},
  })));
  mocks.security.mockResolvedValue({ items: ['deactivate_agent', 'restore_agent', 'test_llm_model_completed'].map(action => ({
    id: action, actor_id: 'admin', action, created_at: '2026-11-07T12:00:00Z', details: {},
  })) });
  const { container } = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceAuditSection selectedTenantId="company" />
  </QueryClientProvider>);
  await screen.findByText('Digital employee restored');
  expect(screen.getAllByText('Research assistant')).toHaveLength(2);
  expect(screen.getByText('Digital employee deactivated')).toBeTruthy();
  expect(screen.getByText('Model test')).toBeTruthy();
  expect(screen.queryByText('Activity recorded')).toBeNull();
  expect(container.querySelectorAll('article')).toHaveLength(3);
  expect((await import('../../i18n/zh.json')).default.enterprise.audit.agentRestored).toBe('数字员工已恢复');
});

it('G3f round 2 item 2: names asset operations and member logins with the supplied employee', async () => {
  mocks.users.mockResolvedValue([{ id: 'admin', display_name: 'Company admin' }]);
  mocks.logs.mockResolvedValue(['ai_asset.create', 'ai_asset.update', 'ai_asset.revoke'].map(action => ({
    id: action, action, user_id: 'admin', agent_id: 'bot', agent_name: 'Research assistant',
    created_at: '2026-11-07T12:00:00Z', details: {},
  })));
  mocks.security.mockResolvedValue({ items: ['login', 'login', 'assistant.provisioned'].map((action, id) => ({
    id: `security-${id}`, action, actor_id: 'admin', created_at: '2026-11-07T12:00:00Z', details: {} })) });
  const { container } = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceAuditSection selectedTenantId="company" />
  </QueryClientProvider>);
  // Item 2: an asset's agent_id may name its acting bot, not its resource type.
  await screen.findByText('Resource created');
  expect(screen.getByText('Resource updated')).toBeTruthy();
  expect(screen.getByText('Resource deactivated')).toBeTruthy();
  expect(screen.getByText('Personal assistant created')).toBeTruthy();
  expect(screen.getAllByText('Member signed in')).toHaveLength(2);
  expect(screen.getAllByText('Research assistant')).toHaveLength(3);
  expect(screen.queryByText('Activity recorded')).toBeNull();
  expect(container.querySelectorAll('article')).toHaveLength(6);
  const labels = (await import('../../i18n/zh.json')).default.enterprise.audit;
  expect(labels.assetCreated).toBe('创建资源');
  expect(labels.assetUpdated).toBe('更新资源');
  expect(labels.assetRevoked).toBe('停用资源');
  expect(labels.assistantCreated).toBe('创建个人助手');
  expect(labels.login).toBe('成员登录');
});

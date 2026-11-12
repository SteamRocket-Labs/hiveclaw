// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import i18n from '../../i18n';
import { agentApi } from '../../api/domains/agents';
import { assistantApi } from '../../api/domains/assistant';
import { usersApi } from '../../api/domains/users';
import { useAuthStore } from '../../stores';
import WorkspaceDigitalEmployeesSection from './WorkspaceDigitalEmployeesSection';

vi.mock('../../api/domains/agents', () => ({ agentApi: {
  getHrAgent: vi.fn(), list: vi.fn(), listPendingRetirement: vi.fn(),
  deactivate: vi.fn(), restore: vi.fn(), transferOwnership: vi.fn(),
  ownerCandidates: vi.fn(), getPermissions: vi.fn(),
} }));
vi.mock('../../api/domains/assistant', () => ({ assistantApi: { get: vi.fn() } }));
vi.mock('../../api/domains/users', () => ({ usersApi: { list: vi.fn() } }));
vi.mock('../../components/AppDialogs', () => ({ showAppToast: vi.fn(), requestAppConfirm: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WorkspaceDigitalEmployeesSection selectedTenantId="tenant" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it('names default assistants by ownership in the zh admin list, custom names untouched', async () => {
  await i18n.changeLanguage('zh');
  useAuthStore.setState({ user: { id: 'admin-1', role: 'org_admin', tenant_id: 'tenant' } as never });
  vi.mocked(agentApi.getHrAgent).mockResolvedValue(null as never);
  vi.mocked(assistantApi.get).mockResolvedValue({ state: 'active', agent: { id: 'assistant-1' } } as never);
  vi.mocked(agentApi.list).mockResolvedValue([
    { id: 'assistant-1', name: 'My Assistant', role_description: "The user's assistant", owner_user_id: 'admin-1' },
    { id: 'assistant-2', name: 'My Assistant', role_description: "The user's assistant", owner_user_id: 'user-2' },
    { id: 'assistant-3', name: 'My Assistant', role_description: "The user's assistant", owner_user_id: 'user-unknown' },
    { id: 'weekly-1', name: '周报助手', role_description: '写周报', owner_user_id: 'admin-1' },
  ] as never);
  vi.mocked(agentApi.listPendingRetirement).mockResolvedValue([]);
  vi.mocked(agentApi.getPermissions).mockResolvedValue({ scope_type: 'company' } as never);
  vi.mocked(usersApi.list).mockResolvedValue([
    { id: 'admin-1', display_name: '管理员', email: 'a@example.test' },
    { id: 'user-2', display_name: '张三', email: 'z@example.test' },
  ] as never);
  renderSection();
  // The admin's own bound assistant keeps the possessive label.
  expect(await screen.findByText('我的助理')).toBeTruthy();
  // Another member's default assistant carries its owner; an unknown owner stays neutral.
  expect(screen.getByText('张三的助理')).toBeTruthy();
  expect(screen.getByText('助理')).toBeTruthy();
  expect(screen.queryByText('My Assistant')).toBeNull();
  // The default role maps to the plain zh label on every default row.
  expect(screen.getAllByText('为你处理工作的助理').length).toBe(3);
  // A real custom name stays untouched.
  expect(screen.getByText('周报助手')).toBeTruthy();
  expect(screen.getByText('写周报')).toBeTruthy();
});

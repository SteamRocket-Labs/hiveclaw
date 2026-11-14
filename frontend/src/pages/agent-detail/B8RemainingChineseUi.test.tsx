// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { useAuthStore } from '../../stores';
import { ThreadItemRenderer } from '../session-workbench/ThreadItemRenderer';
import SessionPermissionActions from './SessionPermissionActions';
import { SessionRuntimePanel } from './SessionRuntimePanel';
import { SessionGoalPanel } from '../session-workbench/SessionGoalPanel';
import WorkspaceDigitalEmployeesSection from '../workspace/WorkspaceDigitalEmployeesSection';

const mocks = vi.hoisted(() => ({ language: 'zh', list: vi.fn(), users: vi.fn(), pending: vi.fn(), confirm: vi.fn() }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: any, options?: any) => translateFromCatalog(mocks.language === 'zh' ? zh : en, key, fallback, options),
  i18n: { language: mocks.language },
}) }));
vi.mock('../../api/domains/agents', () => ({ agentApi: {
  list: mocks.list, getHrAgent: async () => null, listPendingRetirement: mocks.pending,
  getPermissions: async () => ({ scope_type: 'company' }),
} }));
vi.mock('../../api/domains/users', () => ({ usersApi: { list: mocks.users } }));
vi.mock('../../api/domains/assistant', () => ({ assistantApi: { get: async () => ({ agent: { id: 'own' } }) } }));
vi.mock('../../components/AppDialogs', () => ({ requestAppConfirm: mocks.confirm, showAppToast: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); mocks.language = 'zh'; useAuthStore.setState({ user: null }); localStorage.clear(); });

const args = {
  name: '周报助手', first_task: '整理本周工作，不要改写 English literal。',
  attachments: ['report.csv'], dispatch_once: true,
  custom_option: { subject: '周报', unknown_option: false },
  // Both key names must stay distinct in the complete proposal (B8-r2).
  to: 'a@example.test', recipient: 'b@example.test',
};
const request = { permission_request_id: 'approval', tool_name: 'create_employee_from_request', arguments: args,
  expires_at: '2026-11-06T14:13:43.874659+00:00', allow_session_allowed: false };
const t = (key: string, fallback: string, options?: any) => translateFromCatalog(zh, key, fallback, options);

it('B8-r2 keeps the complete approval arguments exact and approves the untouched request', async () => {
  const original = JSON.stringify(request);
  const resolve = vi.fn().mockResolvedValue(undefined);
  const { container } = render(<ThreadItemRenderer item={{ id: 'approval', item_type: 'approval_request', item_status: 'waiting_user',
    item_data: request, audience: 'user', event_type: 'permission', metadata: {} } as never} approvalArguments={args}
    approvalActions={<SessionPermissionActions permissionRequest={request} showArgumentDetails={false} t={t} onResolveSessionPermission={resolve} />} />);
  fireEvent.click(screen.getByText('详情'));
  expect(container.textContent).toContain(new Date(request.expires_at).toLocaleString('zh'));
  expect(container.textContent).not.toContain(request.expires_at);
  fireEvent.click(screen.getByRole('button', { name: '查看将要执行的完整内容' }));
  const display = screen.getByLabelText('操作详情');
  // B8-r2 withdraws key relabeling: every key, value and nested object must
  // equal the exact proposed arguments, not merely retain the same values.
  expect(display.textContent).toBe(JSON.stringify(args, null, 2));
  expect(JSON.parse(display.textContent!)).toEqual(args);
  fireEvent.click(screen.getByRole('button', { name: '本次允许' }));
  await waitFor(() => expect(resolve).toHaveBeenCalledExactlyOnceWith(request, 'allow_once'));
  expect(JSON.stringify(request)).toBe(original);
});

function adminList() {
  useAuthStore.setState({ user: { id: 'admin', role: 'org_admin', tenant_id: 'tenant' } as never });
  const records = [
    { id: 'own', name: 'My Assistant', role_description: "The user's assistant", owner_user_id: 'admin' },
    { id: 'other', name: 'My Assistant', role_description: "The user's assistant", owner_user_id: 'member' },
    { id: 'custom', name: 'My Assistant reviewer', role_description: '自定义名称', owner_user_id: 'member' },
  ];
  mocks.list.mockResolvedValue(records);
  mocks.users.mockResolvedValue([{ id: 'admin', display_name: '管理员' }, { id: 'member', display_name: '张三' }]);
  mocks.pending.mockResolvedValue([{ id: 'other', name: 'My Assistant', inactive_requester_work_count: 1, tracking_version: 'v1' }]);
  mocks.confirm.mockResolvedValue(false);
  const view = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><WorkspaceDigitalEmployeesSection selectedTenantId="tenant" /></MemoryRouter>
  </QueryClientProvider>);
  return { ...view, records };
}

it('B8 localizes default assistant names in the pending admin list without renaming stored data', async () => {
  const { records } = adminList();
  await screen.findAllByText('张三的助理');
  const pending = screen.getByRole('region', { name: '已停用的数字员工' });
  expect(within(pending).getByRole('link').textContent).toBe('张三的助理');
  const row = screen.getByText('我的助理').closest('tr')!;
  expect(within(row).getByText('管理员')).toBeTruthy();
  expect(screen.getByText('My Assistant reviewer')).toBeTruthy();
  expect(records[0].name).toBe('My Assistant');
  expect(records[1].name).toBe('My Assistant');
});

it.each(['deactivate', 'end tracking'])('B8 identifies the owner-localized assistant in the admin %s confirmation', async action => {
  adminList();
  const names = await screen.findAllByText('张三的助理');
  const row = names.map(name => name.closest('tr')).find(Boolean)!;
  fireEvent.click(action === 'deactivate'
    ? within(row).getByRole('button', { name: '停用' })
    : screen.getByRole('button', { name: '结束已停用请求人的任务跟踪' }));
  await waitFor(() => expect(mocks.confirm).toHaveBeenCalledTimes(1));
  expect(mocks.confirm.mock.calls[0][0].message).toContain('张三的助理');
  expect(mocks.confirm.mock.calls[0][0].message).not.toContain('My Assistant');
});

it.each([[30, '不足 1 分钟', '<1m'], [3480, '58 分钟', '58m'], [7200, '2 小时', '2h'], [7500, '2 小时 5 分钟', '2h 5m']] as const)
('B8 localizes remaining time %s without changing English formatting', (seconds, chinese, english) => {
  for (const [language, expected] of [['zh', chinese], ['en', english]]) {
    mocks.language = language;
    const { container, unmount } = render(<SessionGoalPanel agentId="bot" sessionId="session" goals={[
      { id: 'goal', objective: '成员填写的任务', status: 'active', remaining_time_seconds: seconds },
    ] as never} />);
    expect(container.querySelector('.session-goal-facts')?.textContent).toBe(language === 'zh' ? `剩余 ${expected}` : `${expected} left`);
    expect(container.textContent).toContain('成员填写的任务');
    unmount();
  }
});

it.each([
  ['background', 'activity', 'background_agent', '后台任务-1'],
  ['workflows', 'workflow', 'workflow', '工作流运行-1'],
  ['peer_a2a', 'a2a', 'peer_a2a', '委派-1'],
  ['subagents', 'workers', 'subagent', '协作助手-1'],
  ['agent_teams', 'team', 'agent_team', '数字员工团队'],
] as const)('B8 gives the API task-type fallback a localized name in %s', (section, tab, kind, expected) => {
  useAuthStore.setState({ user: { id: 'admin', role: 'org_admin' } as never });
  const item = { id: '120e98ce-d9e6-5e29-a707-7992e369a745', label: kind, task_type: kind, runtime_kind: kind, status: 'running' };
  const original = JSON.stringify(item);
  render(<SessionRuntimePanel messages={[]} activeSession={{ id: 'session' }}
    sessionWorkbench={{ runtime_sections: { [section]: [item] } } as never} />);
  fireEvent.click(screen.getByTestId(`session-runtime-segment-${tab}`));
  expect(screen.getByRole('tabpanel').textContent).toContain(expected);
  expect(screen.getByRole('tabpanel').textContent).not.toContain(kind);
  expect(JSON.stringify(item)).toBe(original);
});

it.each(['member', 'org_admin'])('B8 keeps the expanded runtime panel Chinese for %s while preserving authored names', role => {
  useAuthStore.setState({ user: { id: 'member', role } as never });
  const { container } = render(<SessionRuntimePanel agentId="bot" sessionId="session" activeSession={{ id: 'session', title: '周报' }}
    messages={[]} sessionWorkbench={{ goals: [{ id: 'goal', objective: '写周报', status: 'active', remaining_tokens: 750,
      remaining_continuation_turns: 3, remaining_time_seconds: 3480 }], runtime_sections: {
      background: [{ id: 'background', runtime_kind: 'background_agent', label: '后台检查', status: 'running', token_count: 120, tool_use_count: 2 }],
      agent_teams: [], peer_a2a: [], subagents: [], workflows: [],
    } } as never} />);
  expect(container.textContent).not.toMatch(/Active|tokens?|tools|Team|A2A|Workers|Workflow|BACKGROUND-AGENT|58m/);
  if (role === 'member') {
    expect(screen.queryByRole('tablist')).toBeNull();
    expect(container.textContent).not.toContain('750');
  } else {
    expect(container.textContent).toContain('剩余模型用量 750');
    expect(container.textContent).toContain('剩余 58 分钟');
    expect(container.textContent).toContain('进行中');
    expect(container.textContent).toContain('后台检查');
    expect(container.textContent).toContain('模型用量 120');
    expect(container.textContent).toContain('工具调用 2 次');
    expect(screen.getByRole('tab', { name: /动态/ })).toBeTruthy();
  }
});

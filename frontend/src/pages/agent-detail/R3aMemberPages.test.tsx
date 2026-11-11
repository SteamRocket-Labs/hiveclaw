// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { useAuthStore } from '../../stores';
import { runtimeBudgetApi } from '../../api/domains/runtimeBudgets';
import { agentApi } from '../../api/domains/agents';
import { assistantApi } from '../../api/domains/assistant';
import { dashboardApi } from '../../api/domains/dashboard';
import { hrCreationApi } from '../../api/domains/hrCreation';
import { businessTeamsApi } from '../../api/domains/businessTeams';
import { a2aApi } from '../../api/domains/a2a';
import { machineIdentitiesApi } from '../../api/domains/machineIdentities';
import { remoteA2AApi } from '../../api/domains/remoteA2A';
import DigitalEmployees from '../DigitalEmployees';
import Dashboard from '../Dashboard';
import AssistantPanel from '../assistant/AssistantPanel';
import WorkspaceFeatureHub from '../WorkspaceFeatureHub';
import AgentA2ASection from './AgentA2ASection';
import AgentChatSection from './AgentChatSection';
import AgentStatusSection from './AgentStatusSection';
import AgentDetailHeader, { AgentDetailNavigation } from './AgentDetailHeader';
import AppSidebar from '../layout/AppSidebar';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: any, options?: any) => translateFromCatalog(zh, key, fallback, options),
  i18n: { language: 'zh' },
}) }));
vi.mock('./ToolAccounts', () => ({ default: () => null }));

afterEach(() => { cleanup(); useAuthStore.setState({ user: null }); vi.restoreAllMocks(); });

function mount(children: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>);
}

const own = { id: 'work', user_id: 'member', title: '销售报告', session_kind: 'human_chat' };
function chat(overrides: Record<string, unknown> = {}) {
  const ref = React.createRef<any>();
  const noop = vi.fn();
  return <AgentChatSection {...{
    agent: { id: 'bot', name: 'My Assistant' }, currentUser: { id: 'member', role: 'member' }, isAdmin: false,
    sessions: [own], activeSession: own, allSessions: [], historyMsgs: [], chatMessages: [], attachedFiles: [],
    wsConnected: true, sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: '', runtimeSummary: null, transportNotice: null, activeRunStatus: null,
    chatMessagesSessionId: 'work', historyMessagesSessionId: 'work',
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: noop,
    ...overrides,
  } as unknown as React.ComponentProps<typeof AgentChatSection>} />;
}

it('offers removal in member history only for their own conversation and invokes the existing callback', () => {
  const remove = vi.fn();
  mount(chat({ sessionOnly: true, onDeleteSession: remove, sessions: [own,
    { id: 'foreign', user_id: 'other', title: '同事的对话' },
    { id: 'audit', user_id: 'member', title: '审计视图', operator_view: true },
  ] }));
  fireEvent.click(screen.getByRole('button', { name: '历史' }));
  fireEvent.click(screen.getByRole('button', { name: '从列表移除对话：销售报告' }));
  expect(remove).toHaveBeenCalledExactlyOnceWith('work');
  expect(screen.queryByRole('button', { name: '从列表移除对话：同事的对话' })).toBeNull();
  expect(screen.queryByRole('button', { name: '从列表移除对话：审计视图' })).toBeNull();
});

it('retains the administrator audit browser and its existing own-row removal', () => {
  mount(chat({ isAdmin: true, allSessions: [own, { id: 'foreign', user_id: 'other', title: '同事的对话' }] }));
  expect(screen.getByTestId('detail-session-browser').textContent).toContain('同事的对话');
  expect(screen.getByRole('button', { name: '从列表移除对话：销售报告' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '从列表移除对话：同事的对话' })).toBeNull();
});

it.each(['member', 'org_admin'])('keeps overview operator counters and requests only for administrators (%s)', async (role) => {
  useAuthStore.setState({ user: { id: 'member', role } as never });
  const budget = vi.spyOn(runtimeBudgetApi, 'listRuns').mockResolvedValue([]);
  const health = vi.spyOn(agentApi, 'getRuntimeHealth').mockResolvedValue({ status: 'healthy' } as never);
  const { container } = mount(<AgentStatusSection agent={{ id: 'bot', name: 'My Assistant',
    role_description: "The user's assistant", tokens_used_today: 3300000, tokens_used_month: 10600000,
    tokens_used_total: 20000000 }} llmModels={[]} statusKey="idle" onSelectTab={vi.fn()}
    metrics={{ tasks: { done: 2, total: 4 }, activity: { actions_last_24h: 7 } }} />);
  if (role === 'member') {
    await Promise.resolve();
    expect.soft(container.textContent).not.toMatch(/Token|累计用量|今日 AI 请求|24h 活动|运行保护/);
    expect.soft(budget).not.toHaveBeenCalled();
    expect.soft(health).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("The user's assistant");
  } else {
    await waitFor(() => expect(budget).toHaveBeenCalledWith({ agentId: 'bot', limit: 5 }));
    expect(container.textContent).toContain('3.3M');
    expect(container.textContent).toContain('运行保护');
    expect(health).toHaveBeenCalledWith('bot');
  }
});

it('renders the assistant name, avatar and default role in Chinese before binding resolution', () => {
  vi.spyOn(assistantApi, 'get').mockReturnValue(new Promise(() => {}));
  const user = { id: 'member', role: 'member', tenant_id: 'company' };
  useAuthStore.setState({ user: user as never });
  const agent = { id: 'bot', name: 'My Assistant', role_description: "The user's assistant" };
  const { container } = mount(<>
    <AppSidebar user={user} theme="light" isSidebarCollapsed={false} onToggleSidebar={vi.fn()}
      agents={[agent]} tenants={[]} currentTenant="company" onSwitchTenant={vi.fn()} isChinese
      onToggleTheme={vi.fn()} onOpenNotifications={vi.fn()} unreadCount={0} accountMenuRef={React.createRef()}
      showAccountMenu={false} onToggleAccountMenu={vi.fn()} onToggleLang={vi.fn()}
      onOpenAccountSettings={vi.fn()} onLogout={vi.fn()} versionDisplay={null} hrAgent={null} />
    <AgentDetailHeader agent={agent} agentId="bot" canManage={false} isOperatorOnly={false} isSystemHr={false}
      onError={vi.fn()} statusKey="idle" />
  </>);
  expect.soft(container.textContent).not.toMatch(/My Assistant|The user's assistant/);
  expect.soft(container.querySelector('.sidebar-item-text')?.textContent).toBe('我的助理');
  expect.soft(container.querySelector('.agent-avatar')?.textContent).toBe('我');
  expect(container.querySelector('.agent-detail-avatar')?.textContent).toBe('我');
});

it('opens the bound assistant description editor on the Chinese default without rewriting the stored row', async () => {
  vi.spyOn(assistantApi, 'get').mockResolvedValue({ state: 'active', agent: { id: 'bot', name: 'Provisioned Assistant' } } as never);
  useAuthStore.setState({ user: { id: 'member', role: 'member', tenant_id: 'company' } as never });
  const update = vi.spyOn(agentApi, 'update').mockResolvedValue({} as never);
  const agent = { id: 'bot', name: 'Provisioned Assistant', role_description: "The user's assistant", role_revision: 'a'.repeat(64) };
  mount(<AgentDetailHeader agent={agent} agentId="bot" canManage isOperatorOnly={false} isSystemHr={false}
    onError={vi.fn()} statusKey="idle" />);
  // The header name flips to the localized assistant title only once the
  // binding resolves, so the editor deterministically sees the binding id.
  await screen.findByRole('heading', { name: '我的助理' });
  fireEvent.click(screen.getByRole('button', { name: /为你处理工作的助理/ }));
  const textarea = await screen.findByRole('textbox');
  // The member edits the localized default; the stored sentinel row is untouched.
  expect((textarea as HTMLTextAreaElement).value).toBe(zh.assistant.role);
  fireEvent.click(screen.getByRole('button', { name: zh.common.save }));
  await waitFor(() => expect(update).not.toHaveBeenCalled());
  expect(agent.role_description).toBe("The user's assistant");
});

it('applies the Chinese default to an editor opened before the assistant binding resolves', async () => {
  let resolveBinding: (binding: { state: string; agent: { id: string; name: string } }) => void = () => {};
  vi.spyOn(assistantApi, 'get').mockReturnValue(new Promise((resolve) => { resolveBinding = resolve; }) as never);
  useAuthStore.setState({ user: { id: 'member', role: 'member', tenant_id: 'company' } as never });
  const update = vi.spyOn(agentApi, 'update').mockResolvedValue({} as never);
  const agent = { id: 'bot', name: 'My Assistant', role_description: "The user's assistant", role_revision: 'a'.repeat(64) };
  mount(<AgentDetailHeader agent={agent} agentId="bot" canManage isOperatorOnly={false} isSystemHr={false}
    onError={vi.fn()} statusKey="idle" />);
  fireEvent.click(screen.getByRole('button', { name: /为你处理工作的助理/ }));
  // The editor mounted while the binding was still loading, so the textarea
  // starts on the stored sentinel.
  expect((await screen.findByRole('textbox') as HTMLTextAreaElement).value).toBe("The user's assistant");
  resolveBinding({ state: 'active', agent: { id: 'bot', name: 'Provisioned Assistant' } });
  // The localized default applies once the binding id arrives, and an
  // untouched save still writes nothing (the sentinel row keeps its bytes).
  await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(zh.assistant.role));
  fireEvent.click(screen.getByRole('button', { name: zh.common.save }));
  await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull());
  expect(update).not.toHaveBeenCalled();
});

it.each(['My Assistant ↔ 周报助手', 'Delegation: My Assistant'])('localizes the delegated title in the header and history: %s', (title) => {
  const session = { ...own, title, username: '🤖 My Assistant ↔ 周报助手', session_kind: 'agent_chat', read_only: true };
  const { container } = mount(chat({ activeSession: session, sessions: [session] }));
  fireEvent.click(screen.getByRole('button', { name: '历史' }));
  expect.soft(container.querySelector('.swb-header-title')?.textContent).toContain('我的助理');
  expect.soft(container.querySelector('.swb-history-item-title')?.textContent).toContain('我的助理');
  expect(container.textContent).not.toMatch(/My Assistant|Delegation:/);
});

it('uses Chinese navigation labels for the member overview', () => {
  const { container } = mount(<AgentDetailNavigation visibleTabs={['status', 'a2a', 'tasks']}
    activeTab="status" onSelectTab={vi.fn()} />);
  expect(container.textContent).not.toMatch(/Agent|A2A|Work Ledger|Team/);
  expect(zh.agent.businessTasks.boundaryBody).not.toMatch(/Agent|Work Ledger/);
});

it.each(['done', 'cancelled'] as const)('keeps the %s run label on one line at phone width', (status) => {
  const { container } = mount(<RunDisclosureBlock timeline={{ id: 'run', status, steps: [] }} />);
  const className = container.querySelector('.run-disclosure-title')!.className;
  const css = readFileSync('src/index.css', 'utf8');
  const rule = css.match(/\.run-disclosure-title\s*\{([^}]+)\}/)?.[1] || '';
  expect(className).toContain('run-disclosure-title');
  expect(rule).toMatch(/white-space:\s*nowrap/);
  expect(rule).toMatch(/flex-shrink:\s*0/);
});

it('gives the conversation the phone width while keeping the existing sidebar toggle reachable', () => {
  const css = readFileSync('src/index.css', 'utf8');
  const phone = css.match(/@media\s*\(max-width:\s*600px\)\s*\{([\s\S]*?)\n\}/)?.[1] || '';
  expect(phone).toMatch(/\.main-content\s*\{[^}]*margin-left:\s*0/);
  expect(phone).toMatch(/\.sidebar\.collapsed\s*\{[^}]*width:\s*0/);
  expect(phone).toMatch(/\.sidebar\.collapsed \.sidebar-collapse-btn\s*\{[^}]*visibility:\s*visible/);
});

it('measures the phone new-employee header clear of the floating sidebar toggle', () => {
  const css = readFileSync('src/index.css', 'utf8');
  const phone = css.match(/@media\s*\(max-width:\s*600px\)\s*\{([\s\S]*?)\n\}/)?.[1] || '';
  // Measured boxes from the shipped CSS: the floating toggle sits at left 8px
  // and the platform reserves 60px of clearance for it (the same value the
  // session workbench header uses), so its right edge is 8 + 60 = 68px.
  const toggleLeft = Number(phone.match(/\.sidebar\.collapsed \.sidebar-collapse-btn\s*\{[^}]*left:\s*(\d+)px/s)?.[1]);
  const reserved = Number(phone.match(/\.main-content \.swb-header\s*\{\s*padding-left:\s*(\d+)px/s)?.[1]);
  const headerPad = Number(phone.match(/\.main-content \.agent-detail-hr-header\s*\{\s*padding-left:\s*(\d+)px/s)?.[1] || 0);
  const mainPad = Number(readFileSync('src/styles/tokens.css', 'utf8').match(/--space-6:\s*(\d+)px/)?.[1]);
  const toggleRight = toggleLeft + reserved;
  const avatarLeft = mainPad + headerPad;
  expect(avatarLeft).toBeGreaterThanOrEqual(toggleRight);
  // Desktop is untouched: index.css mentions the header only inside the phone
  // media block (AgentDetail.css keeps its desktop rules unchanged).
  expect(css.match(/agent-detail-hr-header/g)?.length).toBe(1);
});

const storedAssistant = { id: 'bot', name: 'My Assistant', role_description: "The user's assistant",
  status: 'idle', creator_id: 'member', is_owner: true, created_at: '2026-11-07T00:00:00Z' };
const untranslatedMemberText = /My Assistant|The user's assistant|A2A|Team|Subagents?/;

function memberAgentList(extraAgents: unknown[] = []) {
  useAuthStore.setState({ user: { id: 'member', role: 'member', tenant_id: 'company' } as never });
  vi.spyOn(agentApi, 'list').mockResolvedValue([storedAssistant, ...extraAgents] as never);
}

it('renders the stored assistant name, role, avatar and team action in the member directory in Chinese', async () => {
  memberAgentList();
  vi.spyOn(hrCreationApi, 'listMine').mockResolvedValue([]);
  const { container } = mount(<DigitalEmployees />);
  await screen.findByRole('heading', { name: '我的助理' });
  expect.soft(container.querySelector('.employee-avatar')?.textContent).toBe('我');
  expect.soft(screen.getByText('为你处理工作的助理')).toBeTruthy();
  expect.soft(screen.getByRole('link', { name: '团队协作' }).getAttribute('href')).toBe('/agents/bot#a2a');
  // The status badge is member text too: the raw key stays a CSS class only.
  expect.soft(container.querySelector('.employee-status')?.textContent).toBe(zh.employees.status.idle);
  expect.soft(container.querySelector('.employee-status')?.className).toContain('idle');
  expect(container.textContent).not.toMatch(untranslatedMemberText);
});

it('localizes the stopped employee badge instead of showing the stored English status', async () => {
  memberAgentList([{ id: 'stopped-bot', name: '周报助手', role_description: '整理周报', status: 'stopped',
    creator_id: 'member', is_owner: true, created_at: '2026-11-07T00:00:00Z' }]);
  vi.spyOn(hrCreationApi, 'listMine').mockResolvedValue([]);
  const { container } = mount(<DigitalEmployees />);
  await screen.findByRole('heading', { name: '周报助手' });
  const badges = Array.from(container.querySelectorAll('.employee-status'));
  expect.soft(badges.map((badge) => badge.textContent)).toEqual([zh.employees.status.idle, zh.employees.status.stopped]);
  expect.soft(badges[1].className).toContain('stopped');
  expect(container.textContent).not.toMatch(/\bidle\b|\bstopped\b/);
});

it('localizes the dashboard work card, activity, failure summary and employee selection without changing ids', async () => {
  memberAgentList();
  vi.spyOn(dashboardApi, 'getOverview').mockResolvedValue({ recent_sessions: [{ id: 'pair', agent_id: 'bot',
    title: 'My Assistant ↔ 周报助手', session_kind: 'agent_chat', updated_at: '2026-11-07T00:00:00Z' }], session_count: 1,
    query_evidence: { failure_rows_truncated: false },
    recent_activities: [{ id: 'activity', agent_id: 'bot', summary: '已整理资料', created_at: '2026-11-07T00:00:00Z' }],
    tool_failures: { bot: { total_errors: 1, by_tool: [], by_provider: [], by_error_class: [], by_http_status: [], recent_errors: [] } },
  } as never);
  const { container } = mount(<Dashboard />);
  await screen.findByText('为你处理工作的助理');
  await waitFor(() => expect(container.querySelector('.dashboard-pill-btn')?.textContent).toContain('我的助理'));
  expect.soft(container.querySelector('.dashboard-tag')?.textContent).toBe('我的助理');
  expect.soft(container.querySelector('.workspace-list-row strong')?.textContent).toBe('我的助理 ↔ 周报助手');
  expect.soft(container.querySelector('.workspace-list-row small')?.textContent).toBe('我的助理');
  fireEvent.click(screen.getByRole('button', { name: new RegExp(zh.dashboard.home.assignWork) }));
  expect.soft(screen.getByRole('option', { name: '我的助理' }).getAttribute('value')).toBe('bot');
  expect.soft(storedAssistant.name).toBe('My Assistant');
  expect(container.textContent).not.toMatch(untranslatedMemberText);
});

it('localizes the assistant settings identity and candidates while preserving custom names', () => {
  const payload = { state: 'active', agent: storedAssistant, entry: { kind: 'employee', agent_id: 'bot' },
    candidates: [storedAssistant, { id: 'custom', name: '周报助手' }] };
  const { container } = mount(<AssistantPanel payload={payload as never} loading={false}
    onOpen={vi.fn()} onSelect={vi.fn()} onRetry={vi.fn()} />);
  expect.soft(container.querySelector('strong')?.textContent).toBe('我的助理');
  expect.soft(screen.getByRole('radio', { name: '我的助理' }).getAttribute('value')).toBe('bot');
  expect.soft(screen.getByRole('radio', { name: '周报助手' })).toBeTruthy();
  expect(container.textContent).not.toMatch(untranslatedMemberText);
});

it('renders the team hub title, subtitle, cards, employee identity and actions in Chinese', async () => {
  memberAgentList();
  vi.spyOn(businessTeamsApi, 'list').mockResolvedValue({ tenant_id: 'company', teams: [],
    caller: { user_id: 'member', role: 'member' } } as never);
  const { container } = mount(<WorkspaceFeatureHub kind="team" />);
  await waitFor(() => expect(screen.getAllByText('为你处理工作的助理')).toHaveLength(2));
  expect.soft(screen.getByRole('heading', { level: 1, name: '团队协作' })).toBeTruthy();
  expect.soft(container.querySelector('.workbench-page .page-subtitle')?.textContent).toBe(zh.featureHub.team.subtitle);
  expect.soft(screen.getAllByText('我的助理')).toHaveLength(2);
  expect.soft(container.querySelector('.employee-avatar')?.textContent).toBe('我');
  expect(container.textContent).not.toMatch(untranslatedMemberText);
});

it.each([false, true])('renders the selected team sub-tab and its member collaboration copy in Chinese (owner: %s)', async (canManage) => {
  useAuthStore.setState({ user: { id: 'member', role: 'member', tenant_id: 'company' } as never });
  vi.spyOn(a2aApi, 'listCollaborators').mockResolvedValue({ same_owner_agents: [], public_agents: [], collaboration_groups: [] });
  vi.spyOn(a2aApi, 'getManagement').mockResolvedValue({ groups: [] });
  vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue({ agent_id: 'bot', can_issue: false, identities: [] } as never);
  vi.spyOn(remoteA2AApi, 'get').mockResolvedValue({ agent_id: 'bot', state: 'not_configured', can_configure: false,
    allowed_networks: [], runtime_activation: 'not_activated' } as never);
  const { container } = mount(<><AgentDetailNavigation visibleTabs={['status', 'a2a', 'tasks']}
    activeTab="a2a" onSelectTab={vi.fn()} /><AgentA2ASection agentId="bot" canManage={canManage} /></>);
  await screen.findByText('暂无同归属协作对象。');
  if (canManage) {
    await screen.findByText('员工仍使用原先的执行方式。');
    expect.soft(container.textContent).toContain('协议版本: 1.0');
  }
  expect.soft(container.querySelector('.agent-workbench-subtab.active')?.textContent).toBe('协作对象');
  expect(container.textContent).not.toMatch(untranslatedMemberText);
});

it.each([false, true])('localizes the assistant bubble name and avatar in %s history mode without mutating sender identity', (readOnly) => {
  const message = { id: 'reply', role: 'assistant', content: '报告已经整理好了。', sender_name: 'My Assistant', participant_id: 'assistant-participant' };
  const session = { ...own, read_only: readOnly };
  const { container } = mount(chat({ activeSession: session, sessions: [session],
    chatMessages: [message], historyMsgs: [message] }));
  expect.soft(container.querySelector('.session-tui-message-sender')?.textContent).toBe('我的助理');
  expect.soft(container.querySelector('.session-tui-message-avatar')?.textContent).toBe('我');
  expect.soft(message.sender_name).toBe('My Assistant');
  expect(container.textContent).not.toMatch(untranslatedMemberText);
});

it('keeps delegated bubble alignment based on the stored sender identity', () => {
  const session = { ...own, title: 'My Assistant ↔ 周报助手', read_only: true, session_kind: 'agent_chat' };
  const historyMsgs = [
    { id: 'self', role: 'assistant', content: '请整理周报。', sender_name: 'My Assistant', participant_id: 'self' },
    { id: 'peer', role: 'assistant', content: '周报已经整理好了。', sender_name: '周报助手', participant_id: 'peer' },
  ];
  const { container } = mount(chat({ activeSession: session, sessions: [session], historyMsgs }));
  expect.soft(container.querySelector('[data-session-message-id="self"]')?.className).toContain('session-tui-message-row-user');
  expect(container.querySelector('[data-session-message-id="peer"]')?.className).toContain('session-tui-message-row-assistant');
});

it('offsets the collapsed phone toggle by the notice bar height', () => {
  const css = readFileSync('src/index.css', 'utf8');
  const phone = css.match(/@media\s*\(max-width:\s*600px\)\s*\{([\s\S]*?)\n\}/)?.[1] || '';
  const normalTop = phone.match(/\.sidebar\.collapsed \.sidebar-collapse-btn\s*\{[^}]*top:\s*(\d+)px/)?.[1];
  const noticeHeight = css.match(/\.notification-bar\s*\{[^}]*height:\s*(\d+)px/)?.[1];
  const noticeTop = phone.match(/\.has-notification-bar \.sidebar\.collapsed \.sidebar-collapse-btn\s*\{[^}]*top:\s*(\d+)px/)?.[1];
  expect(normalTop).toBe('8');
  expect(noticeHeight).toBe('32');
  expect(Number(noticeTop)).toBe(Number(normalTop) + Number(noticeHeight));
});

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

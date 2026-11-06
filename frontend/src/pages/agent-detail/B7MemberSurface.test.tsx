// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import zh from '../../i18n/zh.json';
import { useAuthStore } from '../../stores';
import AgentChatSection from './AgentChatSection';
import { AssistantMessageBody } from './CanonicalCardAssistantSupplement';
import { SessionRuntimePanel } from './SessionRuntimePanel';
import { ThreadItemRenderer } from '../session-workbench/ThreadItemRenderer';
import SessionPermissionActions from './SessionPermissionActions';
import { SessionGitLine } from './SessionLineageSurface';
import CopyMessageButton from './CopyMessageButton';
import RunDisclosureBlock from './RunDisclosureBlock';
import InlineToolConfigurationCard from './InlineToolConfigurationCard';
import { toolsApi } from '../../api/domains/tools';
import type { ThreadItem } from '../../api/domains/threadItems.generated';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      const value = key.split('.').reduce<any>((node, part) => node?.[part], zh);
      return String(value ?? (typeof fallback === 'string' ? fallback : key))
        .replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String((options ?? fallback as any)?.[name] ?? ''));
    },
    i18n: { language: 'zh' },
  }),
}));
vi.mock('./ToolAccounts', () => ({ default: () => null }));

afterEach(() => { cleanup(); useAuthStore.setState({ user: null }); vi.restoreAllMocks(); });

function chat(overrides: Record<string, unknown> = {}) {
  const ref = React.createRef<any>();
  const noop = vi.fn();
  const props = {
    agent: { id: 'bot', name: '助理' }, currentUser: { id: 'member' }, isAdmin: false,
    sessions: [{ id: 'work', user_id: 'member', title: '销售报告' }], activeSession: null,
    allSessions: [], historyMsgs: [], chatMessages: [], attachedFiles: [], wsConnected: true,
    sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: '', runtimeSummary: null, transportNotice: null, activeRunStatus: null,
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: noop,
    ...overrides,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  return render(<QueryClientProvider client={new QueryClient()}><AgentChatSection {...props} /></QueryClientProvider>);
}

it('makes opened employee-creation approval details readable and keeps raw arguments folded', () => {
  const expires = '2026-11-06T14:13:43.874659+00:00';
  const args = { name: '周报助手', duty: '整理每周工作', core_outputs: ['周报模板'] };
  const request = { tool_name: 'create_employee_from_request', arguments: args, expires_at: expires,
    destructive: true, allow_session_allowed: false, risk_class: 'destructive_delete', confirmation_kind: 'destructive_once' };
  const item = { id: 'approval', item_type: 'approval_request', item_status: 'waiting_user',
    item_data: request, audience: 'user', event_type: 'permission', metadata: {},
    user_action: { kind: 'resolve_approval', impact: '破坏性操作', expires_at: expires, details: [] },
  } as unknown as ThreadItem;
  const t = (key: string, fallback: string) => String(key.split('.').reduce<any>((node, part) => node?.[part], zh) ?? fallback);
  const { container } = render(<ThreadItemRenderer item={item} approvalArguments={args}
    approvalActions={<SessionPermissionActions permissionRequest={request as never} showArgumentDetails={false} t={t} />} />);
  fireEvent.click(screen.getByText('详情'));
  const details = container.querySelector('.thread-item-disclosure')!;
  expect.soft(details.textContent).toContain('创建数字员工');
  expect.soft(details.textContent).toContain('可停用，但不能删除');
  expect.soft(details.textContent).toContain(new Date(expires).toLocaleString('zh'));
  expect.soft(details.textContent).not.toContain('create_employee_from_request');
  expect.soft(details.textContent).not.toContain(expires);
  expect.soft(details.textContent).not.toContain('破坏性操作');
  expect.soft(container.querySelector('pre')).toBeNull();
  expect.soft(container.textContent).not.toContain('删除类操作');
  expect.soft(container.textContent).toContain('此操作只能本次允许');
  fireEvent.click(screen.getByRole('button', { name: '完整参数' }));
  expect(container.querySelector('pre')?.textContent).toBe(JSON.stringify(args, null, 2));
});

it('hides only the platform deliverable declaration from rendered replies', () => {
  const prose = '报告已写入 workspace/report.md，创建结果仍可查看。';
  const { container, rerender } = render(<AssistantMessageBody content={`${prose}\n\nDELIVERABLE: workspace/report.md`}
    streaming={false} supplemental={false} supplementalLabel="补充说明" />);
  expect.soft(container.textContent).toContain(prose);
  expect.soft(container.textContent).not.toContain('DELIVERABLE:');
  rerender(<AssistantMessageBody content={`${prose}\nDELIVERABLE: workspace/rep`} streaming supplemental={false} supplementalLabel="补充说明" />);
  expect(container.textContent).not.toContain('DELIVERABLE:');
});

it('also hides the declaration while assistant prose is still in a running disclosure', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'running', status: 'running', steps: [{
    id: 'prose', kind: 'prose', status: 'running', title: 'Assistant update', visibility: 'visible',
    details: '报告位于 workspace/report.md。\nDELIVERABLE: workspace/report.md',
  }] }} />);
  expect(container.textContent).toContain('报告位于 workspace/report.md。');
  expect(container.textContent).not.toContain('DELIVERABLE:');
});

it('uses Chinese delegated-conversation labels without changing employee-authored text', () => {
  chat({ activeSession: { id: 'delegated', user_id: 'member', session_kind: 'agent_chat',
    title: 'Delegation: 周报助手', username: 'Delegation: 周报助手' }, historyMessagesSessionId: 'delegated' });
  expect.soft(document.body.textContent).toContain('委派对话');
  expect.soft(document.body.textContent).toContain('周报助手');
  expect.soft(document.body.textContent).not.toMatch(/Delegation:|Agent Conversation/);
});

it('localizes platform assistant and action names on the connection card', async () => {
  vi.spyOn(toolsApi, 'getSessionToolConfiguration').mockResolvedValue({ state: 'waiting', target_agent_id: 'bot',
    target_agent_name: 'My Assistant', tool: { id: 'email', name: 'send_email', display_name: 'Send Email' } } as never);
  render(<InlineToolConfigurationCard agentId="bot" sessionId="work" questionId="connect" question="连接邮箱"
    onAnswer={vi.fn()} />);
  await screen.findByText('我的助理');
  expect(screen.getByText('发送邮件', { exact: false })).toBeTruthy();
  expect(document.body.textContent).not.toMatch(/My Assistant|Send Email/);
});

it('labels removing a conversation as removing it from the member list', () => {
  chat({ isAdmin: true, allSessions: [{ id: 'work', user_id: 'member', title: '销售报告' }] });
  expect(screen.getByRole('button', { name: '从列表移除对话：销售报告' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: /Delete session/ })).toBeNull();
});

it('localizes the copy and scroll controls', () => {
  const { unmount } = render(<CopyMessageButton text="正文" />);
  expect.soft(screen.queryByTitle('复制')).toBeTruthy();
  expect.soft(screen.queryByRole('button', { name: '复制' })).toBeTruthy();
  unmount();
  chat({ activeSession: { id: 'work', user_id: 'member' }, chatMessagesSessionId: 'work', showScrollBtn: true });
  expect(screen.getByTitle('滚动到底部')).toBeTruthy();
});

it('uses recovered display text for a message-jump button title and accessible name', () => {
  const checkpoint = { id: 'checkpoint', event_id: 'checkpoint', sequence: 1,
    prompt: '[{"display_content":"[📎 sales.csv]\\n整理销售报告","text":"INTERNAL PROMPT"}]' };
  render(<SessionGitLine activeSessionId="work" axisSessionId="work" checkpoints={[checkpoint]} lineage={[]}
    focusedCheckpointId={null} onNavigateCheckpoint={vi.fn()} />);
  const jump = screen.getByTestId('session-gitline-checkpoint');
  expect.soft(jump.getAttribute('title')).toBe('整理销售报告');
  expect(jump.getAttribute('aria-label')).toBe('整理销售报告');
});

it.each(['member', 'org_admin'])('shows deliverables and decisions to %s with counters reserved for admins', (role) => {
  useAuthStore.setState({ user: { id: 'member', role } as never });
  const { container } = render(<SessionRuntimePanel agent={{ id: 'bot', name: 'My Assistant', is_personal_assistant: true }}
    messages={[{ role: 'assistant', content: '完成', artifacts: [{ name: 'report.md', path: 'workspace/report.md',
      previewKind: 'markdown', runtimeTaskId: 'run-1', snapshotHash: 'hash', sourceAgentName: 'My Assistant' }] }] as never}
    activeSession={{ id: 'work', title: '报告' }} sessionWorkbench={{ session: { id: 'work' }, runtime_sections: {
      runs: [{ id: 'run-1', runtime_kind: 'runtime_task', label: '运行-1', status: 'awaiting_approval', summary: '请确认创建数字员工' }],
    } } as never} />);
  expect(screen.getByText('report.md')).toBeTruthy();
  expect(screen.getByTestId('session-runtime-waiters')).toBeTruthy();
  if (role === 'member') {
    expect.soft(container.querySelector('[role="tablist"]')).toBeNull();
    expect.soft(container.querySelector('[data-testid="session-runtime-summary-strip"]')).toBeNull();
    expect.soft(container.textContent).not.toMatch(/Team|A2A|Workers|Workflow|Activity|BACKGROUND|运行-1|snapshot|My Assistant/);
    expect(container.textContent).toContain('我的助理');
  } else {
    expect(container.querySelector('[role="tablist"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="session-runtime-summary-strip"]')).toBeTruthy();
  }
});

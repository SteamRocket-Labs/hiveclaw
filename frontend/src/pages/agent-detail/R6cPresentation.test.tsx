// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import AgentChatSection from './AgentChatSection';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import type { ChatTranscriptEventPayload } from './chatRuntime';
import { buildThreadTimeline } from '../session-workbench/timelineModel';
import { SessionWorkbenchHeader } from '../session-workbench/SessionWorkbenchChrome';

const locale = vi.hoisted(() => ({ language: 'zh' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
  i18n: { language: locale.language },
}) }));
vi.mock('./ToolAccounts', () => ({ default: () => null }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); locale.language = 'zh'; });

it('removes the optimistic run, clock, header, panel state and stop after a failed send', () => {
  const ref = React.createRef<any>();
  const noop = vi.fn();
  const session = { id: 'work', user_id: 'member', title: '测试对话' };
  const retry = vi.fn();
  const message = { role: 'user' as const, id: 'input', content: '保留这条消息',
    timestamp: new Date(Date.now() - 2000).toISOString(), sendState: 'sending' as const };
  const props = {
    agent: { id: 'bot', name: '助理' }, currentUser: { id: 'member' }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [], chatMessages: [message],
    chatMessagesSessionId: 'work', attachedFiles: [], wsConnected: true, isWaiting: false, isStreaming: false,
    sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: message.content, runtimeSummary: null, activeRunStatus: null, runtimePhase: 'idle',
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: noop,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const frame = (values: typeof props) => <MemoryRouter><QueryClientProvider client={client}>
    <AgentChatSection {...values} />
  </QueryClientProvider></MemoryRouter>;
  const { container, rerender } = render(frame(props));
  expect(screen.getByTestId('run-disclosure-block')).toBeTruthy();
  expect(container.querySelector('.swb-header-status')?.textContent).toContain('运行中');
  const failed = { ...message, sendState: 'failed' as const, sendError: '消息未发送，请重试。', retrySend: retry };
  rerender(frame({ ...props, chatMessages: [failed] }));
  expect.soft(container.querySelector('[data-testid="run-disclosure-block"]')).toBeNull();
  expect.soft(container.querySelector('.swb-header-status')).toBeNull();
  expect.soft(screen.getByTestId('session-runtime-panel').textContent).not.toMatch(/运行中|思考中/);
  expect.soft(screen.queryByTestId('session-runtime-collapsed-running')).toBeNull();
  expect.soft(screen.queryByTestId('session-composer-stop')).toBeNull();
  expect.soft(container.querySelector('.run-disclosure-duration')).toBeNull();
  expect.soft((screen.getByTestId('session-composer-input') as HTMLTextAreaElement).value).toBe(message.content);
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  expect(retry).toHaveBeenCalledOnce();
});

it.each(['zh', 'en'])('localizes the backend default conversation title in header and history (%s)', language => {
  locale.language = language;
  const session = { id: 'work', title: 'Session 11-09 03:41', session_kind: 'human_chat' };
  const model = buildThreadTimeline({ activeSession: session, messages: [], isWaiting: false, isStreaming: false }).header;
  const { container } = render(<MemoryRouter><SessionWorkbenchHeader model={model}
    history={{ agentId: 'bot', sessions: [session], activeSessionId: session.id }} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: language === 'zh' ? '历史' : 'History' }));
  expect(container.querySelector('.swb-header-title')?.textContent).toBe(language === 'zh' ? '新对话' : 'New conversation');
  expect(container.querySelector('.swb-history-item-title')?.textContent).toBe(language === 'zh' ? '会话 11-09 03:41' : 'Session 11-09 03:41');
});

it.each([
  ['save_memory', '保存记忆'], ['search_memory', '查找记忆'], ['load_memory', '读取记忆'],
  ['update_memory', '更新记忆'], ['retire_memory', '忘记记忆'],
  // R11g row 10: a nameless tool step reads 处理请求.
  ['unknown_tool', '处理请求'],
])('names a completed %s group through the shared mapping', (toolName, label) => {
  const timeline = buildRunTimelineFromMessages([
    { id: 'tool', role: 'tool_call', content: '', toolName, toolStatus: 'done' },
    { id: 'answer', role: 'assistant', content: '已完成。' },
  ]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  fireEvent.click(container.querySelector('.run-disclosure-header')!);
  const group = screen.getByTestId('run-disclosure-tool-group-toggle');
  expect(group.textContent).toContain(toolName === 'unknown_tool' ? '其他操作' : label);
  expect(group.textContent).not.toContain('…');
  fireEvent.click(group);
  // R11g row 10: a nameless tool step reads 处理请求.
  expect(container.querySelector('.run-tool-history-title')?.textContent).toBe(label);
});

it.each(['done', 'running'] as const)('describes a %s thinking step with its own lifecycle', status => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'thinking-run', status, steps: [
    { id: 'thought', kind: 'reasoning', title: 'Thinking', status, visibility: 'visible', details: '核对资料。' },
  ] }} />);
  if (status === 'done') fireEvent.click(container.querySelector('.run-disclosure-header')!);
  expect(container.querySelector('.run-step-title')?.textContent).toBe(status === 'done' ? '已思考' : '思考中');
});

it.each(['zh', 'en'])('shows a completed command status without stdout or clipping (%s)', language => {
  locale.language = language;
  const stdout = ['Output: member stdout', ...Array.from({ length: 64 }, (_, i) => `line-${i + 1}`)];
  const output = `📤 Output:\n${stdout.join('\n')}`;
  const details = { toolName: 'run_command', args: { command: 'print-report' }, result: output, exit_code: 0 };
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'command', status: 'running', steps: [
    { id: 'cmd', kind: 'command', title: 'Run command', status: 'done', visibility: 'visible',
      details },
  ] }} />);
  fireEvent.click(container.querySelector('.run-step-toggle')!);
  // R11g: structured exit_code: 0 confirms completion without showing command stdout.
  expect(container.querySelector('.run-step-body p')?.textContent).toBe(language === 'zh' ? '运行命令。成功' : 'Run command. success');
  // R11g: member step details never show command stdout.
  expect(container.textContent).not.toMatch(/Output: member stdout|line-1|📤 Output:|56 lines|省略 56 行/);
  // R11g: hiding command stdout must preserve the original result bytes.
  expect(details.result).toBe(output);
  expect(output).toBe(`📤 Output:\n${stdout.join('\n')}`);
});

it('hides wrapped multiline commands and structured stdout behind neutral status sentences', () => {
  const command = "printf 'first\\n'\nprintf 'last\\n'";
  const stdout = '📤 Output:\nThis header belongs to stdout.';
  const timeline = { id: 'raw-command', status: 'running' as const, steps: [
    { id: 'wrapped', kind: 'command' as const, title: 'Run command', status: 'done' as const, visibility: 'visible' as const,
      details: { toolName: 'run_command', args: { command }, result: `💻 Command: ${command}\n\n📤 Output:\n${stdout}` } },
    { id: 'structured', kind: 'command' as const, title: 'Run command', status: 'done' as const, visibility: 'visible' as const,
      details: { command, output: stdout } },
  ] };
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  for (const button of container.querySelectorAll('.run-step-toggle')) fireEvent.click(button);
  // R11g: done steps without structured exit codes keep their outcomes unconfirmed.
  expect(Array.from(container.querySelectorAll('.run-step-body p'), node => node.textContent)).toEqual([
    '运行命令。结果尚未确认。', '运行命令。结果尚未确认。',
  ]);
  // R11g: member step details never show command stdout.
  expect(container.textContent).not.toMatch(/This header belongs to stdout\.|📤 Output:|💻 Command:|printf/);
});

it('shows a neutral Session V2 command status without stdout when admission exposes only an args hash', () => {
  const common = { schema: 'hive.session_event', schema_version: 2, tenant_id: 'tenant',
    run_id: 'run', invocation_id: 'invocation', provider_tool_use_id: 'call', result_id: 'model-result',
    scope: { level: 'round', run_id: 'run', turn_id: 'turn', round_id: 'round', session_id: 'work', thread_id: 'work' },
    actor: { type: 'tool' }, visibility: { audience: 'direct_user' },
    occurred_at: '2026-11-09T05:40:13Z', persisted_at: '2026-11-09T05:40:13Z' };
  const binding = { invocation_id: 'invocation', provider_tool_use_id: 'call' };
  const command = 'printf report';
  const stdout = 'Original stdout';
  const result = `💻 Command: ${command}\n\n📤 Output:\n${stdout}`;
  const events = [
    { ...common, event_id: 'started', sequence: 1, item_id: 'call-item', item_kind: 'tool_call',
      kind: 'tool_call.started', lifecycle: 'started', payload_schema: 'hive.session.payload.tool_call.started.v2',
      payload: { ...binding, tool_name: 'run_command', args_hash: 'canonical-args-hash' } },
    { ...common, event_id: 'completed', sequence: 2, item_id: 'call-item', item_kind: 'tool_call',
      kind: 'tool_call.completed', lifecycle: 'completed', payload_schema: 'hive.session.payload.tool_call.completed.v2',
      payload: { ...binding, outcome: 'success' } },
    { ...common, event_id: 'result', sequence: 3, item_id: 'result-item', item_kind: 'tool_result',
      kind: 'tool_result.completed', lifecycle: 'completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      payload: { ...binding, parts: [], content: result, outcome: 'success' } },
  ] as unknown as ChatTranscriptEventPayload[];
  const { messages } = hydrateSessionTranscriptEvents(events);
  const timeline = buildRunTimelineFromMessages([...messages, { role: 'assistant', content: '检查结束。' }]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  fireEvent.click(container.querySelector('.run-disclosure-header')!);
  fireEvent.click(container.querySelector('.run-step-toggle')!);
  // R11g: outcome: success without a structured exit code leaves command success unconfirmed.
  expect(container.querySelector('.run-step-body p')?.textContent).toBe('运行命令。结果尚未确认。');
  // R11g: member step details never show command stdout.
  expect(container.textContent).not.toMatch(/Original stdout|📤 Output:|💻 Command:|printf report/);
  expect(events[2].payload?.content).toBe(result);
});

// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import AgentChatSection from './AgentChatSection';
import { applyTranscriptEvent, createEmptyTranscriptReplayState, type AgentChatMessage } from './chatRuntime';
import { buildThreadTimeline } from '../session-workbench/timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
  i18n: { language: 'zh' },
}) }));
vi.mock('./ToolAccounts', () => ({ default: () => null }));
afterEach(cleanup);

const failed: AgentChatMessage = { id: 'input', role: 'user', content: '保留原任务',
  timestamp: '2026-11-09T00:00:00Z', sendState: 'failed', sendError: '消息未发送，请重试。', retrySend: () => {} };

it.each([undefined, 'accepted-run'])('shows running block, header, panel and stop after a server frame, run id: %s', run_id => {
  const initial = { ...createEmptyTranscriptReplayState(), messages: [failed] };
  const accepted = applyTranscriptEvent(initial, { type: 'thinking', content: '正在检查资料。', run_id });
  const ref = React.createRef<any>();
  const noop = vi.fn();
  const session = { id: 'work', user_id: 'member', title: '原任务' };
  const props = {
    agent: { id: 'bot', name: '助理' }, currentUser: { id: 'member' }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [], chatMessages: accepted.messages,
    chatMessagesSessionId: 'work', attachedFiles: [], wsConnected: true,
    isWaiting: accepted.ui.isWaiting, isStreaming: accepted.ui.isStreaming, runtimePhase: accepted.ui.phase,
    sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: failed.content, runtimeSummary: null, activeRunStatus: null,
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: noop,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  const { container } = render(<MemoryRouter><QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })}><AgentChatSection {...props} /></QueryClientProvider></MemoryRouter>);
  expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('running');
  expect(container.querySelector('.swb-header-status')?.textContent).toContain('运行中');
  fireEvent.click(screen.getByTestId('session-runtime-collapse-toggle'));
  fireEvent.click(screen.getByTestId('session-runtime-run-status'));
  expect(screen.getByTestId('session-runtime-panel').textContent).toContain('运行中');
  expect(screen.getByTestId('session-composer-stop')).toBeTruthy();
});

it('does not let a failed input hide the later recorded incomplete run', () => {
  const model = buildThreadTimeline({ messages: [failed,
    { id: 'reasoning', role: 'assistant', content: '', thinking: 'The server had started.', _streaming: true } as AgentChatMessage,
  ], activeSession: { id: 'work' }, isWaiting: false, isStreaming: false, runtimePhase: 'idle' });
  expect(model.cells.at(-1)?.kind).toBe('active_run');
  expect(model.header.status).toBe('failed');
});

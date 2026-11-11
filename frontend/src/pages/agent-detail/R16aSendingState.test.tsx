// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import AgentChatSection from './AgentChatSection';
import type { AgentChatMessage } from './chatRuntime';
import { buildThreadTimeline, type BuildThreadTimelineInput } from '../session-workbench/timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
  i18n: { language: 'zh' },
}) }));
vi.mock('./ToolAccounts', () => ({ default: () => null }));
afterEach(() => { cleanup(); sessionStorage.clear(); });

function runBoundary(runId: string, lifecycle: string, id = `${runId}-${lifecycle}`): AgentChatMessage {
  return { role: 'event', id, content: '', eventType: `run_${lifecycle}`,
    sessionItem: { id, kind: 'run', lifecycle, scope: { level: 'run', run_id: runId, session_id: 'work' },
      payload: {}, actor: { type: 'runtime' }, last_sequence: 5, occurredAt: '2026-11-11T00:00:01Z' } as never };
}

const history: AgentChatMessage[] = [
  { role: 'user', id: 'previous', content: 'Previous request' },
  runBoundary('old-run', 'cancelled'),
];
const sending: AgentChatMessage = { role: 'user', id: 'next', content: 'Fresh request', sendState: 'sending',
  timestamp: '2026-11-11T00:00:02Z' };

function renderThread(input: Partial<BuildThreadTimelineInput>) {
  const ref = React.createRef<any>();
  const noop = vi.fn();
  const session = { id: 'work', user_id: 'member', title: 'Current conversation' };
  const base = {
    agent: { id: 'bot', name: 'Assistant' }, currentUser: { id: 'member' }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [], chatMessages: [],
    chatMessagesSessionId: 'work', attachedFiles: [], wsConnected: true,
    isWaiting: false, isStreaming: false, runtimePhase: 'cancelled', activeRunStatus: null,
    sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: '', runtimeSummary: null,
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: noop,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (next: Partial<BuildThreadTimelineInput>) => <MemoryRouter><QueryClientProvider client={client}>
    <AgentChatSection {...{ ...base, ...next, chatMessages: next.messages } as unknown as React.ComponentProps<typeof AgentChatSection>} />
  </QueryClientProvider></MemoryRouter>;
  const rendered = render(view(input));
  return { ...rendered, update: (next: Partial<BuildThreadTimelineInput>) => rendered.rerender(view(next)) };
}

function expectSurfaces(label: string) {
  expect.soft(screen.getByTestId('session-workbench-header').querySelector('.swb-header-status')?.textContent).toBe(label);
  if (screen.getByTestId('session-runtime-panel').classList.contains('is-collapsed')) {
    fireEvent.click(screen.getByTestId('session-runtime-collapse-toggle'));
  }
  expect.soft(screen.getByTestId('session-runtime-run-status').querySelector('.session-runtime-status')?.textContent).toBe(label);
  expect.soft(screen.queryAllByTestId('run-disclosure-block').at(-1)?.querySelector('.run-disclosure-title')?.textContent).toBe(label);
}

it.each(['cancelled', 'completed', 'failed'])('R16a.3 round2 keeps sending surfaces when a late previous-run %s boundary arrives', lifecycle => {
  const before = { messages: [...history, sending] };
  const view = renderThread(before);
  expectSurfaces('运行中');
  const messages = [...before.messages, runBoundary('old-run', lifecycle, 'late-boundary')];
  view.update({ messages });
  expectSurfaces('运行中');
  const model = buildThreadTimeline({ ...before, messages, activeSession: { id: 'work' }, isWaiting: false, isStreaming: false });
  expect(model.cells.at(-1)).toMatchObject({ id: 'active-run-pending', kind: 'active_run', timeline: { status: 'running' } });
  expect(model.cells.at(-1)).not.toHaveProperty('runId');
  expect(screen.getByText('Fresh request').textContent).toBe('Fresh request');
});

it.each(['cancelled', 'completed', 'failed'])('R16a.3 round2 keeps sending surfaces with previous-run activeRunStatus=%s', activeRunStatus => {
  renderThread({ messages: [...history, sending], activeRunId: 'old-run', activeRunStatus });
  expectSurfaces('运行中');
  expect(screen.queryAllByTestId('run-disclosure-block').at(-1)?.getAttribute('data-status')).toBe('running');
});

it.each(['boundary', 'activeRunStatus', 'none'])('R16a.3 round2 shows the queued input on every surface with old terminal source %s', source => {
  const messages = [...history, { ...sending, sendState: 'queued' as const }];
  if (source === 'boundary') messages.push(runBoundary('old-run', 'cancelled', 'late-boundary'));
  renderThread({ messages, ...(source === 'activeRunStatus' ? { activeRunId: 'old-run', activeRunStatus: 'cancelled' } : {}) });
  expectSurfaces('排队中');
});

it.each(['cancelled', 'completed', 'failed'])('R16a.3 round2 preserves a terminal %s belonging to the newly sent run', lifecycle => {
  renderThread({ messages: [...history, sending,
    { role: 'tool_call', id: 'new-read', content: '', toolName: 'read_file', toolStatus: 'done', eventRuntimeTaskId: 'new-run' },
    runBoundary('new-run', lifecycle)], activeRunId: 'new-run', activeRunStatus: lifecycle });
  expectSurfaces(lifecycle === 'cancelled' ? '已停止' : lifecycle === 'completed' ? '完成' : '失败');
});

it('R16a.3 round2 lets the acknowledged new run drive all surfaces after sending clears', () => {
  const view = renderThread({ messages: [...history, sending], activeRunId: 'old-run', activeRunStatus: 'cancelled' });
  view.update({ messages: [...history, { ...sending, sendState: undefined }], activeRunId: 'new-run', activeRunStatus: 'running',
    runtimePhase: 'thinking', isWaiting: true });
  expectSurfaces('运行中');
  view.update({ messages: [...history, { ...sending, sendState: undefined }, runBoundary('new-run', 'failed')],
    activeRunId: 'new-run', activeRunStatus: 'failed', runtimePhase: 'failed' });
  expectSurfaces('失败');
});

it('R16a.3 round2 clears the queued input label when acknowledgment leaves the backend in its queued phase', () => {
  const view = renderThread({ messages: [...history, { ...sending, sendState: 'queued' }], runtimePhase: 'queued' });
  expectSurfaces('排队中');
  view.update({ messages: [...history, { ...sending, sendState: undefined }], runtimePhase: 'queued', isWaiting: true });
  expectSurfaces('运行中');
});

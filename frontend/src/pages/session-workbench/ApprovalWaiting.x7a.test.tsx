// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import fixture from '../../test/fixtures/x7aRun21Approval.json';
import AgentChatSection from '../agent-detail/AgentChatSection';
import { applySessionActiveRunObservedState, uiForPhase, type AgentChatMessage, type ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import { sessionRunStateFromPayload } from '../agent-detail/runtimeBudgetState';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import { buildThreadTimeline } from './timelineModel';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
  i18n: { language: locale.language },
}) }));
vi.mock('../agent-detail/ToolAccounts', () => ({ default: () => null }));

afterEach(() => { cleanup(); vi.useRealTimers(); window.sessionStorage.clear(); locale.language = 'zh'; });

// Selected run-21 step-8 envelopes: 1, 10, 137–139, 141, 145, 148–149,
// 158, 164–165. IDs and content are anonymized, bookkeeping is omitted,
// and sequences are contiguous. Retain the mixed-plane permission pair,
// the hex/UUID run identity, timestamps, and zero-copy final answer shape.
const events = fixture.events as unknown as ChatTranscriptEventPayload[];
const session = { id: 'session-x7a', user_id: 'member-x7a', title: 'Company plaza' };
const key = `agent-x7a:${session.id}`;
const runId = fixture.activeRun.run_id;

function project(mode: 'live' | 'reload', through: number) {
  if (mode === 'reload') return hydrateSessionTranscriptEvents(events.slice(0, through), 0);
  let messages: AgentChatMessage[] = [];
  let ui = uiForPhase('idle');
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set([key]), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => runId, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, current) => current, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { messages = update(messages); },
    setChatMessagesAfterQueued: (_session, update) => { messages = update(messages); },
    setActivePhase: phase => { ui = uiForPhase(phase); },
    setIsWaiting: value => { ui = { ...ui, isWaiting: value }; },
    setIsStreaming: value => { ui = { ...ui, isStreaming: value }; }, parseChatMsg: message => message,
  };
  for (const event of events.slice(0, through)) applyTranscriptToSessionRuntime(deps, 'agent-x7a', session.id, event, true);
  return { messages, ui };
}

function chat(mode: 'live' | 'reload', through: number, activeStatus: string | null, stop = vi.fn()) {
  const projected = project(mode, through);
  const run = activeStatus ? sessionRunStateFromPayload({ ...fixture.activeRun, status: activeStatus }) : null;
  const ui = run ? applySessionActiveRunObservedState({}, { [key]: projected.ui }, key, run).uiStates[key] : projected.ui;
  const ref = React.createRef<any>();
  const noop = vi.fn();
  const props = {
    agent: { id: 'agent-x7a', name: 'Assistant' }, currentUser: { id: 'member-x7a' }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [],
    chatMessages: projected.messages, chatMessagesSessionId: session.id, attachedFiles: [], wsConnected: true,
    sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: '', runtimeSummary: null, transportNotice: null,
    activeRunId: run?.runId ?? null, activeRunStatus: run?.status ?? null, runtimePhase: ui.phase,
    isWaiting: ui.isWaiting, isStreaming: ui.isStreaming, sessionOnly: true,
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: stop,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  return <AgentChatSection {...props} />;
}

function status(label: string, blockStatus: string) {
  expect.soft(screen.getByTestId('session-workbench-header').querySelector('.swb-header-status')?.textContent).toBe(label);
  const block = screen.getByTestId('run-disclosure-block');
  expect.soft(block.getAttribute('data-status')).toBe(blockStatus);
  expect.soft(block.querySelector('.run-disclosure-header')?.textContent).toContain(label);
}

it.each((['live', 'reload'] as const).flatMap(mode => [4, 5, 6].map(through => ({ mode, through }))))(
  'X7a: $mode suspended/awaiting_session_permission agrees with pending prefix $through', ({ mode, through }) => {
    const client = new QueryClient();
    const view = render(<QueryClientProvider client={client}>{chat(mode, through, 'suspended')}</QueryClientProvider>);
    status('等你决定', 'blocked');
    view.unmount();
    client.clear();
  },
);

it.each([
  ['cancelled', '已停止'], ['completed', '完成'], ['failed', '失败'],
])('X7a: an unresolved permission never overrides the run terminal %s', (lifecycle, label) => {
  const terminal = { ...events.at(-1)!, sequence: 7, lifecycle, kind: `run.${lifecycle}`,
    payload_schema: `hive.session.payload.run.${lifecycle}.v2` };
  const messages = hydrateSessionTranscriptEvents([...events.slice(0, 6), terminal], 0).messages;
  const model = buildThreadTimeline({ messages, activeSession: session, isWaiting: false, isStreaming: false });
  const view = render(<><SessionWorkbenchHeader model={model.header} />{model.cells.map(cell => cell.kind === 'active_run'
    ? <RunDisclosureBlock key={cell.id} timeline={cell.timeline} /> : null)}</>);
  status(label, lifecycle === 'completed' ? 'done' : lifecycle);
  view.unmount();
});

it.each(['live', 'reload'] as const)('X7a: %s waits on the real permission item, freezes the clock, then resumes and completes', async mode => {
  await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } } });
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-14T18:29:03Z'));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const stop = vi.fn();
  const view = render(<QueryClientProvider client={client}>{chat(mode, 3, 'running', stop)}</QueryClientProvider>);
  status('运行中', 'running');
  const duration = () => view.container.querySelector('.run-disclosure-duration')?.textContent;
  const before = duration();
  act(() => { vi.advanceTimersByTime(1000); });
  expect(duration()).not.toBe(before);

  // X7a: the row's own pending item must win even before run.waiting or
  // the next runs/active read arrives. Existing assertions stay unchanged.
  for (const through of [4, 5, 6]) {
    view.rerender(<QueryClientProvider client={client}>{chat(mode, through, through === 6 ? 'suspended' : 'running', stop)}</QueryClientProvider>);
    status('等你决定', 'blocked');
  }
  const waiting = duration();
  act(() => { vi.advanceTimersByTime(84_000); });
  expect(duration()).toBe(waiting);
  const pendingBlock = screen.getByTestId('run-disclosure-block');
  expect(pendingBlock.querySelector('[data-status="blocked"] [role="img"][aria-label="等待处理"]')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /^停止$/ }));
  expect(stop).toHaveBeenCalledTimes(1);

  // Remount while the approval is pending to cover the actual reload path.
  view.unmount();
  const reloaded = render(<QueryClientProvider client={client}>{chat('reload', 6, 'suspended', stop)}</QueryClientProvider>);
  status('等你决定', 'blocked');
  const reloadDuration = reloaded.container.querySelector('.run-disclosure-duration')?.textContent;
  act(() => { vi.advanceTimersByTime(1000); });
  expect(reloaded.container.querySelector('.run-disclosure-duration')?.textContent).toBe(reloadDuration);

  reloaded.rerender(<QueryClientProvider client={client}>{chat(mode, 9, 'running', stop)}</QueryClientProvider>);
  status('运行中', 'running');
  const resumed = reloaded.container.querySelector('.run-disclosure-duration')?.textContent;
  act(() => { vi.advanceTimersByTime(1000); });
  expect(reloaded.container.querySelector('.run-disclosure-duration')?.textContent).not.toBe(resumed);
  reloaded.rerender(<QueryClientProvider client={client}>{chat(mode, events.length, null, stop)}</QueryClientProvider>);
  status('完成', 'done');
  reloaded.unmount();
  client.clear();
});

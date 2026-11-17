// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import rows from '../../test/fixtures/r26aRun21Rows.json';
import AgentChatSection from '../agent-detail/AgentChatSection';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import { isSameSessionRunId } from '../agent-detail/chatRuntime';
import type { AgentChatMessage, ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { buildThreadTimeline } from './timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => { await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, initImmediate: false }); });
afterEach(() => { cleanup(); vi.useRealTimers(); window.sessionStorage.clear(); });

const failure = rows.events.find(event => event.item_kind === 'runtime_failure')!;
const failureScope = failure.scope!;
const events = rows.events.filter(event => event.sequence <= failure.sequence) as unknown as ChatTranscriptEventPayload[];
const sessionId = failureScope.session_id;
function messages(mode: 'live' | 'reload', input = events): AgentChatMessage[] {
  if (mode === 'reload') return hydrateSessionTranscriptEvents(input, 0).messages;
  let current: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => null, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, value) => value, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_key, update) => { current = update(current); },
    setChatMessagesAfterQueued: (_key, update) => { current = update(current); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const event of input) applyTranscriptToSessionRuntime(deps, 'agent', 'session', event, true);
  return current;
}

it.each(['live', 'reload'] as const)('advances the colleague handoff clock (%s)', mode => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(rows.parentTask.completed_at));
  const model = buildThreadTimeline({ messages: messages(mode), activeSession: { id: sessionId },
    isWaiting: false, isStreaming: false, activeRunStatus: null });
  const parent = model.cells.find(cell => cell.kind === 'active_run' && cell.timeline.delegationInFlight);
  if (parent?.kind !== 'active_run') throw new Error('Expected the outstanding colleague handoff');
  expect(parent.timeline.durationIsFinal).toBe(false);
  const { container } = render(<RunDisclosureBlock timeline={parent.timeline} />);
  expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('运行中');
  const before = container.querySelector('.run-disclosure-duration')?.textContent;
  act(() => { vi.advanceTimersByTime(10_000); });
  expect(container.querySelector('.run-disclosure-duration')?.textContent).not.toBe(before);
  for (let pass = 0; pass < 3; pass++) {
    container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]').forEach(button => fireEvent.click(button));
  }
  expect(container.querySelector('[data-status="blocked"] [aria-label="等待同事"]')).toBeTruthy();
});

it.each(['live', 'reload'] as const)('shows an applied handoff stop without changing the parent terminal (%s)', mode => {
  const stoppedAt = new Date(Date.parse(rows.parentTask.completed_at) + 60_000).toISOString();
  const accepted = {
    ...failure, event_id: 'x8-cancel-applied', sequence: events.length + 1,
    item_id: 'x8-cancel', item_kind: 'control_input', kind: 'control_input.accepted', lifecycle: 'accepted',
    payload_schema: 'hive.session.payload.control_input.accepted.v2', occurred_at: rows.parentTask.completed_at,
    payload: { kind: 'cancel_run', expected_run_id: failureScope.run_id },
  } as unknown as ChatTranscriptEventPayload;
  const control = {
    ...accepted, event_id: 'x8-stop-applied', sequence: events.length + 2,
    kind: 'control_input.applied', lifecycle: 'applied', occurred_at: stoppedAt, persisted_at: stoppedAt,
    payload_schema: 'hive.session.payload.control_input.applied.v2',
    payload: { expected_run_id: failureScope.run_id, terminal_status: 'completed' },
  } as unknown as ChatTranscriptEventPayload;
  const projected = messages(mode, [...events, accepted, control]);
  const model = buildThreadTimeline({ messages: projected, activeSession: { id: sessionId },
    isWaiting: false, isStreaming: false, activeRunStatus: null });
  expect(model.header.status).toBe('cancelled');
  const parent = model.cells.find(cell => cell.kind === 'active_run' && isSameSessionRunId(cell.runId, failureScope.run_id));
  if (parent?.kind !== 'active_run') throw new Error('Expected the stopped handoff');
  expect(parent.timeline.status).toBe('cancelled');
  expect(parent.timeline.durationIsFinal).toBe(true);
  expect(parent.timeline.completedAt).toBe(stoppedAt);
  expect(parent.timeline.delegationInFlight).not.toBe(true);
  const { container } = render(<RunDisclosureBlock timeline={parent.timeline} />);
  expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('已停止');
  for (let pass = 0; pass < 3; pass++) {
    container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]').forEach(button => fireEvent.click(button));
  }
  expect(container.querySelector('[aria-label="等待同事"]')).toBeNull();
  const checks = parent.timeline.steps.filter(step => ['check_async_task', 'task_output'].includes(
    (step.details as { toolName?: string })?.toolName || ''));
  expect(checks).toHaveLength(5);
  expect(checks.every(step => (step.details as { memberSummary: { state: string } }).memberSummary.state === 'in_progress')).toBe(true);
  expect(projected.filter(message => message.sessionItem?.kind === 'runtime_failure')).toHaveLength(1);
});

it('keeps the real composer stop reachable after the parent handoff', () => {
  const ref = React.createRef<HTMLDivElement>();
  const noop = vi.fn();
  const stop = vi.fn();
  const session = { id: sessionId, user_id: 'member', title: '同事工作中' };
  const props = {
    agent: { id: 'bot', name: '助理' }, currentUser: { id: 'member' }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [], chatMessages: messages('reload'),
    chatMessagesSessionId: sessionId, attachedFiles: [], wsConnected: true, isWaiting: false, isStreaming: false,
    sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: '', runtimeSummary: null, activeRunStatus: null, runtimePhase: 'done',
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: stop,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container, getByTestId } = render(<MemoryRouter><QueryClientProvider client={client}>
    <AgentChatSection {...props} />
  </QueryClientProvider></MemoryRouter>);
  expect(container.querySelector('.swb-header-status')?.textContent).toContain('运行中');
  fireEvent.click(getByTestId('session-composer-stop'));
  expect(stop).toHaveBeenCalledOnce();
});

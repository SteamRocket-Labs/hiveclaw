// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import AgentChatSection from '../agent-detail/AgentChatSection';
import { uiForPhase, type AgentChatMessage, type ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import type { SessionEventV2 } from './sessionEventStore';
import { buildThreadTimeline } from './timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
  i18n: { language: 'zh' },
}) }));
vi.mock('../agent-detail/ToolAccounts', () => ({ default: () => null }));
vi.mock('../../api/domains/assistant', () => ({ assistantApi: { get: async () => ({ agent: null }) } }));

const session = { id: 'session-x7e', user_id: 'member-x7e', title: 'Company plaza' };
const runId = '11111111-1111-4111-8111-111111111111';
const key = `agent-x7e:${session.id}`;
type Mode = 'live' | 'reload';
function event(sequence: number, kind: string, lifecycle: string, at: string,
  payload: Record<string, unknown> = {}, itemId = kind): SessionEventV2 {
  const level = kind === 'human_input' ? 'session' : ['run', 'control_input'].includes(kind) ? 'run' : 'round';
  const sessionScope = { session_id: session.id, thread_id: session.id };
  const runScope = { ...sessionScope, run_id: runId, turn_id: 'turn' };
  const scope: SessionEventV2['scope'] = level === 'session' ? { ...sessionScope, level }
    : level === 'run' ? { ...runScope, level } : { ...runScope, level, round_id: 'round' };
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'company', item_id: itemId, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope,
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: at, persisted_at: at,
  };
}
const request = { permission_request_id: 'permission', tool_name: 'plaza_create_post',
  arguments: { content: 'Team update' }, allow_session_allowed: true, session_id: session.id,
  runtime_task_id: runId, requested_by_user_id: session.user_id };
// Run22 step8: accepted (105), applied (106) and permission.completed (107)
// arrive while the 4,547 ms resolve POST is pending, before run.queued (111).
// Preserve those payload shapes, same-item updates and timestamps; anonymize identities/content.
const events = [
  event(1, 'human_input', 'accepted', '2026-11-15T03:02:50.663048Z',
    { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Post to the company plaza.' }] }),
  event(2, 'run', 'queued', '2026-11-15T03:02:52.278915Z', {}, runId),
  event(3, 'tool_call', 'started', '2026-11-15T03:03:35.996279Z',
    { name: 'plaza_create_post', arguments: request.arguments }, 'call'),
  event(4, 'tool_permission', 'waiting', '2026-11-15T03:03:37.350516Z', { permission_request: request }, 'permission'),
  event(5, 'run', 'waiting', '2026-11-15T03:03:37.350541Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission' }, runId),
  event(6, 'control_input', 'accepted', '2026-11-15T03:07:36.697189Z',
    { kind: 'permission_response', permission_item_id: 'permission', decision: 'allow_once' }),
  event(7, 'control_input', 'applied', '2026-11-15T03:07:36.789105Z',
    { kind: 'permission_response', permission_item_id: 'permission', decision: 'allow_once' }),
  event(8, 'tool_permission', 'completed', '2026-11-15T03:07:36.910065Z', { decision: 'allow_once' }, 'permission'),
  event(9, 'tool_call', 'completed', '2026-11-15T03:07:40.153301Z', {}, 'call'),
  event(10, 'run', 'queued', '2026-11-15T03:07:40.328015Z', { reason_code: 'session_permission_resolved' }, runId),
  event(11, 'assistant_final', 'completed', '2026-11-15T03:07:53.250731Z', { content: 'Published.' }),
  event(12, 'run', 'completed', '2026-11-15T03:07:53.250751Z', {}, runId),
];
function project(input: SessionEventV2[], mode: Mode): AgentChatMessage[] {
  const wire = input as unknown as ChatTranscriptEventPayload[];
  if (mode === 'reload') return hydrateSessionTranscriptEvents(wire, 0).messages;
  let messages: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set([key]), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: { [key]: uiForPhase('idle') },
      runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => runId, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, current) => current, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { messages = update(messages); },
    setChatMessagesAfterQueued: (_session, update) => { messages = update(messages); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const envelope of wire) applyTranscriptToSessionRuntime(deps, 'agent-x7e', session.id, envelope, true);
  return messages;
}
let client: QueryClient;
afterEach(() => { cleanup(); client?.clear(); sessionStorage.clear(); });
function chat(mode: Mode, through: number, resolve: ReturnType<typeof vi.fn>) {
  const noop = vi.fn();
  const ref = React.createRef<any>();
  const props = {
    agent: { id: 'agent-x7e', name: 'Assistant' }, currentUser: { id: session.user_id }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [],
    chatMessages: project(events.slice(0, through), mode), chatMessagesSessionId: session.id,
    attachedFiles: [], wsConnected: true, sessionsLoading: false, allSessionsLoading: false,
    uploading: false, uploadProgress: -1, allUserFilter: '', chatInput: '', runtimeSummary: null, transportNotice: null,
    activeRunId: runId.replaceAll('-', ''), activeRunStatus: 'suspended', runtimePhase: 'awaiting_approval',
    isWaiting: true, isStreaming: false, sessionOnly: true,
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop,
    onAbortGeneration: noop, onResolveSessionPermission: resolve,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  return <QueryClientProvider client={client}><AgentChatSection {...props} /></QueryClientProvider>;
}
const header = () => screen.getByTestId('session-workbench-header').querySelector('.swb-header-status')?.textContent;

it.each(['live', 'reload'] as const)('X7e: %s keeps applying after HTTP success until its own events arrive', async mode => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let finish!: () => void;
  const resolve = vi.fn(() => new Promise<void>(done => { finish = done; }));
  const view = render(chat(mode, 5, resolve));
  expect(header()).toBe('等你决定');
  fireEvent.click(screen.getByRole('button', { name: '本次允许' }));
  expect(screen.getByText('正在应用决定…')).toBeTruthy();
  await act(async () => { finish(); });
  expect(screen.getByText('正在应用决定…')).toBeTruthy();
  expect((screen.getByRole('button', { name: '本次允许' }) as HTMLButtonElement).disabled).toBe(true);
  expect(resolve).toHaveBeenCalledExactlyOnceWith(request, 'allow_once');
  view.rerender(chat(mode, 8, resolve));
  expect(header()).toBe('运行中');
  expect(screen.queryByText('正在应用决定…')).toBeNull();
  view.rerender(chat(mode, events.length, resolve));
  expect(header()).toBe('完成');
});

it.each(['live', 'reload'] as const)('X7e: %s has no blank/stale-wait gap when decision events beat HTTP', async mode => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let finish!: () => void;
  const resolve = vi.fn(() => new Promise<void>(done => { finish = done; }));
  const view = render(chat(mode, 5, resolve));
  fireEvent.click(screen.getByRole('button', { name: '本次允许' }));
  for (const through of [6, 7, 8, 9]) {
    view.rerender(chat(mode, through, resolve));
    expect(header()).toBe('运行中');
    expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('running');
  }
  expect(screen.queryByRole('button', { name: '本次允许' })).toBeNull();
  await act(async () => { finish(); });
});

it('X7e: restores the same card and existing failure text when the POST fails', async () => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let fail!: (error: unknown) => void;
  const resolve = vi.fn(() => new Promise<void>((_done, reject) => { fail = reject; }));
  render(chat('reload', 5, resolve));
  const button = screen.getByRole('button', { name: '本次允许' }) as HTMLButtonElement;
  fireEvent.click(button);
  expect(screen.getByText('正在应用决定…')).toBeTruthy();
  await act(async () => { fail(undefined); });
  expect(screen.getByRole('alert').textContent).toBe(zh.agent.chat.permission.resolveFailed);
  expect(screen.queryByText('正在应用决定…')).toBeNull();
  expect(button.disabled).toBe(false);
  expect(header()).toBe('等你决定');
});

it.each(['other-permission', 'other-run', 'rejected'])('X7e: does not apply an unrelated/failed decision (%s)', variant => {
  const control = structuredClone(events[5]);
  if (variant === 'other-permission') control.payload.permission_item_id = 'another-permission';
  if (variant === 'other-run') control.scope = { level: 'run', session_id: session.id, thread_id: session.id,
    turn_id: 'turn', run_id: 'another-run' };
  if (variant === 'rejected') {
    control.lifecycle = 'rejected'; control.kind = 'control_input.rejected';
    control.payload_schema = 'hive.session.payload.control_input.rejected.v2';
  }
  const model = buildThreadTimeline({ messages: project([...events.slice(0, 5), control], 'reload'),
    activeSession: session, activeRunId: runId, activeRunStatus: 'suspended', isWaiting: true, isStreaming: false });
  expect(model.header.status).toBe('waiting');
  expect(model.cells.find(cell => cell.kind === 'active_run')?.timeline.status).toBe('blocked');
});

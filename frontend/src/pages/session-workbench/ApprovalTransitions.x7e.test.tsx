// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { chatApi } from '../../api/domains/chat';
import AgentChatSection from '../agent-detail/AgentChatSection';
import BusinessTaskPermissions from '../agent-detail/BusinessTaskPermissions';
import { hydrateSessionTranscriptEvents, projectSessionEventStoreToMessages } from '../agent-detail/sessionEventConsumer';
import type { ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { createSessionEventStore, reduceSessionEvent, type SessionEventV2 } from './sessionEventStore';
import { buildThreadTimeline } from './timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
  i18n: { language: 'zh' },
}) }));
vi.mock('../agent-detail/ToolAccounts', () => ({ default: () => null }));
vi.mock('../../api/domains/assistant', () => ({ assistantApi: { get: async () => ({ agent: null }) } }));
vi.mock('../../api/domains/ccParity', () => ({ ccParityApi: { getSessionWorkbench: async () => ({ runtime_tasks: [] }) } }));

const runId = '11111111-1111-4111-8111-111111111111';
const session = { id: 'session-x7e-r2', user_id: 'member', title: 'Plaza update' };
const request = { permission_request_id: 'permission-1', tool_name: 'plaza_create_post',
  arguments: { content: 'Team update' }, runtime_task_id: runId, session_id: session.id,
  requested_by_user_id: session.user_id, allow_session_allowed: true };
type Mode = 'live' | 'reload';
function event(sequence: number, kind: string, lifecycle: string, payload: Record<string, unknown> = {}, itemId = kind): SessionEventV2 {
  const base = { session_id: session.id, thread_id: session.id };
  const scope: SessionEventV2['scope'] = kind === 'human_input' ? { ...base, level: 'session' }
    : ['run', 'control_input'].includes(kind) ? { ...base, level: 'run', turn_id: 'turn', run_id: runId }
      : { ...base, level: 'round', turn_id: 'turn', run_id: runId, round_id: 'round' };
  const at = `2026-11-15T04:00:${String(sequence).padStart(2, '0')}Z`;
  return { schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'company', item_id: itemId, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`, scope,
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload, occurred_at: at, persisted_at: at };
}
// The run22 same-item sequence; identities, text and times are synthetic.
// Completion deliberately omits permission_request, as on the wire.
const prefix = [
  event(1, 'human_input', 'accepted', { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Publish.' }] }),
  event(2, 'run', 'queued', {}, runId),
  event(3, 'tool_call', 'started', { name: 'plaza_create_post', arguments: request.arguments }, 'call'),
  event(4, 'tool_permission', 'waiting', { permission_request: request }, request.permission_request_id),
  event(5, 'run', 'waiting', { reason_code: 'tool_permission_required', permission_item_id: request.permission_request_id }, runId),
  event(6, 'control_input', 'accepted', { kind: 'permission_response', permission_item_id: request.permission_request_id, decision: 'allow_once' }),
  event(7, 'control_input', 'applied', { kind: 'permission_response', permission_item_id: request.permission_request_id, decision: 'allow_once' }),
  event(8, 'tool_permission', 'completed', { decision: 'allow_once' }, request.permission_request_id),
];
function project(events: SessionEventV2[], mode: Mode) {
  return mode === 'reload'
    ? hydrateSessionTranscriptEvents(events as unknown as ChatTranscriptEventPayload[], 0).messages
    : projectSessionEventStoreToMessages(events.reduce(reduceSessionEvent, createSessionEventStore()));
}
function model(events: SessionEventV2[], mode: Mode, liveStatus = 'suspended', taskStatus?: string) {
  return buildThreadTimeline({ messages: project(events, mode), activeSession: session,
    activeRunId: runId.replaceAll('-', ''), activeRunStatus: liveStatus, isWaiting: true, isStreaming: false,
    sessionWorkbench: taskStatus ? { runtime_tasks: [{ id: runId, status: taskStatus }] } as never : undefined });
}
function assertState(result: ReturnType<typeof model>, header: string, block: string) {
  expect.soft(result.header.status).toBe(header);
  expect.soft(result.cells.find(cell => cell.kind === 'active_run')?.timeline.status).toBe(block);
}
let client: QueryClient | undefined;
afterEach(() => { cleanup(); client?.clear(); sessionStorage.clear(); vi.unstubAllGlobals(); });

it.each((['live', 'reload'] as const).flatMap(mode => ['waiting', 'completed'].map(permission => ({ mode, permission }))))(
  'X7e-r2: $mode does not reuse a tool decision for a later company approval ($permission permission)', ({ mode, permission }) => {
    const events = permission === 'waiting' ? prefix.slice(0, 6) : [...prefix];
    events.push(event(events.length + 1, 'run', 'completed', { reason_code: 'company_approval_required' }, runId));
    const run = project(events, mode).find(message => message.sessionItem?.kind === 'run')!.sessionItem!;
    expect.soft(run.payload.permission_item_id).toBeUndefined();
    assertState(model(events, mode), 'waiting', 'blocked');
  },
);
it.each(['live', 'reload'] as const)('X7e-r2: %s does not inherit an id on a second wait without one', mode => {
  const events = [...prefix, event(9, 'run', 'waiting', { reason_code: 'tool_permission_required' }, runId)];
  expect.soft(project(events, mode).find(message => message.sessionItem?.kind === 'run')!.sessionItem!.payload.permission_item_id).toBeUndefined();
  assertState(model(events, mode), 'waiting', 'blocked');
});
it.each(['live', 'reload'] as const)('X7e-r2: %s a second tool permission waits for its own control', mode => {
  const next = { ...request, permission_request_id: 'permission-2' };
  const events = [...prefix,
    event(9, 'tool_permission', 'waiting', { permission_request: next }, next.permission_request_id),
    event(10, 'run', 'waiting', { reason_code: 'tool_permission_required', permission_item_id: next.permission_request_id }, runId)];
  assertState(model(events, mode), 'waiting', 'blocked');
  events.push(event(11, 'control_input', 'accepted', { kind: 'permission_response', permission_item_id: next.permission_request_id }, 'control-2'));
  assertState(model(events, mode), 'running', 'running');
});
it.each(['live', 'reload'] as const)('X7e-r2: %s a colleague wait outranks an accepted but still-waiting permission', mode => {
  const events = [...prefix.slice(0, 6), event(7, 'run', 'waiting', { reason_code: 'delegation_waiting' }, runId)];
  assertState(model(events, mode, 'running'), 'waiting', 'blocked');
});
it.each((['live', 'reload'] as const).flatMap(mode => ['task', 'live'].flatMap(source =>
  ['failed', 'stopped', 'cancelled', 'needs_reconciliation'].map(status => ({ mode, source, status })))))(
  'X7e-r2: $mode keeps $source $status before its terminal transcript event', ({ mode, source, status }) => {
    const result = model(prefix, mode, source === 'live' ? status : 'suspended', source === 'task' ? status : undefined);
    const expected = ['stopped', 'cancelled'].includes(status) ? 'cancelled' : 'failed';
    assertState(result, expected, expected);
  },
);
it.each((['live', 'reload'] as const).flatMap(mode => ['suspended', 'waiting', 'awaiting_approval'].flatMap(status =>
  [true, false].map(reason => ({ mode, status, reason })))))(
  'X7e-r2: $mode queued does not regress to stale $status (reason=$reason)', ({ mode, status, reason }) => {
    const events = [...prefix, event(9, 'run', 'queued', reason ? { reason_code: 'session_permission_resolved' } : {}, runId)];
    assertState(model(events, mode, status), 'running', 'running');
  },
);
it.each(['live', 'reload'] as const)('X7e-r2: %s does not override a different live approval wait', mode => {
  assertState(model(prefix, mode, 'waiting_budget_approval'), 'waiting', 'blocked');
});

function chat(events: SessionEventV2[], resolve: ReturnType<typeof vi.fn>, connected = true, liveStatus = 'suspended') {
  const noop = vi.fn();
  const ref = React.createRef<any>();
  const props = {
    agent: { id: 'agent', name: 'Assistant' }, currentUser: { id: session.user_id }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [],
    chatMessages: project(events, 'reload'), chatMessagesSessionId: session.id, attachedFiles: [], wsConnected: connected,
    sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1, allUserFilter: '', chatInput: '',
    runtimeSummary: null, transportNotice: null, activeRunId: runId, activeRunStatus: liveStatus, runtimePhase: 'awaiting_approval',
    isWaiting: true, isStreaming: false, sessionOnly: true, historyContainerRef: ref, chatContainerRef: ref,
    chatEndRef: ref, fileInputRef: ref, chatInputRef: ref, uploadAbortRef: { current: null },
    onLoadAllSessions: noop, onCreateNewSession: noop, onSetAllUserFilter: noop, onSelectSession: noop,
    onDeleteSession: noop, onHistoryScroll: noop, onScrollHistoryToBottom: noop, onChatScroll: noop,
    onScrollToBottom: noop, onRemoveAttachedFile: noop, onHandleChatFile: noop, onSetChatInput: noop,
    onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: noop, onResolveSessionPermission: resolve,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  return <QueryClientProvider client={client!}><AgentChatSection {...props} /></QueryClientProvider>;
}
it.each(['completed', 'failed', 'cancelled', 'needs_reconciliation'].flatMap(status =>
  ['before', 'after'].map(http => ({ status, http }))))(
  'X7e-r2: clears applying when the run becomes $status with HTTP $http the terminal', async ({ status, http }) => {
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let finish!: () => void;
    const resolve = vi.fn(() => new Promise<void>(done => { finish = done; }));
    const waiting = [...prefix.slice(0, 5), event(6, 'tool_call', 'completed', { name: 'search_memory' }, 'previous-call')];
    const view = render(chat(waiting, resolve));
    fireEvent.click(screen.getByRole('button', { name: '本次允许' }));
    if (http === 'before') await act(async () => { finish(); });
    // A disconnected stream is not proof of completion. Recovery uses existing events.
    view.rerender(chat(waiting, resolve, false));
    expect(screen.getByText('正在应用决定…')).toBeTruthy();
    // An observed terminal status may arrive before the transcript terminal.
    view.rerender(chat(waiting, resolve, false, status));
    expect.soft(screen.queryByText('正在应用决定…')).toBeNull();
    expect.soft((screen.getByRole('button', { name: '本次允许' }) as HTMLButtonElement).disabled).toBe(false);
    const terminal = [...waiting];
    if (status === 'completed') terminal.push(event(terminal.length + 1, 'assistant_final', 'completed', { content: 'Finished.' }));
    terminal.push(event(terminal.length + 1, 'run', status, {}, runId));
    view.rerender(chat(terminal, resolve, false));
    expect(screen.queryByText('正在应用决定…')).toBeNull();
    expect((screen.getByRole('button', { name: '本次允许' }) as HTMLButtonElement).disabled).toBe(false);
    if (http === 'after') await act(async () => { finish(); });
    expect(screen.queryByText('正在应用决定…')).toBeNull();
  },
);
it('X7e-r2: a rejected control releases a successful HTTP decision on the same card', async () => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const resolve = vi.fn().mockResolvedValue({ status: 'resolved' });
  const view = render(chat(prefix.slice(0, 5), resolve));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '本次允许' })); });
  expect(screen.getByText('正在应用决定…')).toBeTruthy();
  view.rerender(chat([...prefix.slice(0, 5), event(6, 'control_input', 'rejected',
    { kind: 'permission_response', permission_item_id: request.permission_request_id })], resolve));
  expect(screen.queryByText('正在应用决定…')).toBeNull();
  expect((screen.getByRole('button', { name: '本次允许' }) as HTMLButtonElement).disabled).toBe(false);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '本次允许' })); });
  expect(screen.getByText('正在应用决定…')).toBeTruthy();
  view.rerender(chat([...prefix.slice(0, 5),
    event(6, 'control_input', 'rejected', { kind: 'permission_response', permission_item_id: request.permission_request_id }),
    event(7, 'control_input', 'rejected', { kind: 'permission_response', permission_item_id: request.permission_request_id }, 'control-2')], resolve));
  expect(screen.queryByText('正在应用决定…')).toBeNull();
  expect((screen.getByRole('button', { name: '本次允许' }) as HTMLButtonElement).disabled).toBe(false);
});
it.each(['conversation', 'business task'])('X7e-r2: an HTTP-200 rejection restores the %s card with existing failure text', async surface => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/resolve') ? { status: 'rejected' } : []), {
    status: 200, headers: { 'content-type': 'application/json' },
  })));
  const resolve = vi.fn(() => chatApi.resolveSessionPermission('agent', session.id, request.permission_request_id, { action: 'allow_once' }));
  if (surface === 'conversation') render(chat(prefix.slice(0, 5), resolve));
  else render(<BusinessTaskPermissions agentId="agent" task={{ id: 'task', agent_id: 'agent', status: 'doing',
    last_execution_status: 'awaiting_permission', active_runtime_task_id: runId, reflection_session_id: session.id } as never}
    permissions={[{ session_id: session.id, can_resolve: true, request }]} userId={session.user_id} onResolved={vi.fn().mockResolvedValue(undefined)} />);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '本次允许' })); });
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith('/permissions/permission-1/resolve'))).toHaveLength(1);
  expect(screen.getByRole('alert').textContent).toBe(zh.agent.chat.permission.resolveFailed);
  expect(screen.queryByText('正在应用决定…')).toBeNull();
  expect((screen.getByRole('button', { name: '本次允许' }) as HTMLButtonElement).disabled).toBe(false);
});

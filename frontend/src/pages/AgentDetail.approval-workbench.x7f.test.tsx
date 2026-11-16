// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../i18n/zh.json';
import { runId, session, request, run23Waiting, run23AfterApproval, run23Completed } from '../test/fixtures/x7fApproval';

const m = vi.hoisted(() => ({
  listSessions: vi.fn(), transcript: vi.fn(), resolve: vi.fn(), active: vi.fn(), summary: vi.fn(),
  workbench: vi.fn(), ledger: vi.fn(), transport: null as any, props: null as any,
  agent: { id: 'agent-x7f', name: 'Assistant', agent_type: 'cloud', status: 'running', access_level: 'use',
    action_capabilities: { can_use: true, can_manage: false, can_manage_permissions: false, can_operator_inspect: false } },
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
vi.mock('../stores', () => {
  const state = { token: 'test-token', user: { id: 'member-x7f', role: 'member' } };
  const store = Object.assign((selector: (value: typeof state) => unknown) => selector(state), { getState: () => state });
  return { useAuthStore: store };
});
vi.mock('react-router-dom', async original => ({
  ...await original<typeof import('react-router-dom')>(),
  useParams: () => ({ id: 'agent-x7f', sessionId: 'session-x7f' }),
  useLocation: () => ({ pathname: '/agents/agent-x7f/sessions/session-x7f', search: '', hash: '#chat', state: null }),
  useNavigate: () => vi.fn(),
}));
vi.mock('@tanstack/react-query', async original => {
  const actual = await original<typeof import('@tanstack/react-query')>();
  return { ...actual, useQuery: (options: any) => {
    if (String(options.queryKey[0]).startsWith('chat-')) return actual.useQuery(options);
    return { data: options.queryKey[0] === 'agent' ? m.agent : undefined, isLoading: false, error: null, refetch: vi.fn() };
  } };
});
vi.mock('../api/domains/chat', async original => {
  const actual = await original<typeof import('../api/domains/chat')>();
  return { ...actual, chatApi: { ...actual.chatApi, listSessions: m.listSessions, getSessionTranscript: m.transcript,
    getSessionMessages: vi.fn().mockResolvedValue([]), getSessionLineage: vi.fn().mockResolvedValue([]),
    getSessionIndex: vi.fn().mockResolvedValue({ checkpoints: [] }), listSessionDecisions: vi.fn().mockResolvedValue([]),
    getActiveSessionRun: m.active, getRuntimeSummary: m.summary, resolveSessionPermission: m.resolve } };
});
vi.mock('../api/domains/ccParity', async original => {
  const actual = await original<typeof import('../api/domains/ccParity')>();
  return { ...actual, ccParityApi: { ...actual.ccParityApi, getSessionWorkbench: m.workbench,
    getSessionContextUsage: vi.fn().mockResolvedValue(null) } };
});
vi.mock('../api/domains/autonomy', async original => {
  const actual = await original<typeof import('../api/domains/autonomy')>();
  return { ...actual, autonomyApi: { ...actual.autonomyApi, getSessionWorkLedger: m.ledger } };
});
vi.mock('./agent-detail/useSessionTransportController', () => ({ useSessionTransportController: (options: any) => {
  m.transport = options;
  return { wsConnected: true, transportPhase: 'connected', transportReconnectAttempt: 0,
    closeSessionSocket: vi.fn(), getSessionSocket: vi.fn(() => null), reconnectActiveTransport: vi.fn(),
    resetActiveTransportState: vi.fn(), syncActiveSocketState: vi.fn() };
} }));
vi.mock('./agent-detail/AgentChatSection', async original => {
  const { default: Actual } = await original<typeof import('./agent-detail/AgentChatSection')>();
  return { default: (props: React.ComponentProps<typeof Actual>) => { m.props = props; return <Actual {...props} />; } };
});
import AgentDetail from './AgentDetail';

beforeAll(async () => {
  await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, initImmediate: false });
  await import('./agent-detail/AgentChatSection');
});
let client: QueryClient;
afterEach(() => { cleanup(); client?.clear(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); sessionStorage.clear(); });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function flush() { await act(async () => { await vi.advanceTimersByTimeAsync(20); }); }
function receive(events: unknown[]) {
  for (const data of events) m.transport.callbacks.onMessage({ data, session, agentId: session.agent_id,
    sessionId: session.id, key: `${session.agent_id}:${session.id}`, isActiveRuntime: true,
    closeSessionSocket: vi.fn(), failAuthentication: vi.fn() });
}
const header = () => screen.getByTestId('session-workbench-header').querySelector('.swb-header-status')?.textContent;
const duration = () => document.querySelector('.run-disclosure-duration')?.textContent;
// Backend: session_control_plane._user_runtime_task and _timeline_payload.
// The default workbench has no transcript events. The runtime-task timestamps
// come from the existing task record, independently of transcript hydration.
function workbench(status: string, startedAt = '2026-11-16T12:24:11.394376Z') {
  return { schema: 'hive.ccplus.session_workbench.v1', audience: 'user', agent_id: session.agent_id, session,
    timeline: { schema: 'hive.ccplus.session_timeline.v1', truth_source: 'session_event_v2', included: false,
      event_count: 50, window_limit: 50, truncated: true, events: [] },
    active_run: { run_id: runId.replaceAll('-', ''), status, created_at: '2026-11-16T12:24:09.422Z',
      started_at: startedAt, completed_at: null },
    runtime_tasks: [{ id: runId, task_type: 'web_chat_turn', status, user_status: status === 'suspended' ? 'waiting' : 'running',
      created_at: '2026-11-16T12:24:09.422Z', started_at: startedAt }],
    goals: [], teams: [], approvals: [], controls: { can_stop_active_run: true },
  };
}

it('X7f: approval refetch keeps net working time when transcript precedes the workbench response', async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async () => new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } })));
  // Exercise the message store's existing timeout fallback under the fake
  // clock; jsdom's RAF implementation captures its own real timer.
  vi.stubGlobal('requestAnimationFrame', undefined);
  vi.stubGlobal('cancelAnimationFrame', undefined);
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-16T12:27:00.880Z'));
  m.listSessions.mockResolvedValue([session]);
  m.transcript.mockResolvedValue([]);
  m.summary.mockResolvedValue(null);
  m.ledger.mockResolvedValue({ todo_items: [] });
  m.active.mockResolvedValue(workbench('suspended').active_run);
  m.workbench.mockResolvedValue(workbench('suspended'));
  const post = deferred<any>();
  const transcript = deferred<any>();
  const earlyWorkbench = deferred<any>();
  const lateWorkbench = deferred<any>();
  const active = deferred<any>();
  const summary = deferred<any>();
  const ledger = deferred<any>();
  m.resolve.mockReturnValue(post.promise);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  render(<MemoryRouter><QueryClientProvider client={client}><AgentDetail /></QueryClientProvider></MemoryRouter>);
  await flush();
  await flush();
  expect(m.props?.activeSession?.id).toBe(session.id);
  await act(async () => { receive(run23Waiting); });
  await flush();
  expect(header()).toBe('等你决定');
  expect(duration()).toBe('2 分 54 秒');

  vi.setSystemTime(new Date('2026-11-16T12:30:13.685Z'));
  fireEvent.click(screen.getByRole('button', { name: '本次允许' }));
  expect(m.resolve).toHaveBeenCalledExactlyOnceWith(session.agent_id, session.id, request.permission_request_id, { action: 'allow_once' });
  vi.setSystemTime(new Date('2026-11-16T12:30:17.264Z'));
  await act(async () => { receive([run23AfterApproval[run23Waiting.length]]); });
  await flush();
  expect.soft(header()).toBe('运行中');
  const continuation = run23AfterApproval.slice(run23Waiting.length);
  vi.setSystemTime(new Date('2026-11-16T12:30:17.844Z'));
  await act(async () => { receive(continuation.slice(1, 4)); });
  await flush();
  vi.setSystemTime(new Date('2026-11-16T12:30:23.898Z'));
  await act(async () => { receive(continuation.slice(4, 5)); });
  await flush();
  // The tool-result refresh is the earlier workbench request. Its response
  // must not replace the newer post-resolve refetch in the query cache.
  m.workbench.mockReturnValue(earlyWorkbench.promise);
  vi.setSystemTime(new Date('2026-11-16T12:30:25.095Z'));
  await act(async () => { receive(continuation.slice(5, 7)); });
  await flush();

  m.transcript.mockReturnValue(transcript.promise);
  m.workbench.mockReturnValue(lateWorkbench.promise);
  m.active.mockReturnValue(active.promise);
  m.summary.mockReturnValue(summary.promise);
  m.ledger.mockReturnValue(ledger.promise);
  const transcriptCalls = m.transcript.mock.calls.length;
  const workbenchCalls = m.workbench.mock.calls.length;
  const activeCalls = m.active.mock.calls.length;
  const summaryCalls = m.summary.mock.calls.length;
  const ledgerCalls = m.ledger.mock.calls.length;
  vi.setSystemTime(new Date('2026-11-16T12:30:25.596Z'));
  await act(async () => { post.resolve({ schema: 'hive.session_permission_resolution.v2', status: 'resolved',
    permission_request_id: request.permission_request_id, run_id: runId, run_status: 'running' }); });
  await flush();
  expect(m.transcript.mock.calls.length).toBeGreaterThan(transcriptCalls);
  expect(m.workbench.mock.calls.length).toBeGreaterThan(workbenchCalls);
  expect(m.active.mock.calls.length).toBeGreaterThan(activeCalls);
  expect(m.summary.mock.calls.length).toBeGreaterThan(summaryCalls);
  expect(m.ledger.mock.calls.length).toBeGreaterThan(ledgerCalls);

  vi.setSystemTime(new Date('2026-11-16T12:30:25.791Z'));
  await act(async () => { earlyWorkbench.resolve(workbench('suspended')); });
  await flush();
  vi.setSystemTime(new Date('2026-11-16T12:30:25.839Z'));
  await act(async () => { active.resolve(workbench('running').active_run); });
  await flush();
  vi.setSystemTime(new Date('2026-11-16T12:30:25.912Z'));
  await act(async () => { ledger.resolve({ todo_items: [] }); });
  await flush();
  vi.setSystemTime(new Date('2026-11-16T12:30:26.070Z'));
  await act(async () => { summary.resolve(null); });
  await flush();

  vi.setSystemTime(new Date('2026-11-16T12:30:26.842Z'));
  await act(async () => { transcript.resolve(run23AfterApproval.filter(e => 'schema' in e)); });
  await flush();
  expect.soft(header()).toBe('运行中');
  expect.soft(duration()).toBe('3 分 3 秒');
  vi.setSystemTime(new Date('2026-11-16T12:30:29.427Z'));
  await act(async () => { lateWorkbench.resolve(workbench('running')); });
  await flush();
  expect.soft(header()).toBe('运行中');
  expect.soft(duration()).toBe('3 分 5 秒');
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect.soft(duration()).toBe('3 分 6 秒');
  await act(async () => { await vi.advanceTimersByTimeAsync(44_000); });
  expect.soft(duration()).toBe('3 分 50 秒');
  await act(async () => { receive(run23Completed.slice(run23AfterApproval.length)); });
  await flush();
  expect(duration()).toBe('3 分 47 秒');
  cleanup();
  client.clear();
  m.transcript.mockResolvedValue(run23Completed.filter(e => 'schema' in e));
  m.active.mockResolvedValue(null);
  m.workbench.mockResolvedValue({ ...workbench('completed'), active_run: null,
    runtime_tasks: [{ ...workbench('completed').runtime_tasks[0], completed_at: '2026-11-16T12:31:15.225009Z' }] });
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  render(<MemoryRouter><QueryClientProvider client={client}><AgentDetail /></QueryClientProvider></MemoryRouter>);
  await flush();
  await flush();
  expect(header()).toBe('完成');
  expect(duration()).toBe('3 分 47 秒');
});

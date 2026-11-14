// @vitest-environment jsdom
// Run 20, steps 5/6/10: production native/compatibility shapes, sanitized.
// In particular: no queued run item on the continuation; the delegated run
// item precedes its legacy human_input; start notices can name the parent.
import { cleanup, fireEvent, render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import { buildThreadTimeline } from '../session-workbench/timelineModel';
import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
  i18n: { language: 'zh' },
}) }));
afterEach(() => { cleanup(); window.localStorage.clear(); });
const session = 'session-x5a';
const child = 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb';
const parent = 'cccccccc-1111-2222-3333-dddddddddddd';
type Event = Record<string, any>;
function event(sequence: number, kind: string, lifecycle: string, id: string, time: string, payload: Event, run = parent): Event {
  return { schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant', scope: kind === 'human_input' ? { level: 'session', session_id: session, thread_id: session }
      : { level: kind.startsWith('tool_') || kind.startsWith('assistant_') ? 'round' : 'run',
        session_id: session, thread_id: session, turn_id: `turn-${run}`, run_id: run,
        ...(kind.startsWith('tool_') || kind.startsWith('assistant_') ? { round_id: `${run}:round:1` } : {}) },
    item_id: id, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`, actor: { type: 'runtime' },
    visibility: { audience: 'direct_user' }, occurred_at: time, persisted_at: time, payload };
}
function compat(sequence: number, type: string, time: string, content: string, metadata: Event): Event {
  return { schema: 'hive.session_event_compatibility', schema_version: 1, compatibility_status: 'needs_reconciliation',
    reason: 'unmapped_legacy_kind', event_id: `event-${sequence}`, sequence, legacy_kind: 'legacy_unknown',
    legacy_lifecycle: 'legacy_unknown', legacy_event_type: type, legacy_item_type: 'event', legacy_item_status: 'succeeded',
    occurred_at: time, payload: { content, metadata, parts: [{ type: 'event', event_type: type,
      text: content, title: 'Action Started', ...metadata }] } };
}
function input(sequence = 1): Event {
  return event(sequence, 'human_input', 'accepted', `input-${sequence}`, '2026-11-14T04:22:46.482830Z', { content: '请同事整理建议' });
}
function tool(sequence: number, kind: string, state: string, taskId = child.replaceAll('-', '')): Event[] {
  return [
    { ...event(sequence, 'tool_call', 'started', `call-${sequence}`, '2026-11-14T04:25:40Z', {
      tool_name: kind === 'delegation' ? 'delegate_to_agent' : 'check_async_task', args_hash: 'sanitized' }), invocation_id: `inv-${sequence}` },
    { ...event(sequence + 1, 'tool_result', 'completed', `result-${sequence}`, '2026-11-14T04:25:41Z', {
      tool_name: kind === 'delegation' ? 'delegate_to_agent' : 'check_async_task', outcome: 'success', content: '',
      member_summary: { kind, state, task_id: taskId } }), invocation_id: `inv-${sequence}`, parent_item_id: `call-${sequence}` },
  ];
}
function both(events: Event[]): AgentChatMessage[][] {
  let live: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => null, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, messages) => messages, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { live = update(live); },
    setChatMessagesAfterQueued: (_session, update) => { live = update(live); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const e of events) applyTranscriptToSessionRuntime(deps, 'agent', 'session', e as ChatTranscriptEventPayload, true);
  return [live, hydrateSessionTranscriptEvents(events as ChatTranscriptEventPayload[], 0).messages];
}
function mount(messages: AgentChatMessage[], extra: Event = {}) {
  const model = buildThreadTimeline({ messages, activeSession: { id: session }, isWaiting: false, isStreaming: false,
    activeRunStatus: null, ...extra });
  const query = new QueryClient();
  query.setQueryData(['agents', 'tenant'], [{ id: 'colleague' }]);
  localStorage.setItem('current_tenant_id', 'tenant');
  const view = render(<MemoryRouter><QueryClientProvider client={query}>{model.cells.map(cell => cell.kind === 'active_run'
    ? <RunDisclosureBlock key={cell.id} timeline={cell.timeline} viewerOwnsSession />
    : cell.kind === 'user_turn' ? <p key={cell.id} data-testid="task-message">{cell.message.content}</p> : null)}</QueryClientProvider></MemoryRouter>);
  for (let pass = 0; pass < 3; pass++) view.container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]').forEach(b => fireEvent.click(b));
  return { ...view, model };
}

it('X5a-1: a first-task started notice names the child and keeps the parent header running between continuations', () => {
  const first = { ...compat(4, 'runtime_action_started', '2026-11-14T05:12:13Z', '已委派给 新同事，后台执行中。', {
    action_kind: 'a2a_delegation', status: 'running', runtime_task_id: child, child_session_id: 'first-task-session',
    target_agent_name: '新同事' }), run_id: child };
  const events = [input(),
    event(2, 'assistant_commentary', 'completed', 'created', '2026-11-14T05:09:33Z', { content: '已准备创建方案' }),
    event(3, 'run', 'completed', parent, '2026-11-14T05:09:33.931343Z', {}), first];
  const continuation = 'eeeeeeee-1111-2222-3333-ffffffffffff';
  for (const state of [events, [...events,
    event(5, 'assistant_commentary', 'completed', 'waiting-answer', '2026-11-14T05:12:45Z',
      { content: '新同事正在执行首个任务' }, continuation),
    event(6, 'run', 'completed', continuation, '2026-11-14T05:12:46.683953Z', {}),
  ]]) {
    for (const messages of both(state)) {
      const { container, model, unmount } = mount(messages);
      expect(model.header.status).toBe('running');
      // X5a r2: background colleague work needs no member action.
      expect(container.querySelector('[role="img"][aria-label="等待同事"] .tabler-icon-clock')).toBeTruthy();
      unmount();
    }
    const finished = [...state, { ...compat(state.length + 1, 'child_session', '2026-11-14T05:12:49.852Z', '同事已完成这项工作。', {
      reason: 'delegation_terminal', status: 'completed', runtime_task_id: child,
      child_session_id: 'first-task-session', to_agent: 'colleague' }), run_id: child }];
    for (const messages of both(finished)) {
      const { container, model, unmount } = mount(messages);
      expect(model.header.status).toBe('complete');
      expect(container.querySelector('[role="img"][aria-label="等待同事"]')).toBeNull();
      unmount();
    }
  }
});

it('X5a-3: cross-block hex/UUID task identities keep one stopped sentence and the colleague link, live/reload', () => {
  const events = [input(), ...tool(2, 'delegation', 'in_progress'),
    event(4, 'run', 'completed', parent, '2026-11-14T04:25:53Z', {}),
    // Run20 seq127 precedes the linked child_session seq128: neither
    // this live prefix nor the final reload may repeat the stop sentence.
    compat(5, 'runtime_action_failed', '2026-11-14T04:42:31Z', '已停止，同事没有完成这项工作。', {
      action_kind: 'a2a_delegation', status: 'cancelled', runtime_task_id: child, child_session_id: 'child-session' }),
    compat(6, 'child_session', '2026-11-14T04:42:31Z', '已停止，同事没有完成这项工作。', {
      status: 'cancelled', reason: 'delegation_terminal', runtime_task_id: child, child_session_id: 'child-session', to_agent: 'colleague' }),
  ];
  for (const frames of [events.slice(0, -1), events]) {
    for (const messages of both(frames)) {
      const { container, unmount } = mount(messages);
      expect(container.textContent?.split('已停止，同事没有完成这项工作。').length! - 1).toBe(1);
      if (frames.length === events.length) expect(container.querySelector('a[href="/agents/colleague/sessions/child-session"]')).toBeTruthy();
      const rows = [...container.querySelectorAll('.run-step, .run-tool-history-item')];
      expect(rows).toHaveLength(2);
      expect(rows[0].textContent).toContain('同事任务已结束');
      expect(rows.every(r => r.getAttribute('data-status') === 'cancelled' && r.querySelector('[role="img"][aria-label="已停止"]'))).toBe(true);
      unmount();
    }
  }
});

it('X5a-r2-2: a queued run keeps T2−T0 despite later claim times on running, terminal and workbench', () => {
  const start = '2026-11-14T04:35:00Z';
  const events = [{ ...input(), occurred_at: start, persisted_at: start },
    event(2, 'run', 'queued', parent, start, {}),
    event(3, 'run', 'running', parent, '2026-11-14T04:37:00Z', { started_at: '2026-11-14T04:37:00Z' }),
    event(4, 'assistant_commentary', 'completed', 'queued-work', '2026-11-14T04:37:05Z', { content: '整理结果' }),
    event(5, 'run', 'completed', parent, '2026-11-14T04:37:10Z', { started_at: '2026-11-14T04:37:00Z' }),
  ];
  for (const messages of both(events.slice(0, -1))) {
    const { model, unmount } = mount(messages, { sessionWorkbench: { runtime_tasks: [{ id: parent, status: 'running',
      started_at: '2026-11-14T04:37:00Z' }] } });
    const runs = model.cells.filter(c => c.kind === 'active_run');
    expect(runs).toHaveLength(1);
    expect(runs[0].timeline.status).toBe('running');
    expect(Date.parse(runs[0].timeline.startedAt!)).toBe(Date.parse(start));
    unmount();
  }
  for (const messages of both(events)) {
    const { model, unmount } = mount(messages, { sessionWorkbench: { runtime_tasks: [{ id: parent, status: 'completed',
      started_at: '2026-11-14T04:37:00Z', completed_at: '2026-11-14T04:37:10Z' }] } });
    const runs = model.cells.filter(c => c.kind === 'active_run');
    expect(runs).toHaveLength(1);
    expect(runs[0].timeline.status).toBe('done');
    expect(runs[0].timeline.durationMs).toBe(130000);
    unmount();
  }
});

it('X5a-4: a continuation first seen as cancelling uses its server run span, not the control timestamp', () => {
  // Run20 step5 seq340/342/343: no queued/running event for this run.
  const events = [input(),
    event(2, 'assistant_reasoning_private', 'delta', 'reasoning', '2026-11-14T04:35:37.798106Z',
      { phase: 'reasoning_private', block_index: 0, provider_request_id: 'sanitized-provider-request' }),
    event(3, 'run', 'cancelling', parent, '2026-11-14T04:38:21.510771Z', { control_id: 'stop' }),
    event(4, 'run', 'cancelled', parent, '2026-11-14T04:38:22.807647Z', { terminal_source: 'tool_card_finalizer' }),
    event(5, 'run', 'cancelled', parent, '2026-11-14T04:38:22.903661Z', { reason_code: 'runtime_cancelled' }),
  ];
  // Exact run20 /runs/active start. New terminal writes carry it themselves;
  // old events recover the same server span from the existing workbench.
  const start = '2026-11-14T04:35:22.261668Z';
  for (const legacy of [true, false]) {
    const frames = structuredClone(events);
    if (!legacy) frames[3].payload.started_at = start;
    for (const messages of both(frames)) {
      const { model, unmount } = mount(messages, legacy ? { sessionWorkbench: { runtime_tasks: [{ id: parent, status: 'killed',
        started_at: start, completed_at: '2026-11-14T04:38:22.807647Z' }] } } : {});
      const runs = model.cells.filter(c => c.kind === 'active_run');
      expect(runs).toHaveLength(1);
      expect(runs[0].timeline.status).toBe('cancelled');
      expect(runs[0].timeline.durationMs).toBe(180546);
      unmount();
    }
  }
});

const stoppedChild = () => [
  event(1, 'run', 'running', child, '2026-11-14T04:42:25.689211Z', { task_type: 'delegation', started_at: '2026-11-14T04:42:25.689211Z' }, child),
  event(2, 'human_input', 'accepted', 'task-message', '2026-11-14T04:42:30.502793Z', {
    content: '整理季度复盘', legacy: true, legacy_run_id: child,
    metadata: { delegation: true, runtime_task_id: child.replaceAll('-', '') } }, child),
  event(3, 'run', 'cancelled', child, '2026-11-14T04:42:31.407305Z', { task_type: 'delegation',
    started_at: '2026-11-14T04:42:25.689211Z', completed_at: '2026-11-14T04:42:31.407305Z', failure_code: 'task_killed' }, child),
];
function assertStoppedChild(events: Event[]) {
  for (const messages of both(events)) {
    const { model, container, unmount } = mount(messages);
    expect(model.header.status).toBe('cancelled');
    const runs = model.cells.filter(c => c.kind === 'active_run');
    expect(runs).toHaveLength(1);
    expect(runs[0].timeline.status).toBe('cancelled');
    expect(model.cells.findIndex(c => c.kind === 'user_turn')).toBeLessThan(model.cells.findIndex(c => c.kind === 'active_run'));
    expect(container.querySelectorAll('[data-testid="run-disclosure-block"][data-status="running"]')).toHaveLength(0);
    unmount();
  }
}
it('X5a-5: a terminal colleague run before its legacy task message does not create a second running block', () => assertStoppedChild(stoppedChild()));
it('X5a-r2-6: V2 target_run_id orders the stopped block after its task', () => {
  const events = stoppedChild();
  events[1].payload = { content: '整理季度复盘', target_run_id: child };
  assertStoppedChild(events);
});
// The backend integration exports actual persisted events through the member serializer.
it.skipIf(!process.env.X5A_CHILD_EVENTS)('X5a-5 integration: real PostgreSQL stop events render stopped after their task', () => {
  assertStoppedChild(JSON.parse(readFileSync(process.env.X5A_CHILD_EVENTS!, 'utf8')));
});

it('X5a-6: colleague-check and background rows use the accessible waiting marker while the parent is done', () => {
  const events = [input(), ...tool(2, 'colleague_result', 'in_progress'),
    compat(4, 'runtime_action_started', '2026-11-14T04:25:41.674198Z', '已委派给 同事，后台执行中。', {
      action_kind: 'a2a_delegation', status: 'running', runtime_task_id: parent, child_session_id: 'child-session', target_agent_name: '同事' }),
    event(5, 'run', 'completed', parent, '2026-11-14T04:25:53Z', {}),
  ];
  for (const messages of both(events)) {
    const { container, model, unmount } = mount(messages);
    const rows = [...container.querySelectorAll('.run-step, .run-tool-history-item')];
    expect(rows).toHaveLength(2);
    // X5a r2: keep blocked, but distinguish passive waiting from an approval.
    expect(rows.every(r => r.getAttribute('data-status') === 'blocked' && r.querySelector('[role="img"][aria-label="等待同事"] .tabler-icon-clock'))).toBe(true);
    expect(container.querySelector('[role="img"][aria-label="等待处理"]')).toBeNull();
    expect(container.querySelector('.tabler-icon-loader-2')).toBeNull();
    expect(model.header.status).not.toBe('complete');
    unmount();
  }
});

it('X5a-r2-3: a refused runtime result read definitely did not read; unknown stays unconfirmed', () => {
  for (const state of ['not_done', 'unknown']) {
    const events = [input(), ...tool(2, 'runtime_result', state)];
    events[1].payload.tool_name = events[2].payload.tool_name = 'read_runtime_result';
    for (const messages of both(events)) {
      const { container, unmount } = mount(messages);
      if (state === 'not_done') {
        expect(container.textContent).toContain('这次没读到结果');
        expect(container.textContent).not.toContain('结果尚未确认');
      } else expect(container.textContent).toContain('结果尚未确认');
      unmount();
    }
  }
});

it.skipIf(!process.env.X5A_FIRST_TASK_EVENTS)('X5a-r2-1 integration: actual first-task recovery ends the parent header', () => {
  const events = JSON.parse(readFileSync(process.env.X5A_FIRST_TASK_EVENTS!, 'utf8'));
  for (const messages of both(events)) {
    const { container, model, unmount } = mount(messages);
    expect(model.header.status).toBe('complete');
    expect(messages.filter(m => m.eventType === 'runtime_action_started')).toHaveLength(0);
    expect(container.querySelectorAll('[role="img"][aria-label="等待同事"], [role="img"][aria-label="等待处理"]')).toHaveLength(0);
    unmount();
  }
});

it('X5a-2: a hire result is a task-result read, not a colleague-result read', () => {
  const events = [input(), ...tool(2, 'runtime_result', 'done')];
  events[1].payload.tool_name = events[2].payload.tool_name = 'read_runtime_result';
  for (const messages of both(events)) {
    const { container, unmount } = mount(messages);
    expect(container.textContent).toContain('已返回结果');
    expect(container.textContent).not.toContain('读取了同事的结果');
    unmount();
  }
});

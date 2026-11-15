// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import type { AgentChatMessage, ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { composeMixedPlaneSessionMessages, createCompatibilityMessageTimeline, hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import type { SessionWorkbench } from '../../api/domains/ccParity';
import { buildThreadTimeline, type ThreadTimelineModel } from './timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
  i18n: { language: 'zh' },
}) }));
afterEach(() => { cleanup(); vi.useRealTimers(); window.sessionStorage.clear(); });

const at = (time: string) => `2026-11-15T${time}Z`;
const sourceSequence = (event: ChatTranscriptEventPayload) =>
  Number((event as { event_id: string }).event_id.split('-')[1]);
// Selected run-22 step-10 api-evidence envelopes, with unrelated bookkeeping
// removed and delivery sequences made contiguous. Source sequence numbers stay
// in event ids. Identities and prose are anonymized; args_hash is not args.
function fixture() {
  const events: ChatTranscriptEventPayload[] = [];
  const native = (source: number, kind: string, lifecycle: string, time: string, runId: string | null,
    payload: Record<string, unknown>, itemId = `item-${source}`) => {
    const round = ['tool_call', 'tool_result', 'assistant_text', 'assistant_final'].includes(kind);
    events.push({ schema: 'hive.session_event', schema_version: 2, event_id: `source-${source}`,
      sequence: events.length + 1, tenant_id: 'tenant', item_id: itemId, item_kind: kind,
      kind: `${kind}.${lifecycle}`, lifecycle, payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
      scope: runId ? { level: round ? 'round' : 'run', session_id: 'session', thread_id: 'session',
        turn_id: `turn-${runId}`, run_id: runId, ...(round ? { round_id: `round-${runId}` } : {}) }
        : { level: 'session', session_id: 'session', thread_id: 'session' },
      actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
      occurred_at: at(time), persisted_at: at(time),
    } as unknown as ChatTranscriptEventPayload);
  };
  const compat = (source: number, type: string, time: string, metadata: Record<string, unknown>, content = '') => {
    const part = { type: 'event', event_type: type, ...metadata };
    events.push({ schema: 'hive.session_event_compatibility', schema_version: 1,
      compatibility_status: 'needs_reconciliation', reason: 'unmapped_legacy_kind',
      legacy_kind: 'legacy_unknown', legacy_lifecycle: 'legacy_unknown', legacy_event_type: type,
      legacy_item_type: type === 'agent_task_notification' ? 'subagent_activity' : 'event',
      legacy_item_status: 'succeeded', event_id: `source-${source}`, sequence: events.length + 1,
      session_id: 'session', tenant_id: 'tenant', occurred_at: at(time),
      payload: { content, parts: [part], metadata: { role: 'system', event_type: type, ...metadata } },
    } as unknown as ChatTranscriptEventPayload);
  };
  const started = (source: number, time: string, name: string, id: string) =>
    native(source, 'tool_call', 'started', time, 'continuation',
      { tool_name: name, invocation_id: id, args_hash: 'a'.repeat(64) }, id);
  const returned = (source: number, time: string, name: string, id: string, summary: Record<string, unknown>) => {
    native(source, 'tool_call', 'completed', time, 'continuation', { invocation_id: id, outcome: 'success' }, id);
    native(source + 1, 'tool_result', 'completed', time, 'continuation',
      { tool_name: name, invocation_id: id, outcome: 'success', content: '', parts: [], member_summary: summary });
  };
  native(1, 'human_input', 'accepted', '03:14:16.914', null,
    { intent: 'start_turn', content_parts: [{ type: 'text', text: '创建同事并让它介绍自己。' }] });
  native(10, 'run', 'queued', '03:14:18.644', 'parent', { run_id: 'parent' }, 'parent');
  native(70, 'tool_call', 'started', '03:14:43.978', 'parent',
    { tool_name: 'create_employee_from_request', invocation_id: 'create', args_hash: 'b'.repeat(64) }, 'create');
  native(72, 'tool_call', 'completed', '03:14:45.870', 'parent', { invocation_id: 'create', outcome: 'success' }, 'create');
  native(73, 'tool_result', 'completed', '03:14:45.870', 'parent',
    { tool_name: 'create_employee_from_request', invocation_id: 'create', outcome: 'success', content: '', parts: [] });
  native(90, 'assistant_text', 'snapshot', '03:14:53.078', 'parent',
    { phase: 'unknown', content: '已安排创建。', block_index: 0 }, 'parent-text');
  native(91, 'assistant_text', 'completed', '03:14:53.078', 'parent',
    { phase: 'unknown', content: '', block_index: 0 }, 'parent-text');
  native(99, 'assistant_final', 'completed', '03:14:55.019', 'parent',
    { parts: [], zero_copy: true, source_blocks: [{ item_id: 'parent-text', block_index: 0, content_hash: 'parent-hash' }] });
  native(100, 'run', 'completed', '03:14:55.019', 'parent', { started_at: at('03:14:18.731') }, 'parent');
  compat(103, 'runtime_action_started', '03:15:12.752', { status: 'running', action_kind: 'a2a_delegation',
    runtime_task_id: 'colleague', child_session_id: 'child', notification_source: 'a2a' }, '后台执行中。');
  compat(104, 'runtime_action_completed', '03:15:14.924', { status: 'completed', action_kind: 'hr_provisioning',
    task_type: 'hr_provisioning', runtime_task_id: 'hr', task_id: 'hr', notification_source: 'employee_creation' }, '已完成');
  compat(105, 'agent_task_notification', '03:15:14.924', { status: 'ready', item_count: 1,
    task_type: 'runtime_result_integration', notification_source: 'runtime_result_integration' });
  compat(107, 'memory_context_degraded', '03:15:20.646', { status: 'degraded', runtime_task_id: 'continuation' });
  started(126, '03:15:35.052', 'read_runtime_result', 'read-1');
  started(127, '03:15:35.244', 'check_async_task', 'check-1');
  compat(130, 'runtime_action_completed', '03:15:39.816', { status: 'completed', action_kind: 'a2a_delegation',
    runtime_task_id: 'colleague', task_id: 'colleague', child_session_id: 'child', reason: 'delegation_completed' }, '同事已完成。');
  compat(131, 'child_session', '03:15:39.816', { status: 'completed', runtime_task_id: 'colleague',
    child_session_id: 'child', reason: 'delegation_completed' }, '我可以整理销售数据。');
  returned(132, '03:15:41.056', 'read_runtime_result', 'read-1', { kind: 'runtime_result', state: 'done' });
  returned(134, '03:15:41.579', 'check_async_task', 'check-1',
    { kind: 'colleague_result', state: 'in_progress', status: 'completed', task_id: 'colleague' });
  compat(136, 'agent_task_notification', '03:15:41.755', { status: 'ready', item_count: 1,
    task_type: 'runtime_result_integration', notification_source: 'runtime_result_integration' });
  started(168, '03:15:52.908', 'check_async_task', 'check-2');
  returned(170, '03:15:56.908', 'check_async_task', 'check-2', { kind: 'colleague_result', state: 'done', status: 'completed' });
  started(190, '03:16:06.446', 'read_runtime_result', 'read-2');
  returned(192, '03:16:13.516', 'read_runtime_result', 'read-2', { kind: 'colleague_result', state: 'done', status: 'ok' });
  native(216, 'assistant_text', 'snapshot', '03:16:22.520', 'continuation',
    { phase: 'unknown', content: '同事的介绍已返回。', block_index: 0 }, 'continuation-text');
  native(217, 'assistant_text', 'completed', '03:16:22.520', 'continuation',
    { phase: 'unknown', content: '', block_index: 0 }, 'continuation-text');
  native(225, 'assistant_final', 'completed', '03:16:25.185', 'continuation',
    { parts: [], zero_copy: true, source_blocks: [{ item_id: 'continuation-text', block_index: 0, content_hash: 'continuation-hash' }] });
  native(226, 'run', 'completed', '03:16:25.185', 'continuation', { started_at: at('03:15:15.622') }, 'continuation');
  return events;
}

function projectBoth(events: ChatTranscriptEventPayload[]): AgentChatMessage[][] {
  let live: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => 'continuation', markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, messages) => messages, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { live = update(live); },
    setChatMessagesAfterQueued: (_session, update) => { live = update(live); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const event of events) applyTranscriptToSessionRuntime(deps, 'agent', 'session', event, true);
  return [live, hydrateSessionTranscriptEvents(events, 0).messages];
}
function models(source: number, live = true) {
  const events = fixture().filter(event => sourceSequence(event) <= source);
  return projectBoth(events).map(messages => buildThreadTimeline({ messages, activeSession: { id: 'session' },
    isWaiting: live, isStreaming: false, activeRunId: live ? 'continuation' : null,
    activeRunStatus: live ? 'running' : null, runtimePhase: live ? 'tool_running' : 'idle',
    // The task read can lag a completion event. Its old running value must
    // not restart the already finished HR action (run-22's 27 -> 53 seconds).
    sessionWorkbench: { runtime_tasks: [{ id: 'hr', status: 'running', started_at: at('03:14:49.018') }] } as unknown as SessionWorkbench,
  }));
}
const runs = (model: ThreadTimelineModel) => model.cells.filter(cell => cell.kind === 'active_run');

it.each([[127, ['read-1', 'check-1']], [168, ['check-2']], [190, ['read-2']]] as const)(
  'keeps the continuation calls running at source sequence %i, live and after hydration', (sequence, ids) => {
    for (const model of models(sequence)) {
      const run = runs(model).find(cell => cell.runId === 'continuation')!;
      for (const id of ids) expect(run.timeline.steps.find(step => step.id === id)?.status).toBe('running');
      expect(runs(model).filter(cell => cell.runId === 'continuation')).toHaveLength(1);
      const view = render(<RunDisclosureBlock timeline={run.timeline} />);
      fireEvent.click(view.container.querySelector('.run-disclosure-header')!);
      expect(view.container.querySelector('[data-status="interrupted"]')).toBeNull();
      expect(view.container.textContent).not.toContain('已中断');
      view.unmount();
    }
  },
);

it('keeps block order through colleague completion, later calls and reload', () => {
  const expected = ['parent', 'colleague', 'hr', 'continuation'];
  for (const sequence of [127, 130, 131, 136, 168, 190, 226]) {
    for (const model of models(sequence, sequence < 226)) {
      expect(runs(model).map(cell => cell.runId), `source-${sequence}`).toEqual(expected);
      const visibleCells = model.cells.filter(cell => cell.kind !== 'boundary');
      const answer = visibleCells.findIndex(cell => cell.kind === 'assistant_final' && cell.message.content === '同事的介绍已返回。');
      if (answer >= 0) expect(visibleCells[answer - 1]).toMatchObject({ kind: 'active_run', runId: 'continuation' });
    }
  }
});

it('freezes the HR action on its own completion despite a stale running task read', () => {
  vi.useFakeTimers();
  vi.setSystemTime(at('03:15:16.932'));
  for (const model of models(127)) {
    const hr = runs(model).find(cell => cell.runId === 'hr')!;
    expect(hr.timeline).toMatchObject({ status: 'done', completedAt: at('03:15:14.924'), durationIsFinal: true,
      startedAt: at('03:14:49.018'), durationMs: 25_906 });
    expect(hr.timeline.steps.map(step => step.status)).toEqual(['done', 'done']);
    const view = render(<RunDisclosureBlock timeline={hr.timeline} />);
    const header = view.container.querySelector('.run-disclosure-header')!;
    const text = header.textContent;
    expect(header.textContent).toContain('完成');
    act(() => { vi.advanceTimersByTime(30_000); });
    expect(header.textContent).toBe(text);
    view.unmount();
  }
});

it('chooses the active run before a trailing unfinished sibling and leaves that sibling interrupted', () => {
  for (const messages of projectBoth(fixture().filter(event => sourceSequence(event) <= 127))) {
    const model = buildThreadTimeline({ messages: [...messages, { id: 'other-call', role: 'tool_call', content: '',
      toolName: 'read_runtime_result', toolStatus: 'running', eventRuntimeTaskId: 'other-run', timestamp: at('03:15:36.000') }],
    activeSession: { id: 'session' }, isWaiting: true, isStreaming: false,
    activeRunId: 'continuation', activeRunStatus: 'running', runtimePhase: 'tool_running' });
    const run = runs(model).find(cell => cell.runId === 'continuation')!;
    expect(run.timeline.steps.filter(step => ['read-1', 'check-1'].includes(step.id)).map(step => step.status))
      .toEqual(['running', 'running']);
    const sibling = runs(model).find(cell => cell.runId === 'other-run')!;
    expect(sibling.timeline.steps.map(step => step.status)).toEqual(['interrupted']);
  }
});

function action(sequence: number, taskId: string, status = 'completed', childSession?: string,
  type = 'runtime_action_completed'): ChatTranscriptEventPayload {
  const metadata = { status, runtime_task_id: taskId, task_id: taskId,
    action_kind: childSession ? 'a2a_delegation' : 'hr_provisioning',
    ...(childSession ? { child_session_id: childSession } : {}) };
  return { schema: 'hive.session_event_compatibility', schema_version: 1,
    compatibility_status: 'needs_reconciliation', reason: 'unmapped_legacy_kind', legacy_event_type: type,
    event_id: `action-${sequence}`, sequence, session_id: 'session', tenant_id: 'tenant',
    occurred_at: at('03:15:14.924'), payload: { content: taskId,
      parts: [{ type: 'event', event_type: type, ...metadata }], metadata: { role: 'system', ...metadata } },
  } as unknown as ChatTranscriptEventPayload;
}

const withoutSequence = (message: AgentChatMessage): AgentChatMessage => ({ ...message,
  ...(message.threadItem ? { threadItem: { ...message.threadItem, sequence: undefined as unknown as number } } : {}),
});
const withStaleTask = (messages: AgentChatMessage[]) => buildThreadTimeline({ messages,
  activeSession: { id: 'session' }, isWaiting: false, isStreaming: false,
  sessionWorkbench: { runtime_tasks: [{ id: 'hr', status: 'running', started_at: at('03:14:49.018') }] } as unknown as SessionWorkbench,
});

it.each([false, true])('prefers the run terminal over an earlier action completion (missing sequence: %s)', missing => {
  const user = fixture()[0];
  const runEnd = { ...fixture().find(event => sourceSequence(event) === 226), sequence: 3,
    scope: { level: 'run', session_id: 'session', thread_id: 'session', turn_id: 'turn-hr', run_id: 'hr' },
    item_id: 'hr', payload: { started_at: at('03:14:49.018') } } as unknown as ChatTranscriptEventPayload;
  for (const messages of projectBoth([user, action(2, 'hr'), runEnd])) {
    const projected = missing ? messages.map(message => message.eventType === 'runtime_action_completed' ? withoutSequence(message) : message) : messages;
    const hr = runs(withStaleTask(projected)).find(cell => cell.runId === 'hr')!;
    expect(hr.timeline).toMatchObject({ status: 'done', completedAt: at('03:16:25.185'),
      durationIsFinal: true, durationMs: 96_167 });
  }
});

it.each([false, true])('lets a real action sequence beat a missing sequence (missing first: %s)', missingFirst => {
  const messages = projectBoth([fixture()[0], action(2, 'hr'), action(3, 'hr')])[1];
  const actions = messages.filter(message => message.eventType === 'runtime_action_completed');
  const missing = withoutSequence(actions[0]);
  const real = { ...actions[1], timestamp: at('03:15:30.000') };
  const hr = runs(withStaleTask([messages[0], ...(missingFirst ? [missing, real] : [real, missing])]))
    .find(cell => cell.runId === 'hr')!;
  expect(hr.timeline).toMatchObject({ completedAt: at('03:15:30.000'), durationMs: 40_982, durationIsFinal: true });
});

it.each([false, true])('ends a done action despite a stale running read (missing sequence: %s)', missing => {
  for (const messages of projectBoth([fixture()[0], action(2, 'hr', 'done')])) {
    const projected = missing ? messages.map(message => message.eventType === 'runtime_action_completed' ? withoutSequence(message) : message) : messages;
    const hr = runs(withStaleTask(projected)).find(cell => cell.runId === 'hr')!;
    expect(hr.timeline).toMatchObject({ status: 'done', completedAt: at('03:15:14.924'),
      durationIsFinal: true, durationMs: 25_906 });
  }
});

it('pairs two tasks finishing in reverse order with their own start positions', () => {
  const events = [action(1, 'a', 'running', 'child', 'runtime_action_started'),
    action(2, 'marker', 'degraded', undefined, 'memory_context_degraded'),
    action(3, 'b', 'running', 'child', 'runtime_action_started'),
    action(4, 'b', 'completed', 'child'), action(5, 'a', 'completed', 'child')];
  for (const messages of projectBoth(events)) {
    expect(messages.map(message => message.transcriptEventId)).toEqual(['action-5', 'action-2', 'action-4']);
    expect(messages[0].threadItem?.metadata?.runtime_task_id).toBe('a');
    expect(messages[2].threadItem?.metadata?.runtime_task_id).toBe('b');
  }
});

it('reserves exact task pairs before applying the landed parent-id FIFO fold', () => {
  const events = [action(1, 'parent-a', 'running', 'child', 'runtime_action_started'),
    action(2, 'b', 'running', 'child', 'runtime_action_started'),
    action(3, 'b', 'completed', 'child'), action(4, 'child-a', 'completed', 'child')];
  for (const messages of projectBoth(events)) {
    expect(messages.map(message => message.transcriptEventId)).toEqual(['action-4', 'action-3']);
    expect(messages.filter(message => message.eventType === 'runtime_action_started')).toHaveLength(0);
  }
});

it('uses the same index fallback to record and locate an unsequenced terminal', () => {
  const events = [action(1, 'a', 'running', 'child', 'runtime_action_started'),
    action(2, 'marker', 'degraded', undefined, 'memory_context_degraded'), action(3, 'a', 'completed', 'child')];
  const messages = events.flatMap(event => hydrateSessionTranscriptEvents([event], Number(event.sequence) - 1).messages)
    .map(withoutSequence);
  const projected = composeMixedPlaneSessionMessages({ store: undefined, compatibilityMessages: messages,
    timeline: createCompatibilityMessageTimeline() });
  expect(projected.map(message => message.transcriptEventId)).toEqual(['action-3', 'action-2']);
});

it('leaves a manifest-only exact match for the existing FIFO fold when no terminal moved', () => {
  const completed = action(3, 'child-task', 'completed', 'child') as ChatTranscriptEventPayload
    & { payload: { metadata: Record<string, unknown> } };
  completed.payload.metadata.result_manifest = { items: [{ task_type: 'a2a_delegation',
    source_run_id: 'parent', terminal_status: 'completed', child_session_id: 'child' }] };
  const events = [action(1, 'parent', 'running', 'child', 'runtime_action_started'),
    action(2, 'marker', 'degraded', undefined, 'memory_context_degraded'), completed];
  for (const messages of projectBoth(events)) {
    expect(messages.map(message => message.transcriptEventId)).toEqual(['action-3', 'action-2']);
    expect(messages.filter(message => message.eventType === 'runtime_action_started')).toHaveLength(0);
  }
});

it('ranks a zero action sequence after a positive sequence for the same run', () => {
  for (const messages of projectBoth([fixture()[0], action(2, 'hr'), action(3, 'hr')])) {
    const actions = messages.filter(message => message.eventType === 'runtime_action_completed');
    const zero = { ...actions[0], threadItem: { ...actions[0].threadItem!, sequence: 0 } };
    const real = { ...actions[1], timestamp: at('03:15:30.000') };
    expect(zero.timestamp).toBe(at('03:15:14.924'));
    expect(real.threadItem?.sequence).toBe(3);
    const hr = runs(withStaleTask([messages[0], zero, real])).find(cell => cell.runId === 'hr')!;
    expect(hr.timeline).toMatchObject({ completedAt: at('03:15:30.000'), durationMs: 40_982, durationIsFinal: true });
  }
});

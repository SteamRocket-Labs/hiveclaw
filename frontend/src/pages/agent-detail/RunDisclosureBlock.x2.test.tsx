// @vitest-environment jsdom
// X2: a finished run block never changes, and its count, duration and order
// are true, live and after reload. Fixtures mirror E2E run 18 (production
// a5f8f0f1): the skill-preparation rows, the 44/35/38 s failed block, the
// paragraph below the red failure card, the cancelled colleague block, the
// handoff while the colleague works, and the duplicate stop rows.
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import { projectSessionSocketEvent } from './sessionSocketEventProjector';
import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';
import { buildThreadTimeline, buildThreadTimelineCached, createThreadTimelineCache } from '../session-workbench/timelineModel';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
  i18n: { language: locale.language },
}) }));
afterEach(() => { cleanup(); locale.language = 'zh'; window.localStorage.clear(); });

function expand(container: HTMLElement) {
  for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
  for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
}

function renderBlock(timeline: Parameters<typeof RunDisclosureBlock>[0]['timeline']) {
  const view = render(<RunDisclosureBlock timeline={timeline} />);
  expand(view.container);
  return view;
}

/** Rows a member can expand inside one rendered block. */
function renderedRowTitles(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.run-disclosure-steps [data-testid="run-disclosure-step"], .run-disclosure-steps .run-tool-history-item, .run-disclosure-steps [data-testid="run-disclosure-prose"], .run-disclosure-steps [data-testid="run-disclosure-commentary"]')]
    .map(node => node.textContent || '');
}

function headerText(container: HTMLElement): string {
  return container.querySelector('.run-disclosure-header')?.textContent || '';
}

const TENANT = 'tenant';
const SESSION = 'session-x2';

function roundScope(runId: string) {
  return { level: 'round' as const, session_id: SESSION, thread_id: SESSION, turn_id: 'turn-x2', run_id: runId, round_id: 'round-x2' };
}

export function envelope(options: {
  sequence: number; itemKind: string; kind: string; lifecycle: string; itemId: string;
  payload: Record<string, unknown>; occurredAt: string; runId?: string; audience?: string;
  scope?: Record<string, unknown>; actor?: Record<string, unknown>;
  invocation_id?: string; parent_item_id?: string;
}): ChatTranscriptEventPayload {
  const runId = options.runId ?? 'run-x2';
  return {
    schema: 'hive.session_event', schema_version: 2,
    event_id: `event-${options.sequence}`, sequence: options.sequence, ordinal: options.sequence - 1,
    tenant_id: TENANT,
    scope: options.scope ?? (options.itemKind === 'human_input'
      ? { level: 'session', session_id: SESSION, thread_id: SESSION }
      : roundScope(runId)),
    item_id: options.itemId, item_kind: options.itemKind, kind: options.kind, lifecycle: options.lifecycle,
    payload_schema: `hive.session.payload.${options.itemKind}.${options.lifecycle}.v2`,
    actor: options.actor ?? { type: options.itemKind === 'human_input' ? 'user' : 'assistant' },
    visibility: { audience: options.audience || 'direct_user' },
    payload: options.payload,
    occurred_at: options.occurredAt, persisted_at: options.occurredAt,
    ...(options.invocation_id ? { invocation_id: options.invocation_id } : {}),
    ...(options.parent_item_id ? { parent_item_id: options.parent_item_id } : {}),
  } as unknown as ChatTranscriptEventPayload;
}

export function userInput(sequence: number, content: string, occurredAt: string): ChatTranscriptEventPayload {
  return envelope({ sequence, itemKind: 'human_input', kind: 'human_input.accepted', lifecycle: 'accepted', itemId: `input-${sequence}`,
    payload: { content }, occurredAt, actor: { type: 'user', id: 'user-x2' } });
}

export function runLifecycle(sequence: number, runId: string, lifecycle: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled', occurredAt: string, payload: Record<string, unknown> = {}): ChatTranscriptEventPayload {
  return envelope({ sequence, itemKind: 'run', kind: `run.${lifecycle}`, lifecycle, itemId: runId,
    payload: { status: lifecycle, ...payload }, occurredAt, runId,
    scope: { level: 'run', session_id: SESSION, thread_id: SESSION, turn_id: 'turn-x2', run_id: runId },
    actor: { type: 'runtime' } });
}

export function assistantEvent(sequence: number, itemKind: 'assistant_reasoning_summary' | 'assistant_text' | 'assistant_final' | 'assistant_commentary', lifecycle: 'snapshot' | 'delta' | 'completed', runId: string, itemId: string, payload: Record<string, unknown>, occurredAt: string): ChatTranscriptEventPayload {
  return envelope({ sequence, itemKind, kind: `${itemKind}.${lifecycle}`, lifecycle, itemId, payload, occurredAt, runId });
}

export function toolCallPair(sequence: number, runId: string, toolName: string, args: Record<string, unknown>, result: Record<string, unknown>, occurredAt: string, outcome = 'success'): ChatTranscriptEventPayload[] {
  return [
    envelope({ sequence, itemKind: 'tool_call', kind: 'tool_call.started', lifecycle: 'started', itemId: `call-${sequence}`,
      payload: { tool_name: toolName, args }, occurredAt, runId, invocation_id: `invocation-${sequence}` }),
    envelope({ sequence: sequence + 1, itemKind: 'tool_result', kind: 'tool_result.completed', lifecycle: 'completed', itemId: `result-${sequence}`,
      payload: { tool_name: toolName, outcome, content: '', ...result }, occurredAt, runId,
      invocation_id: `invocation-${sequence}`, parent_item_id: `call-${sequence}` }),
  ];
}

export function runtimeFailure(sequence: number, runId: string, occurredAt: string, reasonCode = 'identical_refused_result_read'): ChatTranscriptEventPayload {
  // Production writes the failure run-scoped (web_chat_runtime.py) and it can
  // sequence before run.failed; it carries no started_at, so the typed start
  // falls back to the run item's own first event (X2 r2 item 7).
  return envelope({ sequence, itemKind: 'runtime_failure', kind: 'runtime_failure.recorded', lifecycle: 'recorded', itemId: `failure-${sequence}`,
    payload: { status: 'failed', terminal_reason: reasonCode, failure_code: reasonCode, retryable: true,
      message: '这份结果这次没法安全读取，我没能继续。' },
    occurredAt, runId, actor: { type: 'runtime' },
    scope: { level: 'run', session_id: SESSION, thread_id: SESSION, turn_id: 'turn-x2', run_id: runId } });
}

/** The compatibility envelope the backend serializer returns for an unmapped
 * legacy runtime event (run 18's 「准备所需功能」 and 「运行状态」 rows). */
export function compatibilityEnvelope(sequence: number, legacyEventType: string, content: string, metadata: Record<string, unknown> = {}, occurredAt = '2026-11-13T09:21:36.5Z', payloadExtra: Record<string, unknown> = {}): ChatTranscriptEventPayload {
  return {
    schema: 'hive.session_event_compatibility', schema_version: 1, compatibility_status: 'needs_reconciliation',
    reason: 'unmapped_legacy_kind', event_id: `legacy-${sequence}`, sequence,
    legacy_kind: 'legacy_unknown', legacy_lifecycle: 'legacy_unknown',
    legacy_event_type: legacyEventType, legacy_item_type: 'event', legacy_item_status: 'succeeded',
    payload: { content, parts: [{ type: 'event', event_type: legacyEventType, title: 'Runtime event', text: content, status: 'info' }], metadata: { ...metadata, content }, ...payloadExtra },
    occurred_at: occurredAt,
  } as unknown as ChatTranscriptEventPayload;
}

/** The thread-item jacket `broadcast_web_chat_event` currently wraps around a
 * committed compatibility envelope before the live socket delivers it. */
export function liveBroadcastJacket(committed: ChatTranscriptEventPayload): ChatTranscriptEventPayload {
  const record = committed as unknown as Record<string, unknown>;
  const sequence = Number(record.sequence || 0);
  return {
    ...record,
    schema: 'hive.thread_item.v1', schema_version: 1,
    id: `live:${sequence.toString(16).padStart(32, '0')}`,
    item_type: 'event', item_status: 'succeeded',
    event_type: 'event', type: 'event', role: 'system',
    content: '', parts: [], metadata: { ...record },
  } as unknown as ChatTranscriptEventPayload;
}

/** The raw legacy stream frame the web-chat finalizer broadcasts live. */
export function legacyDoneFrame(content: string, occurredAt: string): ChatTranscriptEventPayload {
  return { id: `live:done-${occurredAt}`, type: 'done', role: 'assistant', content,
    parts: [{ type: 'text', text: content }], created_at: occurredAt, run_id: 'run-x2' } as unknown as ChatTranscriptEventPayload;
}

/** Run both real paths over the same durable events: the live socket applier
 * (`applyTranscriptToSessionRuntime`) and reload hydration
 * (`hydrateSessionTranscriptEvents`). Returns [liveMessages, reloadMessages]. */
export function makeLiveSocketHarness(): { live: () => AgentChatMessage[]; deliver: (event: ChatTranscriptEventPayload) => void } {
  let live: AgentChatMessage[] = [];
  const applierDeps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => 'run-x2', markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, messages) => messages, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { live = update(live); },
    setChatMessagesAfterQueued: (_session, update) => { live = update(live); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  const deliver = (event: ChatTranscriptEventPayload) => {
    projectSessionSocketEvent(
      { data: event, session: { id: SESSION }, agentId: 'agent', sessionId: SESSION, key: 'agent:session', isActiveRuntime: true,
        closeSessionSocket: vi.fn(), failAuthentication: vi.fn() },
      {
        applyTranscriptToSession: (agentId: string, sessionId: string, socketEvent: ChatTranscriptEventPayload, isActiveRuntime: boolean) =>
          applyTranscriptToSessionRuntime(applierDeps, agentId, sessionId, socketEvent, isActiveRuntime),
        activeRunIdOf: () => 'run-x2',
        selectSession: vi.fn(), fetchMySessions: vi.fn(), denySessionRead: vi.fn(),
        setSessionPhase: vi.fn(), sessionPhaseOf: () => 'idle', syncActivePhase: vi.fn(), setActiveRunState: vi.fn(),
        markActiveRunTerminal: () => true, invalidateSessionRuntimeQueries: vi.fn(), reconcileSessionTranscript: vi.fn(),
        shouldInvalidateToolCall: () => false, isTerminalTranscriptToolMessage: () => false,
        normalizeToolCallMessage: message => message, parseChatMsg: message => message,
        setChatMessagesSessionId: vi.fn(), setTransportNotice: vi.fn(),
        enqueueChatMessagesUpdate: (_session: string, update: (messages: AgentChatMessage[]) => AgentChatMessage[]) => { live = update(live); },
        setChatMessagesAfterQueued: (_session: string, update: (messages: AgentChatMessage[]) => AgentChatMessage[]) => { live = update(live); },
        setCreatedAgentId: vi.fn(), setAgentExpired: vi.fn(), invalidateQuery: vi.fn(),
      },
    );
  };
  return { live: () => live, deliver };
}

export function projectBoth(events: ChatTranscriptEventPayload[]): AgentChatMessage[][] {
  const reload = hydrateSessionTranscriptEvents(events, 0).messages;
  const harness = makeLiveSocketHarness();
  for (const event of events) {
    harness.deliver(event);
  }
  return [harness.live(), reload];
}

/** The reload-first-paint projection over a newest-first page (the incremental
 * hydration publishes the newest page before older pages merge). */
export function reloadPage(events: ChatTranscriptEventPayload[], fromSequence: number): AgentChatMessage[] {
  return hydrateSessionTranscriptEvents(events.filter(event => Number(event.sequence) >= fromSequence), fromSequence - 1).messages;
}

export function timelineFor(messages: AgentChatMessage[], activeRunStatus: string | null = null, activeSession: Record<string, unknown> = { id: SESSION }) {
  return buildThreadTimeline({ messages, activeSession: activeSession as never, isWaiting: false, isStreaming: false, activeRunStatus });
}

// ---------------------------------------------------------------------------
// A. One count: the header count equals the rows a member can expand, live and
// after reload, and a block with nothing to expand shows no count.
// ---------------------------------------------------------------------------

it('x2a: the step count equals the expandable rows, live and after reload (run 18 step 3)', () => {
  const answer = 'example.com 能打开，标题 Example Domain；.invalid 域名解析失败。';
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '查一下这两个网页', '2026-11-13T09:21:34Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:21:35Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:21:35.5Z'),
    assistantEvent(4, 'assistant_reasoning_summary', 'snapshot', 'run-x2', 'reason-1', { content: '我来打开这两个网页。' }, '2026-11-13T09:21:36Z'),
    compatibilityEnvelope(5, 'tool_group_activation', 'Activated runtime tool groups for this task.', {}, '2026-11-13T09:21:36.5Z'),
    ...toolCallPair(6, 'run-x2', 'web_fetch', { url: 'https://example.com/' }, { member_summary: { kind: 'web_fetch', state: 'done' }, host: 'example.com' }, '2026-11-13T09:21:37Z'),
    ...toolCallPair(8, 'run-x2', 'web_fetch', { url: 'https://.invalid/' }, { member_summary: { kind: 'web_fetch', state: 'not_done' }, host: 'invalid' }, '2026-11-13T09:21:39Z'),
    assistantEvent(10, 'assistant_text', 'snapshot', 'run-x2', 'text-1', { content: answer, phase: 'unknown' }, '2026-11-13T09:21:41Z'),
    assistantEvent(11, 'assistant_final', 'completed', 'run-x2', 'final-1', { content: answer }, '2026-11-13T09:21:42Z'),
    runLifecycle(12, 'run-x2', 'completed', '2026-11-13T09:21:42.5Z'),
  ];
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    const run = model.cells.find(cell => cell.kind === 'active_run');
    if (run?.kind !== 'active_run') throw new Error('missing run cell');
    const { container, unmount } = renderBlock(run.timeline);
    const rows = renderedRowTitles(container);
    const header = headerText(container);
    const match = header.match(/(\d+)\s*个步骤/);
    expect(match, `header must state a step count for ${rows.length} rows: ${header}`).toBeTruthy();
    // Every counted step is a row the member can expand.
    expect(rows.length).toBe(Number(match![1]));
    expect(rows.some(row => row.includes('准备所需功能'))).toBe(true);
    unmount();
  }
});

it('x2a: a prose row whose display text is empty is neither rendered nor counted', () => {
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '生成报告', '2026-11-13T09:15:13.704Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:15:14Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:15:14.2Z'),
    assistantEvent(4, 'assistant_text', 'snapshot', 'run-x2', 'text-1', { content: '**DELIVERABLE**: report.md', phase: 'unknown' }, '2026-11-13T09:15:20Z'),
    assistantEvent(5, 'assistant_final', 'completed', 'run-x2', 'final-1', { content: '报告已生成：report.md。' }, '2026-11-13T09:15:26Z'),
    runLifecycle(6, 'run-x2', 'completed', '2026-11-13T09:15:26.4Z'),
  ];
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    const run = model.cells.find(cell => cell.kind === 'active_run');
    if (run?.kind !== 'active_run') throw new Error('missing run cell');
    const { container, unmount } = renderBlock(run.timeline);
    // The DELIVERABLE marker line strips to empty display text: the row
    // renders nothing, so the count must not include it either.
    expect(renderedRowTitles(container)).toEqual([]);
    expect(headerText(container)).not.toMatch(/\d+\s*个步骤/);
    unmount();
  }
});

it('x2a: a block with nothing to expand shows no step count (run 18 hello)', () => {
  const answer = '你好，test123!我是你的助理';
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '[E2E] hello', '2026-11-13T09:15:13.704Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:15:14Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:15:14.2Z'),
    assistantEvent(4, 'assistant_text', 'snapshot', 'run-x2', 'text-1', { content: answer, phase: 'unknown' }, '2026-11-13T09:15:20Z'),
    assistantEvent(5, 'assistant_final', 'completed', 'run-x2', 'final-1', { content: answer }, '2026-11-13T09:15:26Z'),
    runLifecycle(6, 'run-x2', 'completed', '2026-11-13T09:15:26.4Z'),
  ];
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    const run = model.cells.find(cell => cell.kind === 'active_run');
    if (run?.kind !== 'active_run') throw new Error('missing run cell');
    const { container, unmount } = renderBlock(run.timeline);
    expect(renderedRowTitles(container)).toEqual([]);
    expect(headerText(container)).not.toMatch(/\d+\s*个步骤/);
    unmount();
  }
});

// ---------------------------------------------------------------------------
// B. One duration: a terminal block's duration is the run's own typed start →
// terminal time, identical live and after reload, frozen at the terminal.
// ---------------------------------------------------------------------------

export const B_RUN = 'run-reads';
export const B_INPUT_AT = '2026-11-13T09:44:27.000Z';
export const B_RUN_QUEUED_AT = '2026-11-13T09:46:46.800Z';
export const B_TERMINAL_AT = '2026-11-13T09:47:21.800Z';

export function failedReadsTurnEvents(withStatusRow: boolean): ChatTranscriptEventPayload[] {
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '把结果整理成三条建议', B_INPUT_AT),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:44:27.3Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:44:27.6Z'),
    ...toolCallPair(4, 'run-x2', 'delegate_to_agent', { agent_name: '同事', task: '三条建议' }, { member_summary: { kind: 'delegation', state: 'in_progress', task_id: 'colleague-task' } }, '2026-11-13T09:44:33Z'),
    assistantEvent(6, 'assistant_final', 'completed', 'run-x2', 'handoff-final', { content: '我先把这项任务交给「同事」，拿到结果后再整理给你。' }, '2026-11-13T09:44:34.8Z'),
    runLifecycle(7, 'run-x2', 'completed', '2026-11-13T09:44:34.9Z'),
    runLifecycle(8, B_RUN, 'queued', B_RUN_QUEUED_AT),
    runLifecycle(9, B_RUN, 'running', B_RUN_QUEUED_AT),
  ];
  let sequence = 10;
  if (withStatusRow) {
    events.push(compatibilityEnvelope(sequence, 'runtime_action_started', '已委派给 同事，后台执行中。', { status: 'running', action_kind: 'a2a_delegation', runtime_task_id: 'colleague-task', target_agent_name: '同事' }, '2026-11-13T09:46:43.8Z'));
    sequence += 1;
  }
  for (let index = 0; index < 5; index += 1) {
    events.push(assistantEvent(sequence, 'assistant_reasoning_summary', 'snapshot', B_RUN, `reads-reason-${index}`, { content: '看看同事的结果。' }, '2026-11-13T09:46:47Z'));
    sequence += 1;
    events.push(...toolCallPair(sequence, B_RUN, 'read_runtime_result', { task_id: 'colleague-task' }, { member_summary: { kind: 'colleague_result', state: 'not_done', task_id: 'colleague-task' }, outcome: 'failed' }, '2026-11-13T09:47:20Z', 'failed'));
    sequence += 2;
  }
  events.push(
    runtimeFailure(sequence, B_RUN, B_TERMINAL_AT),
    runLifecycle(sequence + 1, B_RUN, 'failed', B_TERMINAL_AT, { reason_code: 'identical_refused_result_read' }),
  );
  return events;
}

it('x2b: the terminal failed block shows one typed duration, live and after reload', () => {
  const events = failedReadsTurnEvents(true);
  const [live] = projectBoth(events);
  const reload = hydrateSessionTranscriptEvents(events, 0).messages;
  const durations = [live, reload].map(messages => {
    const model = timelineFor(messages);
    const run = model.cells.find(cell => cell.kind === 'active_run' && cell.timeline.status === 'failed');
    if (run?.kind !== 'active_run') throw new Error('missing failed run cell');
    return run.timeline;
  });
  // The run's own typed start (run.queued 09:46:46.8) → typed terminal
  // (09:47:21.8) = 35 s, per the run 18 judgment, on both paths.
  expect(durations[0].durationMs).toBe(35_000);
  expect(durations[1].durationMs).toBe(35_000);
  expect(durations[0].durationIsFinal).toBe(true);
  const { container } = render(<RunDisclosureBlock timeline={durations[0]} />);
  expect(container.querySelector('.run-disclosure-duration')?.textContent).toMatch(/^35/);
});

it('x2b: a terminal block never shows the running clock after its terminal status', () => {
  vi.useFakeTimers({ now: new Date('2026-11-13T09:46:56.000Z') });
  try {
    const startedAt = '2026-11-13T09:46:36.000Z';
    const view = render(<RunDisclosureBlock timeline={{ id: 'run-x2', status: 'running', startedAt, steps: [
      { id: 'step-1', kind: 'reasoning', title: 'Thinking', status: 'running', visibility: 'visible' },
    ] }} />);
    expect(view.container.querySelector('.run-disclosure-duration')?.textContent).toMatch(/^20/);
    // The typed step timestamps settle the block as failed before the run
    // terminal witness arrives (run 18: 44 s at 09:47:21.766).
    view.rerender(<RunDisclosureBlock timeline={{ id: 'run-x2', status: 'failed', startedAt,
      durationMs: 15_000, steps: [
        { id: 'step-1', kind: 'reasoning', title: 'Thinking', status: 'failed', visibility: 'visible' },
      ] }} />);
    const afterTerminal = view.container.querySelector('.run-disclosure-duration')?.textContent;
    expect(afterTerminal, 'no live-clock residue after the terminal status').not.toBe('20 秒');
    // Once the typed run terminal lands the same value stays.
    view.rerender(<RunDisclosureBlock timeline={{ id: 'run-x2', status: 'failed', startedAt,
      durationMs: 15_000, durationIsFinal: true, steps: [
        { id: 'step-1', kind: 'reasoning', title: 'Thinking', status: 'failed', visibility: 'visible' },
      ] }} />);
    expect(view.container.querySelector('.run-disclosure-duration')?.textContent).toMatch(/^15/);
    if (afterTerminal) expect(afterTerminal).toMatch(/^15/);
  } finally {
    vi.useRealTimers();
  }
});

// ---------------------------------------------------------------------------
// C. One order: the paragraph written before the failure card stays above it,
// live and after reload.
// ---------------------------------------------------------------------------

it('x2c: a paragraph written before the failure card renders above it, live and after reload', () => {
  const paragraph = '我先把这项任务交给「同事」，拿到它的三条建议后再整理给你。';
  const durable: ChatTranscriptEventPayload[] = [
    userInput(1, '把结果整理成三条建议', B_INPUT_AT),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:44:27.3Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:44:27.6Z'),
    { ...assistantEvent(4, 'assistant_text', 'snapshot', 'run-x2', 'handoff-text', { content: paragraph, phase: 'unknown' }, '2026-11-13T09:44:34Z'), payload: { content: paragraph, phase: 'unknown', legacy: true } },
    { ...assistantEvent(5, 'assistant_final', 'completed', 'run-x2', 'handoff-final', { content: paragraph }, '2026-11-13T09:44:34.8Z'), payload: { content: paragraph, legacy: true } },
    runLifecycle(6, 'run-x2', 'completed', '2026-11-13T09:44:34.9Z'),
    runLifecycle(7, B_RUN, 'queued', B_RUN_QUEUED_AT),
    runLifecycle(8, B_RUN, 'running', B_RUN_QUEUED_AT),
    ...toolCallPair(9, B_RUN, 'read_runtime_result', { task_id: 'colleague-task' }, { member_summary: { kind: 'colleague_result', state: 'not_done', task_id: 'colleague-task' }, outcome: 'failed' }, '2026-11-13T09:47:20Z', 'failed'),
    runtimeFailure(11, B_RUN, B_TERMINAL_AT),
    runLifecycle(12, B_RUN, 'failed', B_TERMINAL_AT, { reason_code: 'identical_refused_result_read' }),
  ];
  // Live: the parent's paragraph arrives through the raw stream frame (the
  // web-chat finalizer) in addition to the canonical events, before the
  // canonical tail of the failed run.
  const liveEvents: ChatTranscriptEventPayload[] = [
    durable[0], durable[1], durable[2],
    legacyDoneFrame(paragraph, '2026-11-13T09:44:34.8Z'),
    durable[3], durable[4],
    ...durable.slice(5),
  ];
  const [live, reload] = projectBoth(liveEvents);
  const reloadFull = hydrateSessionTranscriptEvents(durable, 0).messages;
  // The member sees cells: run blocks (their prose rows) and final answers.
  // A paragraph renders once — its in-run copy repeats the answer and hides
  // (repeatsAnswer) — and stays above the later failure card.
  const renderedParagraphCopies = (messages: AgentChatMessage[]): number => {
    const model = timelineFor(messages);
    let copies = 0;
    let paragraphCell = -1;
    let failureCell = -1;
    model.cells.forEach((cell, cellIndex) => {
      if (cell.kind === 'assistant_final') {
        if ((cell.message.content || '').includes('我先把这项任务交给')) { copies += 1; if (paragraphCell < 0) paragraphCell = cellIndex; }
        return;
      }
      if (cell.kind === 'boundary') {
        // Lifecycle and failure cards render as boundary cells below the runs.
        if (/没法安全读取|couldn.t be read safely/.test(`${cell.summary || ''}\n${cell.message.content || ''}`) && failureCell < 0) failureCell = cellIndex;
        return;
      }
      if (cell.kind !== 'active_run') return;
      const { container, unmount } = renderBlock(cell.timeline);
      const text = container.textContent || '';
      // Event messages of this cell (the failure card) render below the block.
      const belowBlockText = cell.sourceMessages
        .filter(entry => entry.message.role === 'event')
        .map(entry => entry.message.content || '').join('\n');
      copies += text.split('我先把这项任务交给').length - 1;
      if (text.includes('我先把这项任务交给') && paragraphCell < 0) paragraphCell = cellIndex;
      if (/没法安全读取|couldn.t be read safely/.test(text + belowBlockText) && failureCell < 0) failureCell = cellIndex;
      unmount();
    });
    expect(paragraphCell, 'the paragraph must render').toBeGreaterThanOrEqual(0);
    expect(failureCell, 'the failure card must render').toBeGreaterThanOrEqual(0);
    expect(paragraphCell, 'the paragraph stays above the failure card').toBeLessThan(failureCell);
    return copies;
  };
  for (const [label, messages] of [['reload', reloadFull], ['live', live], ['reload-from-live-events', reload]] as const) {
    // The live terminal merge must not re-append the earlier run's answer
    // below the newer run's failure card (run 18 05-step-live-block-01).
    expect(renderedParagraphCopies(messages), `${label}: the paragraph renders once`).toBe(1);
  }
});


it('x2c: a legacy answer bound to an earlier run stays above the later failure card', () => {
  const paragraph = '我先把这项任务交给「同事」，拿到它的三条建议后再整理给你。';
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '把结果整理成三条建议', B_INPUT_AT),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:44:27.3Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:44:27.6Z'),
    // The persisted legacy answer row: a compatibility envelope whose
    // legacy_run_id binds it to the earlier run. It has no session item, and
    // the failed run has no canonical final of its own.
    compatibilityEnvelope(4, 'assistant_message', paragraph, {}, '2026-11-13T09:44:34.8Z', { legacy_run_id: 'run-x2' }),
    runLifecycle(5, 'run-x2', 'completed', '2026-11-13T09:44:34.9Z'),
    runLifecycle(6, B_RUN, 'queued', B_RUN_QUEUED_AT),
    runLifecycle(7, B_RUN, 'running', B_RUN_QUEUED_AT),
    ...toolCallPair(8, B_RUN, 'read_runtime_result', { task_id: 'colleague-task' }, { member_summary: { kind: 'colleague_result', state: 'not_done', task_id: 'colleague-task' }, outcome: 'failed' }, '2026-11-13T09:47:20Z', 'failed'),
    runtimeFailure(10, B_RUN, B_TERMINAL_AT),
    runLifecycle(11, B_RUN, 'failed', B_TERMINAL_AT, { reason_code: 'identical_refused_result_read' }),
  ];
  const [live, reload] = projectBoth(events);
  for (const [label, messages] of [['live', live], ['reload', reload]] as const) {
    const paragraphCopies = messages.filter(message => (message.content || '').includes('我先把这项任务交给')).length;
    const paragraphAt = messages.findIndex(message => (message.content || '').includes('我先把这项任务交给'));
    const failureAt = messages.findIndex(message => /没法安全读取|couldn.t be read safely/.test(message.content || ''));
    expect(paragraphAt, `${label}: paragraph present`).toBeGreaterThanOrEqual(0);
    expect(failureAt, `${label}: failure card present`).toBeGreaterThanOrEqual(0);
    expect(paragraphAt, `${label}: paragraph above the failure card`).toBeLessThan(failureAt);
    expect(paragraphCopies, `${label}: the paragraph renders once`).toBe(1);
  }
});

it('x2c: a done frame bound to its run renders once beside that run\'s canonical final, above a later failure', () => {
  const paragraph = '我先把这项任务交给「同事」，拿到它的三条建议后再整理给你。';
  const durable: ChatTranscriptEventPayload[] = [
    userInput(1, '把结果整理成三条建议', B_INPUT_AT),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:44:27.3Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:44:27.6Z'),
    assistantEvent(4, 'assistant_text', 'snapshot', 'run-x2', 'handoff-text', { content: paragraph, phase: 'unknown' }, '2026-11-13T09:44:34Z'),
    assistantEvent(5, 'assistant_final', 'completed', 'run-x2', 'handoff-final', { content: paragraph }, '2026-11-13T09:44:34.8Z'),
    runLifecycle(6, 'run-x2', 'completed', '2026-11-13T09:44:34.9Z'),
    runLifecycle(7, B_RUN, 'queued', B_RUN_QUEUED_AT),
    runLifecycle(8, B_RUN, 'running', B_RUN_QUEUED_AT),
    ...toolCallPair(9, B_RUN, 'read_runtime_result', { task_id: 'colleague-task' }, { member_summary: { kind: 'colleague_result', state: 'not_done', task_id: 'colleague-task' }, outcome: 'failed' }, '2026-11-13T09:47:20Z', 'failed'),
    runtimeFailure(11, B_RUN, B_TERMINAL_AT),
    runLifecycle(12, B_RUN, 'failed', B_TERMINAL_AT, { reason_code: 'identical_refused_result_read' }),
  ];
  // Live: the stream finalizer's done frame (run-bound, X2 r3 item 4) rides
  // the same turn as the canonical final; a later failed run must not adopt
  // or reorder it.
  const liveEvents: ChatTranscriptEventPayload[] = [
    durable[0], durable[1], durable[2],
    legacyDoneFrame(paragraph, '2026-11-13T09:44:34.8Z'),
    ...durable.slice(3),
  ];
  const [live, reload] = projectBoth(liveEvents);
  for (const [label, messages] of [['live', live], ['reload', reload]] as const) {
    const model = timelineFor(messages);
    let copies = 0;
    let paragraphCell = -1;
    let failureCell = -1;
    model.cells.forEach((cell, cellIndex) => {
      if (cell.kind === 'assistant_final') {
        if ((cell.message.content || '').includes('我先把这项任务交给')) { copies += 1; if (paragraphCell < 0) paragraphCell = cellIndex; }
        return;
      }
      if (cell.kind === 'boundary') {
        if (/没法安全读取|couldn.t be read safely/.test(`${cell.summary || ''}\n${cell.message.content || ''}`) && failureCell < 0) failureCell = cellIndex;
        return;
      }
      if (cell.kind !== 'active_run') return;
      const { container, unmount } = renderBlock(cell.timeline);
      const text = container.textContent || '';
      const belowBlockText = cell.sourceMessages
        .filter(entry => entry.message.role === 'event')
        .map(entry => entry.message.content || '').join('\n');
      copies += text.split('我先把这项任务交给').length - 1;
      if (text.includes('我先把这项任务交给') && paragraphCell < 0) paragraphCell = cellIndex;
      if (/没法安全读取|couldn.t be read safely/.test(text + belowBlockText) && failureCell < 0) failureCell = cellIndex;
      unmount();
    });
    expect(paragraphCell, `${label}: paragraph renders`).toBeGreaterThanOrEqual(0);
    expect(failureCell, `${label}: failure card renders`).toBeGreaterThanOrEqual(0);
    expect(paragraphCell, `${label}: paragraph above the failure card`).toBeLessThan(failureCell);
    expect(copies, `${label}: the paragraph renders once`).toBe(1);
  }
});

// ---------------------------------------------------------------------------
// D. No flicker: on reload the colleague block's first rendered status is its
// final status; no provisional done while older pages are still loading.
// ---------------------------------------------------------------------------

export function cancelledColleagueEvents(): ChatTranscriptEventPayload[] {
  const task = 'stop-task';
  return [
    userInput(1, '请同事帮我写周报', '2026-11-13T09:51:14Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:51:15Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:51:15.4Z'),
    ...toolCallPair(4, 'run-x2', 'delegate_to_agent', { agent_name: '同事', task: '写周报' }, { member_summary: { kind: 'delegation', state: 'in_progress', task_id: task } }, '2026-11-13T09:51:21.5Z'),
    compatibilityEnvelope(6, 'runtime_action_started', '已委派给 同事，后台执行中。', { status: 'running', action_kind: 'a2a_delegation', runtime_task_id: task, target_agent_name: '同事' }, '2026-11-13T09:51:21.6Z'),
    compatibilityEnvelope(7, 'agent_task_notification', '1 runtime result(s) ready in integration epoch 1 (terminal=1/expected=1). Parent continuation denied: stopped.',
      { runtime_result_integration_page: true, task_type: 'runtime_result_integration', continuation_status: 'denied', item_count: 1,
        result_manifest: { items: [{ terminal_status: 'killed', result_size_bytes: 96, artifact_count: 0 }] } }, '2026-11-13T09:51:25.4Z'),
    compatibilityEnvelope(8, 'child_session', '已停止，同事没有完成这项工作。', { reason: 'delegation_terminal', status: 'killed', runtime_task_id: task, child_session_id: 'colleague-session', to_agent: 'colleague', to_agent_name: '同事' }, '2026-11-13T09:51:26Z'),
    compatibilityEnvelope(9, 'runtime_action_failed', '已停止，同事没有完成这项工作。', { action_kind: 'a2a_delegation', status: 'killed', runtime_task_id: task, target_agent_name: '同事' }, '2026-11-13T09:51:26.2Z'),
    runLifecycle(10, 'run-x2', 'cancelled', '2026-11-13T09:51:27.5Z'),
  ];
}

it('x2d: every reload prefix renders the final status or no status (run 18 cancelled colleague)', () => {
  const events = cancelledColleagueEvents();
  const settledStatuses = new Map<string, string>();
  for (const cell of timelineFor(hydrateSessionTranscriptEvents(events, 0).messages).cells) {
    if (cell.kind !== 'active_run' || !cell.runId) continue;
    const { container } = render(<RunDisclosureBlock timeline={cell.timeline} />);
    settledStatuses.set(cell.runId, container.querySelector('.run-disclosure-title')?.textContent || '');
  }
  expect([...settledStatuses.values()]).toContain('已停止');
  // The incremental hydration publishes each newest-first page before older
  // pages merge; for every prefix, each block renders its final status or
  // nothing at all — never a provisional one the same load overturns.
  for (let prefix = 2; prefix <= events.length; prefix += 1) {
    for (const cell of timelineFor(reloadPage(events, prefix)).cells) {
      if (cell.kind !== 'active_run') continue;
      const { container } = render(<RunDisclosureBlock timeline={cell.timeline} />);
      const title = container.querySelector('.run-disclosure-title')?.textContent || '';
      const final = cell.runId ? settledStatuses.get(cell.runId) : undefined;
      expect(
        !title || (final !== undefined && title === final),
        `prefix from sequence ${prefix}, cell ${cell.runId || cell.id}: rendered ${JSON.stringify(title)}, final ${JSON.stringify(final)}`,
      ).toBe(true);
    }
  }
});

it('x2d: every reload prefix renders the final status or no status (handoff while the colleague works)', () => {
  const events = handoffWhileColleagueWorksEvents();
  const settled = timelineFor(hydrateSessionTranscriptEvents(events, 0).messages);
  const settledStatuses = new Map<string, string>();
  for (const cell of settled.cells) {
    if (cell.kind !== 'active_run' || !cell.runId) continue;
    const { container } = render(<RunDisclosureBlock timeline={cell.timeline} />);
    settledStatuses.set(cell.runId, container.querySelector('.run-disclosure-title')?.textContent || '');
  }
  expect([...settledStatuses.values()]).toContain('运行中');
  for (let prefix = 2; prefix <= events.length; prefix += 1) {
    for (const cell of timelineFor(reloadPage(events, prefix)).cells) {
      if (cell.kind !== 'active_run') continue;
      const { container } = render(<RunDisclosureBlock timeline={cell.timeline} />);
      const title = container.querySelector('.run-disclosure-title')?.textContent || '';
      const final = cell.runId ? settledStatuses.get(cell.runId) : undefined;
      expect(
        !title || (final !== undefined && title === final),
        `prefix from sequence ${prefix}, cell ${cell.runId || cell.id}: rendered ${JSON.stringify(title)}, final ${JSON.stringify(final)}`,
      ).toBe(true);
    }
  }
});

it('x2d: a mid-turn page renders no status for a cell with no status source; the full load settles the legacy status', () => {
  // A legacy run with no run item, no terminal and no task row: the cell has
  // no authoritative status source at all.
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '查一下资料', '2026-11-13T09:20:00Z'),
    assistantEvent(2, 'assistant_reasoning_summary', 'snapshot', 'run-x2', 'reason-1', { content: '查一下。' }, '2026-11-13T09:20:01Z'),
    ...toolCallPair(3, 'run-x2', 'read_file', { path: 'workspace/notes.md' }, { member_summary: { kind: 'file_read', state: 'done' } }, '2026-11-13T09:20:02Z'),
    { ...assistantEvent(5, 'assistant_final', 'completed', 'run-x2', 'legacy-final', { content: '资料在这里。' }, '2026-11-13T09:20:04Z'), payload: { content: '资料在这里。', legacy: true } },
  ];
  // The newest-first page that starts inside the run publishes no status,
  // count or clock for it.
  for (const from of [2, 3]) {
    for (const cell of timelineFor(reloadPage(events, from)).cells) {
      if (cell.kind !== 'active_run') continue;
      const { container, unmount } = render(<RunDisclosureBlock timeline={cell.timeline} />);
      expect(container.querySelector('.run-disclosure-title')?.textContent || '',
        `page from sequence ${from} must render no status`).toBe('');
      unmount();
    }
  }
  // The complete load settles the pre-X2 legacy rule: the step-derived status.
  const settled = timelineFor(hydrateSessionTranscriptEvents(events, 0).messages);
  const run = settled.cells.find(cell => cell.kind === 'active_run' && cell.timeline.steps.length > 0);
  if (run?.kind !== 'active_run') throw new Error('missing legacy run cell');
  const { container } = render(<RunDisclosureBlock timeline={run.timeline} />);
  expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
});

/** Production stamps the colleague's task id as run_id (and runtime_task_id)
 * on parent-side colleague rows (orchestrator.py ~2886-2896). X2 r3 item 5. */
function withColleagueRunId(events: ChatTranscriptEventPayload[], task: string): ChatTranscriptEventPayload[] {
  return events.map(event => {
    const record = event as unknown as Record<string, unknown>;
    if (record.schema !== 'hive.session_event_compatibility') return event;
    const metadata = ((record.payload as Record<string, unknown> | undefined)?.metadata ?? {}) as Record<string, unknown>;
    return metadata.runtime_task_id === task ? { ...record, run_id: task } as unknown as ChatTranscriptEventPayload : event;
  });
}

it('x2df: D and F hold when the colleague rows carry the task id as run_id, live and after reload', () => {
  const events = withColleagueRunId([...cancelledColleagueEvents()], 'stop-task');
  // F: one stopped sentence per delegated task.
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    for (const cell of model.cells) {
      if (cell.kind !== 'active_run') continue;
      const { container, unmount } = renderBlock(cell.timeline);
      const occurrences = (container.textContent || '').split('已停止，同事没有完成这项工作。').length - 1;
      expect(occurrences, `one stopped row, got ${occurrences}:\n${container.textContent}`).toBe(1);
      unmount();
    }
  }
  // D: every reload prefix still renders the final status or none.
  const settledStatuses = new Map<string, string>();
  for (const cell of timelineFor(hydrateSessionTranscriptEvents(events, 0).messages).cells) {
    if (cell.kind !== 'active_run' || !cell.runId) continue;
    const { container } = render(<RunDisclosureBlock timeline={cell.timeline} />);
    settledStatuses.set(cell.runId, container.querySelector('.run-disclosure-title')?.textContent || '');
  }
  expect([...settledStatuses.values()]).toContain('已停止');
  for (let prefix = 2; prefix <= events.length; prefix += 1) {
    for (const cell of timelineFor(reloadPage(events, prefix)).cells) {
      if (cell.kind !== 'active_run') continue;
      const { container } = render(<RunDisclosureBlock timeline={cell.timeline} />);
      const title = container.querySelector('.run-disclosure-title')?.textContent || '';
      const final = cell.runId ? settledStatuses.get(cell.runId) : undefined;
      expect(!title || (final !== undefined && title === final),
        `run_id-stamped prefix from ${prefix}, cell ${cell.runId || cell.id}: ${JSON.stringify(title)} vs ${JSON.stringify(final)}`).toBe(true);
    }
  }
});

// ---------------------------------------------------------------------------
// E. Header while a colleague works: the parent handoff block must not say
// 完成 while the delegated colleague is still working.
// ---------------------------------------------------------------------------

export function handoffWhileColleagueWorksEvents(): ChatTranscriptEventPayload[] {
  const task = 'live-task';
  return [
    userInput(1, '请同事给我三条提升本周销售的建议', '2026-11-13T09:44:27Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:44:27.3Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:44:27.6Z'),
    assistantEvent(4, 'assistant_reasoning_summary', 'snapshot', 'run-x2', 'handoff-reason', { content: '交给同事。' }, '2026-11-13T09:44:29Z'),
    ...toolCallPair(5, 'run-x2', 'delegate_to_agent', { agent_name: '[E2E] 第十五轮助手', task: '三条建议' }, { member_summary: { kind: 'delegation', state: 'in_progress', task_id: task } }, '2026-11-13T09:44:33Z'),
    assistantEvent(7, 'assistant_text', 'snapshot', 'run-x2', 'handoff-text', { content: '我先把这项任务交给「[E2E] 第十五轮助手」，拿到它的三条建议后再整理给你。', phase: 'unknown' }, '2026-11-13T09:44:34Z'),
    assistantEvent(8, 'assistant_final', 'completed', 'run-x2', 'handoff-final', { content: '我先把这项任务交给「[E2E] 第十五轮助手」，拿到它的三条建议后再整理给你。' }, '2026-11-13T09:44:34.8Z'),
    runLifecycle(9, 'run-x2', 'completed', '2026-11-13T09:44:34.9Z'),
    compatibilityEnvelope(10, 'runtime_action_started', '已委派给 [E2E] 第十五轮助手，后台执行中。', { status: 'running', action_kind: 'a2a_delegation', runtime_task_id: task, target_agent_name: '[E2E] 第十五轮助手' }, '2026-11-13T09:44:35Z'),
  ];
}

it('x2e: the parent handoff block does not say 完成 while the colleague still works, live and after reload', () => {
  for (const messages of projectBoth(handoffWhileColleagueWorksEvents())) {
    const model = timelineFor(messages);
    const handoff = model.cells.find(cell => cell.kind === 'active_run');
    if (handoff?.kind !== 'active_run') throw new Error('missing handoff run cell');
    const { container, unmount } = render(<RunDisclosureBlock timeline={handoff.timeline} />);
    expect(container.querySelector('.run-disclosure-title')?.textContent).not.toBe('完成');
    // X2 r2 item 1: the label is presentation only — the model status stays
    // the parent run's own terminal status.
    expect(handoff.timeline.status).toBe('done');
    expect(container.querySelector('[data-testid="run-disclosure-block"]')?.getAttribute('data-status')).toBe('done');
    unmount();
  }
  // The conversation header follows the same presentation rule for the
  // latest turn (X2 r2 item 1); the sidebar keeps the server status.
  for (const messages of projectBoth(handoffWhileColleagueWorksEvents())) {
    const model = timelineFor(messages);
    expect(model.header.status).not.toBe('complete');
  }
});

it('x2e: once the colleague finishes, the handoff block settles (control)', () => {
  const events = [...handoffWhileColleagueWorksEvents(),
    compatibilityEnvelope(11, 'child_session', '同事已完成这项工作。', { reason: 'delegation_terminal', status: 'completed', runtime_task_id: 'live-task', child_session_id: 'colleague-session', to_agent: 'colleague', to_agent_name: '[E2E] 第十五轮助手' }, '2026-11-13T09:46:36Z')];
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    const handoff = model.cells.find(cell => cell.kind === 'active_run');
    if (handoff?.kind !== 'active_run') throw new Error('missing handoff run cell');
    expect(handoff.timeline.status).toBe('done');
    // X2 r3: the control also pins the label and the conversation header.
    expect(handoff.timeline.delegationInFlight).toBeUndefined();
    const { container, unmount } = render(<RunDisclosureBlock timeline={handoff.timeline} />);
    expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
    unmount();
    expect(model.header.status).toBe('complete');
  }
});

it('x2e: an untyped delegation row never sets the running label on a done block', () => {
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '请同事给我三条建议', '2026-11-13T09:44:27Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:44:27.3Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:44:27.6Z'),
    assistantEvent(4, 'assistant_reasoning_summary', 'snapshot', 'run-x2', 'reason-1', { content: '交给同事。' }, '2026-11-13T09:44:29Z'),
    // An untyped started notification (no runtime_task_id) and an untyped
    // in_progress summary (no task_id): neither can be keyed by the terminal
    // rewrite, so neither may set the in-flight label (X2 r3 item 1).
    ...toolCallPair(5, 'run-x2', 'delegate_to_agent', { agent_name: '同事', task: '三条建议' }, { member_summary: { kind: 'delegation', state: 'in_progress' } }, '2026-11-13T09:44:33Z'),
    compatibilityEnvelope(7, 'runtime_action_started', '已委派给 同事，后台执行中。', { status: 'running', action_kind: 'a2a_delegation', target_agent_name: '同事' }, '2026-11-13T09:44:34Z'),
    assistantEvent(8, 'assistant_final', 'completed', 'run-x2', 'final-1', { content: '已请同事处理。' }, '2026-11-13T09:44:34.8Z'),
    runLifecycle(9, 'run-x2', 'completed', '2026-11-13T09:44:34.9Z'),
  ];
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    const run = model.cells.find(cell => cell.kind === 'active_run' && cell.runId === 'run-x2')
      || model.cells.find(cell => cell.kind === 'active_run');
    if (run?.kind !== 'active_run') throw new Error('missing run cell');
    expect(run.timeline.status).toBe('done');
    expect(run.timeline.delegationInFlight).toBeUndefined();
    const { container, unmount } = render(<RunDisclosureBlock timeline={run.timeline} />);
    expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
    unmount();
  }
});

it('x2e: a group session keeps the parent block and header on their true status (the receipt can never arrive)', () => {
  // Group history holds local V1 rows without a remote ref for every reader
  // (shared_group_history), so the colleague's terminal receipt never reaches
  // this session — the in-flight label must not apply there.
  for (const messages of projectBoth(handoffWhileColleagueWorksEvents())) {
    const model = timelineFor(messages, null, { id: SESSION, audience_kind: 'group' });
    const handoff = model.cells.find(cell => cell.kind === 'active_run');
    if (handoff?.kind !== 'active_run') throw new Error('missing handoff run cell');
    expect(handoff.timeline.status).toBe('done');
    const { container, unmount } = render(<RunDisclosureBlock timeline={handoff.timeline} />);
    expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
    unmount();
    expect(model.header.status).toBe('complete');
  }
});

it('x2e: a reused cell cannot keep the in-flight label when the audience changes', () => {
  const messages = hydrateSessionTranscriptEvents(handoffWhileColleagueWorksEvents(), 0).messages;
  const cache = createThreadTimelineCache();
  const first = buildThreadTimelineCached({ messages, activeSession: { id: SESSION, audience_kind: 'private' },
    isWaiting: false, isStreaming: false, activeRunStatus: null }, cache);
  const handoff = first.cells.find(cell => cell.kind === 'active_run');
  if (handoff?.kind !== 'active_run') throw new Error('missing handoff cell');
  expect(handoff.timeline.delegationInFlight).toBe(true);
  expect(first.header.status).toBe('running');
  // The same message array re-projected for a group reader: the cached cell
  // must not carry the stale flag (the receipt can never arrive there).
  const second = buildThreadTimelineCached({ messages, activeSession: { id: SESSION, audience_kind: 'group' },
    isWaiting: false, isStreaming: false, activeRunStatus: null }, cache);
  const groupHandoff = second.cells.find(cell => cell.kind === 'active_run');
  if (groupHandoff?.kind !== 'active_run') throw new Error('missing group handoff cell');
  expect(groupHandoff.timeline.delegationInFlight).toBeUndefined();
  expect(second.header.status).toBe('complete');
  const { container } = render(<RunDisclosureBlock timeline={groupHandoff.timeline} />);
  expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
});

it('x2e: a second run after the handoff behaves as if there were no handoff', () => {
  const task = 'live-task';
  const events: ChatTranscriptEventPayload[] = [
    ...handoffWhileColleagueWorksEvents(),
    userInput(11, '顺便查一下今天的日期', '2026-11-13T09:45:10Z'),
    runLifecycle(12, 'run-second', 'queued', '2026-11-13T09:45:10.5Z'),
    runLifecycle(13, 'run-second', 'running', '2026-11-13T09:45:10.8Z'),
    ...toolCallPair(14, 'run-second', 'get_current_time', {}, { member_summary: { kind: 'time', state: 'done' } }, '2026-11-13T09:45:12Z'),
    assistantEvent(16, 'assistant_final', 'completed', 'run-second', 'second-final', { content: '今天是 11 月 13 日。' }, '2026-11-13T09:45:14Z'),
    runLifecycle(17, 'run-second', 'completed', '2026-11-13T09:45:14.5Z'),
  ];
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    const secondRun = model.cells.find(cell => cell.kind === 'active_run'
      && cell.runId === 'run-second' && cell.timeline.steps.length > 0);
    if (secondRun?.kind !== 'active_run') throw new Error('missing second run cell');
    // The second run's own block, the conversation header and the placeholder
    // behave exactly as they would without the earlier handoff.
    expect(secondRun.timeline.status).toBe('done');
    const { container, unmount } = render(<RunDisclosureBlock timeline={secondRun.timeline} />);
    expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
    unmount();
    expect(model.header.status).toBe('complete');
    expect(model.cells.some(cell => cell.kind === 'active_run'
      && (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
      && cell.runId === 'run-second')).toBe(false);
    // The earlier handoff block keeps its own per-block label.
    const handoff = model.cells.find(cell => cell.kind === 'active_run' && cell.runId === 'run-x2');
    if (handoff?.kind !== 'active_run') throw new Error('missing handoff cell');
    expect(handoff.timeline.status).toBe('done');
  }
});


// ---------------------------------------------------------------------------
// F. One stop row: one member stop gives one 「已停止…」 row per delegated
// task in a block.
// ---------------------------------------------------------------------------

export function memberStopEvents(): ChatTranscriptEventPayload[] {
  const task = 'stop-task';
  return [
    userInput(1, '请同事帮我写周报', '2026-11-13T09:51:14Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:51:15Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:51:15.4Z'),
    ...toolCallPair(4, 'run-x2', 'delegate_to_agent', { agent_name: '同事', task: '写周报' }, { member_summary: { kind: 'delegation', state: 'in_progress', task_id: task } }, '2026-11-13T09:51:21.5Z'),
    ...toolCallPair(6, 'run-x2', 'check_async_task', { task_id: task }, { member_summary: { kind: 'colleague_result', state: 'in_progress', task_id: task } }, '2026-11-13T09:51:22Z'),
    compatibilityEnvelope(8, 'runtime_action_started', '已委派给 同事，后台执行中。', { status: 'running', action_kind: 'a2a_delegation', runtime_task_id: task, target_agent_name: '同事' }, '2026-11-13T09:51:21.6Z'),
    compatibilityEnvelope(9, 'child_session', '已停止，同事没有完成这项工作。', { reason: 'delegation_terminal', status: 'killed', runtime_task_id: task, child_session_id: 'colleague-session', to_agent: 'colleague', to_agent_name: '同事' }, '2026-11-13T09:51:26Z'),
    compatibilityEnvelope(10, 'runtime_action_failed', '已停止，同事没有完成这项工作。', { action_kind: 'a2a_delegation', status: 'killed', runtime_task_id: task, target_agent_name: '同事' }, '2026-11-13T09:51:26.2Z'),
    runLifecycle(11, 'run-x2', 'cancelled', '2026-11-13T09:51:27.5Z'),
  ];
}

it('x2f: one member stop shows one stopped sentence per delegated task, live and after reload', () => {
  for (const messages of projectBoth(memberStopEvents())) {
    const model = timelineFor(messages);
    for (const cell of model.cells) {
      if (cell.kind !== 'active_run') continue;
      const { container, unmount } = renderBlock(cell.timeline);
      const occurrences = (container.textContent || '').split('已停止，同事没有完成这项工作。').length - 1;
      expect(occurrences, `exactly one stopped row per delegated task, got ${occurrences}:\n${container.textContent}`).toBe(1);
      unmount();
    }
  }
});

it('x2f: one stopped sentence without a child_session row, live and after reload', () => {
  const task = 'stop-task-nochild';
  const events: ChatTranscriptEventPayload[] = [
    userInput(1, '请同事帮我写周报', '2026-11-13T09:51:14Z'),
    runLifecycle(2, 'run-x2', 'queued', '2026-11-13T09:51:15Z'),
    runLifecycle(3, 'run-x2', 'running', '2026-11-13T09:51:15.4Z'),
    ...toolCallPair(4, 'run-x2', 'delegate_to_agent', { agent_name: '同事', task: '写周报' }, { member_summary: { kind: 'delegation', state: 'in_progress', task_id: task } }, '2026-11-13T09:51:21.5Z'),
    compatibilityEnvelope(6, 'runtime_action_started', '已委派给 同事，后台执行中。', { status: 'running', action_kind: 'a2a_delegation', runtime_task_id: task, target_agent_name: '同事' }, '2026-11-13T09:51:21.6Z'),
    compatibilityEnvelope(7, 'runtime_action_failed', '已停止，同事没有完成这项工作。', { action_kind: 'a2a_delegation', status: 'killed', runtime_task_id: task, target_agent_name: '同事' }, '2026-11-13T09:51:26.2Z'),
    runLifecycle(8, 'run-x2', 'cancelled', '2026-11-13T09:51:27.5Z'),
  ];
  for (const messages of projectBoth(events)) {
    const model = timelineFor(messages);
    for (const cell of model.cells) {
      if (cell.kind !== 'active_run') continue;
      const { container, unmount } = renderBlock(cell.timeline);
      const occurrences = (container.textContent || '').split('已停止，同事没有完成这项工作。').length - 1;
      expect(occurrences, `exactly one stopped row, got ${occurrences}:\n${container.textContent}`).toBe(1);
      unmount();
    }
  }
});


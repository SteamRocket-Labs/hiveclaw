// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import type { ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { projectCanonicalTranscriptSnapshot } from '../agent-detail/sessionTranscriptHydration';
import { buildThreadTimeline } from './timelineModel';
import type { SessionCompatibilityEvent, SessionEventV2 } from './sessionEventStore';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (_key: string, fallback: string, values?: Record<string, unknown>) => Object.entries(values || {})
    .reduce((text, [key, value]) => text.replace(`{{${key}}}`, String(value)), fallback),
}) }));
afterEach(() => { cleanup(); window.sessionStorage.clear(); vi.useRealTimers(); });

const at = (seconds: number) => new Date(Date.UTC(2026, 10, 10, 0, 0, seconds)).toISOString();
type Event = SessionEventV2 | SessionCompatibilityEvent;
function event(sequence: number, kind: string, lifecycle: string, seconds: number,
  runId: string | null = 'run-1', payload: Record<string, unknown> = {}, itemId?: string): SessionEventV2 {
  const round = ['assistant_text', 'assistant_final', 'assistant_commentary', 'tool_call', 'tool_result'].includes(kind);
  return { schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant', item_id: itemId || (kind === 'run' ? runId! : `item-${sequence}`), item_kind: kind,
    kind: `${kind}.${lifecycle}`, lifecycle, payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: runId ? { level: round ? 'round' : 'run', session_id: 'session', thread_id: 'session',
      turn_id: `turn-${runId}`, run_id: runId, ...(round ? { round_id: `round-${runId}` } : {}) }
      : { level: 'session', session_id: 'session', thread_id: 'session' },
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: at(seconds), persisted_at: at(seconds) } as SessionEventV2;
}
const user = (seq = 1, seconds = 0) => event(seq, 'human_input', 'accepted', seconds, null,
  { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Write the report.' }] });
const work = (seq: number, seconds: number, runId = 'run-1') => event(seq, 'assistant_commentary', 'completed',
  seconds, runId, { content: 'Preparing the report.' });
const final = (seq: number, seconds: number, runId = 'run-1') => event(seq, 'assistant_final', 'completed', seconds,
  runId, { content: 'The report is ready.' });

it('R15b freezes the colleague clock at its actual execution rather than admission', () => {
  const model = build([user(), event(2, 'run', 'running', 10), work(3, 11),
    event(4, 'run', 'failed', 601, 'run-1', { task_type: 'delegation', started_at: at(10), completed_at: at(601),
      failure_code: 'tool_budget', message: '这次用完了用量预算，没有完成。' })]);
  expect(runs(model)[0].timeline).toMatchObject({ status: 'failed', startedAt: at(10), durationMs: 591_000, durationIsFinal: true });
});

it('R15b leaves duration unknown when the original run has no start time', () => {
  const model = build([user(), work(2, 10), event(3, 'run', 'failed', 601, 'run-1', {
    task_type: 'delegation', started_at: null, failure_code: 'tool_budget', message: '没有完成。' })]);
  expect(runs(model)[0].timeline.status).toBe('failed');
  expect(runs(model)[0].timeline.durationMs).toBeUndefined();
});

it('R15b presents one failure when the child has both legacy reply and canonical terminal evidence', () => {
  const reason = '这次用完了用量预算，没有完成。';
  const legacy = compatibility(4, 591, 'assistant_message', 'run-1');
  legacy.payload = { content: reason, legacy_run_id: 'run-1',
    metadata: { runtime_task_id: 'run-1', status: 'error', failed: true } };
  const events = [user(), event(2, 'run', 'running', 0), work(3, 1), legacy,
    event(5, 'run', 'failed', 591, 'run-1', { task_type: 'delegation', started_at: at(0),
      failure_code: 'tool_budget', message: reason })];
  const messages = hydrateSessionTranscriptEvents(events as unknown as ChatTranscriptEventPayload[]).messages;
  expect(messages.filter(message => message.content === reason)).toHaveLength(1);
  expect(messages.find(message => message.content === reason)?.threadItem?.item_type).toBe('error');
  expect(runs(build(events))[0].timeline.status).toBe('failed');
});
function compatibility(seq: number, seconds: number, type: string, runId?: string): SessionCompatibilityEvent {
  return { schema: 'hive.session_event_compatibility', schema_version: 1, compatibility_status: 'needs_reconciliation',
    event_id: `event-${seq}`, sequence: seq, reason: 'legacy_event_scope_unprovable', session_id: 'session', tenant_id: 'tenant',
    legacy_event_type: type, occurred_at: at(seconds), payload: { content: '', legacy_run_id: runId,
      metadata: { ...(runId ? { runtime_task_id: runId } : {}), status: 'completed' } } };
}
function build(events: Event[], extra: Partial<Parameters<typeof buildThreadTimeline>[0]> = {}) {
  return buildThreadTimeline({ messages: hydrateSessionTranscriptEvents(events as unknown as ChatTranscriptEventPayload[]).messages,
    activeSession: { id: 'session' }, isWaiting: false, isStreaming: false, runtimePhase: 'idle', ...extra });
}
const runs = (model: ReturnType<typeof build>) => model.cells.filter(cell => cell.kind === 'active_run');

it('does not publish partial terminal totals while older transcript pages are loading', () => {
  const events = [user(), event(2, 'run', 'queued', 0), ...Array.from({ length: 69 }, (_, i) => work(i + 3, i + 1)),
    event(72, 'run', 'cancelled', 897)];
  const project = (snapshot: Event[]) => projectCanonicalTranscriptSnapshot({ existing: [],
    snapshot: snapshot as unknown as ChatTranscriptEventPayload[], session: { id: 'session' } as any,
    parseMessage: message => message }).messages;
  const partial = runs(build([], { messages: project(events.slice(-52)) }))[0].timeline;
  expect(partial).toMatchObject({ status: 'cancelled', steps: [] });
  expect(partial.durationMs).toBeUndefined();
  expect(partial.summary).toBeUndefined();
  const complete = runs(build([], { messages: project(events) }))[0].timeline;
  expect(complete.steps).toHaveLength(69);
  expect(complete.durationMs).toBe(897_000);
  expect(complete).toEqual(runs(build(events))[0].timeline);
});

it('keeps a completed run while older transcript pages are loading without publishing partial totals', () => {
  const events = [user(), event(2, 'run', 'queued', 0), ...Array.from({ length: 69 }, (_, i) => work(i + 3, i + 1)),
    event(72, 'run', 'completed', 897)];
  const project = (snapshot: Event[]) => projectCanonicalTranscriptSnapshot({ existing: [],
    snapshot: snapshot as unknown as ChatTranscriptEventPayload[], session: { id: 'session' } as any,
    parseMessage: message => message }).messages;
  const partialRuns = runs(build([], { messages: project(events.slice(-52)) }));
  expect(partialRuns).toHaveLength(1);
  const partial = partialRuns[0].timeline;
  expect(partial).toMatchObject({ status: 'done', steps: [] });
  expect(partial.startedAt).toBeUndefined();
  expect(partial.durationMs).toBeUndefined();
  expect(partial.summary).toBeUndefined();
  // X2 r2 D: a mid-load block renders neither a status, a count nor a clock —
  // the first rendered status of a block is its final status — so the partial
  // page publishes no block at all until the page that completes the run.
  const view = render(<RunDisclosureBlock timeline={partial} />);
  expect(view.queryAllByTestId('run-disclosure-block')).toHaveLength(0);
  const complete = runs(build([], { messages: project(events) }))[0].timeline;
  expect(complete.steps).toHaveLength(69);
  expect(complete.durationMs).toBe(897_000);
  expect(complete).toEqual(runs(build(events))[0].timeline);
});

it('keeps the hello block when the newest transcript page starts inside its run', () => {
  const events = [user(), event(2, 'run', 'queued', 0),
    event(3, 'assistant_text', 'completed', 24, 'run-1', { content: 'Hello.' }, 'hello-text'),
    event(4, 'assistant_final', 'completed', 25, 'run-1',
      { source_blocks: [{ item_id: 'hello-text', block_index: 0, content_hash: 'hello-hash' }] }),
    event(5, 'run', 'completed', 26)];
  const project = (snapshot: Event[]) => projectCanonicalTranscriptSnapshot({ existing: [],
    snapshot: snapshot as unknown as ChatTranscriptEventPayload[], session: { id: 'session' } as any,
    parseMessage: message => message }).messages;
  const partial = build([], { messages: project(events.slice(-3)) });
  expect(runs(partial)).toHaveLength(1);
  expect(runs(partial)[0].timeline).toMatchObject({ status: 'done', steps: [] });
  expect(runs(partial)[0].timeline.startedAt).toBeUndefined();
  expect(runs(partial)[0].timeline.durationMs).toBeUndefined();
  expect(runs(partial)[0].timeline.summary).toBeUndefined();
  expect(partial.cells.filter(cell => cell.kind === 'assistant_final')).toHaveLength(1);
  // X2 r2 D: same as above — no provisional block header on a partial page.
  const view = render(<RunDisclosureBlock timeline={runs(partial)[0].timeline} />);
  expect(view.queryAllByTestId('run-disclosure-block')).toHaveLength(0);
  const complete = build([], { messages: project(events) });
  expect(runs(complete)).toHaveLength(1);
  expect(runs(complete)[0].timeline.steps.map(step => step.id)).toEqual(['hello-text']);
  expect(runs(complete)[0].timeline.durationMs).toBe(26_000);
  expect(complete.cells.filter(cell => cell.kind === 'assistant_final')).toHaveLength(1);
});

// Reduced sequences of the run8 observations: late activation adds 69 -> 71,
// 74 -> 75 and 92 -> 94 steps without any new run.
it.each([[69, 2], [74, 1], [92, 2]])('freezes %i steps across %i late compatibility events', (count, late) => {
  const prefix = [user(), event(2, 'run', 'queued', 0),
    ...Array.from({ length: count }, (_, i) => work(i + 3, i + 1)),
    event(count + 3, 'run', 'cancelled', 897)];
  const before = runs(build(prefix))[0].timeline;
  const model = build([...prefix, ...Array.from({ length: late }, (_, i) =>
    compatibility(count + 4 + i, 1000 + i, 'tool_group_activation'))]);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].timeline).toEqual(before);
  expect(runs(model)[0].timeline.steps).toHaveLength(count);
  expect(model.cells.filter(cell => cell.kind === 'boundary' && cell.message.eventType === 'tool_group_activation'))
    .toHaveLength(late);
});

it('keeps late tool completion outside the frozen tool projection', () => {
  const call = event(3, 'tool_call', 'started', 1, 'run-1', { tool_name: 'read_file', arguments: { path: 'report.md' } }, 'call');
  const prefix = [user(), event(2, 'run', 'queued', 0), call, event(4, 'run', 'cancelled', 10)];
  const result = { ...event(5, 'tool_result', 'completed', 20, 'run-1', { content: 'Late file contents' }, 'result'),
    parent_item_id: 'call' };
  expect(runs(build([...prefix, result]))[0].timeline).toEqual(runs(build(prefix))[0].timeline);
  expect(build([...prefix, result]).cells.some(cell => cell.kind === 'boundary' && cell.message.sessionItem?.id === 'result')).toBe(true);
});

it('freezes a failed run at its runtime_failure witness before a later run.failed event', () => {
  const prefix = [user(), event(2, 'run', 'queued', 0), work(3, 1),
    event(4, 'runtime_failure', 'recorded', 10, 'run-1', { failure_code: 'quota_exhausted', retryable: true })];
  const before = runs(build(prefix))[0].timeline;
  expect(before).toMatchObject({ status: 'failed', durationMs: 10_000, durationIsFinal: true });
  const model = build([...prefix, work(5, 20), event(6, 'run', 'failed', 21)]);
  expect(runs(model)[0].timeline).toEqual(before);
});

it('cannot add a step by completing an existing empty progress call after stop', () => {
  const prefix = [user(), event(2, 'run', 'queued', 0),
    event(3, 'tool_call', 'started', 1, 'run-1', { tool_name: 'report_progress', arguments: {} }, 'progress'),
    event(4, 'run', 'cancelled', 10)];
  const before = runs(build(prefix))[0].timeline;
  const model = build([...prefix, event(5, 'tool_call', 'completed', 20, 'run-1',
    { tool_name: 'report_progress', arguments: { message: 'Late update.' } }, 'progress')]);
  expect(runs(model)[0].timeline).toEqual(before);
  expect(runs(model)[0].timeline.steps).toHaveLength(0);
});

it('still counts compatibility activity belonging to a newly started run', () => {
  const model = build([user(), event(2, 'run', 'queued', 0), work(3, 1), event(4, 'run', 'cancelled', 2),
    user(5, 3), event(6, 'run', 'queued', 4, 'run-2'), compatibility(7, 5, 'tool_group_activation')],
  { isWaiting: true, runtimePhase: 'thinking', activeRunId: 'run-2', activeRunStatus: 'running' });
  expect(runs(model).map(cell => [cell.runId, cell.timeline.status, cell.timeline.steps.length]))
    .toEqual([['run-1', 'cancelled', 1], ['run-2', 'running', 1]]);
});

it('does not hide a counted text step when a zero-copy final arrives after terminal', () => {
  const prefix = [user(), event(2, 'run', 'queued', 0),
    event(3, 'assistant_text', 'completed', 2, 'run-1', { content: 'The report is ready.' }, 'text'),
    event(4, 'run', 'completed', 3)];
  const model = build([...prefix, event(5, 'assistant_final', 'completed', 4, 'run-1',
    { source_blocks: [{ item_id: 'text', block_index: 0, content_hash: 'text-hash' }] })]);
  expect(runs(model)[0].timeline.steps.map(step => step.id)).toEqual(['text']);
  expect(model.cells.filter(cell => cell.kind === 'assistant_final')).toHaveLength(1);
});

it('keeps two runs distinct even when their work arrives before either terminal boundary', () => {
  const model = build([user(), event(2, 'run', 'queued', 0), work(3, 1), work(4, 2),
    ...[6, 7, 8, 9].map((seconds, i) => work(5 + i, seconds, 'run-2')),
    event(9, 'run', 'completed', 46), event(10, 'run', 'completed', 46, 'run-2')]);
  expect(runs(model).map(cell => [cell.runId, cell.timeline.steps.length, cell.timeline.durationMs]))
    .toEqual([['run-1', 2, 46_000], ['run-2', 4, 40_000]]);
});

it('uses the terminal clock instead of freezing at the earlier reply clock', () => {
  const prefix = [user(), work(2, 5), final(3, 333)];
  const live = build(prefix, { isWaiting: true, runtimePhase: 'responding', activeRunId: 'run-1', activeRunStatus: 'running' });
  expect(runs(live)[0].timeline.status).toBe('running');
  const terminal = [...prefix, event(4, 'run', 'completed', 334)];
  const before = runs(build(terminal, { sessionWorkbench: { runtime_tasks: [{ id: 'run-1', status: 'completed',
    started_at: at(0), completed_at: at(335) }] } as any }))[0].timeline;
  expect(before).toMatchObject({ durationMs: 334_000, durationIsFinal: true });
  expect(runs(build(terminal))[0].timeline).toEqual(before);
});

it('does not freeze at the reply time while the run terminal event is still in flight', () => {
  const prefix = [user(), work(2, 5), final(3, 333)];
  const model = build(prefix, { runtimePhase: 'done' });
  expect(model.header.status).toBe('running');
  expect(runs(model)[0].timeline.status).toBe('running');
  const settled = build([...prefix, event(4, 'run', 'completed', 334)]);
  expect(runs(settled)[0].timeline).toMatchObject({ status: 'done', durationMs: 334_000, durationIsFinal: true });
});

it('keeps a running clock monotone when the canonical queued time replaces the optimistic start', () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date(at(68)));
  const pending = build([], { messages: [{ id: 'sent', role: 'user', content: 'Write the report.', timestamp: at(0) }],
    isWaiting: true, runtimePhase: 'starting' });
  const view = render(<RunDisclosureBlock timeline={runs(pending)[0].timeline} presentationKey="session:pending" />);
  expect(screen.getByText('1m 8s')).toBeTruthy();
  const running = build([user(), event(2, 'run', 'queued', 5), work(3, 6)], {
    isWaiting: true, runtimePhase: 'thinking', activeRunId: 'run-1', activeRunStatus: 'running',
    sessionWorkbench: { runtime_tasks: [{ id: 'run-1', status: 'running', started_at: at(5) }] } as any });
  view.rerender(<RunDisclosureBlock timeline={runs(running)[0].timeline} presentationKey="session:run-1" />);
  expect(screen.getByText('1m 8s')).toBeTruthy();
});

it('does not show completion before the reply has arrived', () => {
  const prefix = [user(), event(2, 'run', 'queued', 0), work(3, 1), event(4, 'run', 'completed', 2)];
  const pending = build(prefix, { runtimePhase: 'thinking', isWaiting: true, activeRunStatus: 'running', activeRunId: 'run-1' });
  expect(pending.header.status).toBe('running');
  expect(runs(pending)[0].timeline.status).toBe('running');
  const delivered = build([...prefix, final(5, 3)], { runtimePhase: 'done', terminalTranscriptPending: true });
  expect(delivered.header.status).toBe('complete');
  expect(runs(delivered)[0].timeline.status).toBe('done');
  expect(delivered.cells.filter(cell => cell.kind === 'assistant_final')).toHaveLength(1);
});

it('keeps the hello block after a reply and a fresh hydration', () => {
  const events = [user(), event(2, 'run', 'queued', 0),
    event(3, 'assistant_text', 'completed', 24, 'run-1', { content: 'Hello.' }, 'hello-text'),
    event(4, 'assistant_final', 'completed', 25, 'run-1',
      { source_blocks: [{ item_id: 'hello-text', block_index: 0, content_hash: 'hello-hash' }] }),
    event(5, 'run', 'completed', 26)];
  for (const runtimePhase of ['done', 'idle']) {
    const model = build(events, { runtimePhase });
    expect(runs(model)).toHaveLength(1);
    expect(runs(model)[0].timeline).toMatchObject({ status: 'done', durationMs: 26_000 });
    expect(runs(model)[0].timeline.steps.map(step => step.id)).toEqual(['hello-text']);
    expect(model.cells.filter(cell => cell.kind === 'assistant_final')).toHaveLength(1);
    const view = render(<RunDisclosureBlock timeline={runs(model)[0].timeline} />);
    expect(view.getAllByTestId('run-disclosure-block')).toHaveLength(1);
    view.unmount();
  }
});

it.each([false, true])('never fails an optimistic input before the run has started (waiting: %s)', isWaiting => {
  const events = [user(), event(2, 'run', 'queued', 0), work(3, 1), event(4, 'run', 'cancelled', 5), user(5, 10)];
  const model = build(events, { isWaiting, runtimePhase: 'cancelled' });
  expect(model.header.status).toBe('running');
  expect(runs(model).at(-1)?.timeline.status).toBe('running');
});

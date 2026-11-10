// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import type { ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import type { SessionCompatibilityEvent, SessionEventV2 } from './sessionEventStore';
import { buildThreadTimeline } from './timelineModel';
import { normalizeThreadItemPayload } from './threadItemReducer';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_: string, fallback: string) => fallback }) }));
afterEach(() => { cleanup(); window.sessionStorage.clear(); vi.useRealTimers(); });

const at = (minutes: number) => new Date(Date.UTC(2026, 10, 9, 0, minutes)).toISOString();
function event(sequence: number, kind: string, lifecycle: string, minutes: number,
  runId: string | null = 'run-1', payload: Record<string, unknown> = {}): SessionEventV2 {
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant', item_id: kind === 'run' ? runId! : `item-${sequence}`, item_kind: kind,
    kind: `${kind}.${lifecycle}`, lifecycle, payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: runId ? { level: ['assistant_commentary', 'assistant_text', 'tool_call', 'tool_result', 'event'].includes(kind) ? 'round' : 'run', session_id: 'session', thread_id: 'session',
      turn_id: `turn-${runId}`, run_id: runId, ...(['assistant_commentary', 'assistant_text', 'tool_call', 'tool_result', 'event'].includes(kind) ? { round_id: `round-${runId}` } : {}) }
      : { level: 'session', session_id: 'session', thread_id: 'session' },
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    ...(runId ? { run_id: runId } : {}), occurred_at: at(minutes), persisted_at: at(minutes),
  } as SessionEventV2;
}
const user = (sequence: number, minutes = 0) => event(sequence, 'human_input', 'accepted', minutes, null,
  { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Write the report.' }] });
const work = (sequence: number, minutes = 1, runId = 'run-1') => event(sequence,
  'assistant_commentary', 'completed', minutes, runId, { phase: 'commentary', content: 'Preparing the report.' });
const messages = (events: Array<SessionEventV2 | SessionCompatibilityEvent>) =>
  hydrateSessionTranscriptEvents(events as unknown as ChatTranscriptEventPayload[]).messages;
function build(events: Array<SessionEventV2 | SessionCompatibilityEvent>, extra: Record<string, unknown> = {}) {
  return buildThreadTimeline({ messages: messages(events), activeSession: { id: 'session' },
    isWaiting: false, isStreaming: false, runtimePhase: 'idle', ...extra });
}
const runs = (model: ReturnType<typeof build>) => model.cells.filter(cell => cell.kind === 'active_run');
const initial = [user(1), work(2), event(3, 'run', 'cancelled', 5)];

it('keeps manual session compaction outside the stopped run through transcript hydration', () => {
  const before = runs(build(initial))[0].timeline;
  // Manual /compact is session-level compatibility evidence; canonical
  // context_compaction is reserved for an automatic run/round boundary.
  const compact = { schema: 'hive.session_event_compatibility', schema_version: 1,
    compatibility_status: 'needs_reconciliation', reason: 'legacy_event_scope_unprovable',
    event_id: 'compact-4', sequence: 4, session_id: 'session', tenant_id: 'tenant',
    legacy_event_type: 'session_compact', legacy_item_type: 'context_compaction', legacy_item_status: 'succeeded',
    payload: { content: 'Summary', metadata: { manual: true, command: 'compact' } }, occurred_at: at(38) } as SessionCompatibilityEvent;
  const model = build([...initial, compact]);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].timeline).toEqual(before);
  expect(model.cells.some(cell => cell.kind === 'boundary' && cell.message.eventType === 'session_compact')).toBe(true);
});

it('uses terminal timestamps rather than late step times or a stale running poll', () => {
  const events = [user(1), work(2), event(3, 'run', 'completed', 5), work(4, 13)];
  const model = build(events, { activeRunId: 'run-1', activeRunStatus: 'running', runtimePhase: 'thinking', isWaiting: true });
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].timeline).toMatchObject({ status: 'done', completedAt: at(5), durationMs: 300_000 });
  expect(model.header.status).toBe('complete');
  expect(runs(model)[0].timeline).toEqual(runs(build(events.slice(0, 3)))[0].timeline);
  expect(model.cells.some(cell => cell.kind === 'boundary' && cell.message.content === 'Preparing the report.')).toBe(true);
  const delivery = { id: 'late-file', role: 'tool_call' as const, content: '', toolName: 'write_file',
    toolStatus: 'done' as const, eventRuntimeTaskId: 'run-1', timestamp: at(14),
    // Real compatibility deliveries retain their transcript sequence. F1
    // orders terminal boundaries by that sequence, never by wall time.
    threadItem: normalizeThreadItemPayload({ id: 'late-file', sequence: 5, eventType: 'tool_call',
      tool_name: 'write_file', runtime_task_id: 'run-1', created_at: at(14) })!,
    artifacts: [{ id: 'report', name: 'report.md', path: 'workspace/report.md' }] };
  const withDelivery = build(events, { messages: [...messages(events), delivery] });
  expect(runs(withDelivery)[0].timeline).toEqual(runs(model)[0].timeline);
  expect(withDelivery.cells.some(cell => cell.kind === 'boundary' && cell.message === delivery)).toBe(true);
});

it('keeps a late delegated result after stop without opening a phantom run', () => {
  const model = build([...initial, event(4, 'subagent', 'started', 2, 'run-1', { content: 'Delegating' }),
    event(5, 'subagent', 'failed', 18, 'run-1', { content: 'Child failed' })],
  { runtimePhase: 'cancelled', sessionWorkbench: { runtime_tasks: [{ id: 'run-1', status: 'killed', started_at: at(0), completed_at: at(5) }] } });
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].timeline).toMatchObject({ status: 'cancelled', durationMs: 300_000 });
  expect(model.cells.some(cell => cell.kind === 'boundary' && cell.message.content === 'Child failed')).toBe(true);
});

it('takes a delegated session header and duration from its terminal runtime row', () => {
  const model = build([user(1), work(2)], { activeSession: { id: 'session', session_kind: 'delegation_run', read_only: true, runtime_task_id: 'run-1' },
    runtimePhase: 'thinking', sessionWorkbench: { runtime_tasks: [{ id: 'run-1', task_type: 'delegation',
      status: 'failed', started_at: at(0), completed_at: at(5) }] } });
  expect(model.header.status).toBe('failed');
  expect(runs(model)[0].timeline).toMatchObject({ status: 'failed', completedAt: at(5), durationMs: 300_000 });
  // Delegation transcripts also contain compatibility reasoning without a
  // run scope. The session's durable runtime_task_id supplies its identity.
  const legacy = { schema: 'hive.session_event_compatibility', schema_version: 1,
    compatibility_status: 'needs_reconciliation', reason: 'legacy_event_scope_unprovable',
    event_id: 'old-thinking', sequence: 1, session_id: 'session', tenant_id: 'tenant',
    legacy_event_type: 'thinking', legacy_item_type: 'reasoning', legacy_item_status: 'running',
    payload: { content: 'Working', metadata: { status: 'running' } }, occurred_at: at(1) } as SessionCompatibilityEvent;
  const historical = build([legacy], { activeSession: { id: 'session', session_kind: 'delegation_run', read_only: true, runtime_task_id: 'run-1' },
    runtimePhase: 'thinking', sessionWorkbench: { runtime_tasks: [{ id: 'run-1', task_type: 'delegation',
      status: 'failed', started_at: at(0), completed_at: at(5) }] } });
  expect(historical.header.status).toBe('failed');
  expect(runs(historical)).toHaveLength(1);
  expect(runs(historical)[0].timeline).toMatchObject({ status: 'failed', completedAt: at(5), durationMs: 300_000 });
});

it('keeps settled duration independent of another disclosure sharing the same presentation key', () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date(at(13)));
  const before = runs(build(initial))[0];
  window.sessionStorage.setItem('hive.session.observed-run-duration.v1:session:run-1', '790000');
  const view = render(<RunDisclosureBlock timeline={before.timeline} presentationKey="session:run-1" />);
  expect(view.getByTestId('run-disclosure-block').textContent).toContain('5m');
  expect(view.getByTestId('run-disclosure-block').textContent).not.toContain('13m');
  act(() => vi.advanceTimersByTime(60_000));
  expect(view.getByTestId('run-disclosure-block').textContent).toContain('5m');
  const followup = build([...initial, user(4, 14), work(5, 15, 'run-2')],
    { activeRunId: 'run-2', activeRunStatus: 'running', runtimePhase: 'thinking', isWaiting: true });
  view.rerender(<RunDisclosureBlock timeline={runs(followup)[0].timeline} presentationKey="session:run-1" />);
  expect(view.getByTestId('run-disclosure-block').textContent).toContain('5m');
  expect(runs(followup)[1].timeline.status).toBe('running');
});

it('does not merge different runs before a confirmation continuation answer', () => {
  const model = build([user(1), work(2), event(3, 'run', 'completed', 2), work(4, 3, 'run-2'),
    event(5, 'run', 'completed', 4, 'run-2')]);
  expect(runs(model).map(cell => cell.runId)).toEqual(['run-1', 'run-2']);
  expect(runs(model).map(cell => cell.timeline.durationMs)).toEqual([120_000, 60_000]);
});

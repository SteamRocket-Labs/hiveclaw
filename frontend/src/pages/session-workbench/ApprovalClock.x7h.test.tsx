// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { normalizeRuntimeEventMessage, uiForPhase, type AgentChatMessage, type ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import { createSessionEventStore, type SessionEventV2 } from './sessionEventStore';
import { normalizeThreadItemPayload } from './threadItemReducer';
import { buildThreadTimeline } from './timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => { await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, initImmediate: false }); });
afterEach(() => { cleanup(); vi.useRealTimers(); window.sessionStorage.clear(); });

const runId = '11111111-1111-4111-8111-111111111111';
const session = { id: 'session-x7h', user_id: 'member-x7h' };
const key = `agent-x7h:${session.id}`;
type Mode = 'live' | 'reload';
type Frame = [receivedAt: string, sequence: number, kind: string, lifecycle: string, occurredAt: string];

// Run24 step8 ws-step8.ndjson: exact arrival order, sequence and event times
// from seq115 to the terminal tail. Business content and identities are replaced.
// Earlier phase frames were discarded by the observed reload/navigation while
// waiting. The resumed page starts with the seq120 hydration checkpoint.
const frames: Frame[] = [
  ['2026-11-17T00:08:35.036Z', 115, 'tool_call', 'started', '2026-11-17T00:08:34.617676+00:00'],
  ['2026-11-17T00:08:39.570Z', 116, 'permission', '', '2026-11-17T00:08:38.228095+00:00'],
  ['2026-11-17T00:08:40.946Z', 117, 'tool_permission', 'waiting', '2026-11-17T00:08:39.829856+00:00'],
  ['2026-11-17T00:08:40.956Z', 118, 'tool_call', 'waiting', '2026-11-17T00:08:39.829869+00:00'],
  ['2026-11-17T00:08:40.974Z', 119, 'run', 'waiting', '2026-11-17T00:08:39.829881+00:00'],
  ['2026-11-17T00:08:40.988Z', 120, 'turn', 'waiting', '2026-11-17T00:08:39.829911+00:00'],
  ['2026-11-17T00:17:21.471Z', 121, 'control_input', 'accepted', '2026-11-17T00:17:20.145979+00:00'],
  ['2026-11-17T00:17:21.475Z', 122, 'control_input', 'applied', '2026-11-17T00:17:20.251058+00:00'],
  ['2026-11-17T00:17:21.475Z', 123, 'tool_permission', 'completed', '2026-11-17T00:17:20.356318+00:00'],
  ['2026-11-17T00:17:28.148Z', 124, 'tool_call', 'progress', '2026-11-17T00:17:26.796394+00:00'],
  ['2026-11-17T00:17:28.171Z', 125, 'tool_call', 'completed', '2026-11-17T00:17:27.533551+00:00'],
  ['2026-11-17T00:17:28.186Z', 126, 'tool_result', 'completed', '2026-11-17T00:17:27.533579+00:00'],
  ['2026-11-17T00:17:28.196Z', 127, 'run', 'queued', '2026-11-17T00:17:27.808263+00:00'],
  ['2026-11-17T00:17:32.206Z', 0, 'phase', '', ''],
  ['2026-11-17T00:17:34.827Z', 128, 'model_route', '', '2026-11-17T00:17:33.377602+00:00'],
  ['2026-11-17T00:17:42.959Z', 129, 'memory_context_degraded', '', '2026-11-17T00:17:42.019408+00:00'],
  ['2026-11-17T00:17:47.160Z', 130, 'context_window_status', '', '2026-11-17T00:17:45.697600+00:00'],
  ['2026-11-17T00:17:47.173Z', 131, 'compaction_skipped', '', '2026-11-17T00:17:45.969019+00:00'],
  ['2026-11-17T00:17:54.879Z', 132, 'result_commit', 'prepared', '2026-11-17T00:17:53.615457+00:00'],
  ['2026-11-17T00:17:56.784Z', 0, 'phase', '', ''],
  ['2026-11-17T00:17:57.769Z', 133, 'assistant_text', 'delta', '2026-11-17T00:17:56.941597+00:00'],
  ['2026-11-17T00:17:57.780Z', 134, 'result_commit', 'streaming', '2026-11-17T00:17:56.941620+00:00'],
  ['2026-11-17T00:17:57.788Z', 135, 'assistant_text', 'snapshot', '2026-11-17T00:17:57.135268+00:00'],
  ['2026-11-17T00:17:57.792Z', 136, 'assistant_text', 'completed', '2026-11-17T00:17:57.135288+00:00'],
  ['2026-11-17T00:17:57.806Z', 137, 'result_commit', 'sealed', '2026-11-17T00:17:57.135309+00:00'],
  ['2026-11-17T00:17:57.835Z', 138, 'result_commit', 'round_committed', '2026-11-17T00:17:57.487561+00:00'],
  ['2026-11-17T00:17:59.175Z', 139, 'provider_call_ledger', '', '2026-11-17T00:17:57.738639+00:00'],
  ['2026-11-17T00:18:00.127Z', 0, 'done', '', ''],
  ['2026-11-17T00:18:00.149Z', 0, 'phase', '', ''],
  ['2026-11-17T00:18:00.685Z', 140, 'run_outcome', 'prepared', '2026-11-17T00:17:59.556786+00:00'],
  ['2026-11-17T00:18:00.722Z', 141, 'run_outcome', 'sealed', '2026-11-17T00:17:59.556819+00:00'],
  ['2026-11-17T00:18:00.760Z', 142, 'assistant_final', 'completed', '2026-11-17T00:17:59.777153+00:00'],
  ['2026-11-17T00:18:00.770Z', 143, 'run', 'completed', '2026-11-17T00:17:59.777172+00:00'],
  ['2026-11-17T00:18:00.774Z', 144, 'turn', 'completed', '2026-11-17T00:17:59.777193+00:00'],
  ['2026-11-17T00:18:00.780Z', 145, 'run_outcome', 'terminal_committed', '2026-11-17T00:17:59.777205+00:00'],
];

const request = { permission_request_id: 'permission-x7h', tool_name: 'plaza_create_post',
  arguments: {}, session_id: session.id, runtime_task_id: runId, requested_by_user_id: session.user_id };
function canonical(sequence: number, kind: string, lifecycle: string, at: string,
  payload: Record<string, unknown>, itemId = kind): SessionEventV2 {
  const level = kind === 'human_input' ? 'session' : kind === 'turn' ? 'turn'
    : ['run', 'control_input', 'run_outcome'].includes(kind) ? 'run' : 'round';
  return { schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant-x7h', item_id: itemId, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: { level, session_id: session.id, thread_id: session.id,
      ...(level !== 'session' ? { turn_id: 'turn-x7h' } : {}),
      ...(['run', 'round'].includes(level) ? { run_id: runId } : {}),
      ...(level === 'round' ? { round_id: 'round-x7h' } : {}) } as SessionEventV2['scope'],
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: at, persisted_at: at,
    ...(kind.startsWith('tool_') ? { invocation_id: 'call-x7h' } : {}) };
}
function wire([receivedAt, sequence, kind, lifecycle, at]: Frame): ChatTranscriptEventPayload {
  if (!sequence) {
    // Native live boundary adapters carry sequence 0 and no event timestamp.
    // Their arrival time is the test clock, never an invented created_at.
    return normalizeThreadItemPayload({ id: `live:${receivedAt}`, type: kind, sequence,
      run_id: runId.replaceAll('-', ''), phase: kind === 'done' ? 'done' : 'responding' }) as unknown as ChatTranscriptEventPayload;
  }
  if (!lifecycle) return { schema: 'hive.session_event_compatibility', schema_version: 1,
    compatibility_status: 'needs_reconciliation', reason: 'unmapped_legacy_kind',
    event_id: `compat-${sequence}`, sequence, legacy_event_type: kind,
    occurred_at: at, payload: { legacy_run_id: runId, metadata: kind === 'permission'
      ? { status: 'session_permission_required', permission_request_id: request.permission_request_id, permission_request: request }
      : {} } } as unknown as ChatTranscriptEventPayload;
  const payload: Record<string, unknown> = kind === 'tool_permission' && lifecycle === 'waiting'
    ? { permission_request: request } : kind === 'control_input'
      ? { kind: 'permission_response', permission_item_id: request.permission_request_id, decision: 'allow_once' }
      : kind === 'run' && lifecycle === 'completed' ? { started_at: '2026-11-17T00:06:23.243Z' }
        : lifecycle === 'waiting' ? { reason_code: 'tool_permission_required', permission_item_id: request.permission_request_id }
          : kind === 'tool_call' ? { tool_name: request.tool_name }
            : kind === 'assistant_final' ? { content: 'Done.' } : {};
  const itemId = kind === 'tool_permission' ? request.permission_request_id : kind === 'tool_call' ? 'call-x7h'
    : kind === 'run' ? runId : kind === 'result_commit' ? (sequence < 135 ? 'commit-1' : 'commit-2') : kind;
  return canonical(sequence, kind, lifecycle, at, payload, itemId) as unknown as ChatTranscriptEventPayload;
}

// The recorder starts after input acceptance. This controlled input timestamp
// matches the observed ~00:06:19.2 live anchor; it is not claimed as a raw frame.
const inputAt = '2026-11-17T00:06:19.200Z';
const input = hydrateSessionTranscriptEvents([canonical(1, 'human_input', 'accepted', inputAt,
  { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Publish.' }] }) as unknown as ChatTranscriptEventPayload]).messages;
const waitingMs = Date.parse('2026-11-17T00:17:20.145979Z') - Date.parse('2026-11-17T00:08:39.829856Z');

// state-step8.ndjson: the existing reads remain running with the same start.
// Replay their received values as inputs; no poll is created by this test.
const reads: Array<[string, string]> = [
  ['2026-11-17T00:17:28.291Z', 'running'],
  ['2026-11-17T00:17:28.577Z', 'running'],
  ['2026-11-17T00:17:28.808Z', 'running'],
  ['2026-11-17T00:17:29.601Z', 'running'],
  ['2026-11-17T00:17:30.976Z', 'running'],
  ['2026-11-17T00:17:31.864Z', 'running'],
  ['2026-11-17T00:17:35.133Z', 'running'],
  ['2026-11-17T00:17:58.112Z', 'running'],
];
function replay(mode: Mode) {
  let messages: AgentChatMessage[] = input;
  const events: ChatTranscriptEventPayload[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: { [key]: createSessionEventStore(114) },
      fullHydrationKeys: new Set([key]), replayStates: {}, compatibilityTimelines: {}, visibilityBoundaries: {},
      uiStates: { [key]: uiForPhase('awaiting_approval') }, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => runId, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, current) => [...input, ...current], setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { messages = update(messages); },
    setChatMessagesAfterQueued: (_session, update) => { messages = update(messages); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(),
    parseChatMsg: message => normalizeRuntimeEventMessage(message) || message,
  };
  return {
    push(frame: Frame) {
      const event = wire(frame);
      events.push(event);
      if (mode === 'reload') messages = [...input, ...hydrateSessionTranscriptEvents(events, 114).messages];
      else applyTranscriptToSessionRuntime(deps, 'agent-x7h', session.id, event, true);
    },
    model(status: string) {
      return buildThreadTimeline({ messages, activeSession: session, activeRunId: runId,
        activeRunStatus: status, isWaiting: status === 'suspended', isStreaming: false,
        sessionWorkbench: { runtime_tasks: [{ id: runId, status,
          started_at: '2026-11-17T00:06:23.589583+00:00' }] } as never });
    },
  };
}
function blocks(model: ReturnType<typeof buildThreadTimeline>) {
  return <>{model.cells.map(cell => cell.kind === 'active_run'
    ? <div key={cell.id}><RunDisclosureBlock timeline={cell.timeline} presentationKey={`${session.id}:${cell.runId}`} /></div>
    : null)}</>;
}
const duration = (container: HTMLElement) => container.querySelector('.run-disclosure-duration')?.textContent;
function expectedLive(at: string | number) {
  const seconds = Math.floor((new Date(at).getTime() - Date.parse(inputAt) - waitingMs) / 1000);
  return seconds % 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds / 60} 分钟`;
}

it.each(['live', 'reload'] as const)('X7h: %s preserves the run24 wait through every phase remount, rerender and tick', mode => {
  vi.useFakeTimers();
  const runtime = replay(mode);
  frames.slice(0, 6).forEach(frame => runtime.push(frame));
  vi.setSystemTime(new Date('2026-11-17T00:17:17.479Z'));
  let model = runtime.model('suspended');
  const view = render(blocks(model));
  expect(duration(view.container)).toBe('2 分 21 秒');
  act(() => { vi.advanceTimersByTime(1000); });
  expect(duration(view.container)).toBe('2 分 21 秒');
  let remounts = 0;
  let completed = false;
  const deliveries = [...frames.slice(6).map(frame => ({ at: frame[0], frame })),
    ...reads.map(([at, status]) => ({ at, status })),
    ...frames.filter(frame => frame[2] === 'phase').map(frame => ({
      at: new Date(Date.parse(frame[0]) + 1000).toISOString(), tick: true,
    }))].sort((a, b) => a.at.localeCompare(b.at));
  for (const delivery of deliveries) {
    act(() => { vi.advanceTimersByTime(Date.parse(delivery.at) - Date.now()); });
    if ('tick' in delivery) {
      expect.soft(duration(view.container), `tick at ${delivery.at}`).toBe(completed ? '2 分 56 秒' : expectedLive(delivery.at));
      continue;
    }
    const previous = view.container.querySelector('.run-disclosure');
    if ('frame' in delivery) {
      runtime.push(delivery.frame);
      completed ||= delivery.frame[1] === 143;
    }
    model = runtime.model('running');
    view.rerender(blocks(model));
    if ('frame' in delivery && delivery.frame[2] === 'phase') {
      remounts++;
      expect(view.container.querySelector('.run-disclosure')).not.toBe(previous);
      expect.soft(duration(view.container), `phase at ${delivery.at}`).toBe(expectedLive(delivery.at));
      const cell = model.cells.find(cell => cell.kind === 'active_run')!;
      expect.soft(cell.timeline.startedAt).toBe(inputAt);
      expect.soft(Date.parse(cell.timeline.completedAt!) - Date.parse(inputAt) - cell.timeline.durationMs!).toBeCloseTo(waitingMs, 0);
      view.rerender(blocks(runtime.model('running')));
      expect.soft(duration(view.container), `rerender at ${delivery.at}`).toBe(expectedLive(delivery.at));
    } else if ('status' in delivery) {
      // Reads preserve both the input anchor and the approval deduction.
      expect.soft(duration(view.container), `read at ${delivery.at}`).toBe(expectedLive(delivery.at));
    }
  }
  expect(remounts).toBe(3);
  expect(duration(view.container)).toBe('2 分 56 秒');
  view.unmount();
  window.sessionStorage.clear();
  // A real transcript reload has no transient sequence-zero socket boundaries.
  const reloaded = replay('reload');
  frames.filter(frame => frame[1] > 0).forEach(frame => reloaded.push(frame));
  const final = render(blocks(reloaded.model('completed')));
  expect(duration(final.container)).toBe('2 分 56 秒');
});

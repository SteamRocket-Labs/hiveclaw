// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { uiForPhase, type AgentChatMessage, type ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import type { SessionEventV2 } from './sessionEventStore';
import { buildThreadTimeline } from './timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => { await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, initImmediate: false }); });
afterEach(() => { cleanup(); vi.useRealTimers(); window.sessionStorage.clear(); });

const runId = '11111111-1111-4111-8111-111111111111';
const session = { id: 'session-x7d', user_id: 'member-x7d' };
const key = `agent-x7d:${session.id}`;
type Mode = 'live' | 'reload';
function event(sequence: number, kind: string, lifecycle: string, at: string,
  payload: Record<string, unknown> = {}, itemId = kind): SessionEventV2 {
  const level = kind === 'human_input' ? 'session' : kind === 'turn' ? 'turn'
    : ['run', 'control_input'].includes(kind) ? 'run' : 'round';
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant-x7d', item_id: itemId, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: { level, session_id: session.id, thread_id: session.id,
      ...(level !== 'session' ? { turn_id: 'turn-x7d' } : {}),
      ...(['run', 'round'].includes(level) ? { run_id: runId } : {}),
      ...(level === 'round' ? { round_id: 'round-x7d' } : {}) } as SessionEventV2['scope'],
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: at, persisted_at: at,
    ...(kind.startsWith('tool_') ? { invocation_id: itemId === 'result' ? 'call' : itemId } : {}),
  };
}
const request = (id: string) => ({ permission_request: { permission_request_id: id,
  tool_name: 'plaza_create_post', arguments: { content: 'Test post' }, session_id: session.id,
  runtime_task_id: runId, requested_by_user_id: session.user_id } });

// Run22 step8 api-evidence order: original seq 1,10,99,101-107,109-110,
// 111,126-128. IDs/content are synthetic; retain the observed timestamps,
// same-item updates, round/run scopes, and accepted-before-applied decision.
const run22 = [
  event(1, 'human_input', 'accepted', '2026-11-15T03:02:50.663048Z',
    { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Post to the company plaza.' }] }),
  event(2, 'run', 'queued', '2026-11-15T03:02:52.278915Z', {}, runId),
  event(3, 'tool_call', 'started', '2026-11-15T03:03:35.996279Z',
    { name: 'plaza_create_post', arguments: { content: 'Test post' } }, 'call'),
  event(4, 'tool_permission', 'waiting', '2026-11-15T03:03:37.350516Z', request('permission'), 'permission'),
  event(5, 'tool_call', 'waiting', '2026-11-15T03:03:37.350532Z', {}, 'call'),
  event(6, 'run', 'waiting', '2026-11-15T03:03:37.350541Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission' }, runId),
  event(7, 'turn', 'waiting', '2026-11-15T03:03:37.350568Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission' }),
  event(8, 'control_input', 'accepted', '2026-11-15T03:07:36.697189Z',
    { kind: 'permission_response', permission_item_id: 'permission', decision: 'allow_once' }),
  event(9, 'control_input', 'applied', '2026-11-15T03:07:36.789105Z',
    { kind: 'permission_response', permission_item_id: 'permission', decision: 'allow_once' }),
  event(10, 'tool_permission', 'completed', '2026-11-15T03:07:36.910065Z',
    { decision: 'allow_once' }, 'permission'),
  event(11, 'tool_call', 'completed', '2026-11-15T03:07:40.153301Z', {}, 'call'),
  event(12, 'tool_result', 'completed', '2026-11-15T03:07:40.153338Z', { content: 'Post published.' }, 'result'),
  event(13, 'run', 'queued', '2026-11-15T03:07:40.328015Z',
    { reason_code: 'session_permission_resolved' }, runId),
  event(14, 'assistant_final', 'completed', '2026-11-15T03:07:53.250731Z', { content: 'Published.' }),
  event(15, 'run', 'completed', '2026-11-15T03:07:53.250751Z',
    { started_at: '2026-11-15T03:02:52.653238Z' }, runId),
  event(16, 'turn', 'completed', '2026-11-15T03:07:53.250774Z'),
];

function messages(events: SessionEventV2[], mode: Mode): AgentChatMessage[] {
  const wire = events as unknown as ChatTranscriptEventPayload[];
  if (mode === 'reload') return hydrateSessionTranscriptEvents(JSON.parse(JSON.stringify(wire)), 0).messages;
  let result: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set([key]), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: { [key]: uiForPhase('idle') },
      runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => runId, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, current) => current, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { result = update(result); },
    setChatMessagesAfterQueued: (_session, update) => { result = update(result); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const item of wire) applyTranscriptToSessionRuntime(deps, 'agent-x7d', session.id, item, true);
  return result;
}
function timeline(events: SessionEventV2[], mode: Mode, status: string | null = null) {
  const model = buildThreadTimeline({ messages: messages(events, mode), activeSession: session,
    activeRunId: status ? runId.replaceAll('-', '') : null, activeRunStatus: status,
    isWaiting: Boolean(status), isStreaming: false });
  const runs = model.cells.filter(cell => cell.kind === 'active_run');
  expect(runs).toHaveLength(1);
  return runs[0].timeline;
}
const block = (events: SessionEventV2[], mode: Mode, status: string | null = null) =>
  <RunDisclosureBlock timeline={timeline(events, mode, status)} presentationKey={`${session.id}:${runId}`} />;
const duration = (container: HTMLElement) => container.querySelector('.run-disclosure-duration')?.textContent;

it.each(['live', 'reload'] as const)('X7d: %s resumes the run22 working clock and keeps final/reloaded time at 62 seconds', mode => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-15T03:03:37.500Z'));
  const view = render(block(run22.slice(0, 7), mode, 'suspended'));
  expect(duration(view.container)).toBe('47 秒');
  act(() => { vi.advanceTimersByTime(120_000); });
  expect(duration(view.container)).toBe('47 秒');

  act(() => { vi.setSystemTime(new Date('2026-11-15T03:07:41.021Z')); });
  view.rerender(block(run22.slice(0, 13), mode, 'running'));
  expect.soft(duration(view.container)).toBe('51 秒');
  act(() => { vi.advanceTimersByTime(1000); });
  expect.soft(duration(view.container)).toBe('52 秒');

  view.rerender(block(run22, mode));
  expect.soft(timeline(run22, mode)).toMatchObject({
    startedAt: '2026-11-15T03:02:52.278915Z', durationMs: 61_625, durationIsFinal: true,
  });
  expect.soft(duration(view.container)).toBe('1 分 2 秒');
  view.unmount();
  window.sessionStorage.clear();
  const reload = render(block(run22, 'reload'));
  expect.soft(duration(reload.container)).toBe('1 分 2 秒');
});

const at = (seconds: number) => new Date(Date.UTC(2026, 10, 15, 4, 0, seconds)).toISOString();
const start = () => [
  event(1, 'human_input', 'accepted', at(0), { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Publish.' }] }),
  event(2, 'run', 'queued', at(0), {}, runId),
];
function twoApprovals() {
  return [...start(),
    event(3, 'tool_permission', 'waiting', at(10), request('permission-1'), 'permission-1'),
    event(4, 'control_input', 'accepted', at(30),
      { kind: 'permission_response', permission_item_id: 'permission-1' }, 'control-1'),
    event(5, 'tool_permission', 'completed', at(31), { decision: 'allow_once' }, 'permission-1'),
    event(6, 'tool_permission', 'waiting', at(50), request('permission-2'), 'permission-2'),
    // No control item: permission completion is the existing alternate end.
    event(7, 'tool_permission', 'completed', at(90), { decision: 'allow_once' }, 'permission-2'),
    event(8, 'tool_call', 'completed', at(110), { tool_name: 'plaza_create_post' }, 'call'),
    event(9, 'run', 'completed', at(120), {}, runId),
  ];
}
it.each(['live', 'reload'] as const)('X7d: %s subtracts both approvals and freezes the second wait after the first', mode => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(at(75)));
  const events = twoApprovals();
  const view = render(block(events.slice(0, 6), mode, 'suspended'));
  expect(duration(view.container)).toBe('30 秒');
  act(() => { vi.advanceTimersByTime(5000); });
  expect(duration(view.container)).toBe('30 秒');
  view.rerender(block(events, mode));
  expect(timeline(events, mode)).toMatchObject({ startedAt: at(0), durationMs: 60_000 });
  expect(duration(view.container)).toBe('1 分钟');
});

it.each(['live', 'reload'] as const)('X7d: %s excludes an unanswered approval through the terminal stop', mode => {
  const events = [...start(),
    event(3, 'tool_permission', 'waiting', at(10), request('permission'), 'permission'),
    event(4, 'run', 'cancelled', at(100), {}, runId),
  ];
  expect(timeline(events, mode)).toMatchObject({ status: 'cancelled', durationMs: 10_000, durationIsFinal: true });
  const view = render(block(events, mode));
  expect(duration(view.container)).toBe('10 秒');
});

it.each(['live', 'reload'] as const)('X7d: %s counts work after an unanswered permission expires', mode => {
  const events = [...start(),
    event(3, 'tool_permission', 'waiting', at(10), request('permission'), 'permission'),
    event(4, 'tool_permission', 'cancelled', at(40), { reason_code: 'tool_permission_request_expired' }, 'permission'),
    event(5, 'run', 'queued', at(40), {}, runId),
    event(6, 'tool_call', 'completed', at(70), { tool_name: 'read_file' }, 'call'),
    event(7, 'tool_result', 'completed', at(70), { content: 'Read after expiry.' }, 'result'),
    event(8, 'run', 'completed', at(100), {}, runId),
  ];
  vi.useFakeTimers();
  vi.setSystemTime(new Date(at(20)));
  const view = render(block(events.slice(0, 3), mode, 'suspended'));
  expect(duration(view.container)).toBe('10 秒');
  act(() => { vi.advanceTimersByTime(20_000); });
  expect(duration(view.container)).toBe('10 秒');
  view.rerender(block(events.slice(0, 5), mode, 'running'));
  expect(duration(view.container)).toBe('10 秒');
  act(() => { vi.advanceTimersByTime(1000); });
  expect(duration(view.container)).toBe('11 秒');
  act(() => { vi.setSystemTime(new Date(at(80))); });
  view.rerender(block(events.slice(0, 7), mode, 'running'));
  expect.soft(duration(view.container)).toBe('50 秒');
  act(() => { vi.advanceTimersByTime(1000); });
  expect.soft(duration(view.container)).toBe('51 秒');
  view.rerender(block(events, mode));
  expect.soft(timeline(events, mode)).toMatchObject({ startedAt: at(0), durationMs: 70_000, durationIsFinal: true });
  expect.soft(duration(view.container)).toBe('1 分 10 秒');
  view.unmount();
  const reloaded = render(block(events, 'reload'));
  expect.soft(duration(reloaded.container)).toBe('1 分 10 秒');
});

it.each((['live', 'reload'] as const).flatMap(mode => [false, true].map(control => ({ mode, control }))))(
  'X7d: $mode ends a denied decision at its accepted control or permission end (control: $control)', ({ mode, control }) => {
    const events = [...start(), event(3, 'tool_permission', 'waiting', at(10), request('permission'), 'permission')];
    if (control) events.push(event(4, 'control_input', 'accepted', at(40),
      { kind: 'permission_response', permission_item_id: 'permission', decision: 'deny' }));
    events.push(event(events.length + 1, 'tool_permission', 'denied', at(41), { decision: 'deny' }, 'permission'),
      event(events.length + 2, 'tool_call', 'completed', at(70), { tool_name: 'read_file' }, 'call'),
      event(events.length + 3, 'run', 'completed', at(100), {}, runId));
    expect(timeline(events, mode)).toMatchObject({ durationMs: control ? 70_000 : 69_000, durationIsFinal: true });
  },
);

it.each(['live', 'reload'] as const)('X7d: %s pairs a control arriving before its permission item', mode => {
  const events = [...start(),
    event(3, 'control_input', 'accepted', at(40),
      { kind: 'permission_response', permission_item_id: 'permission', decision: 'allow_once' }),
    event(4, 'tool_permission', 'waiting', at(10), request('permission'), 'permission'),
    event(5, 'control_input', 'applied', at(41), { kind: 'permission_response', permission_item_id: 'permission' }),
    event(6, 'tool_permission', 'completed', at(42), { decision: 'allow_once' }, 'permission'),
    event(7, 'tool_call', 'completed', at(70), { tool_name: 'plaza_create_post' }, 'call'),
    event(8, 'run', 'completed', at(100), {}, runId),
  ];
  expect(timeline(events, mode)).toMatchObject({ durationMs: 70_000, durationIsFinal: true });
});

it.each(['live', 'reload'] as const)('X7d: %s never shows a larger cached wall duration after approval or completion', mode => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-15T03:07:41.021Z'));
  const storageKey = `hive.session.observed-run-duration.v1:${session.id}:${runId}`;
  window.sessionStorage.setItem(storageKey, '300000');
  const view = render(block(run22.slice(0, 13), mode, 'running'));
  expect(duration(view.container)).toBe('51 秒');
  act(() => { vi.advanceTimersByTime(1000); });
  expect(duration(view.container)).toBe('52 秒');
  view.rerender(block(run22, mode));
  expect(duration(view.container)).toBe('1 分 2 秒');
  view.unmount();
  expect(Number(window.sessionStorage.getItem(storageKey))).toBeGreaterThan(61_625);
  const reloaded = render(block(run22, 'reload'));
  expect(duration(reloaded.container)).toBe('1 分 2 秒');
});

it.each(['none', 'colleague', 'company'] as const)('X7d: a run with %s waiting retains its existing duration', kind => {
  const events = [...start(), event(3, 'tool_call', 'completed', at(1), { tool_name: 'plaza_create_post' }, 'call')];
  if (kind !== 'none') events.push(event(4, 'run', kind === 'company' ? 'completed' : 'waiting', at(10),
    { reason_code: kind === 'company' ? 'company_approval_required' : 'delegation_waiting' }, runId));
  events.push(event(events.length + 1, 'run', 'completed', at(120), {}, runId));
  for (const mode of ['live', 'reload'] as const) {
    expect(timeline(events, mode)).toMatchObject({ startedAt: at(0), durationMs: 120_000, durationIsFinal: true });
  }
});

// @vitest-environment jsdom
import React from 'react';
import { cleanup, render } from '@testing-library/react';
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
const session = { id: 'session-x7f', user_id: 'member-x7f' };
const key = `agent-x7f:${session.id}`;
type Mode = 'live' | 'reload';
function event(sequence: number, kind: string, lifecycle: string, at: string,
  payload: Record<string, unknown>, itemId = kind): SessionEventV2 {
  const level = kind === 'human_input' ? 'session' : kind === 'turn' ? 'turn'
    : kind === 'run' ? 'run' : 'round';
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant-x7f', item_id: itemId, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: { level, session_id: session.id, thread_id: session.id,
      ...(level !== 'session' ? { turn_id: 'turn-x7f' } : {}),
      ...(['run', 'round'].includes(level) ? { run_id: runId } : {}),
      ...(level === 'round' ? { round_id: 'round-x7f' } : {}) } as SessionEventV2['scope'],
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: at, persisted_at: at,
    ...(kind.startsWith('tool_') ? { invocation_id: 'call-x7f' } : {}),
  };
}

// Run23 step8 /transcript seq 1,10,122,124-127: retain observed timestamps,
// event order and scope levels; compact sequences and replace identities/content.
const run23Waiting = [
  event(1, 'human_input', 'accepted', '2026-11-16T12:24:06.645302Z',
    { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Publish a post.' }] }),
  event(2, 'run', 'queued', '2026-11-16T12:24:11.256390Z', {}, runId),
  event(3, 'tool_call', 'started', '2026-11-16T12:26:53.618313Z',
    { tool_name: 'plaza_create_post' }, 'call-x7f'),
  event(4, 'tool_permission', 'waiting', '2026-11-16T12:27:00.200886Z', {
    permission_request: { permission_request_id: 'permission-x7f', tool_name: 'plaza_create_post',
      arguments: { content: 'Test post' }, session_id: session.id, runtime_task_id: runId,
      requested_by_user_id: session.user_id, expires_at: '2026-11-16T12:56:55Z' },
  }, 'permission-x7f'),
  event(5, 'tool_call', 'waiting', '2026-11-16T12:27:00.200903Z',
    { permission_item_id: 'permission-x7f' }, 'call-x7f'),
  event(6, 'run', 'waiting', '2026-11-16T12:27:00.200914Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission-x7f' }, runId),
  event(7, 'turn', 'waiting', '2026-11-16T12:27:00.200944Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission-x7f' }),
];

function messages(mode: Mode): AgentChatMessage[] {
  const wire = run23Waiting as unknown as ChatTranscriptEventPayload[];
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
  for (const item of wire) applyTranscriptToSessionRuntime(deps, 'agent-x7f', session.id, item, true);
  return result;
}

it.each(['live', 'reload'] as const)('X7f: %s freezes run23 work at 169 seconds from the run first event', mode => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-16T12:27:00.880Z'));
  const model = buildThreadTimeline({ messages: messages(mode), activeSession: session,
    activeRunId: runId.replaceAll('-', ''), activeRunStatus: 'suspended', isWaiting: true, isStreaming: false });
  const runs = model.cells.filter(cell => cell.kind === 'active_run');
  expect(runs).toHaveLength(1);
  expect(runs[0].timeline.status).toBe('blocked');
  const view = render(<RunDisclosureBlock timeline={runs[0].timeline} presentationKey={`${session.id}:${runId}`} />);
  expect.soft(Date.parse(runs[0].timeline.startedAt || '')).toBe(Date.parse('2026-11-16T12:24:11.256390Z'));
  expect(view.container.querySelector('.run-disclosure-duration')?.textContent).toBe('2 分 49 秒');
});

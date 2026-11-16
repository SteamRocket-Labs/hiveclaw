// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { normalizeRuntimeEventMessage, replayTranscriptEvents, uiForPhase, type AgentChatMessage, type ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import type { SessionEventV2 } from './sessionEventStore';
import { buildThreadTimeline } from './timelineModel';
import { ThreadItemRenderer } from './ThreadItemRenderer';
import { normalizeThreadItemPayload } from './threadItemReducer';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => { await i18n.init({ lng: 'zh', resources: { zh: { translation: zh }, en: { translation: en } }, initImmediate: false }); });
afterEach(() => { cleanup(); vi.useRealTimers(); window.sessionStorage.clear(); void i18n.changeLanguage('zh'); });

const runId = '11111111-1111-4111-8111-111111111111';
const session = { id: 'session-x7f', user_id: 'member-x7f' };
const key = `agent-x7f:${session.id}`;
type Mode = 'live' | 'reload';
const request = { permission_request_id: 'permission-x7f', tool_name: 'plaza_create_post',
  arguments: { content: 'Test post' }, session_id: session.id, runtime_task_id: runId,
  requested_by_user_id: session.user_id, expires_at: '2026-11-16T12:56:55Z' };
const legacyPermission: ChatTranscriptEventPayload = { id: 'legacy-permission-x7f', type: 'permission', event_type: 'permission',
  content: 'Permission required', metadata: { status: 'session_permission_required',
    permission_request_id: request.permission_request_id, permission_request: request },
  created_at: '2026-11-16T12:26:55.217711Z' };
function event(sequence: number, kind: string, lifecycle: string, at: string,
  payload: Record<string, unknown>, itemId = kind): SessionEventV2 {
  const level = kind === 'human_input' ? 'session' : kind === 'turn' ? 'turn'
    : ['run', 'control_input'].includes(kind) ? 'run' : 'round';
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

// Run23 step8 /transcript seq 1,10,122-127: retain observed timestamps,
// event order and scope levels; compact sequences and replace identities/content.
const run23Waiting = [
  event(1, 'human_input', 'accepted', '2026-11-16T12:24:06.645302Z',
    { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Publish a post.' }] }),
  event(2, 'run', 'queued', '2026-11-16T12:24:11.256390Z', {}, runId),
  event(3, 'tool_call', 'started', '2026-11-16T12:26:53.618313Z',
    { tool_name: 'plaza_create_post' }, 'call-x7f'),
  legacyPermission,
  event(4, 'tool_permission', 'waiting', '2026-11-16T12:27:00.200886Z', {
    permission_request: request,
  }, 'permission-x7f'),
  event(5, 'tool_call', 'waiting', '2026-11-16T12:27:00.200903Z',
    { permission_item_id: 'permission-x7f' }, 'call-x7f'),
  event(6, 'run', 'waiting', '2026-11-16T12:27:00.200914Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission-x7f' }, runId),
  event(7, 'turn', 'waiting', '2026-11-16T12:27:00.200944Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission-x7f' }),
];

// Exercise the existing legacy decision contract alongside canonical facts.
// Use the canonical completion time; the raw socket arrival was not recorded.
const decision: ChatTranscriptEventPayload = {
  id: 'decision-x7f', type: 'session_permission_decision', event_type: 'session_permission_decision',
  content: JSON.stringify({ permission_request_id: 'permission-x7f', decision: 'allow_once' }),
  metadata: { permission_request_id: 'permission-x7f', decision: 'allow_once' },
  created_at: '2026-11-16T12:30:17.844117Z',
};
const run23AfterApproval = [
  ...run23Waiting,
  event(8, 'control_input', 'accepted', '2026-11-16T12:30:17.264794Z',
    { kind: 'permission_response', permission_item_id: 'permission-x7f', decision: 'allow_once' }, 'control-x7f'),
  event(9, 'control_input', 'applied', '2026-11-16T12:30:17.617022Z',
    { kind: 'permission_response', permission_item_id: 'permission-x7f', decision: 'allow_once' }, 'control-x7f'),
  event(10, 'tool_permission', 'completed', '2026-11-16T12:30:17.844117Z', { decision: 'allow_once' }, 'permission-x7f'),
  decision,
  event(11, 'tool_call', 'progress', '2026-11-16T12:30:23.897948Z', {}, 'call-x7f'),
  event(12, 'tool_call', 'completed', '2026-11-16T12:30:24.764273Z', { outcome: 'success' }, 'call-x7f'),
  event(13, 'tool_result', 'completed', '2026-11-16T12:30:24.764301Z',
    { tool_name: 'plaza_create_post', outcome: 'success', content: 'Published.' }, 'result-x7f'),
  event(14, 'run', 'queued', '2026-11-16T12:30:25.481135Z', { reason_code: 'session_permission_resolved' }, runId),
];
const run23Completed = [...run23AfterApproval,
  event(15, 'assistant_final', 'completed', '2026-11-16T12:31:15.224997Z', { content: 'Published.' }),
  event(16, 'run', 'completed', '2026-11-16T12:31:15.225009Z', { started_at: '2026-11-16T12:24:11.394376Z' }, runId),
  event(17, 'turn', 'completed', '2026-11-16T12:31:15.225023Z', {}),
];

function messages(events: unknown[], mode: Mode): AgentChatMessage[] {
  const wire = events as ChatTranscriptEventPayload[];
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
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(),
    parseChatMsg: message => normalizeRuntimeEventMessage(message) || message,
  };
  for (const item of wire) applyTranscriptToSessionRuntime(deps, 'agent-x7f', session.id, item, true);
  return result;
}

function timeline(events: unknown[], mode: Mode, status: string | null) {
  const model = buildThreadTimeline({ messages: messages(events, mode), activeSession: session,
    activeRunId: status ? runId.replaceAll('-', '') : null, activeRunStatus: status,
    isWaiting: Boolean(status), isStreaming: false });
  const runs = model.cells.filter(cell => cell.kind === 'active_run');
  expect(runs).toHaveLength(1);
  return runs[0].timeline;
}
const block = (events: unknown[], mode: Mode, status: string | null) =>
  <RunDisclosureBlock timeline={timeline(events, mode, status)} presentationKey={`${session.id}:${runId}`} />;
const duration = (container: HTMLElement) => container.querySelector('.run-disclosure-duration')?.textContent;
function expand(container: HTMLElement) {
  for (let pass = 0; pass < 2; pass++) {
    for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
  }
}

it.each(['live', 'reload'] as const)('X7f: %s keeps the run23 approval wait out after the decision and subsequent live ticks', mode => {
  expect(replayTranscriptEvents([legacyPermission]).messages.some(message => message.sessionPermissionRequest)).toBe(true);
  expect(replayTranscriptEvents([legacyPermission, decision]).messages.some(message => message.sessionPermissionRequest)).toBe(false);
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-16T12:27:00.880Z'));
  const view = render(block(run23Waiting, mode, 'suspended'));
  expect(duration(view.container)).toBe('2 分 54 秒');
  act(() => { vi.advanceTimersByTime(120_000); });
  expect(duration(view.container)).toBe('2 分 54 秒');
  act(() => { vi.setSystemTime(new Date('2026-11-16T12:30:29.428Z')); });
  view.rerender(block(run23AfterApproval, mode, 'running'));
  const resumed = timeline(run23AfterApproval, mode, 'running');
  expect(Date.parse(resumed.completedAt!) - Date.parse(resumed.startedAt!) - resumed.durationMs!).toBe(197_064);
  expect.soft(duration(view.container)).toBe('3 分 5 秒');
  act(() => { vi.advanceTimersByTime(1000); });
  expect.soft(duration(view.container)).toBe('3 分 6 秒');
  act(() => { vi.setSystemTime(new Date('2026-11-16T12:31:14.428Z')); });
  view.rerender(block(run23AfterApproval, mode, 'running'));
  expect.soft(duration(view.container)).toBe('3 分 50 秒');
  view.rerender(block(run23Completed, mode, null));
  expect.soft(duration(view.container)).toBe('3 分 47 秒');
  view.unmount();
  const reloaded = render(block(run23Completed, 'reload', null));
  expect(duration(reloaded.container)).toBe('3 分 47 秒');
});

function compatibility(sequence: number, type: string, content: string, metadata: Record<string, unknown>, at: string) {
  return { schema: 'hive.session_event_compatibility', schema_version: 1, compatibility_status: 'needs_reconciliation',
    reason: 'legacy_unscoped_event', event_id: `compat-${sequence}`, sequence, legacy_event_type: type,
    legacy_kind: type, payload: { content, metadata }, occurred_at: at, session_id: session.id, run_id: runId };
}

// Run23 step6 seq 1,10,91,93-95,103,106-107: the parent stop precedes
// the colleague's terminal rows. Keep the child task/session bindings.
const delegated = [
  event(1, 'human_input', 'accepted', '2026-11-16T12:19:25Z',
    { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Ask a colleague to prepare a report.' }] }),
  event(2, 'run', 'queued', '2026-11-16T12:19:27.244Z', {}, runId),
  event(3, 'tool_call', 'started', '2026-11-16T12:20:57.560907Z',
    { tool_name: 'delegate_to_agent', args: { agent_name: '同事', message: 'Prepare the report.' } }, 'call-x7f'),
  event(4, 'tool_call', 'completed', '2026-11-16T12:21:06.691798Z', { outcome: 'success' }, 'call-x7f'),
  event(5, 'tool_result', 'completed', '2026-11-16T12:21:06.691823Z', {
    tool_name: 'delegate_to_agent', outcome: 'success', content: '',
    member_summary: { kind: 'delegation', state: 'in_progress', status: 'queued', task_id: 'child-task-x7f' },
  }, 'result-x7f'),
  compatibility(6, 'runtime_action_started', '已委派给 同事，后台执行中。', {
    action_kind: 'a2a_delegation', status: 'running', runtime_task_id: runId,
    child_session_id: 'child-session-x7f', target_agent_name: '同事',
  }, '2026-11-16T12:21:07.194790Z'),
  event(7, 'run', 'cancelled', '2026-11-16T12:21:17.572301Z', {}, runId),
];
const colleagueStopped = [...delegated,
  compatibility(8, 'runtime_action_failed', '已停止，同事没有完成这项工作。', {
    action_kind: 'a2a_delegation', status: 'cancelled', reason: 'delegation_terminal',
    runtime_task_id: 'child-task-x7f', child_session_id: 'child-session-x7f', target_agent_name: '同事',
  }, '2026-11-16T12:21:20.891884Z'),
  compatibility(9, 'child_session', '已停止，同事没有完成这项工作。', {
    status: 'cancelled', reason: 'delegation_terminal', runtime_task_id: 'child-task-x7f',
    child_session_id: 'child-session-x7f', to_agent: 'colleague-x7f', to_agent_name: '同事',
  }, '2026-11-16T12:21:20.891884Z'),
];
const colleagueCompleted = [...delegated,
  compatibility(8, 'runtime_action_completed', '同事已完成这项工作。', {
    action_kind: 'a2a_delegation', status: 'completed', reason: 'delegation_terminal',
    runtime_task_id: 'child-task-x7f', child_session_id: 'child-session-x7f', target_agent_name: '同事',
  }, '2026-11-16T12:21:20.891884Z'),
  compatibility(9, 'child_session', '同事已完成这项工作。', {
    status: 'completed', reason: 'delegation_terminal', runtime_task_id: 'child-task-x7f',
    child_session_id: 'child-session-x7f', to_agent: 'colleague-x7f', to_agent_name: '同事',
  }, '2026-11-16T12:21:20.891884Z'),
];
const colleagueRows = (container: HTMLElement) => Array.from(container.querySelectorAll('.run-step, .run-tool-history-item'))
  .map(row => row.textContent).join('\n');
function colleagueBlocks(events: unknown[], mode: Mode, status: string | null) {
  const model = buildThreadTimeline({ messages: messages(events, mode), activeSession: session,
    activeRunId: status ? runId.replaceAll('-', '') : null, activeRunStatus: status,
    isWaiting: Boolean(status), isStreaming: false });
  return <>{model.cells.filter(cell => cell.kind === 'active_run').map(cell =>
    <RunDisclosureBlock key={cell.id} timeline={cell.timeline} presentationKey={`${session.id}:${cell.id}`} />)}</>;
}
it.each((['live', 'reload'] as const).flatMap(mode => ['completed', 'cancelled'].map(outcome => ({ mode, outcome }))))(
  'X7f: $mode keeps the colleague running after parent stop until its own $outcome terminal', ({ mode, outcome }) => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-16T12:21:20.600Z'));
  const view = render(colleagueBlocks(delegated.slice(0, -1), mode, 'running'));
  expand(view.container);
  expect(view.container.textContent).toContain('同事正在处理');
  expect(view.container.querySelector('[aria-label="等待同事"]')).toBeTruthy();
  view.rerender(colleagueBlocks(delegated, mode, null));
  expand(view.container);
  expect(view.container.querySelector('[data-status="cancelled"]')).toBeTruthy();
  expect.soft(colleagueRows(view.container)).not.toContain('已停止');
  expect.soft(colleagueRows(view.container)).not.toContain('没有完成');
  expect.soft(colleagueRows(view.container)).toContain('同事正在处理');
  expect.soft(colleagueRows(view.container)).toContain('后台执行中');
  expect.soft(view.container.querySelector('[aria-label="等待同事"]')).toBeTruthy();
  const terminal = outcome === 'completed' ? colleagueCompleted : colleagueStopped;
  const sentence = outcome === 'completed' ? '同事已完成这项工作。' : '已停止，同事没有完成这项工作。';
  view.rerender(colleagueBlocks(terminal, mode, null));
  expand(view.container);
  expect.soft(colleagueRows(view.container)).toContain(sentence);
  expect.soft(colleagueRows(view.container)).not.toContain('同事正在处理');
  expect.soft(colleagueRows(view.container)).not.toContain('后台执行中');
  view.unmount();
  const reload = render(colleagueBlocks(terminal, 'reload', null));
  expand(reload.container);
  expect(colleagueRows(reload.container)).toContain(sentence);
  expect(reload.container.textContent).not.toContain('同事正在处理');
  expect(reload.container.querySelector('[aria-label="等待同事"]')).toBeNull();
});

it.each((['live', 'reload'] as const).flatMap(mode => ['zh', 'en'].map(language => ({ mode, language }))))(
  'X7f: $mode approval expiry names the browser zone in $language', async ({ mode, language }) => {
    await i18n.changeLanguage(language);
    const permission = messages(run23Waiting, mode).find(message => message.sessionPermissionRequest)!;
    const item = normalizeThreadItemPayload({ id: permission.id, eventType: permission.eventType,
      content: permission.content, status: permission.eventStatus, permission_request: permission.sessionPermissionRequest })!;
    const view = render(<ThreadItemRenderer item={item} approvalArguments={permission.sessionPermissionRequest!.arguments} />);
    fireEvent.click(view.getByText(language === 'zh' ? '详情' : 'Details'));
    const expires = new Date(permission.sessionPermissionRequest!.expires_at!);
    const date = expires.toLocaleString(language);
    const zone = new Intl.DateTimeFormat(language, { timeZoneName: 'long' }).formatToParts(expires)
      .find(part => part.type === 'timeZoneName')!.value;
    expect(view.container.textContent).toContain(language === 'zh' ? `${date}（${zone}）` : `${date} (${zone})`);
    expect(view.container.textContent).not.toContain(Intl.DateTimeFormat().resolvedOptions().timeZone);
  },
);

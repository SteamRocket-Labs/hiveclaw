// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import rows from '../../test/fixtures/x3dRun19Rows.json';
import RunDisclosureBlock from './RunDisclosureBlock';
import { type AgentChatMessage, type ChatTranscriptEventPayload } from './chatRuntime';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import { buildThreadTimeline, type ThreadTimelineModel } from '../session-workbench/timelineModel';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => {
  // The real translation hook has state; preserve its hook-count behavior.
  useState(null);
  return { t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values) };
} }));
vi.mock('i18next', () => ({ default: { t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
  translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values) } }));
afterEach(() => { cleanup(); vi.useRealTimers(); locale.language = 'zh'; localStorage.clear(); sessionStorage.clear(); });
type Event = Record<string, any>;
function project(events: Event[], mode: 'live' | 'reload'): AgentChatMessage[] {
  const wire = events as unknown as ChatTranscriptEventPayload[];
  if (mode === 'reload') return hydrateSessionTranscriptEvents(wire, 0).messages;
  let messages: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => null, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, current) => current, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { messages = update(messages); },
    setChatMessagesAfterQueued: (_session, update) => { messages = update(messages); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const event of wire) applyTranscriptToSessionRuntime(deps, 'agent', 'session', event, true);
  return messages;
}
function model(events: Event[], mode: 'live' | 'reload', extra = {}): ThreadTimelineModel {
  return buildThreadTimeline({ messages: project(events, mode), activeSession: { id: 'session' } as never,
    isWaiting: false, isStreaming: false, ...extra });
}
function mount(timeline: ThreadTimelineModel, owns = false, roster = [{ id: 'colleague', name: '销售同事' }]) {
  const client = new QueryClient();
  localStorage.setItem('current_tenant_id', 'tenant');
  client.setQueryData(['agents', 'tenant'], roster);
  const content = (current: ThreadTimelineModel) => <QueryClientProvider client={client}><MemoryRouter>{current.cells.map(cell => cell.kind === 'active_run'
    ? <RunDisclosureBlock key={cell.id} timeline={cell.timeline} viewerOwnsSession={owns} />
    : cell.kind === 'assistant_final' ? <p key={cell.id}>{cell.message.content}</p> : null
  )}</MemoryRouter></QueryClientProvider>;
  const view = render(content(timeline));
  const expand = () => {
    for (let pass = 0; pass < 3; pass++) for (const button of view.container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
  };
  expand();
  return { ...view, update: (next: ThreadTimelineModel) => { view.rerender(content(next)); expand(); },
    dispose: () => { view.unmount(); client.clear(); } };
}
const rowTexts = (container: HTMLElement) => [...container.querySelectorAll('.run-step, .run-tool-history-item')].map(row => row.textContent);
function failDelegation(events: Event[]) {
  for (const event of events) {
    for (const item of event.payload.metadata?.result_manifest?.items || []) item.terminal_status = 'failed';
    if (!['runtime_action_completed', 'child_session'].includes(event.legacy_event_type)) continue;
    const type = event.legacy_event_type === 'runtime_action_completed' ? 'runtime_action_failed' : 'child_session';
    event.legacy_event_type = type;
    Object.assign(event.payload.metadata, { event_type: type, status: 'failed', reason: 'delegation_terminal' });
    for (const part of event.payload.parts || []) Object.assign(part, { event_type: type, status: 'failed', reason: 'delegation_terminal' });
    event.payload.content = 'The colleague did not finish this work.';
  }
}

it.each(['live', 'reload'] as const)('X9b-2: %s waiting and failed rows link through the same private roster', mode => {
  for (const failed of [false, true]) {
    const events = structuredClone(rows.delegation) as Event[];
    if (!failed) events.splice(7);
    else failDelegation(events);
    for (const owns of [false, true]) for (const listed of [false, true]) {
      const view = mount(model(events, mode), owns, listed ? [{ id: 'colleague', name: '销售同事' }] : []);
      expect(Boolean(view.container.querySelector('a[href^="/agents/colleague/sessions/"]')),
        `${failed ? 'failed' : 'waiting'}, owns=${owns}, listed=${listed}`).toBe(owns && listed);
      view.dispose();
    }
  }
});

it('X9b-3: live/reload keep a later colleague failure after the earlier parent answer', () => {
  const events = structuredClone(rows.delegation) as Event[];
  const parent = events[1].scope.run_id;
  events.splice(7, 0, { ...structuredClone(events[2]), event_id: 'parent-answer', item_id: 'parent-answer',
    item_kind: 'assistant_final', kind: 'assistant_final.completed', lifecycle: 'completed',
    payload_schema: 'hive.session.payload.assistant_final.completed.v2',
    payload: { content: 'The colleague is preparing the suggestions.' } });
  failDelegation(events);
  events.forEach((event, index) => { event.sequence = index + 1; });
  const snapshots = [];
  for (const mode of ['live', 'reload'] as const) {
    const timeline = model(events, mode);
    const answerIndex = timeline.cells.findIndex(cell => cell.kind === 'assistant_final');
    expect(answerIndex).toBeGreaterThanOrEqual(0);
    const resultIndex = timeline.cells.findIndex(cell => cell.kind === 'active_run' && cell.runId !== parent
      && cell.timeline.steps.some(step => step.status === 'failed'));
    expect(resultIndex).toBeGreaterThan(answerIndex);
    const view = mount(timeline);
    snapshots.push({ rows: rowTexts(view.container), counts: [...view.container.querySelectorAll('.run-disclosure-count')].map(n => n.textContent),
      order: timeline.cells.map(cell => cell.kind) });
    view.dispose();
  }
  expect(snapshots[0]).toEqual(snapshots[1]);
});

it.each(['zh', 'en'] as const)('X9b-4: %s empty notices and reads say no result, live/reload', language => {
  locale.language = language;
  const notice = structuredClone(rows.delegation.at(-1)!) as Event;
  notice.payload.metadata.has_kept_result = false; notice.payload.metadata.continuation_status = 'delivered';
  notice.payload.metadata.result_manifest.items.forEach((item: Event) => {
    item.terminal_status = 'failed'; item.result_size_bytes = 0; item.artifact_count = 0;
  });
  const call = structuredClone(rows.delegation[2]) as Event;
  call.event_id = 'read-call'; call.item_id = 'read-call'; call.sequence = 12;
  call.invocation_id = 'read'; call.payload = { tool_name: 'read_runtime_result' };
  const result = structuredClone(rows.delegation[5]) as Event;
  result.event_id = 'read-result'; result.item_id = 'read-result'; result.sequence = 13;
  result.invocation_id = 'read'; result.parent_item_id = 'read-call';
  result.payload = { tool_name: 'read_runtime_result', content: '', outcome: 'success',
    member_summary: { kind: 'runtime_result', state: 'done', found: false } };
  [notice, call, result].forEach((event, index) => { event.sequence = index + 1; });
  for (const mode of ['live', 'reload'] as const) {
    const view = mount(model([notice, call, result], mode));
    expect(view.container.textContent).toContain(language === 'zh' ? '同事已结束，没有返回结果。' : 'The colleague ended without returning a result.');
    expect(view.container.textContent).not.toMatch(/收到 1 项任务结果|已返回结果|Received 1 task results|Result returned/);
    view.dispose();
  }
});

it('X9b-5: ask_user_question never adds a transient generic row or changes the step count', () => {
  const events = structuredClone(rows.delegation.slice(0, 2)) as Event[];
  const call = structuredClone(rows.delegation[2]) as Event;
  const start = Date.parse(events[0].occurred_at);
  const at = (seconds: number) => new Date(start + seconds * 1000).toISOString();
  call.occurred_at = call.persisted_at = at(32);
  call.payload.tool_name = 'ask_user_question'; events.push(call);
  const completed = { ...structuredClone(call), event_id: 'question-completed', sequence: 4,
    lifecycle: 'completed', kind: 'tool_call.completed', payload_schema: 'hive.session.payload.tool_call.completed.v2',
    occurred_at: at(39), persisted_at: at(39), payload: { outcome: 'success' } };
  const result = structuredClone(rows.delegation[5]) as Event;
  result.sequence = 5; result.occurred_at = result.persisted_at = at(39);
  result.payload = { tool_name: 'ask_user_question', content: JSON.stringify({
    type: 'user_clarification', blocking: true, questions: [{ header: 'Scope', question: 'Which scope?', options: [] }],
  }) };
  vi.useFakeTimers();
  vi.setSystemTime(new Date(start + 39_000));
  for (const mode of ['live', 'reload'] as const) {
    const before = mount(model(events, mode, { isWaiting: true }));
    expect(before.container.textContent).not.toContain('处理请求');
    const count = before.container.querySelector('.run-disclosure-count')?.textContent;
    const duration = before.container.querySelector('.run-disclosure-duration')?.textContent;
    before.dispose();
    const after = mount(model([...events, completed, result], mode, { isWaiting: true }));
    expect(after.container.querySelector('.run-disclosure-count')?.textContent).toBe(count);
    expect(after.container.querySelector('.run-disclosure-duration')?.textContent).toBe(duration);
    after.dispose();
  }
});

it.each(['live', 'reload'] as const)('X9b-7: %s applies the matching approval event to its waiting tool row', mode => {
  const events = structuredClone(rows.approval.slice(0, 6)) as Event[];
  const permission = events[3]; const parent = events[1].scope.run_id;
  const control: Event = { ...structuredClone(events[1]), event_id: 'decision', item_id: 'decision', sequence: 7,
    item_kind: 'control_input', kind: 'control_input.applied', lifecycle: 'applied',
    payload_schema: 'hive.session.payload.control_input.applied.v2',
    payload: { kind: 'permission_response', permission_item_id: permission.item_id, decision: 'allow_once' } };
  for (const variant of ['matched', 'other-permission', 'other-run', 'rejected', 'deny'] as const) {
    const change = structuredClone(control);
    if (variant === 'other-permission') change.payload.permission_item_id = 'other';
    if (variant === 'other-run') change.scope.run_id = 'other';
    if (variant === 'rejected') {
      change.lifecycle = 'rejected'; change.kind = 'control_input.rejected';
      change.payload_schema = 'hive.session.payload.control_input.rejected.v2';
    }
    if (variant === 'deny') change.payload.decision = 'deny';
    const timeline = model([...events, change], mode, { activeRunId: parent, activeRunStatus: 'suspended' });
    const call = timeline.cells.flatMap(cell => cell.kind === 'active_run' ? cell.timeline.steps : [])
      .find(step => (step.details as any)?.toolName === 'plaza_create_post');
    expect(call?.status, variant).toBe(variant === 'matched' ? 'running' : 'blocked');
    if (variant === 'matched') expect(timeline.header.status).toBe('running');
  }
});

it.each(['zh', 'en'] as const)('X9b-6: %s first rendered finished block uses its persisted status and duration', language => {
  locale.language = language;
  const events = structuredClone(rows.delegation) as Event[];
  const child = events[8].payload.metadata.runtime_task_id;
  const endedAt = events[8].occurred_at;
  const startedAt = new Date(Date.parse(endedAt) - 607_000).toISOString();
  failDelegation(events);
  const questionStart = new Date(Date.parse(endedAt) + 10_000).toISOString();
  const questionEnd = new Date(Date.parse(questionStart) + 40_000).toISOString();
  const call = structuredClone(events[2]);
  Object.assign(call, { event_id: 'question-call', item_id: 'question-call', sequence: 12,
    invocation_id: 'question', occurred_at: questionStart, persisted_at: questionStart });
  call.scope.run_id = 'continuation'; call.payload = { tool_name: 'ask_user_question' };
  const result = structuredClone(events[5]);
  Object.assign(result, { event_id: 'question-result', item_id: 'question-result', sequence: 13,
    invocation_id: 'question', parent_item_id: call.item_id, occurred_at: questionEnd, persisted_at: questionEnd });
  result.scope.run_id = 'continuation'; result.payload = { tool_name: 'ask_user_question', content: JSON.stringify({
    type: 'user_clarification', blocking: true, questions: [{ header: 'Scope', question: 'Which scope?', options: [] }],
  }) };
  events.push(call, result);
  const messages = project(events, 'reload');
  const base = { messages, activeSession: { id: 'session' } as never, isWaiting: false, isStreaming: false };
  const pending = buildThreadTimeline({ ...base, sessionWorkbench: null });
  const first = mount(pending);
  expect([...first.container.querySelectorAll('[data-testid="run-disclosure-block"]')]
    .filter(block => block.getAttribute('data-status') === 'failed')).toHaveLength(0);
  expect(first.container.querySelector('[data-testid="run-disclosure-block"][data-status="blocked"]')).toBeNull();
  const persisted = buildThreadTimeline({ ...base, sessionWorkbench: { runtime_tasks: [{ id: child,
    status: 'failed', started_at: startedAt, completed_at: endedAt },
    { id: 'continuation', status: 'completed', started_at: questionStart, completed_at: questionEnd }] } as never });
  first.update(persisted);
  const saved = first;
  const block = [...saved.container.querySelectorAll('[data-testid="run-disclosure-block"]')]
    .find(block => block.getAttribute('data-status') === 'failed');
  expect(block?.textContent).toContain(language === 'zh' ? '10 分 7 秒' : '10m 7s');
  expect([...saved.container.querySelectorAll('[data-status="done"]')]
    .some(block => block.textContent?.includes(language === 'zh' ? '40 秒' : '40s'))).toBe(true);
  saved.dispose();
});

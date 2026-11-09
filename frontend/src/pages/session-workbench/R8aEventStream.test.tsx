// @vitest-environment jsdom
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import AgentChatSection from '../agent-detail/AgentChatSection';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { buildThreadTimeline } from './timelineModel';
import type { SessionEventV2, SessionCompatibilityEvent } from './sessionEventStore';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (_key: string, fallback?: string) => fallback ?? _key, i18n: { language: 'en' },
}) }));
vi.mock('../agent-detail/ToolAccounts', () => ({ default: () => null }));
afterEach(cleanup);
const at = (m: number) => new Date(Date.UTC(2026, 10, 9, 0, m)).toISOString();
const rounds = new Set(['assistant_commentary', 'assistant_text', 'tool_call', 'tool_result']);
function ev(sequence: number, kind: string, lifecycle: string, minute: number,
  payload: Record<string, unknown> = {}, runId: string | null = 'run-1', itemId?: string): SessionEventV2 {
  return { schema: 'hive.session_event', schema_version: 2, event_id: `e-${sequence}`, sequence,
    tenant_id: 'tenant', item_id: itemId ?? (kind === 'run' ? runId! : `item-${sequence}`), item_kind: kind,
    kind: `${kind}.${lifecycle}`, lifecycle, payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: runId ? { level: rounds.has(kind) ? 'round' : 'run', session_id: 'session', thread_id: 'session',
      turn_id: `turn-${runId}`, run_id: runId, ...(rounds.has(kind) ? { round_id: `round-${runId}` } : {}) }
      : { level: 'session', session_id: 'session', thread_id: 'session' },
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: at(minute), persisted_at: at(minute) } as SessionEventV2;
}
function compat(sequence: number, type: string, minute: number, metadata: Record<string, unknown> = {}): SessionCompatibilityEvent {
  return { schema: 'hive.session_event_compatibility', schema_version: 1,
    compatibility_status: 'needs_reconciliation', reason: 'legacy_event_scope_unprovable',
    event_id: `e-${sequence}`, sequence, session_id: 'session', tenant_id: 'tenant',
    legacy_event_type: type, legacy_item_type: type === 'session_compact' ? 'context_compaction' : 'event',
    legacy_item_status: 'completed', payload: { content: type, metadata }, occurred_at: at(minute) };
}
const user = (seq: number, minute = 0) => ev(seq, 'human_input', 'accepted', minute,
  { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Write the report.' }] }, null);
const work = (seq: number, minute: number) => ev(seq, 'assistant_commentary', 'completed', minute,
  { phase: 'commentary', content: `Working ${seq}` });
const completed = [user(1), ev(2, 'run', 'queued', 0), work(3, 1), work(4, 2),
  ev(5, 'assistant_text', 'completed', 4, { content: 'Here is the report.' }), ev(6, 'run', 'completed', 5)];
function build(events: Array<SessionEventV2 | SessionCompatibilityEvent>, extra: Record<string, unknown> = {}) {
  return buildThreadTimeline({ messages: hydrateSessionTranscriptEvents(events as any).messages,
    activeSession: { id: 'session' }, isWaiting: false, isStreaming: false, runtimePhase: 'idle', ...extra });
}
const runs = (m: ReturnType<typeof build>) => m.cells.filter(c => c.kind === 'active_run');

it('preserves the steps and queued-to-terminal duration of a real completed Web event stream', () => {
  const model = build(completed);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].sourceMessages.map(e => e.message.id)).toEqual(expect.arrayContaining(['item-3', 'item-4']));
  expect(runs(model)[0].timeline).toMatchObject({ status: 'done', startedAt: at(0), completedAt: at(5),
    durationMs: 300_000, durationIsFinal: true });
  const task = { id: 'run-1', status: 'completed', started_at: at(2), completed_at: at(5) };
  for (const runtime_tasks of [[task], []]) {
    expect(runs(build(completed, { sessionWorkbench: { runtime_tasks } }))[0].timeline).toEqual(runs(model)[0].timeline);
  }
});

it('preserves stopped Web steps through controls, duplicate cancellation, recovery, manual compact and admission', () => {
  const events = [user(1), ev(2, 'run', 'queued', 0),
    ev(3, 'tool_call', 'unavailable', 1, { tool_name: 'bash' }),
    ev(4, 'tool_result', 'completed', 1, { content: 'Unavailable' }),
    ev(5, 'tool_call', 'completed', 2, { tool_name: 'read_file' }),
    ev(6, 'tool_result', 'completed', 2, { content: 'Read' }),
    compat(7, 'tool_group_activation', 3), compat(8, 'pack_activation', 3),
    ev(9, 'control_input', 'accepted', 13, { action: 'stop' }, 'run-1', 'stop'),
    ev(10, 'control_input', 'started', 13, {}, 'run-1', 'stop'), ev(11, 'run', 'cancelling', 14),
    ev(12, 'control_input', 'applied', 14, {}, 'run-1', 'stop'), ev(13, 'run', 'cancelled', 15),
    ev(14, 'run', 'cancelled', 16), ev(15, 'recovery_action', 'completed', 17),
    compat(16, 'session_compact', 38, { manual: true }), user(17, 39),
    ...['prepared', 'started', 'sealed', 'admitted'].map((state, i) => ev(18 + i, 'input_admission', state, 39, {}, null, 'admission'))];
  const model = build(events);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].sourceMessages.map(e => e.message.id)).toEqual(expect.arrayContaining(['item-3', 'item-5']));
  expect(runs(model)[0].timeline).toMatchObject({ status: 'cancelled', completedAt: at(15), durationMs: 900_000 });
  expect(model.cells.some(c => c.kind === 'boundary' && c.message.eventType === 'session_compact')).toBe(true);
});

it('uses sequence rather than wall time to put only a post-terminal step outside the block', () => {
  const model = build([...completed, work(7, 1)]);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].sourceMessages.some(e => e.message.id === 'item-3')).toBe(true);
  expect(runs(model)[0].sourceMessages.some(e => e.message.id === 'item-7')).toBe(false);
  expect(model.cells.some(c => c.kind === 'boundary' && c.message.id === 'item-7')).toBe(true);
});

it('keeps a company approval pause nonfinal and includes the later same-run step', () => {
  const events = [user(1), ev(2, 'run', 'queued', 0), work(3, 1),
    ev(4, 'run', 'completed', 2, { reason_code: 'company_approval_required' }), work(5, 3)];
  expect(hydrateSessionTranscriptEvents(events as any).store?.items['run-1'].terminal).toBe(false);
  const model = build(events, { runtimePhase: 'awaiting_approval', isWaiting: true });
  expect(runs(model)[0].timeline.durationIsFinal).not.toBe(true);
  expect(runs(model)[0].timeline.status).not.toBe('done');
  expect(model.header.status).not.toBe('complete');
  expect(runs(model)[0].sourceMessages.some(e => e.message.id === 'item-5')).toBe(true);
  const resumed = build([...events, ev(6, 'run', 'completed', 5)]);
  expect(runs(resumed)[0].timeline).toMatchObject({ status: 'done', durationMs: 300_000, durationIsFinal: true });
});

it.each(['human_chat', 'trigger_run', 'delegation_run'])('does not apply the session runtime id to a new unlabelled turn (%s)', session_kind => {
  const messages = [...hydrateSessionTranscriptEvents(completed as any).messages,
    { id: 'next', role: 'user' as const, content: 'Follow up', timestamp: at(8), sendState: 'sending' as const },
    { id: 'tool', role: 'tool_call' as const, content: '', toolName: 'bash', toolStatus: 'running' as const, timestamp: at(9) }];
  const model = build(completed, { messages, activeSession: { id: 'session', session_kind, read_only: session_kind === 'delegation_run', runtime_task_id: 'run-1' },
    isWaiting: true, runtimePhase: 'thinking', sessionWorkbench: { runtime_tasks: [
      { id: 'run-1', status: 'completed', started_at: at(0), completed_at: at(5) }] } });
  expect(runs(model).at(-1)?.timeline.status).toBe('running');
  expect(runs(model).at(-1)?.runId).not.toBe('run-1');
  expect(model.header.status).toBe('running');
});

it('renders an ordinary bash tool call arriving after completion', () => {
  const messages = hydrateSessionTranscriptEvents([...completed,
    ev(7, 'tool_call', 'completed', 7, { tool_name: 'bash', arguments: { command: 'pwd' } })] as any).messages;
  const ref = React.createRef<any>();
  const noop = vi.fn();
  const session = { id: 'session', user_id: 'member', title: 'Report' };
  const props = { agent: { id: 'bot', name: 'Assistant' }, currentUser: { id: 'member' }, isAdmin: false,
    sessions: [session], activeSession: session, allSessions: [], historyMsgs: [], chatMessages: messages,
    chatMessagesSessionId: 'session', attachedFiles: [], wsConnected: true, isWaiting: false, isStreaming: false,
    runtimePhase: 'idle', sessionsLoading: false, allSessionsLoading: false, uploading: false, uploadProgress: -1,
    allUserFilter: '', chatInput: '', runtimeSummary: null, activeRunStatus: null,
    historyContainerRef: ref, chatContainerRef: ref, chatEndRef: ref, fileInputRef: ref, chatInputRef: ref,
    uploadAbortRef: { current: null }, onLoadAllSessions: noop, onCreateNewSession: noop,
    onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop, onHistoryScroll: noop,
    onScrollHistoryToBottom: noop, onChatScroll: noop, onScrollToBottom: noop, onRemoveAttachedFile: noop,
    onHandleChatFile: noop, onSetChatInput: noop, onHandlePaste: noop, onSendChatMsg: noop, onAbortGeneration: noop,
  } as unknown as React.ComponentProps<typeof AgentChatSection>;
  render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <AgentChatSection {...props} /></QueryClientProvider></MemoryRouter>);
  expect(document.querySelector('[data-thread-item-type="tool_call"]')?.textContent).toContain('bash');
});

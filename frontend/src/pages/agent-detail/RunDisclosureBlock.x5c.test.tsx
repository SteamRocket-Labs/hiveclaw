// @vitest-environment jsdom
// Run21 native file rows, private reasoning and compatibility tool activation.
// Keep the production envelopes; replace identities and omit private payloads.
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import { buildThreadTimeline } from '../session-workbench/timelineModel';
import type { SessionCompatibilityEvent, SessionEventV2 } from '../session-workbench/sessionEventStore';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
  i18n: { language: locale.language },
}) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); locale.language = 'zh'; });

function event(sequence: number, kind: string, lifecycle: string, id: string, payload: Record<string, unknown>, seconds = 0): SessionEventV2 {
  const time = `2026-11-14T17:52:${String(5 + seconds).padStart(2, '0')}Z`;
  return { schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant', scope: kind === 'human_input' ? { level: 'session', session_id: 'session', thread_id: 'session' }
      : kind === 'run' ? { level: 'run', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run' }
        : { level: 'round', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run', round_id: 'round' },
    item_id: id, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`, actor: { type: kind === 'human_input' ? 'user' : 'runtime' },
    visibility: { audience: kind === 'assistant_reasoning_private' ? 'private_provider' : 'direct_user' },
    occurred_at: time, persisted_at: time, payload };
}

function start(): SessionEventV2[] {
  return [event(1, 'human_input', 'accepted', 'input', { content: '整理报告' }),
    event(2, 'run', 'queued', 'run', {}), event(3, 'run', 'running', 'run', {})];
}

function deniedFile(tool: string, filename: string): SessionEventV2[] {
  const invocation = { invocation_id: 'invocation', provider_tool_use_id: 'provider-call' };
  return [...start(),
    { ...event(4, 'tool_call', 'started', 'call', { tool_name: tool, args_hash: 'sanitized', effect_state: 'prepared_not_started' }), ...invocation },
    { ...event(5, 'tool_call', 'denied', 'call', { outcome: 'denied', retryable: false }), ...invocation },
    { ...event(6, 'tool_result', 'completed', 'result', { tool_name: tool, outcome: 'denied', retryable: false,
      content: '', result: '', parts: [], member_summary: { kind: tool === 'edit_file' ? 'file_edit' : 'file_write', state: 'not_done', filename } }),
      ...invocation, parent_item_id: 'call' },
    event(7, 'run', 'completed', 'run', {}, 17)];
}

/** Exercise the incremental socket transcript applier and independent hydration. */
function both(events: Array<SessionEventV2 | SessionCompatibilityEvent>): AgentChatMessage[][] {
  let live: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => null, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, messages) => messages, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { live = update(live); },
    setChatMessagesAfterQueued: (_session, update) => { live = update(live); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const frame of events) applyTranscriptToSessionRuntime(deps, 'agent', 'session', frame as unknown as ChatTranscriptEventPayload, true);
  return [live, hydrateSessionTranscriptEvents(events as unknown as ChatTranscriptEventPayload[], 0).messages];
}

function mount(messages: AgentChatMessage[]) {
  const model = buildThreadTimeline({ messages, activeSession: { id: 'session' }, isWaiting: false, isStreaming: false, activeRunStatus: null });
  const runs = model.cells.filter(cell => cell.kind === 'active_run');
  expect(runs).toHaveLength(1);
  const view = render(<RunDisclosureBlock timeline={runs[0].timeline} />);
  const header = view.container.querySelector<HTMLButtonElement>('button.run-disclosure-header');
  if (header?.getAttribute('aria-expanded') === 'false') fireEvent.click(header);
  return view;
}

function expandRows(container: HTMLElement) {
  for (const group of container.querySelectorAll<HTMLDetailsElement>('details.run-tool-group')) group.open = true;
  for (const button of container.querySelectorAll<HTMLButtonElement>('.run-step-toggle, .run-tool-history-toggle')) {
    if (button.getAttribute('aria-expanded') === 'false') fireEvent.click(button);
  }
}

it('X5c-1: denied edit/write rows retain their safe filename and explain the denial, live and after reload', () => {
  for (const language of ['zh', 'en'] as const) {
    locale.language = language;
    for (const tool of ['edit_file', 'write_file']) {
      for (const filename of ['sales-analysis-sales-run21.md', 'client_secret.pem']) {
        for (const messages of both(deniedFile(tool, filename))) {
          const { container, unmount } = mount(messages);
          // Preserve the catalog's existing English singular for one expandable row.
          expect(container.querySelector('.run-disclosure-count')?.textContent).toBe(language === 'zh' ? '1 个步骤' : '1 step');
          const row = container.querySelector('.run-step, .run-tool-history-item');
          expect(row?.getAttribute('data-status')).toBe('failed');
          const subtitle = () => row?.querySelector('.run-step-summary, .run-tool-history-summary')?.textContent;
          const safe = filename === 'sales-analysis-sales-run21.md';
          expect(subtitle()).toBe(safe ? filename : undefined);
          expandRows(container);
          expect(subtitle()).toBe(safe ? filename : undefined);
          expect(row?.querySelector('p')?.textContent).toBe((language === 'zh' ? zh : en).agent.chat.disclosure.failure.permission);
          expect(row?.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe((language === 'zh' ? zh : en).agent.chat.disclosure.failedMarker);
          expect(container.textContent).not.toMatch(/workspace\/|client_secret|auth_or_permission|denied|edit_file|write_file/);
          unmount();
        }
      }
    }
  }
});

it('X5c-3: a private-reasoning-only block has no count and preserves its status/duration, live and after reload', () => {
  vi.spyOn(i18n, 't').mockImplementation(((key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback, values)) as unknown as typeof i18n.t);
  const reasoning = [...start(),
    event(4, 'assistant_reasoning_private', 'snapshot', 'reasoning', { phase: 'reasoning_private', block_index: 0, provider_request_id: 'request' }, 15),
    event(5, 'assistant_reasoning_private', 'completed', 'reasoning', { phase: 'reasoning_private', block_index: 0, provider_request_id: 'request' }, 16),
    event(6, 'run', 'completed', 'run', { started_at: '2026-11-14T17:52:05Z' }, 17)];
  for (const messages of both(reasoning)) {
    const { container, unmount } = mount(messages);
    expect(container.querySelector('.run-step-toggle')?.textContent).toBe('已思考');
    expect(container.querySelector<HTMLButtonElement>('.run-step-toggle')?.disabled).toBe(true);
    expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
    expect(container.querySelector('[data-testid="run-disclosure-block"]')?.getAttribute('data-status')).toBe('done');
    // Preserve the existing localized duration format, including its space.
    expect(container.querySelector('.run-disclosure-duration')?.textContent).toBe('17 秒');
    expect(container.querySelector('.run-disclosure-count')).toBeNull();
    unmount();
  }
});

it('X5c-4: enabling plaza tools reports completion rather than a returned result, live and after reload', () => {
  const message = 'Discovered deferred tools: plaza_get_new_posts';
  const part = { type: 'event', event_type: 'deferred_tools_delta', title: 'Deferred Tools Updated', status: 'info', text: message,
    tool_groups: [{ name: 'plaza_pack', tools: ['plaza_get_new_posts', 'plaza_create_post', 'plaza_add_comment'], source: 'system' }], trigger_tool: 'tool_search' };
  const activation: SessionCompatibilityEvent = { schema: 'hive.session_event_compatibility', schema_version: 1, compatibility_status: 'needs_reconciliation',
    reason: 'unmapped_legacy_kind', event_id: 'event-4', sequence: 4, tenant_id: 'tenant', session_id: 'session', run_id: 'run',
    legacy_kind: 'legacy_unknown', legacy_lifecycle: 'legacy_unknown', legacy_event_type: 'deferred_tools_delta',
    legacy_item_type: 'event', legacy_item_status: 'succeeded', occurred_at: '2026-11-14T17:52:10Z',
    payload: { content: JSON.stringify({ ...part, message }), parts: [part], metadata: { ...part, message, runtime_task_id: 'run' } } };
  for (const language of ['zh', 'en'] as const) {
    locale.language = language;
    for (const messages of both([...start(), activation, event(5, 'run', 'completed', 'run', {}, 17)])) {
      const { container, unmount } = mount(messages);
      expect(container.querySelector('.run-tool-history-title, .run-step-title')?.textContent).toBe(language === 'zh' ? '准备所需功能' : 'Tools ready');
      expandRows(container);
      expect(container.querySelector('p')?.textContent).toBe(language === 'zh' ? '准备所需功能。完成' : 'Tools ready. Done');
      expect(container.textContent).not.toMatch(/已返回结果|Result returned|plaza_pack|plaza_get_new_posts|deferred_tools_delta/);
      unmount();
    }
  }
});

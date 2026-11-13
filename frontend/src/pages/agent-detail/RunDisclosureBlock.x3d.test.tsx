// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import rows from '../../test/fixtures/x3dRun19Rows.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import { consumeSessionEnvelope, hydrateSessionTranscriptEvents, projectSessionEventStoreToMessages } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';
import { buildThreadTimeline } from '../session-workbench/timelineModel';
import type { SessionEventStore } from '../session-workbench/sessionEventStore';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
  i18n: { language: locale.language },
}) }));
afterEach(() => { cleanup(); locale.language = 'zh'; window.localStorage.clear(); });

// Sanitized, selected run-19 envelopes. Calls retain args_hash rather than
// invented args. Started notifications retain the PARENT runtime_task_id;
// terminal notifications name the CHILD task. Their child_session_id matches
// after UUID normalization. Source sequences: step 5 67–95, step 6 91–108,
// step 8 220–232 and step 4 78/85. IDs/content are anonymized, unrelated
// bookkeeping is omitted and delivery sequences are contiguous.
type Event = Record<string, any>;
const copy = (events: Event[]) => structuredClone(events);

function projectBoth(events: Event[]): AgentChatMessage[][] {
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
  for (const event of events) applyTranscriptToSessionRuntime(deps, 'agent', 'session', event as ChatTranscriptEventPayload, true);
  return [live, hydrateSessionTranscriptEvents(events as ChatTranscriptEventPayload[], 0).messages];
}

function renderMessages(messages: AgentChatMessage[]) {
  const model = buildThreadTimeline({ messages, activeSession: { id: 'session' } as never,
    isWaiting: false, isStreaming: false, activeRunStatus: null });
  const view = render(<>{model.cells.map(cell => cell.kind === 'active_run'
    ? <RunDisclosureBlock key={cell.id} timeline={cell.timeline} /> : null)}</>);
  for (let pass = 0; pass < 3; pass++) {
    view.container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]').forEach(button => fireEvent.click(button));
    view.container.querySelectorAll('details').forEach(details => { details.open = true; });
  }
  return view;
}

function memberRow(container: HTMLElement, sentence: string) {
  return [...container.querySelectorAll('.run-step, .run-tool-history-item')].find(row => row.textContent?.includes(sentence));
}

it.each(['zh', 'en'] as const)('X3d-1 marks a colleague still working live and after reload in %s, even after parent completion', language => {
  locale.language = language;
  const events = copy(rows.delegation).filter(e => !['runtime_action_completed', 'child_session', 'agent_task_notification'].includes(e.legacy_event_type));
  for (const messages of projectBoth(events)) {
    const { container, unmount } = renderMessages(messages);
    const row = memberRow(container, language === 'zh' ? '同事正在处理，结果回来后会在这里继续。' : 'The colleague is working. The result will return here.');
    expect(row, 'run-19 waiting sentence must remain available').toBeTruthy();
    expect(row?.getAttribute('data-status')).toBe('running');
    expect(row?.querySelector(`[role="img"][aria-label="${language === 'zh' ? '运行中' : 'Running'}"] .tabler-icon-loader-2`)).toBeTruthy();
    unmount();
  }
});

it('X3d-1 marks the actual approval-waiting tool row live and after reload (run 19 step 8)', () => {
  const events = copy(rows.approval).filter(e => e.sequence <= rows.approval.find(e => e.item_kind === 'run' && e.lifecycle === 'waiting')!.sequence);
  for (const messages of projectBoth(events)) {
    const { container, unmount } = renderMessages(messages);
    const row = memberRow(container, '发布帖子到公司广场');
    expect(row).toBeTruthy();
    expect(row?.getAttribute('data-status')).toBe('blocked');
    expect(row?.querySelector('[role="img"][aria-label="等待处理"] .tabler-icon-alert-circle')).toBeTruthy();
    unmount();
  }
});

it.each(['completed', 'failed', 'cancelled'] as const)('X3d-2 folds the parent-id started notice into the same child task after %s', status => {
  const events = copy(rows.delegation);
  for (const event of events) {
    if (['runtime_action_completed', 'child_session'].includes(event.legacy_event_type)) {
      event.payload.metadata.status = status;
      event.payload.parts.forEach((part: Event) => { part.status = status; });
      if (status !== 'completed') event.payload.content = '同事没有完成这项工作。';
    }
    for (const item of event.payload.metadata?.result_manifest?.items || []) item.terminal_status = status;
  }
  for (const messages of projectBoth(events)) {
    expect(messages.filter(m => m.eventType === 'runtime_action_started')).toHaveLength(0);
    const { container, unmount } = renderMessages(messages);
    expect(container.textContent).not.toContain('后台执行中');
    expect(container.textContent).not.toContain('同事正在处理');
    const sentence = status === 'completed' ? '同事已完成这项工作。' : status === 'cancelled' ? '已停止，同事没有完成这项工作。' : '同事没有完成这项工作。';
    const row = memberRow(container, sentence);
    expect(row).toBeTruthy();
    expect(row?.getAttribute('data-status')).toBe(status === 'completed' ? 'done' : status);
    if (status !== 'completed') expect(row?.querySelector(`[role="img"][aria-label="${status === 'cancelled' ? '已停止' : '未完成'}"]`)).toBeTruthy();
    unmount();
  }
});

it('X3d-2 matches manifest-only terminal evidence by child session, without matching a different task of the same colleague', () => {
  const events = copy(rows.delegation).filter(e => !['runtime_action_completed', 'child_session'].includes(e.legacy_event_type));
  events.forEach((e, i) => { e.sequence = i + 1; });
  for (const messages of projectBoth(events)) expect(messages.filter(m => m.eventType === 'runtime_action_started')).toHaveLength(0);
  const differentTask = copy(rows.delegation);
  const started = differentTask.find(e => e.legacy_event_type === 'runtime_action_started')!;
  started.payload.metadata.child_session_id = 'another-child-session';
  started.payload.parts[0].child_session_id = 'another-child-session';
  for (const messages of projectBoth(differentTask)) expect(messages.filter(m => m.eventType === 'runtime_action_started')).toHaveLength(1);
});

it('X3d-3 marks the stopped integration notification from its typed manifest, live and after reload', () => {
  for (const messages of projectBoth(copy(rows.stopped))) {
    const { container, unmount } = renderMessages(messages);
    const row = memberRow(container, '同事任务已结束');
    expect(row).toBeTruthy();
    expect(row?.getAttribute('data-status')).toBe('cancelled');
    expect(row?.querySelector('[role="img"][aria-label="已停止"] .tabler-icon-player-stop')).toBeTruthy();
    expect(container.textContent).not.toContain('后台执行中');
    expect(container.textContent).not.toContain('结果已保留');
    unmount();
  }
});

it('X3d-3 keeps an integration notification containing a completed result from being marked stopped', () => {
  const events = copy(rows.stopped);
  const metadata = events.at(-1)!.payload.metadata;
  const manifest = metadata.result_manifest;
  metadata.has_kept_result = true;
  metadata.item_count = 2;
  manifest.items.push({ ...manifest.items[0], source_run_id: 'another-task', terminal_status: 'completed', result_size_bytes: 100 });
  for (const messages of projectBoth(events)) {
    const { container, unmount } = renderMessages(messages);
    expect(memberRow(container, '收到 2 项任务结果')?.getAttribute('data-status')).toBe('done');
    unmount();
  }
});

it('X3d-3 omits only the empty permission_resolved row that produced step 8’s question-mark “运行状态”', () => {
  for (const messages of projectBoth(copy(rows.approval))) {
    const resolved = messages.find(m => m.eventType === 'permission_resolved');
    expect(resolved?.sessionItem?.kind).toBe('tool_permission');
    expect(resolved?.content).toBe('');
    const timeline = buildRunTimelineFromMessages(messages);
    expect(timeline.steps.filter(step => step.systemEvent?.type === 'permission_resolved')).toHaveLength(0);
    const { container, unmount } = renderMessages(messages);
    expect(container.textContent).not.toContain('运行状态');
    expect(container.textContent).toContain('发布帖子到公司广场');
    unmount();
  }
});

it.each([
  ['workspace_input_capture_unavailable', 'auth_or_permission', '这次未能取得可读取的文件内容。'],
  ['content_source_access_denied', 'auth_or_permission', '当前没有权限完成这次操作。'],
  [null, 'auth_or_permission', '当前没有权限完成这次操作。'],
  [null, 'not_found', '没有找到要读取的文件。'],
  ['content_source_changed', 'auth_or_permission', '文件或其来源已变化，这次没有读取。'],
  ['workspace_input_revision_changed', 'operation_failed', '文件或其来源已变化，这次没有读取。'],
  [null, 'provider_unavailable', '这次未能取得可读取的文件内容。'],
  ['unknown_read_reason', 'auth_or_permission', '这次没有读取文件'],
] as const)('X3d-4 uses typed read reason %s / class %s through the canonical consumer and live/reload render', (reason, errorClass, sentence) => {
  const events = copy(rows.refusedRead);
  const result = events.at(-1)!;
  result.payload.member_summary.error_class = errorClass;
  if (reason) result.payload.metadata.workspace_input_receipt.reason_code = reason;
  else delete result.payload.metadata.workspace_input_receipt;
  // Direct consumer/store projection also protects result-to-call pairing.
  let store: SessionEventStore | undefined;
  for (const event of events) store = consumeSessionEnvelope(event as ChatTranscriptEventPayload, store, 0).store;
  const direct = projectSessionEventStoreToMessages(store!);
  for (const messages of [direct, ...projectBoth(events)]) {
    const { container, unmount } = renderMessages(messages);
    const row = memberRow(container, sentence);
    expect(row, sentence).toBeTruthy();
    expect(row?.getAttribute('data-status')).toBe('failed');
    expect(row?.querySelector('[role="img"][aria-label="未完成"]')).toBeTruthy();
    if (reason === 'workspace_input_capture_unavailable') expect(container.textContent).not.toContain('没有权限');
    expect(container.textContent).not.toContain('workspace_input_');
    unmount();
  }
});

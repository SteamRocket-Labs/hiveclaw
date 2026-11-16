// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import rows from '../../test/fixtures/x5eRun22Rows.json';
import run21 from '../../test/fixtures/r26aRun21Rows.json';
import stoppedRows from '../../test/fixtures/x5eRun22StoppedRows.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import type { AgentChatMessage, ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import { buildThreadTimeline } from './timelineModel';
import { shouldRenderThreadItemInConversation, ThreadItemRenderer } from './ThreadItemRenderer';
import { buildRunAggregateSummary } from '../agent-detail/chatDisclosureReducer';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
const translate = (key: string, fallback?: string, values?: Record<string, unknown>) =>
  translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate, i18n: { language: locale.language } }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); locale.language = 'zh'; });

// Selected run22 api-evidence envelopes: step10 104-105,
// step2 25-28/99-102, and step0's run21 history (the existing R26a fixture).
// Keep shapes, ordering, typed facts and timestamps; anonymize identities and
// omit unrelated bookkeeping. Source sequences remain in event ids.
type Event = Record<string, any>;
function both(events: Event[]): AgentChatMessage[][] {
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
  for (const frame of events) applyTranscriptToSessionRuntime(deps, 'agent', 'session', frame as ChatTranscriptEventPayload, true);
  return [live, hydrateSessionTranscriptEvents(events as ChatTranscriptEventPayload[], 0).messages];
}
function mount(messages: AgentChatMessage[]) {
  vi.spyOn(i18n, 't').mockImplementation(translate as never);
  const model = buildThreadTimeline({ messages, activeSession: { id: 'session' },
    isWaiting: false, isStreaming: false, activeRunStatus: null });
  const view = render(<>{model.cells.map(cell => cell.kind === 'active_run'
    ? <RunDisclosureBlock key={cell.id} timeline={cell.timeline} viewerOwnsSession />
    : cell.kind === 'boundary' && cell.message.threadItem && shouldRenderThreadItemInConversation(cell.message.threadItem, false)
      ? <ThreadItemRenderer key={cell.id} item={cell.message.threadItem} /> : null)}</>);
  for (let pass = 0; pass < 3; pass++) {
    for (const button of view.container.querySelectorAll('button[aria-expanded="false"]')) fireEvent.click(button);
    for (const group of view.container.querySelectorAll('details')) group.open = true;
  }
  return { ...view, model };
}
const source = (event: Event) => Number(event.event_id.split('-').at(-1));

it.each(['zh', 'en'] as const)('X5e-2 names employee creation from typed facts, including its display name when carried (%s)', language => {
  locale.language = language;
  for (const name of [null, '报表助手']) {
    const events: Event[] = structuredClone(rows.creation);
    const created = events.find(event => source(event) === 104)!;
    created.payload.metadata.target_agent_name = name;
    for (const messages of both(events)) {
      const { container, unmount } = mount(messages);
      const expected = language === 'zh' ? name ? '已创建数字员工「报表助手」。' : '已创建数字员工。'
        : name ? 'Created the digital employee “报表助手”.' : 'Created a digital employee.';
      const row = [...container.querySelectorAll('.run-step, .run-tool-history-item')].find(row => row.querySelector('p')?.textContent === expected);
      expect(row, `name=${name}`).toBeTruthy();
      expect(row?.getAttribute('data-status')).toBe('done');
      expect(container.textContent).not.toMatch(/hr_provisioning|employee_creation|runtime-result/);
      unmount();
    }
  }
});

it.each(['zh', 'en'] as const)('X5e-4 includes successful writes and edits alongside reads (%s)', language => {
  locale.language = language;
  for (const tool of ['write_file', 'edit_file']) {
    const events: Event[] = structuredClone(rows.files);
    for (const event of events) {
      if (event.payload.tool_name === 'write_file') event.payload.tool_name = tool;
      if (event.payload.member_summary?.kind === 'file_write') event.payload.member_summary.kind = tool === 'write_file' ? 'file_write' : 'file_edit';
    }
    for (const messages of both(events)) {
      const { container, unmount } = mount(messages);
      const summary = container.querySelector('.run-disclosure-summary')?.textContent;
      expect(summary).toContain(language === 'zh' ? '读取 1 个文件' : 'Read 1 file');
      expect(summary).toContain(language === 'zh' ? tool === 'write_file' ? '写入 1 个文件' : '编辑 1 个文件'
        : tool === 'write_file' ? 'Wrote 1 file' : 'Edited 1 file');
      unmount();
    }
  }
});

it('X5e-5 clears only the current waiting notice after the observed colleague settles, preserving check history', () => {
  const events: Event[] = run21.events;
  const failure = events.find(event => event.item_kind === 'runtime_failure')!;
  for (const finished of [false, true]) {
    for (const messages of both(events.filter(event => finished || event.sequence <= failure.sequence))) {
      const { container, model, unmount } = mount(messages);
      expect(model.header.status).toBe(finished ? 'complete' : 'running');
      expect(container.querySelectorAll('p[role="status"].thread-item-content')).toHaveLength(finished ? 0 : 1);
      expect(messages.filter(message => message.sessionItem?.kind === 'runtime_failure')).toHaveLength(1);
      expect([...container.querySelectorAll('.run-step, .run-tool-history-item')].some(row => row.textContent?.includes('同事正在处理'))).toBe(true);
      unmount();
    }
  }
});

it.each(['file_write', 'file_edit', 'file_delete', undefined])('X5e-r2-1 counts fs_write by its typed mode (%s)', kind => {
  const events: Event[] = structuredClone(rows.files);
  for (const event of events) {
    if (event.payload.tool_name === 'write_file') event.payload.tool_name = 'fs_write';
    if (event.payload.member_summary?.kind === 'file_write') {
      if (kind) event.payload.member_summary.kind = kind;
      else delete event.payload.member_summary.kind;
    }
  }
  for (const messages of both(events)) {
    const { model, unmount } = mount(messages);
    const run = model.cells.find(cell => cell.kind === 'active_run')!;
    if (run.kind !== 'active_run') throw new Error('Missing run');
    expect(run.timeline.summary?.files).toBe(1);
    expect(run.timeline.summary?.writes ?? 0).toBe(!kind || kind === 'file_write' ? 1 : 0);
    expect(run.timeline.summary?.edits ?? 0).toBe(kind === 'file_edit' ? 1 : 0);
    unmount();
  }
});

it.each(['office_document_create', 'office_document_apply'])('X5e-r2-2 counts successful %s with the existing success/external gate', tool => {
  for (const outcome of ['success', 'denied']) {
    const events: Event[] = structuredClone(rows.files);
    for (const event of events) {
      if (event.payload.tool_name === 'write_file') event.payload.tool_name = tool;
      if (event.payload.member_summary?.kind === 'file_write') {
        delete event.payload.member_summary;
        event.payload.outcome = outcome;
      }
    }
    for (const messages of both(events)) {
      const { model, unmount } = mount(messages);
      const run = model.cells.find(cell => cell.kind === 'active_run')!;
      if (run.kind !== 'active_run') throw new Error('Missing run');
      expect(run.timeline.summary?.writes ?? 0).toBe(tool === 'office_document_create' && outcome === 'success' ? 1 : 0);
      expect(run.timeline.summary?.edits ?? 0).toBe(tool === 'office_document_apply' && outcome === 'success' ? 1 : 0);
      const external = buildRunAggregateSummary(run.timeline.steps.map(step => ({ ...step, presentation: 'external' })));
      expect(external?.writes ?? 0).toBe(0);
      expect(external?.edits ?? 0).toBe(0);
      unmount();
    }
  }
});

it.each(['read_runtime_result', 'check_subagent'])('X5e-r2-3 preserves %s observations and stamps their typed terminals in both languages', tool => {
  const events: Event[] = structuredClone(run21.events);
  const checkIds = new Set(events.filter(event => ['check_async_task', 'task_output'].includes(event.payload.tool_name))
    .map(event => event.invocation_id || event.payload.invocation_id));
  for (const event of events) {
    if (['check_async_task', 'task_output'].includes(event.payload.tool_name)) event.payload.tool_name = tool;
  }
  const failure = events.find(event => event.item_kind === 'runtime_failure')!;
  for (const language of ['zh', 'en'] as const) {
    locale.language = language;
    for (const finished of [false, true]) {
      for (const messages of both(events.filter(event => finished || event.sequence <= failure.sequence))) {
        const checks = messages.filter(message => message.toolName === tool && message.toolMemberSummary
          && checkIds.has(message.sessionItem?.invocationId));
        expect(checks.length).toBeGreaterThan(0);
        for (const check of checks) {
          expect(check.toolMemberSummary).toMatchObject({ kind: 'colleague_result', state: 'in_progress' });
          expect(Boolean(check.toolTaskTerminalStatus)).toBe(finished);
        }
        const { container, unmount } = mount(messages);
        expect(container.querySelectorAll('p[role="status"].thread-item-content')).toHaveLength(finished ? 0 : 1);
        expect(container.textContent).toContain(language === 'zh' ? '同事正在处理' : 'The colleague is working');
        unmount();
      }
    }
  }
});

it('X5e-r2-3 leaves waiting visible for an unidentified check and for no recoverable checks', () => {
  const events: Event[] = structuredClone(run21.events);
  const checks = events.filter(event => event.payload.member_summary?.kind === 'colleague_result'
    && event.payload.member_summary.state === 'in_progress');
  expect(checks.length).toBeGreaterThan(1);
  delete checks.at(-1)!.payload.member_summary.task_id;
  for (const language of ['zh', 'en'] as const) {
    locale.language = language;
    for (const messages of both(events)) {
      for (const noChecks of [false, true]) {
        const retained = noChecks ? messages.filter(message => message.toolMemberSummary?.kind !== 'colleague_result') : messages;
        const { container, unmount } = mount(retained);
        expect(container.querySelectorAll('p[role="status"].thread-item-content')).toHaveLength(1);
        unmount();
      }
    }
  }
});

it('X5e-r2-4 applies the existing display-name rule to creation names', () => {
  const events: Event[] = structuredClone(rows.creation);
  events.find(event => source(event) === 104)!.payload.metadata.target_agent_name = 'My Assistant';
  for (const language of ['zh', 'en'] as const) {
    locale.language = language;
    for (const messages of both(events)) {
      const { container, unmount } = mount(messages);
      expect(container.textContent).toContain(language === 'zh' ? '已创建数字员工「我的助理」。' : 'Created the digital employee “My Assistant”.');
      if (language === 'zh') expect(container.textContent).not.toContain('My Assistant');
      unmount();
    }
  }
});

it.each(['zh', 'en'] as const)('X5e-r2-5 keeps every late stop row inside the original stopped block (%s)', language => {
  locale.language = language;
  const counts: number[] = [];
  const durations: string[] = [];
  for (const sequence of [938, 939, 940, 941]) {
    for (const messages of both(stoppedRows.filter(event => source(event) <= sequence))) {
      const { container, model, unmount } = mount(messages);
      expect.soft(container.querySelectorAll('[data-testid="run-disclosure-block"]'), `source-${sequence}`).toHaveLength(1);
      expect.soft(model.header.status).toBe('cancelled');
      expect.soft(container.querySelector('[data-testid="run-disclosure-block"]')?.getAttribute('data-status')).toBe('cancelled');
      counts.push(container.querySelectorAll('.run-step, .run-tool-history-item').length);
      durations.push(container.querySelector('.run-disclosure-duration')?.textContent || '');
      const sentence = language === 'zh' ? '已停止，同事没有完成这项工作。' : 'Stopped; the colleague did not finish this work.';
      if (sequence >= 939) expect.soft([...container.querySelectorAll('.run-step, .run-tool-history-item')]
        .filter(row => row.textContent?.includes(sentence))).toHaveLength(1);
      if (sequence === 941) expect.soft([...container.querySelectorAll('.run-step, .run-tool-history-item')]
        .some(row => row.textContent?.includes(language === 'zh' ? '同事任务已结束' : 'The colleague task has ended'))).toBe(true);
      unmount();
    }
  }
  expect(counts).toEqual([2, 2, 2, 2, 2, 2, 3, 3]);
  expect(new Set(durations).size).toBe(1);
});

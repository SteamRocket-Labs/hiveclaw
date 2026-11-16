// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import rows from '../../test/fixtures/x5eRun22Rows.json';
import run21 from '../../test/fixtures/r26aRun21Rows.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import type { AgentChatMessage, ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import { buildThreadTimeline } from './timelineModel';
import { shouldRenderThreadItemInConversation, ThreadItemRenderer } from './ThreadItemRenderer';

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

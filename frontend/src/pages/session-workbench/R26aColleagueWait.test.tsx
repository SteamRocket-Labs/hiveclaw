// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import rows from '../../test/fixtures/r26aRun21Rows.json';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from '../agent-detail/sessionTranscriptApplier';
import { agentChatMessageRunId, isSameSessionRunId, type AgentChatMessage, type ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import { buildThreadTimeline, type BuildThreadTimelineInput } from './timelineModel';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import { shouldRenderThreadItemInConversation, ThreadItemRenderer } from './ThreadItemRenderer';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: i18n.t.bind(i18n),
  i18n: { language: 'zh' },
}) }));
// The duration formatter reads the shared i18n instance, as the real page does.
beforeAll(async () => { await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, initImmediate: false }); });
afterEach(cleanup);

// Run 21, step 10: selected transcript sequences 1/10, 49/52, 63/64,
// 72/73, 76/77, 94/97, 118/121, 150/153, 169/172, 186/189,
// 221/224/225, 226/227, 241/244, 272/273, 281/282. Keep the actual
// native/compatibility shapes and times; anonymize identities and prose,
// omit unrelated bookkeeping and renumber the delivery sequence contiguously.
// parentTask keeps the /runs/active start witness and transcript terminal.
type Event = Record<string, any>;
const events = rows.events as Event[];
const failure = events.find(event => event.item_kind === 'runtime_failure')!;
const parentRun = failure.scope.run_id;
const session = failure.scope.session_id;
const waiting = events.filter(event => event.sequence <= failure.sequence);

function projectBoth(input: Event[]): AgentChatMessage[][] {
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
  for (const event of input) applyTranscriptToSessionRuntime(deps, 'agent', 'session', event as ChatTranscriptEventPayload, true);
  return [live, hydrateSessionTranscriptEvents(input as ChatTranscriptEventPayload[], 0).messages];
}

function mount(messages: AgentChatMessage[], extra: Partial<BuildThreadTimelineInput> = {}) {
  const model = buildThreadTimeline({ messages, activeSession: { id: session },
    sessionWorkbench: { schema: 'hive.session_workbench.v1', agent_id: 'agent', session: { id: session },
      turn: { truth_source: 'session_events', event_count: messages.length }, controls: {},
      runtime_tasks: [rows.parentTask], goals: [], teams: [] },
    isWaiting: false, isStreaming: false, activeRunStatus: null, ...extra });
  const view = render(<>
    <SessionWorkbenchHeader model={model.header} />
    {model.cells.map(cell => cell.kind === 'active_run'
      ? <section key={cell.id} data-run={cell.runId}><RunDisclosureBlock timeline={cell.timeline} /></section>
      : cell.kind === 'boundary' && cell.message.threadItem && shouldRenderThreadItemInConversation(cell.message.threadItem, false)
        ? <ThreadItemRenderer key={cell.id} item={cell.message.threadItem} />
        : cell.kind === 'assistant_final' ? <p key={cell.id}>{cell.message.content}</p> : null)}
  </>);
  for (let pass = 0; pass < 3; pass++) {
    view.container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]').forEach(button => fireEvent.click(button));
    view.container.querySelectorAll('details').forEach(details => { details.open = true; });
  }
  const parent = model.cells.find(cell => cell.kind === 'active_run' && isSameSessionRunId(cell.runId, parentRun));
  if (parent?.kind !== 'active_run') throw new Error('Expected the stopped reading turn');
  const block = [...view.container.querySelectorAll<HTMLElement>('section[data-run]')]
    .find(element => isSameSessionRunId(element.dataset.run, parentRun))!;
  return { ...view, model, parent, block };
}

it.each(['live', 'reload'])('R26a: a running-read stop waits neutrally, then the next turn delivers (%s)', mode => {
  for (const [input, stillWorking] of [[waiting, true], [events, false]] as const) {
    const messages = projectBoth([...input])[mode === 'live' ? 0 : 1];
    // The backend event and its typed failure remain intact in both consumers.
    expect(messages.filter(message => message.sessionItem?.kind === 'runtime_failure')).toHaveLength(1);
    expect(messages.find(message => message.sessionItem?.kind === 'runtime_failure')?.sessionItem?.payload.failure_code)
      .toBe('identical_running_task_read');
    const { container, model, parent, block, getByTestId, unmount } = mount(messages);
    expect(model.header.status).toBe(stillWorking ? 'running' : 'complete');
    expect(getByTestId('session-workbench-header').textContent).not.toContain('失败');
    expect(container.querySelector('.thread-item-error')).toBeNull();
    expect(container.textContent).not.toContain('运行错误');
    expect(container.textContent).toContain('同事正在处理，结果回来后会在这里继续。');
    expect(container.querySelector('[data-testid="thread-item-retry-turn"]')).toBeNull();
    expect(parent.timeline.status).toBe('done');
    expect(Boolean(parent.timeline.delegationInFlight)).toBe(stillWorking);
    expect(block.querySelector('.run-disclosure-title')?.textContent).toBe(stillWorking ? '运行中' : '完成');
    expect(parent.timeline.durationMs).toBe(108805);
    expect(parent.timeline.durationIsFinal).toBe(true);
    expect(block.textContent).not.toContain('失败');
    // Existing Chinese duration copy includes spaces; keep the exact typed
    // 108805 ms assertion above and the existing localized formatter here.
    expect(block.textContent).toContain('1 分 49 秒');
    if (!stillWorking) {
      const answer = model.cells.find(cell => cell.kind === 'assistant_final' && cell.message.content.includes('大家好'));
      expect(answer?.kind).toBe('assistant_final');
      if (answer?.kind === 'assistant_final') expect(isSameSessionRunId(agentChatMessageRunId(answer.message), parentRun)).toBe(false);
      expect(container.textContent).toContain('大家好，我是新同事，负责整理周报。');
      expect(container.textContent).toContain('同事已完成这项工作。');
    }
    unmount();
  }
});

it.each(['live', 'reload'])('R26a: each check keeps its running observation and waiting marker after delivery (%s)', mode => {
  const messages = projectBoth(events)[mode === 'live' ? 0 : 1];
  const { parent, block } = mount(messages);
  const checks = parent.timeline.steps.filter(step => ['check_async_task', 'task_output'].includes(
    (step.details as { toolName?: string })?.toolName || ''));
  expect(checks).toHaveLength(5);
  for (const step of checks) {
    expect((step.details as { memberSummary: { state: string } }).memberSummary.state).toBe('in_progress');
  }
  const checkRows = [...block.querySelectorAll<HTMLElement>('.run-step, .run-tool-history-item')]
    .filter(row => row.textContent?.includes('同事正在处理'));
  expect(checkRows).toHaveLength(5);
  expect(checkRows.every(row => !row.textContent?.includes('同事已完成'))).toBe(true);
  expect(checkRows.every(row => row.dataset.status === 'blocked'
    && row.querySelector('[role="img"][aria-label="等待同事"] .tabler-icon-clock'))).toBe(true);
});

it.each(['identical_terminal_task_read', 'identical_refused_result_read'])('R26a: %s retains its failure presentation', code => {
  const input = structuredClone(waiting);
  input.find(event => event.item_kind === 'runtime_failure')!.payload.failure_code = code;
  for (const messages of projectBoth(input)) {
    const { model, parent, container, unmount } = mount(messages);
    expect(model.header.status).toBe('failed');
    expect(parent.timeline.status).toBe('failed');
    expect(container.querySelector('.thread-item-error')).toBeTruthy();
    expect(container.querySelector('[data-testid="session-workbench-header"]')?.textContent).toContain('失败');
    unmount();
  }
});

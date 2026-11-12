// @vitest-environment jsdom
// R18a: a failed run's clock must span its real start and end, and an
// effect-free unknown-delivery timeout shows the plain provider-failure copy.
import React from 'react';
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { consumeSessionEnvelope, hydrateSessionTranscriptEvents, projectSessionEventStoreToMessages } from '../agent-detail/sessionEventConsumer';
import type { ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import type { SessionEventV2 } from './sessionEventStore';
import { buildThreadTimeline } from './timelineModel';
import { ThreadItemRenderer } from './ThreadItemRenderer';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => {
  await i18n.init({ lng: 'zh', resources: { zh: { translation: zh }, en: { translation: en } }, initImmediate: false });
});
afterEach(cleanup);

const at = (seconds: number) => new Date(Date.UTC(2026, 10, 12, 0, 0, seconds)).toISOString();
function event(sequence: number, kind: string, lifecycle: string, seconds: number,
  runId: string | null = 'run-1', payload: Record<string, unknown> = {}): SessionEventV2 {
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant', item_id: kind === 'run' ? runId! : `item-${sequence}`, item_kind: kind,
    kind: `${kind}.${lifecycle}`, lifecycle, payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: runId ? { level: ['assistant_commentary', 'assistant_text', 'assistant_final', 'tool_call', 'tool_result', 'event'].includes(kind) ? 'round' : 'run', session_id: 'session', thread_id: 'session',
      turn_id: `turn-${runId}`, run_id: runId, ...(['assistant_commentary', 'assistant_text', 'assistant_final', 'tool_call', 'tool_result', 'event'].includes(kind) ? { round_id: `round-${runId}` } : {}) }
      : { level: 'session', session_id: 'session', thread_id: 'session' },
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    ...(runId ? { run_id: runId } : {}), occurred_at: at(seconds), persisted_at: at(seconds),
  } as SessionEventV2;
}
const user = (sequence: number, seconds = 0) => event(sequence, 'human_input', 'accepted', seconds, null,
  { intent: 'start_turn', content_parts: [{ type: 'text', text: '查一下公司知识库里有没有关于「白鹭计划」的资料' }] });
const toolPair = (sequence: number, seconds: number) => [
  event(sequence, 'tool_call', 'started', seconds, 'run-1', { name: 'search_company_kb', arguments: {} }),
  event(sequence + 1, 'tool_result', 'completed', seconds + 2, 'run-1',
    { name: 'search_company_kb', content: '{"status":"empty"}' }),
];
const messages = (events: SessionEventV2[]) =>
  hydrateSessionTranscriptEvents(events as unknown as ChatTranscriptEventPayload[]).messages;

it('keeps the queued-to-terminal span when the run ends needs_reconciliation', () => {
  // E2E run 15 shape: input and queued at t=0, ~8.7 min of searching, the
  // stream times out and the run is fenced needs_reconciliation at t=524.
  const events: SessionEventV2[] = [
    user(1),
    event(2, 'run', 'queued', 0, 'run-1', { run_id: 'run-1' }),
  ];
  let seq = 3;
  for (let index = 0; index < 6; index++) {
    events.push(...toolPair(seq, 40 + index * 80));
    seq += 2;
  }
  events.push(event(seq, 'run', 'needs_reconciliation', 524, 'run-1', { reason: 'ambiguous_provider_send' }));
  const model = buildThreadTimeline({ messages: messages(events), activeSession: { id: 'session' },
    isWaiting: false, isStreaming: false, runtimePhase: 'idle' });
  const failed = model.cells.filter(cell => cell.kind === 'active_run');
  expect(failed).toHaveLength(1);
  expect(failed[0].timeline).toMatchObject({
    status: 'failed', startedAt: at(0), completedAt: at(524), durationMs: 524_000, durationIsFinal: true,
  });
});

function failureCard(payload: Record<string, unknown>, expectedCode: string) {
  const failure: SessionEventV2 = {
    ...event(1, 'runtime_failure', 'recorded', 5, 'run-1', payload),
    payload_schema: 'hive.session.payload.runtime_failure.recorded.v2',
  };
  const { store } = consumeSessionEnvelope(failure as unknown as ChatTranscriptEventPayload, undefined, 0);
  if (!store) throw new Error('failure_event_not_consumed');
  const item = projectSessionEventStoreToMessages(store).find(message => message.eventType === 'runtime_failure')?.threadItem;
  expect(item?.item_data).toMatchObject({ code: expectedCode });
  return item!;
}

it.each(['zh', 'en'])('renders the transient timeout sentence without a retry promise for a timed-out stream in %s', async language => {
  await i18n.changeLanguage(language);
  // E2E run 15 round 30: ReadTimeout with delivery_state unknown, classified
  // failed because the request held only function tools. R18a round 2 ruling 4:
  // the copy names the real transient cause, not a configuration problem, and
  // offers no retry action (retry_safe stays false for unknown delivery).
  const item = failureCard({ status: 'failed', terminal_reason: 'provider_error', failure_code: 'timeout',
    delivery_state: 'unknown', retry_safe: false, retryable: true, requires_user_decision: false,
    content: '', message: '' }, 'timeout');
  const expected = language === 'zh'
    ? '模型服务响应超时，这次没有完成，请稍后再试。'
    : 'The model service timed out and this task did not finish. Please try again later.';
  const { container, queryByTestId } = render(<ThreadItemRenderer item={item} />);
  expect(container.textContent).toContain(expected);
  expect(container.textContent).not.toContain('请联系管理员');
  expect(container.textContent).not.toContain('Ask an administrator');
  expect(container.textContent).not.toContain('这次请求没有确认完成。');
  expect(queryByTestId('thread-item-retry-turn')).toBeNull();  // R18a round 3 nit d: no retry action
});

it.each(['zh', 'en'])('renders the plain nothing-found sentence when the empty-search guard stopped the run in %s', async language => {
  await i18n.changeLanguage(language);
  // R18a round 2 ruling 2: the loop guard's stop ends the run failed with
  // failure_code repeated_empty_search; the member card carries the same plain
  // sentence the delegation/business readers see.
  const item = failureCard({ status: 'failed', terminal_reason: 'loop_guard', failure_code: 'repeated_empty_search',
    retry_safe: false, retryable: false, requires_user_decision: false,
    content: '', message: '' }, 'repeated_empty_search');
  const expected = language === 'zh'
    ? '多次搜索都没有找到相关资料。'
    : 'Several searches found no matching material.';
  const { container, queryByTestId } = render(<ThreadItemRenderer item={item} />);
  expect(container.textContent).toContain(expected);
  expect(queryByTestId('thread-item-retry-turn')).toBeNull();  // R18a round 3 nit d: no retry action
});

it('still shows the delivery-unknown copy when the failure is not a timeout', async () => {
  await i18n.changeLanguage('zh');
  const item = failureCard({ status: 'failed', terminal_reason: 'provider_error', failure_code: 'provider_error',
    delivery_state: 'unknown', retry_safe: false, retryable: false, requires_user_decision: false,
    content: '', message: '' }, 'provider_error');
  const { container } = render(<ThreadItemRenderer item={item} />);
  expect(container.textContent).toContain('这次请求没有确认完成。');
});

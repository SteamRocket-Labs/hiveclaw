// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import creationTranscript from '../session-workbench/fixtures/r5i-creation.transcript.json';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { getRuntimeEventMessage, type ChatTranscriptEventPayload } from './chatRuntime';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import RunDisclosureBlock from './RunDisclosureBlock';

const locale = vi.hoisted(() => ({ language: 'zh' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
}) }));
afterEach(cleanup);

function expand(container: HTMLElement) {
  for (const toggle of container.querySelectorAll<HTMLButtonElement>('.run-disclosure-header[aria-expanded="false"]')) {
    fireEvent.click(toggle);
  }
  for (const toggle of container.querySelectorAll<HTMLButtonElement>('.run-step-toggle[aria-expanded="false"]')) {
    fireEvent.click(toggle);
  }
  for (const toggle of container.querySelectorAll<HTMLElement>('[data-testid="run-disclosure-tool-group-toggle"]')) {
    fireEvent.click(toggle);
  }
  for (const toggle of container.querySelectorAll<HTMLButtonElement>('.run-tool-history-toggle[aria-expanded="false"]')) {
    fireEvent.click(toggle);
  }
}

it.each(['zh', 'en'])('renders the recorded result integration metadata in %s, including opened details', language => {
  locale.language = language;
  const { messages } = hydrateSessionTranscriptEvents(creationTranscript.events as ChatTranscriptEventPayload[]);
  const results = messages.filter(message => message.threadItem?.metadata?.runtime_result_integration_page);
  expect(results).toHaveLength(2);
  const originalContents = results.map(message => message.content);
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(results)} />);
  expand(container);
  const rows = container.querySelectorAll('[data-testid="run-disclosure-step"]');
  expect(rows).toHaveLength(2);
  for (const row of rows) {
    expect(row.textContent).toContain(language === 'zh' ? '收到 1 项任务结果' : 'Received 1 task result');
    expect(row.textContent).not.toMatch(/runtime|epoch|terminal|expected|integration|[a-f0-9]{8}-/i);
  }
  expect(results.map(message => message.content)).toEqual(originalContents);
  expect(originalContents[0]).toContain('integration epoch 1');
});

it.each(['zh', 'en'])('keeps denied results available for review in %s without exposing the reason code', language => {
  locale.language = language;
  const message = getRuntimeEventMessage({ type: 'agent_task_notification',
    message: '2 runtime result(s) ready in integration epoch 9 (terminal=2/expected=3). Parent continuation denied: requester_inactive.',
    runtime_result_integration_page: true, item_count: 2, continuation_status: 'denied', continuation_denial: 'requester_inactive' });
  expect(message).toBeTruthy();
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([message!])} />);
  expand(container);
  expect(container.textContent).toContain(language === 'zh' ? '收到 2 项任务结果' : 'Received 2 task results');
  expect(container.textContent).toContain(language === 'zh' ? '结果已保留，可供查看；本次对话未继续。' : 'Results are kept for review; this conversation did not continue.');
  expect(container.textContent).not.toMatch(/runtime|epoch|terminal|expected|requester_inactive|denied/i);
});

it.each(['zh', 'en'])('localizes other member-visible system producers in %s and keeps model prose', language => {
  locale.language = language;
  const events = ['tool_group_activation', 'pack_activation', 'deferred_tools_delta', 'hook_progress', 'hook_summary',
    'hook_attachment', 'hook_blocked', 'workflow_run', 'workflow_step', 'team_member', 'schedule_fire',
    'agent_task_notification', 'runtime_action_completed'];
  const messages = events.map((type, index) => getRuntimeEventMessage({ type, id: `event-${index}`,
    message: 'INTERNAL_PRODUCER_TEXT command_hook 81efc180-9869-44af-8188-e33c25e3d27d',
    status: 'completed', task_notification: type === 'agent_task_notification', tool_groups: ['filesystem'],
  })!);
  expect(messages.every(Boolean)).toBe(true);
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    ...messages, { role: 'assistant', content: 'Keep this exact model progress.', eventType: 'assistant_commentary' },
  ])} />);
  expand(container);
  expect(container.textContent).toContain('Keep this exact model progress.');
  expect(container.textContent).not.toMatch(/INTERNAL_PRODUCER_TEXT|command_hook|81efc180|filesystem|Workflow step|Runtime Tool|Hook /);
  expect(container.querySelectorAll('[data-testid="run-disclosure-step"], .run-tool-history-item').length).toBe(events.length);
});

it('preserves task result prose and the member objective while translating system labels', () => {
  locale.language = 'zh';
  const messages = [
    getRuntimeEventMessage({ type: 'agent_task_notification', status: 'completed',
      message: 'Background task completed: Original task result.', summary: 'Original task result.' })!,
    getRuntimeEventMessage({ type: 'child_session', status: 'completed', message: 'Original delegated result.' })!,
    getRuntimeEventMessage({ type: 'goal', status: 'running', message: 'Goal started: Original member objective.',
      objective: 'Original member objective.' })!,
  ];
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
  expand(container);
  for (const text of ['Original task result.', 'Original delegated result.', 'Original member objective.']) {
    expect(container.textContent).toContain(text);
  }
  expect(container.textContent).not.toMatch(/Background task completed|Goal started/);
});

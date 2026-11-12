// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import creationTranscript from '../session-workbench/fixtures/r5i-creation.transcript.json';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { getRuntimeEventMessage, type AgentChatMessage, type ChatTranscriptEventPayload } from './chatRuntime';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import RunDisclosureBlock from './RunDisclosureBlock';

const locale = vi.hoisted(() => ({ language: 'zh' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
}) }));
afterEach(cleanup);

function mount(messages: AgentChatMessage[]) {
  const result = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
  for (const selector of ['.run-disclosure-header[aria-expanded="false"]', '.run-step-toggle[aria-expanded="false"]']) {
    for (const button of result.container.querySelectorAll<HTMLButtonElement>(selector)) fireEvent.click(button);
  }
  return result.container;
}

it.each(['zh', 'en'])('keeps the recorded delegation sentence and actor in %s', language => {
  locale.language = language;
  const { messages } = hydrateSessionTranscriptEvents(creationTranscript.events as ChatTranscriptEventPayload[]);
  const delegated = messages.filter(message => message.eventType === 'runtime_action_completed'
    && message.content === '白鹭验收助手 已完成委派任务。');
  expect(delegated).toHaveLength(1);
  const row = mount(delegated).querySelector('[data-testid="run-disclosure-step"]')!;
  expect(row.textContent).toContain('白鹭验收助手 已完成委派任务。');
  expect(delegated[0].content).toBe('白鹭验收助手 已完成委派任务。');
});

it.each(['这个账号已停用，没有做任何更改。', '你无权访问这位数字员工。'])(
  'keeps the specific stored approval return: %s', sentence => {
    locale.language = 'zh';
    const message = getRuntimeEventMessage({ type: 'agent_task_notification', message: sentence,
      runtime_result_integration_page: true, item_count: 1, continuation_status: 'needs_reconciliation',
      continuation_reason: 'requester_inactive' })!;
    const row = mount([message]).querySelector('[data-testid="run-disclosure-step"]')!;
    expect(row.textContent).toContain(sentence);
    expect(row.textContent).not.toContain('结果已保留，可供查看');
    expect(message.content).toBe(sentence);
  },
);

it.each([
  { type: 'runtime_action_failed', message: '需要人工核查', action_kind: 'subagent', notification_source: 'subagent' },
  { type: 'workflow_run', message: 'Workflow run needs_reconciliation' },
  { type: 'agent_task_notification', message: 'Background task needs_reconciliation: 报告已保留。', summary: '报告已保留。' },
])('keeps manual review distinct from failure for $type', event => {
  locale.language = 'zh';
  const message = getRuntimeEventMessage({ status: 'needs_reconciliation', ...event })!;
  const row = mount([message]).querySelector('[data-testid="run-disclosure-step"]')!;
  expect(row.textContent).toContain('需要人工核查');
  expect(row.textContent).not.toContain('失败');
});

it.each(['zh', 'en'])('keeps the team-member name when translating its template in %s', language => {
  locale.language = language;
  const message = getRuntimeEventMessage({ type: 'team_member', status: 'created', member_name: '白鹭验收助手',
    message: 'Team member created: 白鹭验收助手' })!;
  const row = mount([message]).querySelector('[data-testid="run-disclosure-step"]')!;
  expect(row.textContent).toContain('白鹭验收助手');
  expect(row.textContent).toContain(language === 'zh' ? '已添加数字员工' : 'Digital employee added');
  expect(row.textContent).not.toContain('Team member created:');
});

it('keeps the recorded child-session title and original result prose', () => {
  locale.language = 'zh';
  const { messages } = hydrateSessionTranscriptEvents(creationTranscript.events as ChatTranscriptEventPayload[]);
  const children = messages.filter(message => message.eventType === 'child_session'
    && message.eventReason === 'delegation_completed');
  expect(children).toHaveLength(1);
  const row = mount(children).querySelector('[data-testid="run-disclosure-step"]')!;
  // X1: a completed delegation receipt titles the direction it traveled.
  expect(row.querySelector('.run-step-title')?.textContent).toBe('同事交回的结果');
  expect(row.textContent).toContain(children[0].content);
});

it('keeps every unmatched plain system sentence, including names and failure details', () => {
  locale.language = 'zh';
  const events = ['runtime_action_started', 'runtime_action_failed', 'team_member', 'workflow_step', 'agent_task_notification'];
  const messages = events.map((type, index) => getRuntimeEventMessage({ type, id: `plain-${index}`,
    status: 'failed', message: `白鹭验收助手 的任务没有完成：请补充第 ${index + 1} 份资料。`,
    ...(type === 'runtime_action_failed' ? { action_kind: 'a2a_delegation', notification_source: 'a2a' } : {}),
    runtime_result_integration_page: type === 'agent_task_notification', item_count: 1,
  })!);
  const container = mount(messages);
  for (const message of messages) expect(container.textContent).toContain(message.content);
});

it('leaves the company once-only approval on its dedicated surface', () => {
  locale.language = 'zh';
  const message = getRuntimeEventMessage({ type: 'permission', status: 'permission_resolved', tool_name: 'list_files',
    decision: 'allow_once', message: "Company approval authorized this exact 'list_files' request once." })!;
  const timeline = buildRunTimelineFromMessages([message]);
  expect(timeline.steps[0].presentation).toBe('external');
  expect(mount([message]).querySelector('[data-testid="run-disclosure-step"]')).toBeNull();
  expect(message.content).toBe("Company approval authorized this exact 'list_files' request once.");
});

it('keeps a task actor whose name happens to equal the producer fallback', () => {
  locale.language = 'zh';
  const message = getRuntimeEventMessage({ type: 'agent_task_notification', status: 'completed',
    child_agent_name: 'Background task', summary: '报告已准备好。', message: 'Background task completed: 报告已准备好。' })!;
  const row = mount([message]).querySelector('[data-testid="run-disclosure-step"]')!;
  expect(row.textContent).toContain('Background task');
  expect(row.textContent).toContain('报告已准备好。');
});

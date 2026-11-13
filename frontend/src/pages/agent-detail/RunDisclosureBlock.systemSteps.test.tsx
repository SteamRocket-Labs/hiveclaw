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
    runtime_result_integration_page: true, item_count: 2, continuation_status: 'denied', continuation_denial: 'requester_inactive',
    // X3b item 7: a genuine kept result needs a typed body or artifact fact.
    result_manifest: { items: [{ terminal_status: 'completed', result_size_bytes: 500, artifact_count: 0 }] } });
  expect(message).toBeTruthy();
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([message!])} />);
  expand(container);
  expect(container.textContent).toContain(language === 'zh' ? '收到 2 项任务结果' : 'Received 2 task results');
  expect(container.textContent).toContain(language === 'zh' ? '结果已保留，可供查看；本次对话未继续。' : 'Results are kept for review; this conversation did not continue.');
  expect(container.textContent).not.toMatch(/runtime|epoch|terminal|expected|requester_inactive|denied/i);
});

it.each(['zh', 'en'])('localizes other member-visible system producers in %s and keeps model prose', language => {
  locale.language = language;
  const stepId = '81efc180-9869-44af-8188-e33c25e3d27d';
  const events = [
    { type: 'tool_group_activation', message: 'Activated MCP runtime tool group.' },
    { type: 'pack_activation', message: 'Activated runtime tool groups for this task.' },
    { type: 'deferred_tools_delta', message: 'Discovered deferred tools: read_file, list_files' },
    { type: 'workflow_run', message: 'Workflow run completed' },
    { type: 'workflow_step', message: `Workflow step ${stepId} completed`, workflow_step_id: stepId },
    { type: 'team_member', message: 'Team member created: 白鹭助手', member_name: '白鹭助手' },
    { type: 'schedule_fire', message: 'Workflow trigger fired: 周报计划', trigger_name: '周报计划' },
    { type: 'agent_task_notification', message: 'Background task completed: 报告已准备好。', summary: '报告已准备好。' },
    { type: 'runtime_action_completed', message: `Workflow step ${stepId} completed`, workflow_step_id: stepId, action_kind: 'workflow' },
    { type: 'runtime_action_started', message: 'Subagent 资料助手 is running in the background.', target_agent_name: '资料助手' },
    { type: 'goal', message: 'Goal started: 整理本周资料', objective: '整理本周资料' },
    { type: 'file_changes', message: 'file_changes', file_change_count: 2 },
  ];
  const messages = events.map((event, index) => getRuntimeEventMessage({ id: `event-${index}`,
    status: 'completed', tool_groups: ['filesystem'], ...event })!);
  expect(messages.every(Boolean)).toBe(true);
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    ...messages, { role: 'assistant', content: 'Keep this exact model progress.', eventType: 'assistant_commentary' },
  ])} />);
  expand(container);
  expect(container.textContent).toContain('Keep this exact model progress.');
  expect(container.textContent).not.toMatch(/INTERNAL_PRODUCER_TEXT|command_hook|81efc180|filesystem|Workflow step|Runtime Tool|Hook /);
  expect(container.textContent).not.toMatch(/read_file|list_files|Workflow run|Team member created:|Subagent |Goal started:/);
  expect(container.textContent).not.toContain('file_changes');
  expect(container.textContent).toContain(language === 'zh' ? '已更新 2 个文件' : 'Updated 2 files');
  for (const text of ['白鹭助手', '周报计划', '报告已准备好。', '资料助手', '整理本周资料']) {
    expect(container.textContent).toContain(text);
  }
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

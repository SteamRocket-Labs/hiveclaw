// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
}) }));
afterEach(cleanup);

it.each([
  ['delegate_to_agent', '委派工作'],
  ['check_async_task', '查看同事进度'],
  ['list_async_tasks', '查看同事进度'],
  ['task_output', '读取任务结果'],
  ['read_runtime_result', '读取任务结果'],
  ['send_message_to_agent', '联系同事'],
  ['send_agent_session_message', '补充同事任务说明'],
])('distinguishes the member action for %s', (toolName, title) => {
  const timeline = buildRunTimelineFromMessages([{ role: 'tool_call', content: '', toolName, toolStatus: 'done' }]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  fireEvent.click(container.querySelector('.run-disclosure-header')!);
  expect(container.querySelector('.run-step-title, .run-tool-history-title')?.textContent).toBe(title);
});

it.each(['pending', 'running'])('does not claim a returned result for a %s delegate', status => {
  const result = JSON.stringify({ ok: true, operation: 'delegate', status,
    task_id: 'private-task-id', child_session_id: 'private-session-id' });
  const timeline = buildRunTimelineFromMessages([{ role: 'tool_call', content: '', toolName: 'delegate_to_agent',
    toolStatus: 'done', toolResult: result, toolRawResult: result }]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  fireEvent.click(container.querySelector('.run-disclosure-header')!);
  fireEvent.click(container.querySelector('.run-step-toggle, .run-tool-history-toggle')!);
  expect(container.textContent).toContain('同事正在处理');
  expect(container.textContent).not.toMatch(/已返回结果|private-/);
});

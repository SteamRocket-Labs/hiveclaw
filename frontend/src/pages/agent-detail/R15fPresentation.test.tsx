// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import { readFileSync } from 'node:fs';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { ArtifactCards } from './ArtifactSurface';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages, type RunTimelineSnapshot } from './chatDisclosureReducer';
import { getRuntimeEventMessage } from './chatRuntime';

const translate = (key: string, fallback?: string, values?: Record<string, unknown>) =>
  translateFromCatalog(zh, key, fallback, values);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
beforeEach(() => { vi.spyOn(i18n, 't').mockImplementation(translate as never); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); sessionStorage.clear(); });

const phoneRules = [...readFileSync('src/index.css', 'utf8').matchAll(/@media\s*\(max-width:\s*600px\)\s*\{([\s\S]*?)\n\}/g)]
  .map(match => match[1]).join('\n');
function rule(selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return phoneRules.match(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`))?.[1] || '';
}

it('R15f.1 keeps the full filename, extension and metadata without hidden action width at 390px', () => {
  const name = 'sales-report-sales-run11-2026-11-20.md';
  const { container } = render(<ArtifactCards agentId="bot" artifacts={[
    { name, path: `workspace/${name}`, previewKind: 'markdown', size: 8192 },
  ]} />);
  expect(container.querySelector('.chat-artifact-name')?.textContent).toBe(name);
  expect(container.querySelector('.chat-artifact-meta')?.textContent).toBe('markdown · 8 KB');
  expect(rule('.chat-artifact-actions')).toMatch(/display:\s*none/);
  expect(phoneRules).toMatch(/\.chat-artifact-row:focus-within \.chat-artifact-actions\s*\{[^}]*display:\s*inline-flex/);
  expect(rule('.chat-artifact-main')).toMatch(/flex-wrap:\s*wrap/);
  expect(rule('.chat-artifact-name')).toMatch(/white-space:\s*normal/);
  expect(rule('.chat-artifact-name')).toMatch(/overflow-wrap:\s*anywhere/);
  expect(rule('.chat-artifact-meta')).toMatch(/flex:\s*0 0 100%/);
});

it('R15f.2 wraps between complete duration, aggregate and step-count items at 390px', () => {
  const timeline: RunTimelineSnapshot = { id: 'stopped', status: 'cancelled', durationMs: 87_000,
    summary: { files: 1, searches: 6, commands: 0 }, steps: Array.from({ length: 18 }, (_, index) => ({
      id: `step-${index}`, kind: 'search', title: 'Search web', status: 'done', visibility: 'collapsed',
    })) };
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expect(container.querySelector('.run-disclosure-duration')?.textContent).toBe('1 分 27 秒');
  expect(container.querySelector('.run-disclosure-count')?.textContent).toBe('18 个步骤');
  expect([...container.querySelectorAll('.run-disclosure-stat')].map(node => node.textContent?.replace(/^ · /, '')))
    .toEqual(['读取 1 个文件', '搜索网页 6 次']);
  expect(rule('.run-disclosure-header')).toMatch(/flex-wrap:\s*wrap/);
  for (const selector of ['.run-disclosure-duration', '.run-disclosure-stat', '.run-disclosure-count']) {
    expect(phoneRules).toMatch(new RegExp(`${selector.replaceAll('.', '\\.')}[^}]*white-space:\\s*nowrap`));
    expect(phoneRules).toMatch(new RegExp(`${selector.replaceAll('.', '\\.')}[^}]*flex-shrink:\\s*0`));
  }
  expect(rule('.run-disclosure-summary.is-aggregate')).toMatch(/display:\s*contents/);
});

const budget = '[Runtime Limit] This turn stopped because the configured token budget was exhausted (1001932/1000000 tokens used).';
const roundLimit = 'I reached the configured tool-round limit (100) before I could finish. The current state has been saved; continue the task to resume from here, or raise max_tool_rounds for this agent.';
function failures(reason: string, childTask = 'task-1') {
  return [
    getRuntimeEventMessage({ id: 'status', type: 'runtime_action_failed', status: 'failed',
      runtime_task_id: 'task-1', action_kind: 'a2a_delegation', target_agent_name: '周报助手',
      message: `周报助手 的委派任务失败：${reason}` })!,
    getRuntimeEventMessage({ id: 'child', type: 'child_session', status: 'failed', runtime_task_id: childTask,
      message: reason, to_agent_name: '周报助手' })!,
  ];
}

it('R15f.4 states the same delegated failure once and retains the employee name', () => {
  const reason = '这次用完了用量预算，没有完成。';
  const messages = failures(reason);
  const before = JSON.stringify(messages);
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
  expect(container.textContent?.split(reason)).toHaveLength(2);
  expect(container.textContent).toContain('周报助手');
  fireEvent.click(container.querySelector('.run-step-toggle')!);
  expect(container.textContent?.split(reason)).toHaveLength(2);
  expect(JSON.stringify(messages)).toBe(before);
});

it('keeps separate tasks and unmatched failure sentences even when their reasons agree', () => {
  const messages = failures('请补充附件。', 'another-task');
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
  expect(container.querySelectorAll('[data-testid="run-disclosure-step"]')).toHaveLength(2);
  expect(container.textContent?.split('请补充附件。')).toHaveLength(3);
});

it.each([
  [budget, '这次用完了用量预算，没有完成。'],
  [roundLimit, '这次已达到运行轮次上限，没有完成。可以重试继续。'],
])('R15f.5 maps the exact old runtime failure at display time: %s', (reason, chinese) => {
  const messages = failures(reason);
  const before = JSON.stringify(messages);
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
  expect(container.textContent).toContain(chinese);
  expect(container.textContent).not.toMatch(/Runtime Limit|1001932|1000000|tool-round limit|max_tool_rounds/);
  expect(container.textContent?.split(chinese)).toHaveLength(2);
  expect(JSON.stringify(messages)).toBe(before);
});

it.each([
  '请补充第 2 份资料。', `${budget} Additional details.`,
  '[Runtime Limit] This turn stopped before the next tool action.',
  'I reached the configured tool-round limit (100) before I could finish. Custom recovery.',
])('leaves other stored failure text intact: %s', reason => {
  const message = getRuntimeEventMessage({ type: 'child_session', status: 'failed', message: reason })!;
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([message])} />);
  fireEvent.click(container.querySelector('.run-step-toggle')!);
  expect(container.querySelector('.md-content')?.textContent).toContain(reason);
  expect(message.content).toBe(reason);
});

function keptResults(): RunTimelineSnapshot {
  const messages = [
    getRuntimeEventMessage({ id: 'integration', type: 'agent_task_notification',
      message: '1 runtime result(s) ready in integration epoch 9 (terminal=1/expected=1). Parent continuation denied: parent_stopped.',
      runtime_result_integration_page: true, item_count: 1, continuation_status: 'denied' })!,
    getRuntimeEventMessage({ id: 'result', type: 'child_session', status: 'completed',
      message: `**要点一｜${'完成报告'.repeat(44)}**\n\n- 核对资料\n\n查看 [报告](https://example.test/report) 和 \`摘要\`。` })!,
  ];
  return { ...buildRunTimelineFromMessages(messages), durationMs: 3000 };
}

it('R15f.6 labels kept results without claiming completion or displaying a growing duration', () => {
  vi.useFakeTimers();
  const timeline = keptResults();
  const view = render(<RunDisclosureBlock timeline={timeline} />);
  expect(view.container.querySelector('.run-disclosure-title')?.textContent).toBe('收到任务结果');
  expect(view.container.querySelector('.run-disclosure-duration')).toBeNull();
  act(() => { vi.advanceTimersByTime(90_000); });
  view.rerender(<RunDisclosureBlock timeline={{ ...timeline, durationMs: 93_000 }} />);
  expect(view.container.querySelector('.run-disclosure-title')?.textContent).toBe('收到任务结果');
  expect(view.container.querySelector('.run-disclosure-duration')).toBeNull();
});

it('R15f.6 renders the kept result preview without raw markdown even past the preview clipping point', () => {
  const { container } = render(<RunDisclosureBlock timeline={keptResults()} />);
  fireEvent.click(container.querySelector('.run-disclosure-header')!);
  const preview = container.querySelectorAll('.run-step-summary')[1];
  expect(preview?.textContent).toContain('要点一｜完成报告');
  expect(preview?.textContent).not.toMatch(/\*\*|`|\[报告\]|https:\/\//);
});

it('retains the completed run label and duration for ordinary successful work', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'done', status: 'done', durationMs: 3000,
    steps: [{ id: 'result', kind: 'event', title: 'Runtime event', status: 'done', visibility: 'collapsed' }] }} />);
  expect(container.querySelector('.run-disclosure-title')?.textContent).toBe('完成');
  expect(container.querySelector('.run-disclosure-duration')?.textContent).toBe('3 秒');
});

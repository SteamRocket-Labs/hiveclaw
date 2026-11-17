// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import { getRuntimeEventMessage } from './chatRuntime';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
}) }));
afterEach(() => { cleanup(); locale.language = 'zh'; });

function expand(container: HTMLElement) {
  for (let level = 0; level < 3; level++) {
    for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
    for (const details of container.querySelectorAll('details')) details.open = true;
  }
}

const rawMessage = "The delegated instruction's upstream sources are not currently authorized or available. Revise or reconcile this input.";
const reasons = [
  ['delegation_instruction_source_access_denied', '同事无法使用这次任务需要的资料，没有继续。', 'The colleague could not use the materials needed for this task and did not continue.'],
  ['delegation_instruction_source_unavailable', '同事暂时无法读取这次任务需要的资料，没有继续。', 'The colleague could not read the materials needed for this task and did not continue.'],
  ['unknown_delegation_reason', '同事没有完成这项工作。', 'The colleague did not finish this work.'],
] as const;
it.each((['zh', 'en'] as const).flatMap(language => reasons.map(([code, chinese, english]) =>
  ({ language, code, sentence: language === 'zh' ? chinese : english }))))(
  'localizes delegation refusal $code by reason, never backend prose ($language)', ({ language, code, sentence }) => {
    locale.language = language;
    const events = [
      { id: 'action', sequence: 1, event_type: 'runtime_action_failed', role: 'system',
        content: `Alice 的委派任务失败：${rawMessage}`, metadata: { action_kind: 'a2a_delegation', status: 'failed',
          runtime_task_id: 'child', target_agent_name: 'Alice', reason: code } },
      { id: 'child', sequence: 2, event_type: 'child_session', role: 'system', content: rawMessage,
        metadata: { reason: code, status: 'failed', runtime_task_id: 'child', to_agent_name: 'Alice' } },
    ];
    const messages = hydrateSessionTranscriptEvents(events, 0).messages;
    const before = JSON.stringify(messages);
    const { container, unmount } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
    expand(container);
    expect(container.textContent).toContain(sentence);
    expect(container.textContent).toContain('Alice');
    expect(container.querySelectorAll('[data-testid="run-disclosure-step"]')).toHaveLength(1);
    expect(container.textContent).not.toMatch(/upstream|reconcile|receipt|authority|source|委派任务失败：/);
    expect(container.textContent).not.toContain(rawMessage);
    expect(JSON.stringify(messages)).toBe(before);
    unmount();
  });

it.each(['zh', 'en'] as const)('hides arbitrary backend user_message for unknown delegation failures (%s)', language => {
  locale.language = language;
  const message = getRuntimeEventMessage({ type: 'runtime_action_failed', status: 'failed', action_kind: 'a2a_delegation',
    target_agent_name: 'Alice', error_code: 'future_reason', message: 'BACKEND PRIVATE PROSE' })!;
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([message])} />);
  expand(container);
  expect(container.textContent).toContain(language === 'zh' ? '同事没有完成这项工作。' : 'The colleague did not finish this work.');
  expect(container.textContent).not.toContain('BACKEND PRIVATE PROSE');
});

const tools = [
  ['save_skill', ['保存技能', '正在保存技能。', '已保存技能。', '这次没有保存技能。', '保存技能超时。'],
    ['Save skill', 'Saving a skill.', 'Saved a skill.', 'The skill was not saved this time.', 'Saving the skill timed out.']],
  ['reconcile_skill_candidate', ['检查技能保存情况', '正在检查技能保存情况。', '已检查技能保存情况。', '这次没有检查技能保存情况。', '检查技能保存情况超时。'],
    ['Check skill saving', 'Checking whether the skill was saved.', 'Checked whether the skill was saved.',
      'Could not check whether the skill was saved.', 'Checking whether the skill was saved timed out.']],
] as const;
it.each((['zh', 'en'] as const).flatMap(language => tools.flatMap(([toolName, chinese, english]) =>
  ['running', 'done', 'failed', 'timeout'].map((state, index) => ({ language, toolName, state,
    title: (language === 'zh' ? chinese : english)[0], sentence: (language === 'zh' ? chinese : english)[index + 1] })))))(
  'names $toolName and its $state sentence and group ($language)', ({ language, toolName, state, title, sentence }) => {
    locale.language = language;
    const timeline = buildRunTimelineFromMessages([{ id: 'tool', role: 'tool_call', content: '', toolName,
      toolStatus: state === 'running' ? 'running' : 'done',
      ...(['failed', 'timeout'].includes(state) ? { toolResult: `<tool_error>${JSON.stringify({ ok: false,
        error_class: state === 'timeout' ? 'timeout' : 'execution_error' })}</tool_error>` } : {}) }],
    { activeRun: state === 'running' });
    const { container, unmount } = render(<RunDisclosureBlock timeline={timeline} />);
    expand(container);
    const heading = container.querySelector('.run-step-title, .run-tool-history-title');
    expect([...heading?.childNodes || []].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('')).toBe(title);
    expect(container.querySelector('p')?.textContent).toBe(sentence);
    const group = container.querySelector('.run-tool-group-label');
    if (state === 'running' || state === 'done') expect(group?.textContent).toContain(title);
    expect(container.textContent).not.toMatch(/处理请求|其他操作|本次操作：读取文件|Tool\. Result returned/);
    unmount();
  });

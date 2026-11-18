// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
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

const budget = '[Runtime Limit] This turn stopped because the configured token budget was exhausted (1001932/1000000 tokens used).';
const rounds = 'I reached the configured tool-round limit (100) before I could finish. The current state has been saved; continue the task to resume from here, or raise max_tool_rounds for this agent.';
const limits = [
  [budget, '这次用完了用量预算，没有完成。', 'This turn used up its usage budget and did not finish.'],
  [rounds, '这次已达到运行轮次上限，没有完成。可以重试继续。', 'This turn reached its round limit and did not finish. Retry to continue.'],
] as const;
it.each((['zh', 'en'] as const).flatMap(language => ['delegation_terminal', 'delegation_failed'].flatMap(reason =>
  limits.map(([message, chinese, english]) => ({ language, reason, message, sentence: language === 'zh' ? chinese : english })))))(
  'keeps the localized limit on production delegation rows ($language, $reason, $message)', ({ language, reason, message, sentence }) => {
    locale.language = language;
    const messages = hydrateSessionTranscriptEvents([
      { id: 'action', sequence: 1, event_type: 'runtime_action_failed', role: 'system',
        content: `Alice 的委派任务失败：${message}`, metadata: { action_kind: 'a2a_delegation', status: 'failed',
          runtime_task_id: 'child', target_agent_name: 'Alice', reason } },
      { id: 'child', sequence: 2, event_type: 'child_session', role: 'system', content: message,
        metadata: { reason, status: 'failed', runtime_task_id: 'child', to_agent_name: 'Alice' } },
    ], 0).messages;
    const before = JSON.stringify(messages);
    const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
    expand(container);
    expect(container.querySelectorAll('[data-testid="run-disclosure-step"]')).toHaveLength(1);
    expect(container.textContent?.split(sentence)).toHaveLength(2);
    expect(container.textContent).toContain('Alice');
    expect(container.textContent).not.toMatch(/Runtime Limit|tool-round limit|max_tool_rounds/);
    expect(JSON.stringify(messages)).toBe(before);
  });

it.each((['zh', 'en'] as const).flatMap(language => [
  ['delegation_instruction_source_access_denied', '同事无法使用这次任务需要的资料，没有继续。', 'The colleague could not use the materials needed for this task and did not continue.'],
  ['delegation_instruction_source_unavailable', '同事暂时无法读取这次任务需要的资料，没有继续。', 'The colleague could not read the materials needed for this task and did not continue.'],
].map(([reason, chinese, english]) => ({ language, reason, sentence: language === 'zh' ? chinese : english }))))(
  'prefers the known delegation code over a legacy limit sentence ($language, $reason)', ({ language, reason, sentence }) => {
    locale.language = language;
    const messages = hydrateSessionTranscriptEvents([{ id: 'child', sequence: 1, event_type: 'child_session', role: 'system',
      content: budget, metadata: { reason, status: 'failed', runtime_task_id: 'child', to_agent_name: 'Alice' } }], 0).messages;
    const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
    expand(container);
    expect(container.textContent).toContain(sentence);
    expect(container.textContent).not.toContain(language === 'zh' ? '这次用完了用量预算' : 'used up its usage budget');
  });

/** Native member calls carry args_hash, not arguments; results keep their typed JSON. */
function nativeTool(toolName: string, result: string, failed = false) {
  return [
    { item_kind: 'tool_call', lifecycle: 'started', payload: { tool_name: toolName, args_hash: 'a'.repeat(64) } },
    { item_kind: 'tool_call', lifecycle: failed ? 'failed' : 'completed', payload: { outcome: failed ? 'failed' : 'success' } },
    { item_kind: 'tool_result', lifecycle: 'completed', payload: { tool_name: toolName, result, content: result, outcome: failed ? 'failed' : 'success' } },
  ].map((part, index) => ({ ...part, schema: 'hive.session_event', schema_version: 2,
    event_id: `event-${index}`, sequence: index + 1, tenant_id: 'tenant', item_id: `${part.item_kind}-invocation`,
    invocation_id: 'invocation', provider_tool_use_id: 'provider-call',
    kind: `${part.item_kind}.${part.lifecycle}`, payload_schema: `hive.session.payload.${part.item_kind}.${part.lifecycle}.v2`,
    scope: { level: 'round', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run', round_id: 'round' },
    actor: { type: part.item_kind === 'tool_call' ? 'assistant' : 'tool' }, visibility: { audience: 'direct_user' },
    occurred_at: '2026-11-17T04:06:52Z', persisted_at: '2026-11-17T04:06:52Z',
  }));
}

const submitted = '🟡 save_skill submitted for review at evolution/skill_candidates/candidate/\n- active_skill_created: false';
const activated = JSON.stringify({ schema: 'hive.reviewed_skill_activation_result.v1', status: 'provisional', active_skill_created: true });
const captured = JSON.stringify({ schema: 'hive.skill_candidate_operation.v1', status: 'captured', active_skill_created: false });
const cancelled = JSON.stringify({ schema: 'hive.skill_candidate_operation.v1', status: 'cancelled', active_skill_created: false });
const skillResults: Array<{ name: string; toolName: string; args?: Record<string, unknown>; result: string; zh: string; en: string }> = [
  { name: 'submission', toolName: 'save_skill', args: {}, result: submitted,
    zh: '已提交技能，审核通过后才能使用。', en: 'Submitted the skill for review; it can be used after approval.' },
  { name: 'reviewed activation', toolName: 'save_skill', args: { reviewed_revision_id: 'reviewed' }, result: activated,
    zh: '已保存技能。', en: 'Saved a skill.' },
  { name: 'native reviewed activation', toolName: 'save_skill', result: activated,
    zh: '已保存技能。', en: 'Saved a skill.' },
  { name: 'native already installed', toolName: 'save_skill', result: JSON.stringify({ schema: 'hive.reviewed_skill_activation_result.v1', status: 'already_installed', active_skill_created: false }),
    zh: '已保存技能。', en: 'Saved a skill.' },
  { name: 'status', toolName: 'reconcile_skill_candidate', args: { action: 'status' }, result: captured,
    zh: '已检查技能保存情况。', en: 'Checked whether the skill was saved.' },
  { name: 'retry', toolName: 'reconcile_skill_candidate', args: { action: 'retry' }, result: captured,
    zh: '已提交技能，审核通过后才能使用。', en: 'Submitted the skill for review; it can be used after approval.' },
  { name: 'cancel', toolName: 'reconcile_skill_candidate', args: { action: 'cancel' }, result: cancelled,
    zh: '已取消这次技能提交。', en: 'Cancelled this skill submission.' },
  { name: 'native cancel', toolName: 'reconcile_skill_candidate', result: cancelled,
    zh: '已取消这次技能提交。', en: 'Cancelled this skill submission.' },
  { name: 'native submission without arguments', toolName: 'save_skill', result: submitted,
    zh: '技能保存请求已返回结果。', en: 'The skill save request returned a result.' },
  { name: 'native status or retry without arguments', toolName: 'reconcile_skill_candidate', result: captured,
    zh: '技能保存操作已返回结果。', en: 'The skill request returned a result.' },
];
it.each((['zh', 'en'] as const).flatMap(language => skillResults.map(test => ({ ...test, language, sentence: test[language] }))))(
  'describes the real skill result: $name ($language)', ({ language, toolName, args, result, sentence }) => {
    locale.language = language;
    const messages = args ? [{ id: 'tool', role: 'tool_call' as const, content: '', toolName, toolArgs: args,
      toolStatus: 'done' as const, toolResult: result }]
      : hydrateSessionTranscriptEvents(nativeTool(toolName, result) as never, 0).messages;
    const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
    expand(container);
    expect(container.querySelector('p')?.textContent).toBe(sentence);
    expect(container.textContent).not.toMatch(/active_skill_created|hive\.|skill_candidates/);
  });

const failures = [
  ['approval_required', '这次操作需要你确认后才能继续。', 'This operation needs your confirmation.'],
  ['provider_unavailable', '所需服务暂时不可用，操作未完成。', 'The required service is unavailable; the operation did not complete.'],
  ['timeout', null, null],
  ['execution_error', null, null],
] as const;
it.each((['zh', 'en'] as const).flatMap(language => ['save_skill', 'reconcile_skill_candidate'].flatMap(toolName =>
  failures.map(([errorClass, chinese, english]) => ({ language, toolName, errorClass, sentence: language === 'zh' ? chinese : english })))))(
  'preserves typed skill failures: $toolName $errorClass ($language)', ({ language, toolName, errorClass, sentence }) => {
    locale.language = language;
    const saving = toolName === 'save_skill';
    const timedOut = errorClass === 'timeout';
    const expected = sentence || (language === 'zh'
      ? saving ? timedOut ? '保存技能超时。' : '这次没有保存技能。'
        : timedOut ? '检查技能保存情况超时。' : '这次没有检查技能保存情况。'
      : saving ? timedOut ? 'Saving the skill timed out.' : 'The skill was not saved this time.'
        : timedOut ? 'Checking whether the skill was saved timed out.' : 'Could not check whether the skill was saved.');
    const result = `<tool_error>${JSON.stringify({ ok: false, tool_name: toolName, provider: 'skill_candidate',
      error_class: errorClass, outcome: errorClass === 'approval_required' ? 'approval_required' : 'unknown' })}</tool_error>`;
    const messages = hydrateSessionTranscriptEvents(nativeTool(toolName, result, true) as never, 0).messages;
    const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
    expand(container);
    expect(container.querySelector('p')?.textContent).toBe(expected);
    expect(container.textContent).not.toMatch(/tool_error|error_class|provider_unavailable/);
  });

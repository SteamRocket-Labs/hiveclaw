// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import type { ChatTranscriptEventPayload } from './chatRuntime';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import RunDisclosureBlock from './RunDisclosureBlock';

const locale = vi.hoisted(() => ({ language: 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
}) }));
afterEach(cleanup);

const modelSummary = '模型原写摘要：Keep  DOUBLE  spaces； Alice 的委派任务失败：请补充。';
type SentenceCase = {
  name: string;
  type: string;
  metadata: Record<string, unknown>;
  chinese: string;
  english: string;
};

const sentences: SentenceCase[] = [{
  name: 'delegation started', type: 'runtime_action_started',
  metadata: { status: 'running', action_kind: 'a2a_delegation', target_agent_name: 'Alice' },
  chinese: '已委派给 Alice，后台执行中。', english: 'Delegated to Alice; running in the background.',
}, ...[
  ['completed', 'runtime_action_completed', '已完成', 'Completed'],
  ['failed', 'runtime_action_failed', '执行失败', 'Failed'],
  ['killed', 'runtime_action_failed', '已停止', 'Stopped'],
  ['needs_reconciliation', 'runtime_action_failed', '需要人工核查', 'Review required'],
  ['blocked', 'runtime_action_blocked', '需要处理', 'Needs attention'],
].map(([status, type, chinese, english]) => ({
  name: `result integration ${status}`, type, chinese, english,
  metadata: { status, action_kind: 'subagent', task_type: 'subagent', notification_source: 'subagent',
    runtime_action_from_result_integration: true },
})), ...[
  ['requester_inactive', '这个账号已停用，没有做任何更改。', 'This account is inactive. No changes were made.'],
  ['agent_execute_denied', '你无权访问这位数字员工。', 'You do not have access to this employee.'],
  ['user_prompt_submit_blocked', '这次输入被规则阻止，未继续执行。', 'This input was blocked by a rule. Work did not continue.'],
  ['admission_refused', '这次输入未被接受，未继续执行。', 'This input was not accepted. Work did not continue.'],
  ['origin_run_not_suspended', '对话已停止或结束，本次未继续执行。', 'The conversation has stopped or ended. Work did not continue.'],
  ['approval_origin_unavailable', '原对话暂不可用，本次未继续执行。', 'The original conversation is unavailable. Work did not continue.'],
  ['approval_return_unavailable', '审批结果暂时无法送回，原任务未继续。', 'The approval result could not be returned. Work did not continue.'],
  ['unrecognized_reason', '审批结果已保留，本次对话未继续。', 'The approval result is kept; this conversation did not continue.'],
  ['', '审批结果已返回。', 'The approval result has returned.'],
].map(([reason, chinese, english]) => ({
  name: `approval return ${reason || 'delivered'}`, type: 'agent_task_notification', chinese, english,
  metadata: { runtime_result_integration_page: true, task_type: 'runtime_result_integration', status: 'ready',
    item_count: 1, continuation_status: reason ? 'needs_reconciliation' : 'delivered',
    ...(reason ? { continuation_reason: reason } : {}),
    result_manifest: { items: [{ source_kind: 'approval', terminal_status: 'completed', result_size_bytes: 0, artifact_count: 0 }] } },
}))];

function transcript(sentence: SentenceCase): ChatTranscriptEventPayload[] {
  const events = [{
    id: 'platform-event', sequence: 1, event_type: sentence.type, role: 'system',
    content: sentence.chinese, metadata: sentence.metadata,
  }, {
    id: 'model-result', sequence: 2, event_type: 'child_session', role: 'system',
    content: modelSummary, metadata: { status: 'completed', reason: 'delegation_completed',
      runtime_task_id: 'other-task', to_agent_name: 'Alice' },
  }];
  return events.map(event => ({
    schema: 'hive.session_event_compatibility', schema_version: 1,
    compatibility_status: 'needs_reconciliation', reason: 'unmapped_legacy_kind',
    event_id: event.id, sequence: event.sequence, legacy_kind: 'legacy_unknown', legacy_lifecycle: 'legacy_unknown',
    legacy_event_type: event.event_type, legacy_item_type: 'event', legacy_item_status: 'succeeded',
    payload: { content: event.content, metadata: event.metadata },
  } as unknown as ChatTranscriptEventPayload));
}

function expand(container: HTMLElement) {
  for (const button of container.querySelectorAll<HTMLButtonElement>('.run-disclosure-header[aria-expanded="false"]')) fireEvent.click(button);
}

it.each(sentences)('localizes $name from typed facts and preserves the model summary', sentence => {
  for (const language of ['zh', 'en']) {
    locale.language = language;
    const events = transcript(sentence);
    const stored = JSON.stringify(events);
    const { messages } = hydrateSessionTranscriptEvents(events);
    expect(messages).toHaveLength(2);
    const before = JSON.stringify(messages);
    const { container, unmount } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
    expand(container);
    const row = container.querySelector('[data-testid="run-disclosure-step"]')!;
    expect(row.querySelector('.run-step-summary')?.textContent).toBe(language === 'zh' ? sentence.chinese : sentence.english);
    for (const button of container.querySelectorAll<HTMLButtonElement>('.run-step-toggle[aria-expanded="false"]')) fireEvent.click(button);
    expect(row.querySelector('.md-content')?.textContent).toBe(language === 'zh' ? sentence.chinese : sentence.english);
    expect(container.querySelectorAll('[data-testid="run-disclosure-step"]')[1]
      .querySelector('.md-content')?.textContent).toBe(modelSummary);
    expect(messages[1].content).toBe(modelSummary);
    expect(JSON.stringify(messages)).toBe(before);
    expect(JSON.stringify(events)).toBe(stored);
    unmount();
  }
});

// @vitest-environment jsdom
import React from 'react';
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { consumeSessionEnvelope, projectSessionEventStoreToMessages } from '../agent-detail/sessionEventConsumer';
import type { ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import type { SessionEventV2 } from './sessionEventStore';
import { ThreadItemRenderer } from './ThreadItemRenderer';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => {
  await i18n.init({ lng: 'zh', resources: { zh: { translation: zh }, en: { translation: en } }, initImmediate: false });
});
afterEach(cleanup);

function failureCard(reason: string) {
  const event: SessionEventV2 = {
    schema: 'hive.session_event', schema_version: 2, event_id: 'failure-event', sequence: 1,
    tenant_id: 'tenant', scope: { level: 'run', session_id: 'session', thread_id: 'session',
      turn_id: 'turn', run_id: 'run' }, item_id: 'failure', item_kind: 'runtime_failure',
    kind: 'runtime_failure.recorded', lifecycle: 'recorded',
    payload_schema: 'hive.session.payload.runtime_failure.recorded.v2', actor: { type: 'runtime' },
    visibility: { audience: 'direct_user' },
    // Match the content-free terminal witness: no failure_code, only the existing reason.
    payload: { status: 'failed', terminal_reason: reason, retry_safe: false, retryable: false,
      requires_user_decision: false, content: '', message: '' },
    occurred_at: '2026-11-11T00:00:00Z', persisted_at: '2026-11-11T00:00:00Z',
  };
  const { store } = consumeSessionEnvelope(event as unknown as ChatTranscriptEventPayload, undefined, 0);
  if (!store) throw new Error('failure_event_not_consumed');
  const item = projectSessionEventStoreToMessages(store).find(message => message.eventType === 'runtime_failure')?.threadItem;
  expect(item?.item_data).toMatchObject({ code: null, reason });
  return item!;
}

it.each(['zh', 'en'])('renders persistence failure from the live event in %s', async language => {
  await i18n.changeLanguage(language);
  const item = failureCard('persistence_error');
  const expected = language === 'zh' ? '这次没有完成，请稍后再试。' : 'This task did not finish. Please try again later.';
  expect(item.user_summary).toBe('This task did not finish. Please try again later.');
  const { container } = render(<ThreadItemRenderer item={item} />);
  expect(container.textContent).toContain(expected);
  expect(container.textContent).not.toMatch(/模型服务|model service|配置|configuration/);
});

it.each(['provider_bad_request', 'provider_error', 'turn_abort', 'model_not_found', 'prompt_too_long'])(
  'renders the unavailable sentence from the live %s event', async reason => {
    const item = failureCard(reason);
    for (const language of ['zh', 'en']) {
      await i18n.changeLanguage(language);
      const expected = language === 'zh'
        ? '模型服务未能完成本次任务，请联系管理员检查模型配置。'
        : 'The model service could not finish this task. Ask an administrator to check the model configuration.';
      const { container, unmount } = render(<ThreadItemRenderer item={item} />);
      expect(container.textContent).toContain(expected);
      expect(container.textContent).not.toContain('Please try again later.');
      unmount();
    }
  },
);

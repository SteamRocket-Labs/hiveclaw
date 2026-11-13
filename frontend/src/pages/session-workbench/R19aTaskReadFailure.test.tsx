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

it.each(['identical_terminal_task_read', 'identical_running_task_read'])(
  'renders the member sentence without retry for %s', async code => {
    await i18n.changeLanguage('zh');
    const event: SessionEventV2 = {
      schema: 'hive.session_event', schema_version: 2, event_id: 'failure', sequence: 1,
      tenant_id: 'tenant', scope: { level: 'run', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run' },
      item_id: 'failure', item_kind: 'runtime_failure', kind: 'runtime_failure.recorded', lifecycle: 'recorded',
      payload_schema: 'hive.session.payload.runtime_failure.recorded.v2', actor: { type: 'runtime' },
      visibility: { audience: 'direct_user' }, occurred_at: '2026-11-12T00:00:00Z', persisted_at: '2026-11-12T00:00:00Z',
      payload: { status: 'failed', terminal_reason: 'loop_guard', failure_code: code,
        retry_safe: false, retryable: false, requires_user_decision: false, content: '', message: '' },
    };
    const { store } = consumeSessionEnvelope(event as unknown as ChatTranscriptEventPayload, undefined, 0);
    if (!store) throw new Error('failure_not_consumed');
    const item = projectSessionEventStoreToMessages(store).find(message => message.eventType === 'runtime_failure')?.threadItem;
    if (!item) throw new Error('failure_not_projected');
    const { container, queryByTestId } = render(<ThreadItemRenderer item={item} />);
    expect(container.textContent).toContain(code === 'identical_terminal_task_read'
      ? '同事那边的任务已经结束且没有成功，我没有再重复查询。你可以让我重新安排这项工作。'
      : '同事正在处理，结果回来后会在这里继续。');
    expect(container.textContent).not.toContain('管理员');
    expect(queryByTestId('thread-item-retry-turn')).toBeNull();
  },
);

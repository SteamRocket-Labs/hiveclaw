import { expect, it } from 'vitest';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { runtimeFailurePresentation } from './runtimeFailurePresentation';
import { normalizeThreadItemPayload } from './threadItemReducer';

it.each(['provider_bad_request', 'provider_error', 'turn_abort', 'model_not_found', 'prompt_too_long'])(
  'directs members to an administrator for %s', code => {
    const presentation = runtimeFailurePresentation(code, false, 'rejected');
    expect(presentation.kind).toBe('unavailable');
    expect(presentation.fallback)
      .toBe('The model service could not finish this task. Ask an administrator to check the model configuration.');
    expect(zh.sessionWorkbench.threadItem.failure.unavailable)
      .toBe('模型服务未能完成本次任务，请联系管理员检查模型配置。');
    expect(en.sessionWorkbench.threadItem.failure.unavailable).toBe(presentation.fallback);
    const item = normalizeThreadItemPayload({ type: 'runtime_action_failed', status: 'failed',
      failure_code: code, delivery_state: 'rejected', retryable: false });
    expect(item?.user_summary).toBe(zh.sessionWorkbench.threadItem.failure.unavailable);
  },
);

it('uses the existing persistence terminal reason when the event has no failure code', () => {
  expect(runtimeFailurePresentation(undefined, false, undefined, 'persistence_error')).toEqual({
    kind: 'persistence_error', fallback: 'This task did not finish. Please try again later.',
  });
});

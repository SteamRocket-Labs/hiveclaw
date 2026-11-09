import { expect, it } from 'vitest';
import zh from '../../i18n/zh.json';
import { runtimeFailurePresentation } from './runtimeFailurePresentation';
import { normalizeThreadItemPayload } from './threadItemReducer';

it('shows the existing non-retry copy for a deterministic provider rejection', () => {
  const presentation = runtimeFailurePresentation('provider_bad_request', false, 'rejected');
  expect(presentation.kind).toBe('unavailable');
  expect(zh.sessionWorkbench.threadItem.failure.unavailable)
    .toBe('模型服务未能完成本次任务，请检查配置后再试。');
  const item = normalizeThreadItemPayload({ type: 'runtime_action_failed', status: 'failed',
    failure_code: 'provider_bad_request', delivery_state: 'rejected', retryable: false });
  expect(item?.user_summary).toBe(zh.sessionWorkbench.threadItem.failure.unavailable);
});

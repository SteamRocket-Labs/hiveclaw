// @vitest-environment jsdom
import React from 'react';
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { normalizeThreadItemPayload } from './threadItemReducer';
import { ThreadItemRenderer } from './ThreadItemRenderer';
import { runtimeFailurePresentation } from './runtimeFailurePresentation';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
beforeAll(async () => {
  await i18n.init({ lng: 'zh', resources: { zh: { translation: zh }, en: { translation: en } }, initImmediate: false });
});
afterEach(cleanup);

it.each(['persistence_error', 'turn_abort', 'provider_error', 'provider_bad_request'])(
  'renders a plain failure sentence for members after %s', reason => {
    const item = normalizeThreadItemPayload({ type: 'runtime_failure', status: 'failed', audience: 'user',
      terminal_reason: reason, code: reason, retryable: false, message: 'Private diagnostic text' });
    expect(item).not.toBeNull();
    const { container } = render(<ThreadItemRenderer item={item!} />);
    expect(container.textContent).toContain('这次没有完成，请稍后再试。');
    expect(container.textContent).not.toMatch(/模型服务|检查配置|Private diagnostic/);
  },
);

it('keeps the English fallback free of configuration instructions', () => {
  expect(runtimeFailurePresentation('persistence_error', false).fallback)
    .toBe('This task did not finish. Please try again later.');
  expect(en.sessionWorkbench.threadItem.failure.unavailable)
    .toBe('This task did not finish. Please try again later.');
});

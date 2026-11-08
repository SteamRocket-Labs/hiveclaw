import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { expect, it } from 'vitest';
import zh from '../../i18n/zh.json';
import { normalizeThreadItemPayload } from './threadItemReducer';
import { ThreadItemRenderer } from './ThreadItemRenderer';

it.each([
  ['runtime_action_blocked', 'needs_reconciliation', '需要管理员处理'],
  ['runtime_action_blocked', 'blocked', '审批未通过'],
  ['runtime_action_failed', 'failed', '执行失败'],
])('localizes the non-successful result event %s with the real Chinese catalog', async (type, status, label) => {
  const i18n = createInstance();
  await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, initImmediate: false });
  const item = normalizeThreadItemPayload({ type, status, content: `${status}: durable result ready`,
    metadata: { runtime_action_from_result_integration: true,
      ...(status === 'blocked' ? { notification_source: 'approval' } : {}) } });
  expect(item).not.toBeNull();
  const markup = renderToStaticMarkup(<I18nextProvider i18n={i18n}><ThreadItemRenderer item={item!} /></I18nextProvider>);
  expect(markup).toContain(label);
  expect(markup).not.toMatch(/ready|needs_reconciliation|The task stopped|needs attention/);
  if (status === 'blocked') expect(markup).not.toContain('需要管理员处理'); // N6: a plain rejection needs no admin intervention.
});

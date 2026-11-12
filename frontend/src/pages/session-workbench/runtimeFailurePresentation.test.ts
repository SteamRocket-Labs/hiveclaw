import { expect, it } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { runtimeFailurePresentation } from './runtimeFailurePresentation';

// Approval, remote, subagent and creation results also reach this failure code;
// neither the member sentence nor its fallback may assume a colleague or summary.
const sentence = {
  en: "This result couldn't be read safely this time, so I couldn't continue. You can ask me to arrange this work again.",
  zh: '这份结果这次没法安全读取，我没能继续。你可以让我重新安排这项工作。',
};

it('uses the source-neutral result refusal fallback', () => {
  expect(runtimeFailurePresentation('identical_refused_result_read', false)).toEqual({
    kind: 'refused_result',
    fallback: sentence.en,
  });
});

it.each([['en', en, sentence.en], ['zh', zh, sentence.zh]] as const)(
  'uses the source-neutral result refusal in %s',
  (_language, catalog, expected) => {
    expect(catalog.sessionWorkbench.threadItem.failure.refusedResult).toBe(expected);
  },
);

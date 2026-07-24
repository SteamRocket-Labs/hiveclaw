import { describe, expect, it } from 'vitest';

import {
  htmlLanguage,
  isChineseLanguage,
  nextInterfaceLanguage,
  syncDocumentLanguage,
} from './language';

describe('interface language normalization', () => {
  it.each(['zh', 'zh-CN', 'ZH-hans'])('treats %s as Chinese and switches it to English', (language) => {
    expect(isChineseLanguage(language)).toBe(true);
    expect(nextInterfaceLanguage(language)).toBe('en');
  });

  it.each(['en', 'en-US', undefined])('switches %s to Chinese', (language) => {
    expect(isChineseLanguage(language)).toBe(false);
    expect(nextInterfaceLanguage(language)).toBe('zh');
  });

  it.each([
    ['zh', 'zh-CN'],
    ['zh-CN', 'zh-CN'],
    ['ZH-hans', 'zh-CN'],
    ['en', 'en'],
    ['en-US', 'en'],
    [undefined, 'en'],
  ])('maps %s to the document language %s', (language, expected) => {
    expect(htmlLanguage(language)).toBe(expected);
  });

  it('updates the shared document root for every routed layout', () => {
    const root = { lang: 'zh-CN' };

    syncDocumentLanguage('en-US', root);

    expect(root.lang).toBe('en');
  });
});

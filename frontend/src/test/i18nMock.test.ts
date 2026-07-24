import { describe, expect, it } from 'vitest';

import { translateFromCatalog } from './i18nMock';

const catalog = {
  common: {
    greeting: '你好，{{name}}',
  },
};

describe('translateFromCatalog', () => {
  it('resolves nested catalog keys and interpolates named values', () => {
    expect(translateFromCatalog(catalog, 'common.greeting', { name: 'Rocky' })).toBe('你好，Rocky');
  });

  it('uses an explicit fallback only when the catalog key is absent', () => {
    expect(translateFromCatalog(catalog, 'common.missing', 'Fallback')).toBe('Fallback');
    expect(translateFromCatalog(catalog, 'common.missing')).toBe('common.missing');
  });
});

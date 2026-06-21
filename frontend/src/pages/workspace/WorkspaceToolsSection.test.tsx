import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  ToolConfigSecretListField,
  countToolConfigListValues,
  normalizeToolConfigListValue,
} from './WorkspaceToolsSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      return key.split('.').pop() ?? key;
    },
  }),
}));

describe('WorkspaceToolsSection AnySearch key pool helpers', () => {
  it('normalizes comma and newline separated tool config lists', () => {
    expect(normalizeToolConfigListValue('key-a\nkey-b,key-c\n\n key-d ')).toEqual([
      'key-a',
      'key-b',
      'key-c',
      'key-d',
    ]);
    expect(countToolConfigListValues(['key-a', '', 'key-b'])).toBe(2);
  });

  it('renders multiline password fields as a counted secret key pool', () => {
    const markup = renderToStaticMarkup(
      <ToolConfigSecretListField
        field={{
          key: 'anysearch_api_keys',
          label: 'AnySearch API keys',
          placeholder: 'one key per line',
          description: 'Optional AnySearch API key pool.',
        }}
        value={'__HIVE_SECRET_SET__\n__HIVE_SECRET_SET__\n__HIVE_SECRET_SET__'}
        onChange={vi.fn()}
      />,
    );

    expect(markup).toContain('AnySearch API keys');
    expect(markup).toContain('3 keys configured');
    expect(markup).toContain('Calls rotate across saved keys');
    expect(markup).toContain('Add key');
    expect(markup).toContain('Remove');
  });
});

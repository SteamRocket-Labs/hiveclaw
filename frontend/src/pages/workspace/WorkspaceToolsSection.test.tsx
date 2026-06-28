import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  ToolConfigSecretListField,
  countToolConfigListValues,
  isExtensionOrAddonTool,
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
  it('treats only L2 extensions and dynamic connectors as toggleable tools', () => {
    expect(isExtensionOrAddonTool({
      type: 'builtin',
      name: 'web_search',
      governance_taxonomy: {
        layer: 'agent_base',
        l2_visible: false,
        enterprise_toggleable: false,
      },
    })).toBe(false);
    expect(isExtensionOrAddonTool({
      type: 'builtin',
      name: 'exa_search',
      governance_taxonomy: {
        layer: 'platform_addon',
        l2_visible: true,
        enterprise_toggleable: true,
      },
    })).toBe(true);
    expect(isExtensionOrAddonTool({
      type: 'mcp',
      name: 'mcp_vendor_search',
      governance_taxonomy: null,
    })).toBe(true);
  });

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

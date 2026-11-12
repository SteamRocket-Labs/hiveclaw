import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

import {
  ToolConfigSecretListField,
  getWorkspaceToolGovernanceState,
  getWorkspaceProviderAuthDisplay,
  sortWorkspaceToolsForDisplay,
  countToolConfigListValues,
  isExtensionOrAddonTool,
  normalizeToolConfigListValue,
  resolveWorkspaceToolCapability,
} from './WorkspaceToolsSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      const template = typeof fallbackOrOptions === 'string'
        ? fallbackOrOptions
        : typeof fallbackOrOptions === 'object' && fallbackOrOptions !== null && 'defaultValue' in fallbackOrOptions
          ? String(fallbackOrOptions.defaultValue)
          : null;
      const values = typeof fallbackOrOptions === 'object' && fallbackOrOptions !== null ? fallbackOrOptions : options;
      if (template !== null) {
        return template.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(values?.[name] ?? ''));
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

  it('derives execution mode and effective status from tool switch plus company policy', () => {
    const definitions = [
      { capability: 'external.web.search', tools: ['exa_search'] },
      { capability: 'external.api.call', tools: ['custom_api__*'] },
    ];

    expect(resolveWorkspaceToolCapability({ name: 'exa_search' }, definitions)).toBe('external.web.search');
    expect(resolveWorkspaceToolCapability({ name: 'custom_api__crm_lookup' }, definitions)).toBe('external.api.call');

    expect(getWorkspaceToolGovernanceState({
      tool: { enabled: false },
      capability: 'external.web.search',
      policy: { allowed: true, requires_approval: true },
    })).toEqual({ executionMode: 'approval', effectiveStatus: 'disabled' });

    expect(getWorkspaceToolGovernanceState({
      tool: { enabled: true },
      capability: 'external.web.search',
      policy: { allowed: true, requires_approval: true },
    })).toEqual({ executionMode: 'approval', effectiveStatus: 'approval_required' });

    expect(getWorkspaceToolGovernanceState({
      tool: { enabled: true },
      capability: 'external.web.search',
      policy: { allowed: true, requires_approval: false },
    })).toEqual({ executionMode: 'auto', effectiveStatus: 'auto_allowed' });

    expect(getWorkspaceToolGovernanceState({
      tool: { enabled: true },
      capability: 'external.web.search',
      policy: { allowed: false, requires_approval: false },
    })).toEqual({ executionMode: 'auto', effectiveStatus: 'legacy_denied' });
  });

  it('sorts web_pack tools with advanced routers first and keyed XCrawl last', () => {
    const sorted = sortWorkspaceToolsForDisplay([
      { id: 'xcrawl', name: 'xcrawl_scrape', category: 'web_pack' },
      { id: 'exa', name: 'exa_search', category: 'web_pack' },
      { id: 'fetch', name: 'advanced_web_fetch', category: 'web_pack' },
      { id: 'search', name: 'advanced_web_search', category: 'web_pack' },
      { id: 'any', name: 'anysearch_search', category: 'web_pack' },
    ]);

    expect(sorted.map((tool) => tool.name)).toEqual([
      'advanced_web_search',
      'advanced_web_fetch',
      'anysearch_search',
      'exa_search',
      'xcrawl_scrape',
    ]);
  });

  it('maps provider auth metadata to localized badge copy', () => {
    const t = (_key: string, fallback: string) => fallback;

    expect(getWorkspaceProviderAuthDisplay({
      mode: 'no_key_default',
      keyless_supported: true,
      credential_optional: true,
      key_required: false,
      label: 'No key by default',
      description: 'Routes to no-key-capable web providers by default.',
    }, t)).toEqual({
      className: 'is-no-key',
      description: 'Runs without an API key by default; optional keys only raise limits or production control.',
      label: 'No key by default',
    });

    expect(getWorkspaceProviderAuthDisplay({
      mode: 'key_required',
      keyless_supported: false,
      credential_optional: false,
      key_required: true,
      label: 'Key required',
      description: 'This provider requires a key.',
    }, t)).toEqual({
      className: 'is-key-required',
      description: 'Requires a configured provider key before agents can use it.',
      label: 'Key required',
    });
  });
});

describe('G4b admin extension labels stay plain Chinese', () => {
  it('pins the extension list and review labels the admin actually reads', () => {
    // G4b item 3: 「Default」/「可选 key」 class labels and the URL field read plainly.
    expect(zh.enterprise.tools.defaultTag).toBe('默认');
    expect(zh.enterprise.tools.providerAuthOptionalKey).toBe('可选密钥');
    expect(zh.enterprise.tools.providerAuthNoKeyDefault).toBe('默认免密钥');
    expect(zh.enterprise.tools.providerAuthKeyRequired).toBe('需要密钥');
    expect(zh.enterprise.tools.jsonConfig).toBe('服务地址');
    expect(zh.enterprise.tools.rawRemoteEvidence).toBe('服务方提供的说明（未核实）');
    expect(en.enterprise.tools.jsonConfig).toBe('Server URL');
    // The install route the member's assistant relays matches these zh labels.
    expect(zh.nav.settings).toBe('设置');
    expect(zh.nav.enterprise).toBe('公司后台');
  });
});

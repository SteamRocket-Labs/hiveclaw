import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { FeishuRuntimeStatusCard } from './FeishuRuntimeStatusCard';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      return _key;
    },
  }),
}));

describe('FeishuRuntimeStatusCard', () => {
  it('renders a ready state for CLI-backed office tooling', () => {
    const markup = renderToStaticMarkup(
      <FeishuRuntimeStatusCard
        isAdmin
        status={{
          ok: true,
          scope: 'global',
          message: 'Feishu CLI is ready.',
          cli_enabled: true,
          cli_available: true,
          cli_bin: 'lark-cli',
          base_tasks_ready: true,
          docs_read_ready: true,
        }}
      />,
    );

    expect(markup).toContain('Feishu Runtime Status');
    expect(markup).toContain('lark-cli');
    expect(markup).toContain('Base / Tasks');
    expect(markup).toContain('Ready');
  });

  it('renders agent access details when channel auth exists but CLI is not ready', () => {
    const markup = renderToStaticMarkup(
      <FeishuRuntimeStatusCard
        isAdmin
        status={{
          ok: true,
          scope: 'agent',
          message: 'Docs can use channel auth, but Base/Tasks still need CLI auth.',
          cli_enabled: true,
          cli_available: false,
          cli_bin: 'lark-cli',
          channel_configured: true,
          office_access: true,
          base_tasks_ready: false,
          docs_read_ready: true,
        }}
      />,
    );

    expect(markup).toContain('Channel Auth');
    expect(markup).toContain('Configured');
    expect(markup).toContain('CLI Auth');
    expect(markup).toContain('Needs Attention');
  });
});

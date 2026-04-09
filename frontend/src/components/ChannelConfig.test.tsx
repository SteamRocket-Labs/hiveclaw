import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import ChannelConfig from './ChannelConfig';

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

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = String(queryKey[0]);
    if (key === 'feishu-runtime-status' && queryKey[1] === 'agent-1') {
      return {
        data: {
          ok: true,
          scope: 'agent',
          message: 'Docs can use channel auth, but Base/Tasks still need CLI auth.',
          cli_enabled: true,
          cli_available: false,
          cli_bin: 'lark-cli',
          channel_configured: true,
          office_access: true,
          docs_read_ready: true,
          base_tasks_ready: false,
        },
      };
    }
    return { data: null };
  },
  useMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

vi.mock('../api/domains/channels', () => ({
  channelApi: {},
}));

describe('ChannelConfig', () => {
  it('surfaces Feishu runtime status within the Feishu channel module', () => {
    vi.stubGlobal('window', { location: { origin: 'http://localhost:3008' } });
    const markup = renderToStaticMarkup(<ChannelConfig mode="edit" agentId="agent-1" />);

    expect(markup).toContain('Feishu Runtime Status');
    expect(markup).toContain('Base / Tasks');
    expect(markup).toContain('Channel Auth');
  });
});

import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChannelConfig from './ChannelConfig';

const testState = vi.hoisted(() => ({
  feishuConfig: null as Record<string, unknown> | null,
}));

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
    if (key === 'channel' && queryKey[1] === 'agent-1') {
      return { data: testState.feishuConfig };
    }
    if (key === 'feishu-runtime-status' && queryKey[1] === 'agent-1') {
      return {
        data: {
          ok: true,
          scope: 'agent',
          message: 'Docs can use channel auth, but Base/Tasks still need CLI auth.',
          cli_enabled: true,
          cli_available: false,
          cli_bin: 'lark-cli',
          cardkit_dependency_ready: true,
          cardkit_verified: false,
          cardkit_last_error: 'missing scope',
          cardkit_probe_supported: true,
          cardkit_ready: true,
          tenant_channel_configured: true,
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
  channelApi: {
    get: vi.fn(),
    webhookUrl: vi.fn(),
    getChannelConfig: vi.fn(),
    getChannelWebhook: vi.fn(),
  },
}));

describe('ChannelConfig', () => {
  beforeEach(() => {
    testState.feishuConfig = null;
  });

  it('surfaces Feishu runtime status within the Feishu channel module', () => {
    vi.stubGlobal('window', { location: { origin: 'http://localhost:3008' } });
    const markup = renderToStaticMarkup(<ChannelConfig mode="edit" agentId="agent-1" />);

    expect(markup).toContain('Feishu Runtime Status');
    expect(markup).toContain('Base / Tasks');
    expect(markup).toContain('Channel Auth');
    expect(markup).toContain('CardKit Dependencies');
    expect(markup).toContain('CardKit Verified');
  });

  it('shows an explicit Feishu or Lark platform selector for the IM channel', () => {
    const markup = renderToStaticMarkup(
      <ChannelConfig mode="create" values={{}} onChange={vi.fn()} />,
    );

    expect(markup).toContain('Platform');
    expect(markup).toContain('Feishu (China)');
    expect(markup).toContain('Lark (Global)');
  });

  it('uses QR registration as the primary Agent Detail setup path', () => {
    const markup = renderToStaticMarkup(<ChannelConfig mode="edit" agentId="agent-1" />);

    expect(markup).toContain('Scan to create or bind the app');
    expect(markup).toContain('Manual configuration (advanced)');
  });

  it('does not present saved credentials as a live WebSocket connection', () => {
    testState.feishuConfig = {
      is_configured: true,
      is_connected: false,
      app_id: 'cli_connecting',
      extra_config: {
        connection_mode: 'websocket',
        connection_status: 'connecting',
        platform_region: 'lark_global',
      },
    };

    const markup = renderToStaticMarkup(<ChannelConfig mode="edit" agentId="agent-1" />);

    expect(markup).toContain('App created; connecting WebSocket');
    expect(markup).not.toContain('WebSocket connected');
  });

  it('sends an admin-unlinked Feishu identity back to QR verification', () => {
    testState.feishuConfig = {
      is_configured: false,
      is_connected: false,
      app_id: 'cli_requires_rebind',
      extra_config: {
        connection_mode: 'websocket',
        connection_status: 'identity_rebind_required',
        platform_region: 'feishu_cn',
      },
    };

    const markup = renderToStaticMarkup(<ChannelConfig mode="edit" agentId="agent-1" />);

    expect(markup).toContain('Rebind required');
    expect(markup).toContain('Scan to create or bind the app');
    expect(markup).not.toContain('WebSocket connection failed; scan again');
  });

  it('marks manually configured Feishu transport as requiring user QR verification', () => {
    testState.feishuConfig = {
      is_configured: true,
      is_connected: true,
      app_id: 'cli_manual',
      extra_config: {
        connection_mode: 'websocket',
        connection_status: 'connected',
        identity_status: 'unverified_manual',
        platform_region: 'lark_global',
      },
    };

    const markup = renderToStaticMarkup(<ChannelConfig mode="edit" agentId="agent-1" />);

    expect(markup).toContain('Rebind required');
    expect(markup).toContain('Reconnect by QR code');
  });

  it('does not expose the retired Atlassian Rovo channel', async () => {
    const { channelApi } = await import('../api/domains/channels');
    const markup = renderToStaticMarkup(<ChannelConfig mode="edit" agentId="agent-1" />);

    expect(markup).not.toContain('Atlassian');
    expect(markup).not.toContain('Jira / Confluence / Compass');
    expect(markup).not.toContain('Rovo MCP');
    expect(channelApi.getChannelConfig).not.toHaveBeenCalledWith('agent-1', 'atlassian-channel');
  });
});

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import LocalAgents, {
  DEFAULT_HIVE_CONNECT_INSTALL_GUIDE,
  activationCodeFromSearch,
  buildSetupInstruction,
  browserChannelWsUrl,
  canSendLocalAgentMessage,
  channelEventText,
  channelSessionIdFromSearch,
  connectionPresenceStatus,
  isOnlineConnection,
  localAgentMessageDeliveryState,
  mergeChannelEvents,
  resolveActiveLocalChannelSessionId,
} from './LocalAgents';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      return key.split('.').pop() || key;
    },
  }),
}));

vi.mock('react-router-dom', () => ({
  useLocation: () => ({ pathname: '/local-agents', search: '', hash: '' }),
}));

vi.mock('../api/domains/localBridge', () => ({
  localBridgeApi: {
    listConnections: vi.fn().mockResolvedValue({ connections: [] }),
    listAgentConnections: vi.fn().mockResolvedValue({ connections: [] }),
    getInstallGuide: vi.fn().mockResolvedValue({
      product_name: 'Hive Connect',
      skill_repo_url: '',
      skill_name: 'hive-connect',
      npm_package: '',
      binary_name: 'hive-connect',
      install_skill_command: '',
      install_cli_command: '',
      login_command: '',
      status_command: '',
      run_command: '',
      user_prompt: '帮我安装 Hive Connect skill，并连接到 Hive。',
      instructions: [
        '帮我安装 Hive Connect skill，并连接到 Hive。',
        '请按下面流程自动完成：',
        '1. Hive Connect 安装源不可用（unavailable）；请停止并联系 Hive 管理员，配置已批准的 HIVE_CONNECT_SKILL_REPO_URL 与 HIVE_CONNECT_NPM_PACKAGE。不要猜测仓库或 package 名称。',
      ],
    }),
    approvePairing: vi.fn(),
    rejectPairing: vi.fn(),
    getDefaultChannelSession: vi.fn().mockResolvedValue({
      id: 'session-1',
      chat_session_id: null,
      source: 'web',
      status: 'active',
      created_at: null,
    }),
    getAgentDefaultChannelSession: vi.fn().mockResolvedValue({
      id: 'agent-session-1',
      chat_session_id: 'chat-session-1',
      source: 'web',
      status: 'active',
      created_at: null,
    }),
    createChannelSession: vi.fn(),
    sendChannelMessage: vi.fn(),
    sendAgentChannelMessage: vi.fn(),
    getChannelTimeline: vi.fn().mockResolvedValue({ session: { id: 'session-1' }, events: [] }),
    createBrowserChannelWsTicket: vi.fn().mockResolvedValue({
      ticket: 'hbwt_test',
      expires_in: 60,
      single_use: false,
    }),
    listChannelEvents: vi.fn().mockResolvedValue({ events: [] }),
    listWorkspaceFiles: vi.fn().mockResolvedValue([]),
    readWorkspaceFile: vi.fn(),
    downloadWorkspaceFile: vi.fn(),
    uploadWorkspaceFile: vi.fn(),
  },
}));

describe('LocalAgents page', () => {
  it('keeps binding status separate from online presence', () => {
    expect(connectionPresenceStatus({ status: 'active' } as any)).toBe('unknown');
    expect(connectionPresenceStatus({ status: 'active', presence_status: 'offline' } as any)).toBe('offline');
    expect(connectionPresenceStatus({ status: 'revoked', presence_status: 'online' } as any)).toBe('offline');
    expect(
      isOnlineConnection({
        status: 'active',
        presence_status: 'online',
        last_seen_at: '2026-01-01T00:00:00Z',
      } as any),
    ).toBe(true);
  });

  it('allows durable queueing while a bound local runner is offline, but not before pairing', () => {
    expect(canSendLocalAgentMessage({ localAgentBound: false, messageBusy: false, content: 'hello' })).toBe(false);
    expect(canSendLocalAgentMessage({ localAgentBound: true, messageBusy: true, content: 'hello' })).toBe(false);
    expect(canSendLocalAgentMessage({ localAgentBound: true, messageBusy: false, content: '   ' })).toBe(false);
    expect(canSendLocalAgentMessage({ localAgentBound: true, messageBusy: false, content: 'hello' })).toBe(true);
  });

  it('distinguishes approval wait from a dispatched local message', () => {
    expect(
      localAgentMessageDeliveryState({
        id: 'message-1',
        status: 'waiting_approval',
        approval_id: 'approval-1',
      } as any),
    ).toEqual({ kind: 'warning', state: 'waiting_approval', reference: 'approval-1' });
    expect(
      localAgentMessageDeliveryState({ id: 'message-2', status: 'pending' } as any),
    ).toEqual({ kind: 'success', state: 'queued', reference: 'message-2' });
  });

  it('renders approval lifecycle events as user-facing state, not protocol names', () => {
    expect(channelEventText({ type: 'approval_required', payload: {} } as any)).toBe('Waiting for owner approval');
    expect(
      channelEventText({ type: 'approval_resolved', payload: { status: 'approved' } } as any),
    ).toBe('Owner approved this action');
    expect(
      channelEventText({ type: 'approval_resolved', payload: { status: 'rejected' } } as any),
    ).toBe('Owner rejected this action');
  });

  it('normalizes activation codes from Hive Connect login links', () => {
    expect(activationCodeFromSearch('?user_code=hive-abcd-1234')).toBe('HIVE-ABCD-1234');
    expect(activationCodeFromSearch('?foo=bar')).toBe('');
  });

  it('selects the route-bound local channel session before the default session', () => {
    expect(channelSessionIdFromSearch('?session_id=channel-session-1')).toBe('channel-session-1');
    expect(channelSessionIdFromSearch('?session=channel-session-2')).toBe('channel-session-2');
    expect(channelSessionIdFromSearch('?foo=bar')).toBe('');
    expect(
      resolveActiveLocalChannelSessionId({
        explicitSessionId: null,
        routeChannelSession: { id: 'route-channel-session' } as any,
        defaultChannelSession: { id: 'default-channel-session' } as any,
      }),
    ).toBe('route-channel-session');
    expect(
      resolveActiveLocalChannelSessionId({
        explicitSessionId: 'explicit-channel-session',
        routeChannelSession: { id: 'route-channel-session' } as any,
        defaultChannelSession: { id: 'default-channel-session' } as any,
      }),
    ).toBe('explicit-channel-session');
  });

  it('builds browser websocket URLs for persistent local channel sessions', () => {
    expect(
      browserChannelWsUrl(
        'session-1',
        'hbwt browser/token',
        { protocol: 'https:', host: 'hive.example' } as Location,
      ),
    ).toBe('wss://hive.example/ws/local-agents/sessions/session-1?ticket=hbwt+browser%2Ftoken');
    expect(
      browserChannelWsUrl(
        'session-1',
        'ticket',
        { protocol: 'http:', host: 'localhost:3008' } as Location,
      ),
    ).toBe('ws://localhost:3008/ws/local-agents/sessions/session-1?ticket=ticket');
  });

  it('merges replayed timeline events and websocket events without duplicates', () => {
    const first = {
      id: 'event-1',
      sequence: 1,
      session_id: 'session-1',
      message_id: 'message-1',
      direction: 'hive_to_local',
      type: 'message',
      payload: { content: 'hello' },
    };
    const second = {
      id: 'event-2',
      sequence: 2,
      session_id: 'session-1',
      message_id: 'message-1',
      direction: 'local_to_hive',
      type: 'delta',
      payload: { text: 'working' },
    };

    expect(mergeChannelEvents([], [second, first, second]).map((event) => event.id)).toEqual(['event-1', 'event-2']);
  });

  it('renders automatic authentication instead of manual pairing controls', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    const markup = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <LocalAgents />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Local Agent Channel');
    expect(markup).toContain('Local agents behave like regular Hive agents with a local runtime.');
    expect(markup).toContain('Keep them private or share them with your workspace through normal Agent permissions.');
    expect(markup).toContain('Chat');
    expect(markup).toContain('Workspace');
    expect(markup).toContain('Direct local chat');
    expect(markup).toContain('Attach file');
    expect(markup).toContain('Automatic authentication');
    expect(markup).toContain('Hive Connect');
    expect(markup).toContain('Hive Connect 安装源不可用（unavailable）；请停止');
    expect(markup).not.toContain('npx skills add');
    expect(markup).not.toContain('hive-connect login');
    expect(markup).not.toContain('hive-connect daemon install');
    expect(markup).toContain('The local agent is offline. Keep Hive Connect installed; it will reconnect automatically');
    expect(markup).not.toContain('验证 Hive 连接状态');
    expect(markup).not.toContain('执行 hive-connect run，保持本地 Agent 在线');
    expect(markup).not.toContain('runner');
    expect(markup).not.toContain('poll fallback');
    expect(markup).not.toContain('--hive-url');
    expect(markup).not.toContain('hive-bridge');
    expect(markup).not.toContain('cc-connect');
    expect(markup).not.toContain('Approve link');
    expect(markup).not.toContain('Pairing code');
    expect(markup).not.toContain('paste the HIVE code');
    expect(markup).not.toContain('Hive agent id');
    expect(markup).not.toContain('owner identity');
    expect(markup).not.toContain('user-level IM channel');
    expect(markup).not.toContain('Overview');
    expect(markup).not.toContain('Knowledge');
    expect(markup).not.toContain('Settings');
  });

  it('renders as an embedded local agent detail channel for a real agent', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    const markup = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <LocalAgents agentId="agent-local-1" agentName="Codex on Mac" embedded />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Codex on Mac');
    expect(markup).toContain('Chat');
    expect(markup).toContain('Workspace');
    expect(markup).not.toContain('Local Agent Channel</h2>');
  });

  it('builds setup instructions around the background daemon instead of a foreground runner', () => {
    const guide = buildSetupInstruction({
      product_name: 'Hive Connect',
      skill_repo_url: 'https://example.com/hive-connect-skill.git',
      skill_name: 'hive-connect',
      npm_package: '@example/hive-connect',
      binary_name: 'hive-connect',
      install_skill_command: 'npx skills add https://example.com/hive-connect-skill.git --skill hive-connect',
      install_cli_command: 'npm install -g @example/hive-connect',
      login_command: 'hive-connect login',
      status_command: 'hive-connect status',
      run_command: 'hive-connect daemon install --config ~/.hive-connect/config.toml --force',
      user_prompt: '帮我安装 Hive Connect skill，并连接到 Hive。',
      instructions: [],
    } as any);

    expect(guide).toContain('hive-connect daemon install --config ~/.hive-connect/config.toml --force，安装并启动后台常驻服务。');
    expect(guide).toContain('npm install -g @example/hive-connect');
    expect(guide).toContain('可选：执行 hive-connect status，确认本机仍保留 Hive 登录绑定（这不代表在线）。');
    expect(guide).toContain('回到 Hive 页面查看本地 Agent 在线标记');
    expect(guide.indexOf('hive-connect daemon install')).toBeLessThan(guide.indexOf('hive-connect status'));
    expect(guide).not.toContain('验证 Hive 连接状态');
    expect(guide).not.toContain('runner');
    expect(guide).not.toContain('poll fallback');
  });

  it('blocks setup when either install source is missing', () => {
    const guide = buildSetupInstruction({
      ...DEFAULT_HIVE_CONNECT_INSTALL_GUIDE,
      npm_package: '@example/hive-connect',
      install_cli_command: 'npm install -g @example/hive-connect',
      instructions: [],
    });

    expect(guide).toContain('安装源不可用（unavailable）');
    expect(guide).not.toContain('npm install -g @example/hive-connect');
    expect(guide).not.toContain('hive-connect login');
  });
});

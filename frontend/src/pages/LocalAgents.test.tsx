import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import LocalAgents, { activationCodeFromSearch, connectionPresenceStatus, isOnlineConnection } from './LocalAgents';

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

vi.mock('../api/domains/localBridge', () => ({
  localBridgeApi: {
    listConnections: vi.fn().mockResolvedValue({ connections: [] }),
    approvePairing: vi.fn(),
    rejectPairing: vi.fn(),
    createChannelSession: vi.fn(),
    sendChannelMessage: vi.fn(),
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

  it('normalizes activation codes from Hive Bridge login links', () => {
    expect(activationCodeFromSearch('?user_code=hive-abcd-1234')).toBe('HIVE-ABCD-1234');
    expect(activationCodeFromSearch('?foo=bar')).toBe('');
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
    expect(markup).toContain('Chat');
    expect(markup).toContain('Workspace');
    expect(markup).toContain('Direct local chat');
    expect(markup).toContain('Attach file');
    expect(markup).toContain('Automatic authentication');
    expect(markup).toContain('npx skills add https://github.com/rocky2431/hive-bridge-skill --skill hive-bridge');
    expect(markup).toContain('hive-bridge login');
    expect(markup).not.toContain('Approve link');
    expect(markup).not.toContain('Pairing code');
    expect(markup).not.toContain('paste the HIVE code');
    expect(markup).not.toContain('Hive agent id');
    expect(markup).not.toContain('Overview');
    expect(markup).not.toContain('Knowledge');
    expect(markup).not.toContain('Settings');
  });
});

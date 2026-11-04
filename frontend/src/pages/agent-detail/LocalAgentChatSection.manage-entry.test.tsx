// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { localBridgeApi } from '../../api/domains/localBridge';
import LocalAgentChatSection from './LocalAgentChatSection';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
vi.mock('../session-workbench/SessionComposer', () => ({ SessionComposer: () => null }));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

// The conversation is a local bot's default screen (B5), so its managers need
// the same visible 设置 entry other bots get — gated by the bot's existing
// action_capabilities.can_manage and independent of any session read.
function renderLocalChat(agent: Record<string, unknown>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // The default session read fails: the entry must survive that.
  vi.spyOn(localBridgeApi, 'getAgentDefaultChannelSession').mockRejectedValue(new Error('bridge offline'));
  vi.spyOn(localBridgeApi, 'listAgentConnections').mockResolvedValue({ connections: [] } as any);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/agents/local']}>
        <LocalAgentChatSection agentId="local" agent={agent as any} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it('offers a local bot\'s managers its 设置 entry even when the default session read fails (B5 review)', async () => {
  renderLocalChat({ id: 'local', agent_type: 'local_agent', action_capabilities: { can_manage: true } });

  const entry = await screen.findByRole('link', { name: 'Settings' });
  expect(entry.getAttribute('href')).toBe('/agents/local?manage=true#settings');
});

it('shows no management entry on the local conversation for people who cannot manage the bot', async () => {
  renderLocalChat({ id: 'local', agent_type: 'local_agent', action_capabilities: { can_use: true } });

  await waitFor(() => expect(screen.getByTestId('session-workbench-header')).toBeTruthy());
  expect(screen.queryByRole('link', { name: 'Settings' })).toBeNull();
});

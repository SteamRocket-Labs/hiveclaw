// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { localBridgeApi } from '../../api/domains/localBridge';
import LocalAgentChatSection from './LocalAgentChatSection';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
vi.mock('../session-workbench/SessionWorkbenchChrome', () => ({
  SessionWorkbenchHeader: ({ model }: any) => <div data-testid="session">{model.sessionId}</div>,
}));
vi.mock('../session-workbench/SessionComposer', () => ({ SessionComposer: () => null }));

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function Location() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}{location.search}{location.hash}</div>;
}

it.each([
  ['/agents/local/sessions/selected', 'selected'],
  ['/agents/local?manage=true#chat', 'old'],
  ['/agents/local#status', 'old'],
  ['/agents/local/sessions/loading', ''],
])('preserves the requested route and never substitutes a cached default: %s', async (route, sessionId) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(['local-agent-detail-default-session', 'local'], { id: 'channel-old', chat_session_id: 'old' });
  client.setQueryData(['local-agent-detail-route-session', 'local', 'selected'], { id: 'channel-selected', chat_session_id: 'selected' });
  vi.spyOn(localBridgeApi, 'getAgentChannelSession').mockImplementation(() => new Promise(() => {}));
  const getDefault = vi.spyOn(localBridgeApi, 'getAgentDefaultChannelSession');
  vi.spyOn(localBridgeApi, 'getChannelTimeline').mockResolvedValue({ events: [], next_cursor: 0 } as any);
  vi.spyOn(localBridgeApi, 'listAgentConnections').mockResolvedValue({ connections: [] } as any);
  vi.spyOn(localBridgeApi, 'createBrowserChannelWsTicket').mockImplementation(() => new Promise(() => {}));
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Location />
        <Routes>
          <Route path="/agents/:id/sessions/:sessionId?" element={<LocalAgentChatSection agentId="local" agent={{ id: 'local' }} />} />
          <Route path="/agents/:id" element={<LocalAgentChatSection agentId="local" agent={{ id: 'local' }} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(screen.getByTestId('session').textContent).toBe(sessionId));
  expect(screen.getByTestId('location').textContent).toBe(route);
  expect(getDefault).not.toHaveBeenCalled();
  cleanup();
  client.clear();
});

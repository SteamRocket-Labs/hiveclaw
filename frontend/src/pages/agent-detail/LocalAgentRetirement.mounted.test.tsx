// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { localBridgeApi } from '../../api/domains/localBridge';
import LocalAgentChatSection from './LocalAgentChatSection';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
vi.mock('../../components/LocalAgentRequestControls', () => ({ default: () => <div>Existing request recovery</div> }));
vi.mock('../../components/LocalAgentDeviceControls', () => ({ default: () => null }));
let client: QueryClient;
afterEach(() => { cleanup();client?.clear();vi.restoreAllMocks(); });

it('opens retained local history through GET without creating a default session or enabling new execution', async () => {
  const work = { id: 'local-work', chat_session_id: 'original-work', agent_id: 'retiring-employee',
    source: 'web', status: 'active', title: 'Original local work' };
  vi.spyOn(localBridgeApi, 'listAgentChannelSessions').mockResolvedValue([work]);
  vi.spyOn(localBridgeApi, 'getAgentChannelSession').mockResolvedValue(work);
  const create = vi.spyOn(localBridgeApi, 'getAgentDefaultChannelSession');
  const send = vi.spyOn(localBridgeApi, 'sendAgentChannelMessage');
  vi.spyOn(localBridgeApi, 'listAgentConnections').mockResolvedValue({ connections: [] } as any);
  vi.spyOn(localBridgeApi, 'getChannelTimeline').mockResolvedValue({ session: work, next_cursor: 1, events: [{
    id: 'retained-result', sequence: 1, session_id: work.id, direction: 'local_to_hive', type: 'result',
    payload: { output: 'Retained local report' }, created_at: '2026-10-20T00:00:00Z' }] } as any);
  vi.spyOn(localBridgeApi, 'createBrowserChannelWsTicket').mockRejectedValue(new Error('Runner is offline; saved history remains available'));
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = <LocalAgentChatSection agentId="retiring-employee" agent={{ id: 'retiring-employee', name: 'Retiring employee',
    deactivation_reason: 'retirement_pending', action_capabilities: { can_use: false } }} />;
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/agents/retiring-employee']}>
    <Routes><Route path="/agents/:id" element={content} /><Route path="/agents/:id/sessions/:sessionId" element={content} /></Routes>
  </MemoryRouter></QueryClientProvider>);
  expect(await screen.findByText('Retained local report')).toBeTruthy();
  await waitFor(() => expect(view.container.querySelector('textarea')?.disabled).toBe(true));
  expect(screen.getByText('Existing request recovery')).toBeTruthy();
  expect(create).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
});

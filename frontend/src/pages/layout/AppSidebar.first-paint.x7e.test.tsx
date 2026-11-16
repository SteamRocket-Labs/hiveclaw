// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { assistantApi } from '../../api/domains/assistant';
import AppSidebar from './AppSidebar';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (_key: string, fallback?: string) => fallback || _key,
}) }));
vi.mock('../../api/domains/assistant', () => ({ assistantApi: { get: vi.fn() } }));

const props = {
  user: { id: 'member', tenant_id: 'company', role: 'member' },
  theme: 'light' as const, isSidebarCollapsed: false, onToggleSidebar: vi.fn(),
  tenants: [{ id: 'company', name: 'Company' }], currentTenant: 'company', onSwitchTenant: vi.fn(),
  isChinese: true, onToggleTheme: vi.fn(), onOpenNotifications: vi.fn(), unreadCount: 0,
  accountMenuRef: React.createRef<HTMLDivElement>(), showAccountMenu: false, onToggleAccountMenu: vi.fn(),
  onToggleLang: vi.fn(), onOpenAccountSettings: vi.fn(), onLogout: vi.fn(), versionDisplay: null,
};
// Run22 reload: GET /agents/ finishes at 02:44:09.161; /assistant at 02:44:10.151.
// The assistant is tenth by recency; its binding must decide the first visible order.
const agents = [
  ...Array.from({ length: 9 }, (_, i) => ({ id: `recent-${i}`, name: `Colleague ${i}`,
    created_at: `2026-11-${String(14 - i).padStart(2, '0')}T00:00:00Z` })),
  { id: 'own', name: 'My Assistant', created_at: '2026-10-01T00:00:00Z' },
];
let client: QueryClient;
afterEach(() => { cleanup(); client.clear(); localStorage.clear(); vi.clearAllMocks(); });
const visibleBotLinks = () => screen.queryAllByRole('link')
  .map(link => link.getAttribute('href')).filter(href => href?.startsWith('/agents/'));

it.each(['bound', 'unbound', 'failed'])('X7e: holds the visible order until its binding settles (%s)', async state => {
  let release!: () => void;
  vi.mocked(assistantApi.get).mockReturnValue(new Promise((resolve, reject) => {
    release = () => state === 'failed' ? reject(new Error('Fixture failure'))
      : resolve({ agent: state === 'bound' ? { id: 'own' } : null } as never);
  }));
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><AppSidebar {...props} agents={agents} /></MemoryRouter></QueryClientProvider>);
  await waitFor(() => expect(assistantApi.get).toHaveBeenCalledTimes(1));
  expect(visibleBotLinks()).toEqual([]);
  expect(screen.getByRole('button', { name: 'New digital employee' })).toBeTruthy();
  await act(async () => { release(); });
  await waitFor(() => expect(visibleBotLinks()).toHaveLength(10));
  const expected = state === 'bound' ? [agents[9], ...agents.slice(0, 9)] : agents;
  expect(visibleBotLinks()).toEqual(expected.map(agent => `/agents/${agent.id}#chat`));
});

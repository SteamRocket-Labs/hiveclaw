// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { SidebarConversation } from '../../api/domains/chat';
import AppSidebar from './AppSidebar';

vi.mock('../../api/domains/assistant', () => ({
  assistantApi: { get: vi.fn().mockResolvedValue({ agent: null }) },
}));

const props = {
  user: { id: 'member', tenant_id: 'company', role: 'member' },
  theme: 'light' as const, isSidebarCollapsed: false, onToggleSidebar: vi.fn(),
  tenants: [{ id: 'company', name: 'Company' }], currentTenant: 'company', onSwitchTenant: vi.fn(),
  isChinese: false, onToggleTheme: vi.fn(), onOpenNotifications: vi.fn(), unreadCount: 0,
  accountMenuRef: React.createRef<HTMLDivElement>(), showAccountMenu: false, onToggleAccountMenu: vi.fn(),
  onToggleLang: vi.fn(), onOpenAccountSettings: vi.fn(), onLogout: vi.fn(), versionDisplay: null,
};
const agents = ['Preview', 'Running', 'Waiting', 'Failed', 'Fresh'].map(name => ({
  id: name.toLowerCase(), name: `${name} bot`,
}));
let client: QueryClient;

afterEach(() => {
  cleanup();
  client.clear();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it.each([
  { language: 'zh', running: '运行中…', waiting: '等你决定', failed: '失败', yesterday: '昨天' },
  { language: 'en', running: 'Working…', waiting: 'Needs you', failed: 'Failed', yesterday: 'Yesterday' },
])('renders the member’s previews, run states and local dates in $language', async labels => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 10, 18, 12));
  // The endpoint strips a heading, emphasis, and link syntax, and clips the first line to 80 characters.
  const plainPreview = `Updated answer ${'界'.repeat(64)}…`;
  const rows: SidebarConversation[] = [
    { agent_id: 'preview', session_id: 'p', state: null, preview: plainPreview,
      last_message_at: new Date(2026, 10, 18, 9, 5).toISOString() },
    { agent_id: 'running', session_id: 'r', state: 'running', preview: 'Old line',
      last_message_at: new Date(2026, 10, 17, 18).toISOString() },
    { agent_id: 'waiting', session_id: 'w', state: 'waiting', preview: 'Old line',
      last_message_at: new Date(2026, 10, 14, 23).toISOString() },
    { agent_id: 'failed', session_id: 'f', state: 'failed', preview: 'Old line', last_message_at: null },
  ];
  const fetchSidebar = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(rows))));
  vi.stubGlobal('fetch', fetchSidebar);
  const i18n = createInstance();
  await i18n.init({ lng: labels.language, resources: { en: { translation: en }, zh: { translation: zh } } });
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}><MemoryRouter><AppSidebar {...props} agents={agents} /></MemoryRouter></I18nextProvider>
    </QueryClientProvider>,
  );

  const preview = await screen.findByText(plainPreview);
  expect(preview.children).toHaveLength(0);
  expect(preview.textContent).toHaveLength(80);
  expect(within(preview.closest('a')!).getByText('09:05').getAttribute('datetime')).toBe(rows[0].last_message_at);
  expect(screen.getByText(labels.running).getAttribute('data-state')).toBe('running');
  expect(screen.getByText(labels.waiting)).toBeTruthy();
  expect(screen.getByText(labels.failed)).toBeTruthy();
  expect(screen.getByText(labels.yesterday)).toBeTruthy();
  expect(screen.getByText('11-14')).toBeTruthy();
  expect(screen.queryByText('Old line')).toBeNull();
  const fresh = screen.getByText('Fresh bot').closest('a')!;
  expect(fresh.querySelector('.sidebar-agent-preview, time')).toBeNull();
  expect(fetchSidebar).toHaveBeenCalledTimes(1);
  expect(fetchSidebar.mock.calls[0][0]).toBe('/api/agents/sessions/sidebar');

  rerender(<QueryClientProvider client={client}>
    <I18nextProvider i18n={i18n}><MemoryRouter><AppSidebar {...props} agents={agents} isSidebarCollapsed /></MemoryRouter></I18nextProvider>
  </QueryClientProvider>);
  expect(screen.queryByText(plainPreview)).toBeNull();
  expect(screen.queryByText(labels.running)).toBeNull();
});

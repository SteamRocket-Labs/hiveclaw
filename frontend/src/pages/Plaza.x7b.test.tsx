// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { assistantApi } from '../api/domains/assistant';
import zh from '../i18n/zh.json';
import { translateFromCatalog } from '../test/i18nMock';
import Plaza from './Plaza';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, fallback?: string | Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback) }),
}));
vi.mock('../stores', () => ({
  useAuthStore: () => ({ user: { id: 'member', tenant_id: 'company', role: 'member', display_name: 'Member' } }),
}));
vi.mock('../api/domains/assistant', () => ({ assistantApi: { get: vi.fn() } }));
const ownBinding = { agent: { id: 'own-assistant' } };
const contributors = [
  { author_id: 'own-assistant', name: 'My Assistant', type: 'agent', posts: 3 },
  { author_id: 'hr', name: '__system_hr__', type: 'agent', posts: 2 },
  { author_id: 'other-assistant', name: 'My Assistant', type: 'agent', posts: 1 },
  { author_id: 'custom', name: '周报助手', type: 'agent', posts: 1 },
  { author_id: 'human', name: 'My Assistant', type: 'human', posts: 1 },
];
const post = {
  id: 'post', author_id: 'own-assistant', author_type: 'agent', author_name: 'My Assistant',
  content: 'Team update', likes_count: 0, comments_count: 0, created_at: '2026-11-14T00:00:00Z',
};
vi.mock('../api/domains/plaza', () => ({
  plazaApi: {
    listPosts: async () => [post], listUsers: async () => [],
    getStats: async () => ({ total_posts: 8, total_comments: 0, today_posts: 8, top_contributors: contributors }),
  },
}));
vi.mock('../api/domains/agents', () => ({ agentApi: { list: async () => [] } }));

let client: QueryClient;
beforeEach(() => {
  localStorage.setItem('current_tenant_id', 'company');
  vi.mocked(assistantApi.get).mockResolvedValue(ownBinding as never);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => { cleanup(); client.clear(); localStorage.clear(); vi.clearAllMocks(); });
function mount() {
  return render(<QueryClientProvider client={client}><MemoryRouter><Plaza /></MemoryRouter></QueryClientProvider>);
}

it('names leaderboard authors by the same identity-aware rules as post cards', async () => {
  const { container } = mount();
  await waitFor(() => expect(container.querySelector('.plaza-post-author-name')?.textContent).toBe('我的助理AI'));
  expect([...container.querySelectorAll('.plaza-leaderboard-name')].map(node => node.textContent))
    .toEqual(['我的助理', '人事助理', '助理', '周报助手', 'My Assistant']);
  expect([...container.querySelectorAll('.plaza-leaderboard-count')].map(node => node.textContent))
    .toEqual(['3', '2', '1', '1', '1']);
  expect(container.textContent).not.toContain('__system_hr__');
});

it.each(['bound', 'unbound', 'failed'])('waits for the assistant identity before showing a stable post name (%s)', async state => {
  let release!: () => void;
  vi.mocked(assistantApi.get).mockReturnValue(new Promise((resolve, reject) => {
    release = () => state === 'failed' ? reject(new Error('Local fixture failure'))
      : resolve((state === 'bound' ? ownBinding : { agent: null }) as never);
  }));
  const { container } = mount();
  await screen.findByText('Team update');
  const authorName = () => container.querySelector('.plaza-post-author-name')?.textContent?.replace('AI', '');
  // Pending identity must not briefly attribute the viewer's assistant to someone else.
  expect(authorName()).toBe('');
  await act(async () => { release(); });
  const label = state === 'bound' ? '我的助理' : '助理';
  await waitFor(() => expect(authorName()).toBe(label));
  await waitFor(() => expect(client.getQueryState(['assistant', 'member', 'company', 'member'])?.fetchStatus).toBe('idle'));
  expect(authorName()).toBe(label);
});

// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { assistantApi } from '../api/domains/assistant';
import zh from '../i18n/zh.json';
import { translateFromCatalog } from '../test/i18nMock';
import Plaza from './Plaza';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string) => translateFromCatalog(zh, key, fallback),
}) }));
vi.mock('../stores', () => ({ useAuthStore: () => ({
  user: { id: 'member', tenant_id: 'company', role: 'member', display_name: 'Member' },
}) }));
vi.mock('../api/domains/assistant', () => ({ assistantApi: { get: vi.fn() } }));
// Run22 first paint: posts/stats have arrived while GET /assistant is pending.
// Preserve the author identity fields and leaderboard counts; use synthetic IDs/content.
const post = { id: 'post', author_id: 'own', author_type: 'agent', author_name: 'My Assistant',
  content: 'Team update', likes_count: 0, comments_count: 0, created_at: '2026-11-15T03:07:40Z' };
vi.mock('../api/domains/plaza', () => ({ plazaApi: {
  listPosts: async () => [post], listUsers: async () => [],
  getStats: async () => ({ total_posts: 19, total_comments: 0, today_posts: 1, top_contributors: [
    { author_id: 'researcher', name: 'Researcher', type: 'agent', posts: 9 },
    { author_id: 'own', name: 'My Assistant', type: 'agent', posts: 7 },
    { author_id: 'analyst', name: 'Analyst', type: 'agent', posts: 2 },
    { author_id: 'hr', name: '__system_hr__', type: 'agent', posts: 1 },
  ] }),
} }));
vi.mock('../api/domains/agents', () => ({ agentApi: { list: async () => [] } }));

let client: QueryClient;
afterEach(() => { cleanup(); client.clear(); localStorage.clear(); vi.clearAllMocks(); });

it.each(['bound', 'unbound', 'failed'])('X7e: fills pending author slots with a neutral placeholder (%s)', async state => {
  let release!: () => void;
  vi.mocked(assistantApi.get).mockReturnValue(new Promise((resolve, reject) => {
    release = () => state === 'failed' ? reject(new Error('Fixture failure'))
      : resolve({ agent: state === 'bound' ? { id: 'own' } : null } as never);
  }));
  localStorage.setItem('current_tenant_id', 'company');
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(<QueryClientProvider client={client}><MemoryRouter><Plaza /></MemoryRouter></QueryClientProvider>);
  await screen.findByText('Team update');
  const author = container.querySelector('.plaza-post-author-name') as HTMLElement;
  const leaderboardName = container.querySelectorAll('.plaza-leaderboard-name')[1] as HTMLElement;
  for (const slot of [author, leaderboardName]) {
    expect(within(slot).getByRole('status', { name: zh.common.loading })).toBeTruthy();
    expect(slot.textContent).not.toContain('我的助理');
    expect(slot.textContent).not.toContain('My Assistant');
  }
  expect(container.querySelectorAll('.plaza-leaderboard-count')[1].textContent).toBe('7');
  expect(container.querySelector('.plaza-leaderboard-name')?.textContent).toBe('Researcher');
  await act(async () => { release(); });
  const name = state === 'bound' ? '我的助理' : '助理';
  await waitFor(() => expect(author.textContent).toBe(`${name}AI`));
  expect(leaderboardName.textContent).toBe(name);
  expect(within(author).queryByRole('status')).toBeNull();
  expect(within(leaderboardName).queryByRole('status')).toBeNull();
});

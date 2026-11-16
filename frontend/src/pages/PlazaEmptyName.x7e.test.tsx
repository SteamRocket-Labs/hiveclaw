// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import Plaza from './Plaza';
import { assistantApi } from '../api/domains/assistant';
import zh from '../i18n/zh.json';
import { translateFromCatalog } from '../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, fallback?: string) => translateFromCatalog(zh, key, fallback) }) }));
vi.mock('../stores', () => ({ useAuthStore: () => ({ user: { id: 'member', tenant_id: 'company', role: 'member' } }) }));
vi.mock('../api/domains/assistant', () => ({ assistantApi: { get: vi.fn() } }));
const authors = [
  { author_id: 'human', author_type: 'human', author_name: '' },
  { author_id: 'agent', author_type: 'agent', author_name: '' },
  { author_id: 'own', author_type: 'agent', author_name: 'My Assistant' },
];
const posts = authors.map((author, i) => ({ ...author, id: `post-${i}`, content: `Update ${i}`,
  likes_count: 0, comments_count: 1, created_at: '2026-11-15T00:00:00Z' }));
vi.mock('../api/domains/plaza', () => ({ plazaApi: {
  listPosts: async () => posts, listUsers: async () => [],
  getStats: async () => ({ total_posts: 3, total_comments: 1, today_posts: 3, top_contributors:
    authors.map(a => ({ author_id: a.author_id, type: a.author_type, name: a.author_name, posts: 1 })) }),
  getPost: async () => ({ ...posts[0], comments: authors.map((author, i) => ({ ...author, id: `comment-${i}`,
    post_id: 'post-0', content: `Comment ${i}`, created_at: '2026-11-15T00:00:00Z' })) }),
} }));
vi.mock('../api/domains/agents', () => ({ agentApi: { list: async () => [] } }));
let client: QueryClient;
afterEach(() => { cleanup(); client.clear(); localStorage.clear(); vi.clearAllMocks(); });

it('X7e-r2: only the pending default-assistant name has a loading status, including comments', async () => {
  let finish!: () => void;
  vi.mocked(assistantApi.get).mockReturnValue(new Promise(resolve => { finish = () => resolve({ agent: { id: 'own' } } as never); }));
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(<QueryClientProvider client={client}><MemoryRouter><Plaza /></MemoryRouter></QueryClientProvider>);
  const post = (await screen.findByText('Update 0')).closest('.plaza-post') as HTMLElement;
  fireEvent.click(within(post).getByRole('button', { name: '1' }));
  await screen.findByText('Comment 2');
  for (const selector of ['.plaza-post-author-name', '.plaza-leaderboard-name', '.plaza-comment-name']) {
    const slots = container.querySelectorAll<HTMLElement>(selector);
    expect(slots).toHaveLength(3);
    expect.soft(within(slots[0]).queryByRole('status')).toBeNull();
    expect.soft(within(slots[1]).queryByRole('status')).toBeNull();
    expect(within(slots[2]).getByRole('status', { name: zh.common.loading })).toBeTruthy();
  }
  await act(async () => { finish(); });
  await waitFor(() => expect(container.querySelectorAll('.plaza-name-placeholder')).toHaveLength(0));
});
